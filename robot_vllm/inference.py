"""FIFO admission for blocking model transports, shared across robot tasks.

Canceling a caller does not stop its HTTP thread. Its permit remains occupied
until the transport actually returns; late results never reach the caller.
All queue state is owned by the coordinator's asyncio event loop.
"""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import math
import time

from .protocol import ProviderError, ProviderTimeout


class InferenceSlot:
    def __init__(self, pool, queue_time_s):
        self.pool, self.queue_time_s = pool, queue_time_s
        self.work = None
        self.released = False

    async def run(self, function, *args):
        if self.work is not None or self.released or self.pool.closed:
            raise ProviderError("inference_slot_unavailable")
        started = time.monotonic()
        self.pool.started += 1
        self.work = asyncio.create_task(asyncio.to_thread(function, *args))
        self.pool.work.add(self.work)

        def finished(work):
            self.pool.work.discard(work)
            self.pool.service_time_s += time.monotonic() - started
            if work.cancelled() or work.exception() is not None:
                self.pool.errors += 1
            else:
                self.pool.completed += 1
            self.release()

        self.work.add_done_callback(finished)
        try:
            return await asyncio.shield(self.work)
        except asyncio.CancelledError:
            self.pool.canceled_inflight += 1
            raise

    def release(self):
        if not self.released:
            self.released = True
            self.pool.active -= 1
            self.pool._wake()


class InferencePool:
    def __init__(self, *, max_concurrency=4, max_queue=64, queue_timeout_s=30):
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 64:
            raise ValueError("invalid_inference_max_concurrency")
        if type(max_queue) is not int or not 0 <= max_queue <= 1024:
            raise ValueError("invalid_inference_max_queue")
        if (type(queue_timeout_s) not in (int, float) or not math.isfinite(queue_timeout_s)
                or not 0 < queue_timeout_s <= 120):
            raise ValueError("invalid_inference_queue_timeout")
        self.limit, self.max_queue, self.queue_timeout = max_concurrency, max_queue, queue_timeout_s
        self.waiting, self.work = deque(), set()
        self.active = self.peak_active = self.started = self.completed = self.errors = 0
        self.overloaded = self.queue_timeouts = self.canceled_queued = self.canceled_inflight = 0
        self.queue_time_s = self.service_time_s = 0.0
        self.closed = False

    def _slot(self, started):
        elapsed = time.monotonic() - started
        self.queue_time_s += elapsed
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        return InferenceSlot(self, elapsed)

    def _wake(self):
        while not self.closed and self.waiting and self.active < self.limit:
            future, started = self.waiting.popleft()
            if not future.done():
                future.set_result(self._slot(started))

    async def _acquire(self):
        if self.closed:
            raise ProviderError("inference_pool_closed")
        started = time.monotonic()
        if self.active < self.limit and not self.waiting:
            return self._slot(started)
        if len(self.waiting) >= self.max_queue:
            self.overloaded += 1
            raise ProviderError("inference_queue_full")
        future = asyncio.get_running_loop().create_future()
        item = future, started
        self.waiting.append(item)
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.queue_timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
            # Admission may race cancellation: return a granted permit as well.
            if future.done() and not future.cancelled() and future.exception() is None:
                future.result().release()
            else:
                future.cancel()
            if item in self.waiting:
                self.waiting.remove(item)
            if isinstance(exc, asyncio.CancelledError):
                self.canceled_queued += 1
                raise
            self.queue_timeouts += 1
            raise ProviderTimeout("inference_queue_timeout") from exc

    @asynccontextmanager
    async def admit(self):
        slot = await self._acquire()
        try:
            yield slot
        finally:
            if slot.work is None:
                slot.release()
            # A started transport releases its own slot in its done callback.

    def close(self):
        self.closed = True
        while self.waiting:
            future, _ = self.waiting.popleft()
            if not future.done():
                future.set_exception(ProviderError("inference_pool_closed"))

    def snapshot(self):
        return {"max_concurrency": self.limit, "max_queue": self.max_queue,
                "queue_timeout_s": self.queue_timeout, "active_slots": self.active,
                "inflight_transports": len(self.work), "queued": len(self.waiting),
                "peak_active_slots": self.peak_active, "started": self.started,
                "transport_completed": self.completed, "transport_errors": self.errors,
                "overloaded": self.overloaded, "queue_timeouts": self.queue_timeouts,
                "canceled_queued": self.canceled_queued, "canceled_inflight": self.canceled_inflight,
                "admitted_queue_time_s": self.queue_time_s, "transport_time_s": self.service_time_s,
                "closed": self.closed}
