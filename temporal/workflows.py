import asyncio
import json
from datetime import datetime, timedelta, timezone

from temporalio import workflow
from temporalio.common import RetryPolicy

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
      - 6:00 PM  → category "checkin" (check task completion; always fires)

    Runs indefinitely until cancelled.
    """

    # Fixed daily slots: (hour, category, check_activity)
    _SLOTS = [
        (8,  "task",    False),
        (11, "protip",  True),
        (14, "protip",  True),
        (17, "protip",  True),
        (18, "checkin", False),
    ]

    @workflow.run
    async def run(self, inp: FollowupCycleInput) -> None:
        while True:
            now_ist = _ist_now()
            current_minutes = now_ist.hour * 60 + now_ist.minute

            # Find the next slot that hasn't passed yet today
            next_slot = None
            for hour, category, check_activity in self._SLOTS:
                if hour * 60 > current_minutes:
                    next_slot = (hour, category, check_activity)
                    break

            if next_slot is None:
                # All slots done today — sleep until 8AM tomorrow
                delay = _seconds_until_ist_time(8)
                workflow.logger.info(
                    f"[{inp.session_id}] All slots done, sleeping {delay:.0f}s until 8AM tomorrow"
                )
                await asyncio.sleep(delay)
                # After waking up at 8AM, immediately fire the 8AM slot
                # instead of re-evaluating (avoids the exact-hour edge case)
                next_slot = self._SLOTS[0]

            hour, category, check_activity = next_slot

            # If we just woke up from overnight sleep, delay will be ~0
            delay = _seconds_until_ist_time(hour)
            # If delay is > 23 hours, we're already at/past this hour — fire now
            if delay > 23 * 3600:
                delay = 0

            if delay > 0:
                workflow.logger.info(
                    f"[{inp.session_id}] Next: '{category}' at {hour}:00 IST in {delay:.0f}s"
                )
                await asyncio.sleep(delay)

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
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                workflow.logger.info(
                    f"[{inp.session_id}] Fired '{category}' nudge"
                )
            except Exception as exc:
                workflow.logger.error(
                    f"[{inp.session_id}] Failed to fire '{category}': {exc}"
                )

            # Buffer past this slot so _next_slot() advances to the next one
            await asyncio.sleep(120)


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
