import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class BulkheadResult:
    accepted: bool
    result: Any = None
    elapsed_ms: int = 0


class AsyncBulkhead:
    def __init__(self, name: str, max_concurrency: int):
        self.name = name
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> BulkheadResult:
        if self._semaphore.locked():
            return BulkheadResult(accepted=False)
        await self._semaphore.acquire()

        started_at = time.monotonic()
        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return BulkheadResult(accepted=True, result=result, elapsed_ms=elapsed_ms)
        finally:
            self._semaphore.release()


def build_rejected_response(resource_type: str, max_concurrency: int) -> dict[str, Any]:
    return {
        "success": False,
        "message": f"{resource_type} connectivity checks are busy. Please retry later.",
        "error_type": "rate_limited",
        "max_concurrency": max_concurrency,
    }
