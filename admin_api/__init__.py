"""Admin API package for Happy Jumper.

This package intentionally avoids importing routers/handlers at import time to
prevent circular imports. Import from ``admin_api.routes`` or
``admin_api.handlers`` explicitly where needed.
"""

__all__: list[str] = []
