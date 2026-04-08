import asyncio
import json
from datetime import datetime, timedelta, timezone

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal.activities import execute_http_request
    from temporal.models import FollowupCycleInput, HttpResponse, ScheduledHttpTask

# IST = UTC+5:30
_IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_now() -> datetime:
    return workflow.now().astimezone(timezone(_IST_OFFSET))


def _seconds_until_ist_time(hour: int, minute: int = 0) -> float:
    """Seconds from now until the next occurrence of HH:MM IST."""
    now_ist = _ist_now()
    target = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_ist:
        target += timedelta(days=1)
    return (target - now_ist).total_seconds()


@workflow.defn
class FollowupCycleWorkflow:
    """
    Long-running workflow that fires follow-up nudges on a fixed daily schedule (IST):
      - 8:00 AM  → category "task"    (give today's task; always fires)
      - 11:00 AM → category "protip"  (pro tip; n8n checks 2.5hr activity)
      - 2:00 PM  → category "protip"
      - 5:00 PM  → category "protip"
      - 8:00 PM  → category "checkin" (check task completion; always fires)

    Runs indefinitely until cancelled.
    """

    # Fixed daily slots: (hour, category, check_activity)
    _SLOTS = [
        (8,  "task",    False),
        (11, "protip",  True),
        (14, "protip",  True),
        (17, "protip",  True),
        (20, "checkin", False),
    ]

    @workflow.run
    async def run(self, inp: FollowupCycleInput) -> None:
        while True:
            now_ist = _ist_now()
            current_hour = now_ist.hour

            # Find the next slot that hasn't fired yet today
            next_slot = None
            for hour, category, check_activity in self._SLOTS:
                if hour > current_hour:
                    next_slot = (hour, category, check_activity)
                    break

            if next_slot is None:
                # All slots done today — sleep until 8AM tomorrow
                delay = _seconds_until_ist_time(8)
            else:
                hour, category, check_activity = next_slot
                delay = _seconds_until_ist_time(hour)

            workflow.logger.info(
                f"[{inp.session_id}] Next slot in {delay:.0f}s "
                f"({next_slot[1] if next_slot else 'task (tomorrow)'})"
            )
            await asyncio.sleep(delay)

            if next_slot is None:
                # Woke up for 8AM — loop back to pick it up
                continue

            hour, category, check_activity = next_slot

            payload = json.dumps({
                "session_id": inp.session_id,
                "user_id": inp.user_id,
                "category": category,
                "check_activity": check_activity,
            })

            task = ScheduledHttpTask(
                url=inp.webhook_url,
                method="POST",
                scheduled_at=workflow.now().isoformat(),
                headers={"Content-Type": "application/json"},
                body=payload,
                timeout_seconds=30,
            )

            try:
                await workflow.execute_activity(
                    execute_http_request,
                    task,
                    start_to_close_timeout=timedelta(seconds=40),
                    retry_policy=workflow.RetryPolicy(maximum_attempts=3),
                )
                workflow.logger.info(
                    f"[{inp.session_id}] Fired '{category}' nudge"
                )
            except Exception as exc:
                workflow.logger.error(
                    f"[{inp.session_id}] Failed to fire '{category}': {exc}"
                )

            # Small buffer so we don't re-fire the same slot
            await asyncio.sleep(60)


@workflow.defn
class ScheduledHttpTaskWorkflow:
    """Workflow that waits until a scheduled time, then executes an HTTP request."""

    @workflow.run
    async def run(self, task: ScheduledHttpTask) -> HttpResponse:
        scheduled_time = datetime.fromisoformat(task.scheduled_at)

        now = workflow.now()
        delay = (scheduled_time - now).total_seconds()

        if delay > 0:
            workflow.logger.info(
                f"Sleeping for {delay:.0f}s until {task.scheduled_at}"
            )
            await asyncio.sleep(delay)
        else:
            workflow.logger.info("Scheduled time is in the past, executing immediately")

        result = await workflow.execute_activity(
            execute_http_request,
            task,
            start_to_close_timeout=timedelta(seconds=task.timeout_seconds + 10),
        )

        workflow.logger.info(f"Task completed with status {result.status_code}")
        return result
