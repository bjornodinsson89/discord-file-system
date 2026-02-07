"""Startup smoke check for Happy Jumper API process."""

from web.app import app


def main() -> None:
    paths = {route.path for route in app.routes}
    if "/api/health" not in paths:
        raise SystemExit("health endpoint not registered")
    print("healthcheck: ok")


if __name__ == "__main__":
    main()
