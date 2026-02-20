import asyncio

import cogs.events as events


def test_setup_hook_initializes_then_loads_extensions(monkeypatch):
    calls: list[str] = []

    async def _init_db():
        calls.append("init_database")

    async def _run_migrations(_pool):
        calls.append("run_migrations")

    def _init_torn_api():
        calls.append("init_torn_api")

    async def _init_security():
        calls.append("init_security")

    async def _load_extension(name: str):
        calls.append(f"load_extension:{name}")

    monkeypatch.setattr(events.config, "validate_config", lambda: calls.append("validate_config"))
    monkeypatch.setattr(events, "init_database", _init_db)
    monkeypatch.setattr(events, "run_migrations", _run_migrations)
    monkeypatch.setattr(events, "get_pool", lambda: object())
    monkeypatch.setattr(events, "init_torn_api", _init_torn_api)
    monkeypatch.setattr(events, "init_security", _init_security)
    monkeypatch.setattr(events.admin_handlers, "set_bot_instance", lambda _bot: calls.append("set_bot_instance"))
    monkeypatch.setattr(events, "EXTENSIONS", ["cogs.raffles", "cogs.free_raffle"])
    monkeypatch.setattr(events.bot, "load_extension", _load_extension)

    asyncio.run(events.setup_hook())

    assert calls[:4] == ["validate_config", "init_database", "run_migrations", "init_torn_api"]
    assert "load_extension:cogs.raffles" in calls
    assert "load_extension:cogs.free_raffle" in calls
    assert calls.index("run_migrations") < calls.index("load_extension:cogs.raffles")
