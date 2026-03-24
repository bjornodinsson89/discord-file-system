import asyncio
import logging

import pytest

import bot as bot_entry


class _FakeHTTPException(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class _FakeClient:
    def __init__(self, plan):
        self._plan = list(plan)
        self.start_calls = 0
        self.close_calls = 0
        self.concurrent_starts = 0
        self.max_concurrent_starts = 0

    async def start(self, _token: str):
        self.start_calls += 1
        self.concurrent_starts += 1
        self.max_concurrent_starts = max(self.max_concurrent_starts, self.concurrent_starts)
        try:
            step = self._plan.pop(0)
            if isinstance(step, BaseException):
                raise step
            if asyncio.iscoroutine(step):
                return await step
            if callable(step):
                result = step()
                if asyncio.iscoroutine(result):
                    return await result
                return result
            return step
        finally:
            self.concurrent_starts -= 1

    async def close(self):
        self.close_calls += 1


def test_login_429_retries_with_backoff(monkeypatch):
    stop_event = asyncio.Event()
    delays: list[float] = []

    async def _sleep_once():
        stop_event.set()

    client = _FakeClient(
        [
            _FakeHTTPException("Discord /users/@me rate limited", status=429),
            _sleep_once,
        ]
    )

    def _delay_spy(failure_count: int, **_kwargs) -> float:
        delays.append(float(failure_count))
        return 0.0

    monkeypatch.setattr(bot_entry, "_compute_backoff_delay", _delay_spy)

    asyncio.run(bot_entry.run_discord_startup_loop(client, token="abc", stop_event=stop_event))

    assert client.start_calls == 2
    assert client.close_calls >= 1
    assert delays == [1.0]


def test_cloudflare_1015_is_retryable():
    error = _FakeHTTPException(
        "<html><title>1015</title><body>Cloudflare: You are being rate limited</body></html>"
    )
    assert bot_entry._is_discord_rate_limited_error(error) is True


def test_backoff_grows_and_caps():
    first = bot_entry._compute_backoff_delay(1, base_delay=30, max_delay=900, jitter_max=0)
    second = bot_entry._compute_backoff_delay(2, base_delay=30, max_delay=900, jitter_max=0)
    high = bot_entry._compute_backoff_delay(99, base_delay=30, max_delay=900, jitter_max=0)
    assert first == 30
    assert second == 60
    assert high == 900


def test_successful_run_allows_fresh_backoff_sequence(monkeypatch):
    stop_event_first = asyncio.Event()
    stop_event_second = asyncio.Event()
    calls: list[int] = []

    def _delay_spy(failure_count: int, **_kwargs) -> float:
        calls.append(failure_count)
        return 0.0

    monkeypatch.setattr(bot_entry, "_compute_backoff_delay", _delay_spy)

    async def _stop_first():
        stop_event_first.set()

    async def _stop_second():
        stop_event_second.set()

    first_client = _FakeClient(
        [_FakeHTTPException("rate limited", status=429), _FakeHTTPException("rate limited", status=429), _stop_first]
    )
    second_client = _FakeClient([_FakeHTTPException("rate limited", status=429), _stop_second])

    asyncio.run(
        bot_entry.run_discord_startup_loop(first_client, token="abc", stop_event=stop_event_first)
    )
    asyncio.run(
        bot_entry.run_discord_startup_loop(second_client, token="abc", stop_event=stop_event_second)
    )

    assert calls == [1, 2, 1]


def test_missing_token_fails_fast():
    stop_event = asyncio.Event()
    client = _FakeClient([])
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        asyncio.run(bot_entry.run_discord_startup_loop(client, token="", stop_event=stop_event))


def test_retries_do_not_create_parallel_start_tasks(monkeypatch):
    stop_event = asyncio.Event()
    client = _FakeClient([_FakeHTTPException("rate limited", status=429), lambda: stop_event.set()])
    monkeypatch.setattr(bot_entry, "_compute_backoff_delay", lambda *_args, **_kwargs: 0.0)

    asyncio.run(bot_entry.run_discord_startup_loop(client, token="abc", stop_event=stop_event))

    assert client.max_concurrent_starts == 1
    assert client.start_calls == 2


def test_retry_logging_truncates_large_error_body(monkeypatch, caplog):
    stop_event = asyncio.Event()
    huge_body = "<html>" + ("A" * 5000) + "</html>"
    client = _FakeClient([_FakeHTTPException(huge_body, status=429), lambda: stop_event.set()])
    monkeypatch.setattr(bot_entry, "_compute_backoff_delay", lambda *_args, **_kwargs: 0.0)

    with caplog.at_level(logging.WARNING):
        asyncio.run(bot_entry.run_discord_startup_loop(client, token="abc", stop_event=stop_event))

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "Discord startup rate_limited" in joined
    assert len(joined) < 1200
    assert "AAAAAA" in joined
