import httpx
from temporalio import activity

from temporal.models import HttpResponse, ScheduledHttpTask


@activity.defn
async def execute_http_request(task: ScheduledHttpTask) -> HttpResponse:
    """Execute an HTTP request."""

    activity.logger.info(f"Executing {task.method} request to {task.url}")

    async with httpx.AsyncClient(timeout=task.timeout_seconds) as client:
        response = await client.request(
            method=task.method,
            url=task.url,
            headers=task.headers,
            content=task.body,
        )

    result = HttpResponse(
        status_code=response.status_code,
        body=response.text[:5000],
        headers=dict(response.headers),
    )

    activity.logger.info(f"Response: {result.status_code}")
    return result
