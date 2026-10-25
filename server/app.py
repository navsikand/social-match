"""FastAPI app wiring. FROZEN (integrator): routers land here as agents finish them."""
import asyncio
import contextlib
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import config, db, logging as smlog, sweep
from server import (
    activity,
    chat,
    inbox,
    negotiate,
    pairing,
    photo_api,
    scheduling,
    schedules,
    state,
)
from server.webui import router as webui

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "static")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    log = smlog.get("app")
    db.init_db()
    log.info(
        "startup db=%s log_level=%s ai_configured=%s ai_url_host=%s base_url=%s",
        config.db_path(),
        smlog.level_name(),
        config.ai_api_url() is not None,
        (config.ai_api_url() or "").split("/")[2] if config.ai_api_url() else None,
        config.base_url(),
    )
    task = asyncio.create_task(sweep.loop())
    nego = None
    if config.ai_api_url() is not None:
        # Assistant-vs-assistant offers only run when the chat provider is configured;
        # negotiate.loop is a singleton and refuses a second start.
        nego = asyncio.create_task(negotiate.loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if nego is not None:
            nego.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await nego

app = FastAPI(title="Social Match", version="0.1.0", lifespan=lifespan)
app.include_router(pairing.router)
app.include_router(state.router)
app.include_router(scheduling.router)
app.include_router(activity.router)
app.include_router(inbox.router)
app.include_router(chat.router)
app.include_router(schedules.router)
app.include_router(photo_api.router)
app.include_router(webui.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _sensitive_no_store(request: Request, call_next):
    """Gate review P2: photos, matches and every JSON error are per-viewer and revocable, so
    mark the whole API surface (and served images) non-cacheable, including refusals.

    Also the request log: one line per request with status + duration. Bodies are never
    logged (transcripts and payloads are private); method, path, status and timing are enough
    to see whether a turn was even reached by the server.
    """
    import time

    started = time.monotonic()
    path = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        smlog.get("http").exception(
            "request method=%s path=%s status=EXCEPTION ms=%.1f",
            request.method, path, (time.monotonic() - started) * 1000,
        )
        raise
    if path.startswith("/api/") or path.startswith("/media/"):
        response.headers["Cache-Control"] = "no-store"
    if path.startswith("/api/") or path.startswith("/chat") or path.startswith("/schedule"):
        smlog.get("http").info(
            "request method=%s path=%s status=%s ms=%.1f",
            request.method, path, response.status_code, (time.monotonic() - started) * 1000,
        )
    return response

@app.exception_handler(HTTPException)
async def _flat_http_exception(request: Request, exc: HTTPException):
    """CONTRACT §2 wants {"error", "detail"} at the top level. db.http_error raises
    HTTPException(detail={"error": ...}), which FastAPI would nest under "detail" —
    flattening it here makes every module's error shape identical without touching
    the agents' routers (state/scheduling already answer flat via JSONResponse)."""
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        content = {"error": exc.detail["error"], "detail": exc.detail.get("detail", "")}
    else:
        content = {"detail": exc.detail}
    headers = getattr(exc, "headers", None)
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


@app.get("/health")
def health():
    return {"ok": True, "now": db.utcnow()}
