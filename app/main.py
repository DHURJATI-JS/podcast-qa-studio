"""ASGI entry point: ``uvicorn app.main:asgi_app``.

Request path (outermost first):
HostGuard -> Socket.IO (``/socket.io``) | FastAPI[RequestContext -> Talisman (CSP nonce, HSTS, ...)
-> BodyLimit -> CSRF -> GZip -> routes]
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import httpx
import socketio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware

from app.ai import registry
from app.ai.embeddings import Embedder
from app.ai.router import AIRouter
from app.assets import STATIC_DIR, CachedStaticFiles
from app.config import get_settings
from app.db import close_db, init_db
from app.jobs.emitter import ServerEmitter
from app.jobs.service import AIUnavailable, JobConflict
from app.logging_setup import setup_logging
from app.realtime import create_sio
from app.redis_client import close_redis, init_redis
from app.routes import api, auth, health, pages
from app.routes.api import DuplicateSubmit
from app.security.csrf import CSRFMiddleware
from app.security.middleware import BodyLimitMiddleware, HostGuardMiddleware, RequestContextMiddleware, Talisman
from app.security.ratelimit import RateLimited
from app.security.sessions import LoginRequired, SignedOut, clear_session_cookie, current_user, signout_message
from app.services.exports import shutdown_pool
from app.services.podcasts import NotFound
from app.services.tts import KokoroClient
from app.services.tts_cache import AudioDiskCache
from app.templating import render

log = logging.getLogger("app")
settings = get_settings()
setup_logging(settings.log_level, settings.log_json)
sio = create_sio(settings)

GZIP_EXCLUDE = (
    "application/grpc", "application/gzip", "application/x-gzip", "application/zip", "application/pdf", "audio/*",
    "font/woff", "font/woff2", "image/avif", "image/gif", "image/jpeg", "image/png", "image/webp",
    "text/event-stream", "video/*",
    "text/html",  # HTML carries CSRF tokens: never compress it (BREACH)
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(settings.database_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow)
    redis = init_redis(settings.redis_url)
    http = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        headers={"User-Agent": "PodcastQAStudio/1.0"},
    )
    app.state.http = http
    app.state.router = AIRouter(
        registry.build_providers(settings, http), redis, temperature=settings.ai_temperature, cooldown=settings.ai_circuit_cooldown
    )
    app.state.embedder = Embedder(settings, http)
    app.state.kokoro = KokoroClient(settings, http)
    app.state.tts_cache = AudioDiskCache("/tmp/pq-tts-cache" if not settings.is_test else f"/tmp/pq-tts-cache-test-{os.getpid()}")
    app.state.emitter = ServerEmitter(sio)
    app.state.sio = sio
    log.info("%s started (env=%s)", settings.app_name, settings.app_env)
    try:
        yield
    finally:
        await http.aclose()
        shutdown_pool()
        await close_redis()
        await close_db()


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def create_app() -> FastAPI:
    docs = settings.app_env == "development"
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
        middleware=[
            Middleware(RequestContextMiddleware),
            Middleware(Talisman, force_https=settings.force_https, hsts=settings.cookie_secure),
            Middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes),
            Middleware(CSRFMiddleware),
            Middleware(GZipMiddleware, minimum_size=800, exclude_content_types=GZIP_EXCLUDE),
        ],
    )
    app.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(api.router)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(STATIC_DIR / "img" / "favicon.svg", media_type="image/svg+xml")

    async def error_page(request: Request, status: int, title: str, message: str):
        try:
            await current_user(request)
        except Exception:  # noqa: BLE001 - error pages must render even if Redis is down
            request.state.user_loaded = True
            request.state.user = None
        return render(request, "error.html", {"status": status, "title": title, "message": message, "page": "error"}, status_code=status)

    @app.exception_handler(SignedOut)
    async def _signed_out(request: Request, exc: SignedOut):
        """This device was signed out - nothing in the request was processed; say why."""
        from urllib.parse import quote

        from app.templating import flash

        message = signout_message(exc.info)
        if _is_api(request):
            response = JSONResponse(
                {"detail": message, "code": "signed_out", "reason": exc.info.get("reason", "expired")}, status_code=401
            )
        else:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            response = RedirectResponse(f"/login?next={quote(target)}", status_code=303)
        flash(response, message, "error")  # still explained on the sign-in page after a reload
        clear_session_cookie(response)
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if _is_api(request):
            return JSONResponse({"detail": "Please sign in.", "code": "auth"}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        from urllib.parse import quote

        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        if _is_api(request):
            return JSONResponse({"detail": "Not found."}, status_code=404)
        return await error_page(request, 404, "Not found", "That page doesn't exist, or it isn't yours.")

    @app.exception_handler(RateLimited)
    async def _rate_limited(request: Request, exc: RateLimited):
        headers = {"Retry-After": str(exc.retry_after)}
        if _is_api(request):
            return JSONResponse({"detail": exc.message, "code": "rate_limited", "retry_after": exc.retry_after}, status_code=429, headers=headers)
        response = await error_page(request, 429, "Slow down", exc.message)
        response.headers.update(headers)
        return response

    @app.exception_handler(AIUnavailable)
    async def _ai_unavailable(request: Request, exc: AIUnavailable):
        return JSONResponse({"detail": str(exc), "code": "ai_unavailable"}, status_code=503)

    @app.exception_handler(JobConflict)
    async def _conflict(request: Request, exc: JobConflict):
        return JSONResponse({"detail": str(exc), "code": "busy"}, status_code=409)

    @app.exception_handler(DuplicateSubmit)
    async def _duplicate(request: Request, exc: DuplicateSubmit):
        return JSONResponse({"detail": "Duplicate submission ignored.", "code": "duplicate"}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        problems = []
        for err in exc.errors()[:5]:
            field = ".".join(str(p) for p in err.get("loc", [])[1:]) or "request"
            problems.append(f"{field}: {err.get('msg', 'invalid value')}")
        detail = "Please check your input - " + "; ".join(problems)
        if _is_api(request):
            return JSONResponse({"detail": detail, "code": "validation"}, status_code=422)
        return await error_page(request, 422, "Invalid input", detail)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        if _is_api(request):
            return JSONResponse({"detail": message}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
        if exc.status_code == 404:
            return await error_page(request, 404, "Not found", "That page doesn't exist, or it isn't yours.")
        if exc.status_code == 405:
            return await error_page(request, 405, "Not allowed", "That action isn't available here.")
        return await error_page(request, exc.status_code, "Something went wrong", message)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        if _is_api(request):
            return JSONResponse({"detail": "Something went wrong on our side. Please try again."}, status_code=500)
        return await error_page(request, 500, "Something went wrong", "An unexpected error happened. Please try again.")

    return app


app = create_app()
asgi_app = HostGuardMiddleware(socketio.ASGIApp(sio, other_asgi_app=app, socketio_path="socket.io"), settings.allowed_host_set)
