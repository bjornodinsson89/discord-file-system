"""
Happy Jumper Web Dashboard - Main Application
FastAPI app serving both dashboard UI and Admin API.
"""

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
import logging
from pathlib import Path

import config
from web.auth import router as auth_router
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
from utils import init_database, get_database, init_security, init_torn_api

log = logging.getLogger("happy_jumper.web")

# ============================================================================
# APP INITIALIZATION
# ============================================================================

app = FastAPI(
    title="Happy Jumper Dashboard",
    description="Admin & Creation Panel for Happy Jumper Discord Bot",
    version="2.0.0"
)


@app.on_event("startup")
async def startup_event():
    """Initialize process-scoped services for API workers."""
    if not config.RUN_WEB:
        log.info("RUN_WEB is disabled; API process will still answer health checks")
        return
    await init_database()
    init_torn_api()
    await init_security()
    log.info("Web process dependencies initialized")


@app.on_event("shutdown")
async def shutdown_event():
    """Close shared resources on process shutdown."""
    try:
        db = get_database()
    except RuntimeError:
        return
    await db.close()

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=list({config.FRONTEND_URL, config.DASHBOARD_URL, "http://localhost:5173", "http://127.0.0.1:5173"}),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# ROUTES
# ============================================================================

# Auth routes
app.include_router(auth_router, prefix="/auth", tags=["Authentication"])

# Admin API routes
app.include_router(guild_router, prefix="/api/guilds", tags=["Guilds"])
app.include_router(sessions_router, prefix="/api/sessions", tags=["Sessions"])
app.include_router(raffles_router, prefix="/api/raffles", tags=["Raffles"])
app.include_router(insurance_router, prefix="/api/insurance", tags=["Insurance"])
app.include_router(settings_router, prefix="/api/settings", tags=["Settings"])
app.include_router(audit_router, prefix="/api/audit", tags=["Audit"])
app.include_router(stats_router, prefix="/api/stats", tags=["Statistics"])
app.include_router(members_router, prefix="/api/members", tags=["Members"])
app.include_router(blacklist_router, prefix="/api/blacklist", tags=["Blacklist"])

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
            return Response(content="Not Found", status_code=404)

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
# HEALTH CHECK
# ============================================================================

@app.get("/api/health")
async def health_check():
    """Health check endpoint for Railway."""
    return {"status": "healthy", "service": "happy-jumper"}

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """Return index.html for 404s (SPA routing)."""
    if request.url.path.startswith(("/api/", "/auth/", "/login/auth/")):
        return Response(content="Not Found", status_code=404)

    if frontend_build.exists():
        return FileResponse(frontend_build / "index.html")

    return Response(content="Not Found", status_code=404)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Consistent JSON error payload for API routes."""
    if request.url.path.startswith("/api/"):
        message = "Internal server error" if exc.status_code >= 500 else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": message, "status": exc.status_code}}
        )
    return Response(content="Not Found", status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Fallback exception mapping with server-side traceback logging."""
    log.exception("Unhandled API exception on %s: %s", request.url.path, exc)
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Internal server error", "status": 500}}
        )
    return Response(content="Internal server error", status_code=500)
