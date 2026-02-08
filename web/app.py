"""
Happy Jumper Web Dashboard - Main Application
FastAPI app serving both dashboard UI and Admin API.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
import logging
from pathlib import Path

import config
from web.auth import router as auth_router
from web.csrf import enforce_csrf, get_or_create_csrf_token
from admin_api.routes import (
    guild_router,
    sessions_router,
    raffles_router,
    insurance_router,
    settings_router,
    audit_router,
    stats_router,
    members_router,
    blacklist_router,
)
from utils import init_database, init_security, init_torn_api

log = logging.getLogger("happy_jumper.web")

# ============================================================================
# APP INITIALIZATION
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and tear down API process resources on the running loop."""
    app.state.mode = "WEB"
    if config.SERVICE_MODE != "WEB":
        raise RuntimeError(
            f"Refusing to start web service with SERVICE_MODE={config.SERVICE_MODE!r}. "
            "Set SERVICE_MODE=WEB for the web service."
        )
    app.state.db = None
    app.state.torn_api = None
    app.state.security = None

    log.info("Starting process mode=WEB")
    config.validate_config()

    db = await init_database()
    torn_api = init_torn_api()
    security = await init_security()

    app.state.db = db
    app.state.torn_api = torn_api
    app.state.security = security
    log.info("WEB process dependencies initialized")

    try:
        yield
    finally:
        if app.state.torn_api is not None:
            await app.state.torn_api.close()
            log.info("Torn API client closed")

        if app.state.db is not None:
            await app.state.db.close()


app = FastAPI(
    title="Happy Jumper Dashboard",
    description="Admin & Creation Panel for Happy Jumper Discord Bot",
    version="2.0.0",
    lifespan=lifespan,
)

# ============================================================================
# MIDDLEWARE
# ============================================================================

# Session middleware for OAuth
app.add_middleware(
    SessionMiddleware,
    secret_key=config.DASHBOARD_SECRET_KEY,
    max_age=86400 * 7,  # 7 days
    https_only=config.SESSION_COOKIE_SECURE,
    same_site=config.SESSION_COOKIE_SAMESITE,
)

# CORS for development
cors_origins = [
    config.FRONTEND_URL,
    config.DASHBOARD_URL,
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
allow_origins = sorted({origin.strip() for origin in cors_origins if origin and origin.strip()})

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ensure_csrf_cookie(request: Request, call_next):
    """Ensure an explicit CSRF token cookie exists for authenticated browser sessions."""
    response = await call_next(request)
    if request.session.get("user"):
        token = get_or_create_csrf_token(request)
        response.set_cookie(
            "csrf_token",
            token,
            secure=config.SESSION_COOKIE_SECURE,
            httponly=False,
            samesite=config.SESSION_COOKIE_SAMESITE,
        )
    return response

# ============================================================================
# ROUTES
# ============================================================================

# Auth routes
app.include_router(auth_router, prefix="/auth", tags=["Authentication"])

# Admin API routes
api_dependencies = [Depends(enforce_csrf)]
app.include_router(guild_router, prefix="/api/guilds", tags=["Guilds"], dependencies=api_dependencies)
app.include_router(sessions_router, prefix="/api/sessions", tags=["Sessions"], dependencies=api_dependencies)
app.include_router(raffles_router, prefix="/api/raffles", tags=["Raffles"], dependencies=api_dependencies)
app.include_router(insurance_router, prefix="/api/insurance", tags=["Insurance"], dependencies=api_dependencies)
app.include_router(settings_router, prefix="/api/settings", tags=["Settings"], dependencies=api_dependencies)
app.include_router(audit_router, prefix="/api/audit", tags=["Audit"], dependencies=api_dependencies)
app.include_router(stats_router, prefix="/api/stats", tags=["Statistics"], dependencies=api_dependencies)
app.include_router(members_router, prefix="/api/members", tags=["Members"], dependencies=api_dependencies)
app.include_router(blacklist_router, prefix="/api/blacklist", tags=["Blacklist"], dependencies=api_dependencies)

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/api/health")
@app.get("/health")
async def health_check():
    """Health check endpoint for Railway."""
    mode = getattr(app.state, "mode", "WEB")
    return {"status": "healthy", "service": "happy-jumper", "mode": mode}

# ============================================================================
# STATIC FILES & SPA
# ============================================================================

frontend_build = Path(__file__).parent.parent / "frontend" / "dist"

if frontend_build.exists():
    # Serve static assets
    app.mount("/assets", StaticFiles(directory=frontend_build / "assets"), name="assets")

    @app.get("/", response_class=HTMLResponse)
    async def spa_root():
        """Serve the SPA entrypoint at the site root.

        In some FastAPI/Starlette versions, the catch-all route
        ("/{full_path:path}") may not match an empty path reliably. Having an
        explicit root route avoids a default 404 JSON response.
        """
        index_file = frontend_build / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"error": "Frontend not built"}

    # Serve index.html for all other routes (SPA)
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve React SPA for all non-API routes."""
        # Let API/auth paths fall through to normal 404 handling.
        if full_path.startswith(("api/", "auth/", "login/auth/")):
            return JSONResponse(status_code=404, content={"detail": "Not Found", "code": "not_found"})

        index_file = frontend_build / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"error": "Frontend not built"}
else:
    @app.get("/")
    async def root():
        return {
            "message": "Happy Jumper Dashboard API",
            "status": "Frontend not built - run 'npm run build' in frontend directory",
            "docs": "/docs"
        }

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """Return index.html for SPA routes and JSON errors for API routes."""
    if request.url.path.startswith(("/api/", "/auth/", "/login/auth/")):
        return JSONResponse(
            status_code=404,
            content={"detail": "Not Found", "code": "not_found"},
        )

    if frontend_build.exists():
        return FileResponse(frontend_build / "index.html")

    return Response(content="Not Found", status_code=404)


def _route_name(request: Request) -> str:
    endpoint = request.scope.get("endpoint")
    return getattr(endpoint, "__name__", "unknown_route")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Consistent JSON error payload for API routes."""
    route = _route_name(request)
    if request.url.path.startswith("/api/"):
        if exc.status_code >= 500:
            log.exception("HTTPException on route=%s path=%s status=%s", route, request.url.path, exc.status_code)
        message = "Internal server error" if exc.status_code >= 500 else str(exc.detail)
        code = "internal_error" if exc.status_code >= 500 else "http_error"
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": message, "code": code, "route": route},
        )
    return Response(content=str(exc.detail), status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Fallback exception mapping with server-side traceback logging."""
    route = _route_name(request)
    log.exception("Unhandled exception route=%s path=%s", route, request.url.path)
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "code": "internal_error", "route": route},
        )
    return Response(content="Internal server error", status_code=500)
