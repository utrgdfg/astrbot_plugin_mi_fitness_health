"""Small asyncio helpers whose timeout does not wait for cancellation cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


def _consume_task_result(task: asyncio.Task) -> None:
    """Retrieve detached task failures so asyncio never reports them as unhandled."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def await_cancellation_safe(awaitable: Awaitable[T]) -> T:
    """Finish non-cancellable owned work before propagating task cancellation."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        raise asyncio.CancelledError
    return task.result()


async def await_with_hard_timeout(
    awaitable: Awaitable[T],
    timeout: float,
    *,
    registry: set[asyncio.Task] | None = None,
) -> T:
    """Return within ``timeout`` even when the child ignores cancellation.

    ``asyncio.wait_for`` waits for cancellation cleanup after its deadline. Some
    provider implementations can therefore keep a pre-LLM hook blocked well past
    the requested timeout. This helper cancels pending work but deliberately does
    not await that cancellation on the latency-sensitive caller path.
    """
    task = asyncio.ensure_future(awaitable)
    if registry is not None:
        registry.add(task)

    def finalize(done: asyncio.Task) -> None:
        if registry is not None:
            registry.discard(done)
        _consume_task_result(done)

    task.add_done_callback(finalize)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, float(timeout)))
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task not in done:
        task.cancel()
        raise TimeoutError
    return task.result()
