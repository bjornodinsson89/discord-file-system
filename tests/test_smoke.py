import asyncio


def test_imports():
    import api.main  # noqa: F401
    import web.app  # noqa: F401
    import bot  # noqa: F401


def test_web_health_endpoint_function():
    from web.app import health_check

    payload = asyncio.run(health_check())
    assert payload["status"] == "healthy"
    assert payload["service"] == "happy-jumper"
    assert payload["mode"] in {"WEB", "BOT"}
