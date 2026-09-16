import asyncio
from dataclasses import replace
import time

import pytest

from robot_vllm.control import Rejected
from robot_vllm.sensing import SenseKey, SharedPerception


def key(**changes):
    return replace(SenseKey("camera", "frame1", "cal1", "model1", "objects", 0), **changes)


def test_shared_compute_cancelled_subscriber_does_not_cancel_others():
    async def scenario():
        pool = SharedPerception()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def compute():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"targets": [1, 2]}
        captured = time.monotonic()
        first = asyncio.create_task(pool.get(key(), captured, compute))
        await entered.wait()
        second = asyncio.create_task(pool.get(key(), captured, compute))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        value = await second
        value["targets"].append(99)
        assert await pool.get(key(), captured, compute) == {"targets": [1, 2]}
        assert calls == 1
        assert pool.snapshot()["joined_inflight"] == 1
        await pool.close()
    asyncio.run(scenario())


def test_scene_invalidation_discards_inflight_result_and_requires_new_generation():
    async def scenario():
        pool = SharedPerception()
        entered, release = asyncio.Event(), asyncio.Event()
        async def compute():
            entered.set()
            await release.wait()
            return {"column": 1}
        captured = time.monotonic()
        work = asyncio.create_task(pool.get(key(), captured, compute))
        await entered.wait()
        pool.invalidate("camera")
        release.set()
        with pytest.raises(Rejected, match="invalidated"):
            await work
        assert pool.snapshot()["cached_entries"] == 0
        with pytest.raises(Rejected, match="invalidated"):
            await pool.get(key(), captured, compute)
        assert await pool.get(key(generation=1, frame="frame2"), time.monotonic(), compute) == {"column": 1}
        await pool.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", [{"source": "camera2"}, {"frame": "frame2"},
    {"calibration": "cal2"}, {"processor": "model2"}, {"query": "different-question"}])
def test_identity_components_prevent_incorrect_reuse(change):
    async def scenario():
        pool = SharedPerception()
        calls = 0
        async def compute():
            nonlocal calls
            calls += 1
            return {"version": calls}
        captured = time.monotonic()
        await pool.get(key(), captured, compute)
        assert await pool.get(key(**change), captured, compute) == {"version": 2}
        with pytest.raises(Rejected, match="identity_reused"):
            await pool.get(key(), captured + .000001, compute)
        await pool.close()
    asyncio.run(scenario())


def test_capacity_expiry_failed_compute_and_lru_eviction():
    async def scenario():
        pool = SharedPerception(max_entries=1, max_inflight=1, max_age_s=1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def fail():
            entered.set()
            await release.wait()
            raise RuntimeError("provider failed")
        captured = time.monotonic()
        work = asyncio.create_task(pool.get(key(), captured, fail))
        await entered.wait()
        with pytest.raises(Rejected, match="capacity"):
            await pool.get(key(frame="frame2"), captured, fail)
        release.set()
        with pytest.raises(RuntimeError):
            await work
        assert pool.snapshot()["inflight"] == pool.snapshot()["cached_entries"] == 0
        async def good():
            return {"value": 1}
        with pytest.raises(Rejected, match="expired"):
            await pool.get(key(), captured - 2, good)
        await pool.get(key(), captured, good)
        await pool.get(key(frame="frame2"), captured, good)
        assert pool.snapshot()["cached_entries"] == 1
        assert key() not in pool.cache
        await pool.close()
    asyncio.run(scenario())
