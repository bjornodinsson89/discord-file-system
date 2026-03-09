import asyncio
import importlib


def _reload_config(monkeypatch, **env):
    for key in [
        "DB_POOL_MIN_SIZE",
        "DB_POOL_MAX_SIZE",
        "DB_POOL_MAX_INACTIVE_LIFETIME",
        "DB_HEAVY_WORKER_CONCURRENCY",
        "DB_WORKER_STARTUP_JITTER_SECONDS",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    import config

    return importlib.reload(config)


def test_db_pool_config_env_parsing_and_clamps(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        DB_POOL_MIN_SIZE=0,
        DB_POOL_MAX_SIZE=1,
        DB_POOL_MAX_INACTIVE_LIFETIME=5,
        DB_HEAVY_WORKER_CONCURRENCY=0,
        DB_WORKER_STARTUP_JITTER_SECONDS=-1,
    )

    assert cfg.DB_POOL_MIN_SIZE == 2
    assert cfg.DB_POOL_MAX_SIZE == 2
    assert cfg.DB_POOL_MAX_INACTIVE_LIFETIME == 30
    assert cfg.DB_HEAVY_WORKER_CONCURRENCY == 2
    assert cfg.DB_WORKER_STARTUP_JITTER_SECONDS == 0


def test_create_pool_uses_configured_pool_kwargs(monkeypatch):
    import config as config_module
    import repositories.base as base_module

    monkeypatch.setattr(config_module, "DB_POOL_MIN_SIZE", 3)
    monkeypatch.setattr(config_module, "DB_POOL_MAX_SIZE", 11)
    monkeypatch.setattr(config_module, "DB_POOL_MAX_INACTIVE_LIFETIME", 456)

    captured = {}

    async def fake_create_pool(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(base_module.asyncpg, "create_pool", fake_create_pool)

    pool = asyncio.run(base_module.create_pool())

    assert pool is not None
    assert captured["min_size"] == 3
    assert captured["max_size"] == 11
    assert captured["max_inactive_connection_lifetime"] == 456
