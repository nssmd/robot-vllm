"""Bounded sharing of perception for identical, immutable sensor snapshots.

This service shares evidence, never robot actions or observation tickets. Callers
must keep their own action admission and validate the original scene generation.
"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import math
import time

from .control import Rejected, clone


@dataclass(frozen=True)
class SenseKey:
    source: str
    frame: str
    calibration: str
    processor: str
    query: str
    generation: int


class SharedPerception:
    def __init__(self, *, max_entries=64, max_inflight=8, max_age_s=30):
        if (type(max_entries) is not int or not 1 <= max_entries <= 4096
                or type(max_inflight) is not int or not 1 <= max_inflight <= 64
                or type(max_age_s) not in (int, float) or not math.isfinite(max_age_s)
                or not 0 < max_age_s <= 3600):
            raise ValueError("invalid_shared_perception_limits")
        self.max_entries, self.max_inflight, self.max_age_s = max_entries, max_inflight, max_age_s
        self.cache, self.inflight, self.generations = OrderedDict(), {}, {}
        self.hits = self.joined = self.started = self.invalidated = 0
        self.closed = False

    def generation(self, source):
        return self.generations.get(source, 0)

    def invalidate(self, source):
        """Call on scene-affecting actions, external change or calibration changes."""
        self.generations[source] = self.generation(source) + 1
        self.invalidated += 1
        for key in list(self.cache):
            if key.source == source:
                del self.cache[key]

    def validate(self, key, captured_at):
        if self.closed:
            raise Rejected("perception_closed")
        if (not isinstance(key, SenseKey) or any(not isinstance(v, str) or not v
                for v in (key.source, key.frame, key.calibration, key.processor, key.query))
                or type(key.generation) is not int or key.generation < 0):
            raise Rejected("invalid_perception_identity")
        if (type(captured_at) not in (int, float) or not math.isfinite(captured_at)
                or not 0 <= time.monotonic() - captured_at <= self.max_age_s):
            raise Rejected("expired_perception")
        if key.generation != self.generation(key.source):
            raise Rejected("invalidated_perception")

    async def get(self, key, captured_at, compute):
        self.validate(key, captured_at)
        if key in self.cache:
            captured, value = self.cache[key]
            if captured != captured_at:
                raise Rejected("frame_identity_reused")
            self.hits += 1
            self.cache.move_to_end(key)
            return clone(value)
        entry = self.inflight.get(key)
        if entry is None:
            if len(self.inflight) >= self.max_inflight:
                raise Rejected("perception_capacity_exceeded")
            self.started += 1

            async def work():
                value = clone(await compute())
                self.validate(key, captured_at)
                self.cache[key] = captured_at, value
                self.cache.move_to_end(key)
                while len(self.cache) > self.max_entries:
                    self.cache.popitem(last=False)
                return value

            task = asyncio.create_task(work())
            self.inflight[key] = captured_at, task

            def finished(future):
                self.inflight.pop(key, None)
                if not future.cancelled():
                    future.exception()  # Retrieve exceptions even if every subscriber canceled.

            task.add_done_callback(finished)
        else:
            captured, task = entry
            if captured != captured_at:
                raise Rejected("frame_identity_reused")
            self.joined += 1
        value = await asyncio.shield(task)
        self.validate(key, captured_at)
        return clone(value)

    def snapshot(self):
        return {"computations": self.started, "cache_hits": self.hits,
                "joined_inflight": self.joined, "invalidations": self.invalidated,
                "cached_entries": len(self.cache), "inflight": len(self.inflight)}

    async def close(self):
        self.closed = True
        # Drain shared requests: cancellation of a subscriber isn't upstream stop.
        await asyncio.gather(*(item[1] for item in list(self.inflight.values())), return_exceptions=True)
        self.cache.clear()
