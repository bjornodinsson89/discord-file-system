import asyncio

from utils.tasks import supervise


def test_supervisor_restarts_until_success():
    state = {"calls": 0}
    done = asyncio.Event()

    async def _worker():
        state["calls"] += 1
        if state["calls"] < 3:
            raise RuntimeError("boom")
        done.set()

    async def _run():
        sup = supervise(
            "unit_supervisor_success",
            _worker,
            restart=True,
            backoff=(0.01, 0.01, 0.01),
        )
        await asyncio.wait_for(done.wait(), timeout=1.0)
        await sup.stop()

    asyncio.run(_run())
    assert state["calls"] == 3


def test_supervisor_stop_cancels_restart_loop():
    state = {"calls": 0}

    async def _worker():
        state["calls"] += 1
        raise RuntimeError("always fails")

    async def _run():
        sup = supervise(
            "unit_supervisor_stop",
            _worker,
            restart=True,
            backoff=(0.05, 0.05, 0.05),
        )
        await asyncio.sleep(0.02)
        await sup.stop()
        calls_after_stop = state["calls"]
        await asyncio.sleep(0.1)
        assert state["calls"] == calls_after_stop

    asyncio.run(_run())
