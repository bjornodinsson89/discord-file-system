"""Web package for Happy Jumper.

Keep package imports side-effect free to avoid circular import chains.
Import concrete modules directly (for example ``web.app`` or ``web.auth``)
from callers.
"""

__all__: list[str] = []
