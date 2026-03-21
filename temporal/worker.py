import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from temporal.activities import execute_http_request
from temporal.workflows import ScheduledHttpTaskWorkflow

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = "scheduled-http-tasks"


async def main():
    client = await Client.connect(TEMPORAL_HOST)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ScheduledHttpTaskWorkflow],
        activities=[execute_http_request],
    )

    print(f"Worker started, listening on task queue: {TASK_QUEUE}")
    print(f"Connected to: {TEMPORAL_HOST}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
