import asyncio
from datetime import datetime, timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal.activities import execute_http_request
    from temporal.models import HttpResponse, ScheduledHttpTask


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
