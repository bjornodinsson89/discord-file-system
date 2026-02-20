import asyncio
import logging

from services.jump_monitor import JumpMonitor


def test_jump_monitor_emits_heartbeat_logs(caplog):
    monitor = JumpMonitor(poll_interval_seconds=0)
    monitor.heartbeat_interval_seconds = 0
    state = {"calls": 0}

    async def _fake_poll_once(_jump_id: int) -> bool:
        state["calls"] += 1
        return state["calls"] < 2

    monitor._poll_once = _fake_poll_once  # type: ignore[method-assign]

    async def _run() -> None:
        await monitor._poll_loop(42)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_run())

    assert "jump_monitor heartbeat jump_id=42" in caplog.text
