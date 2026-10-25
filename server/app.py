"""FastAPI app wiring. FROZEN (integrator): routers land here as agents finish them."""
import asyncio
import contextlib
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import db, sweep
from server import inbox, pairing, scheduling, state
from server.webui import router as webui

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "static")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(sweep.loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Social Match", version="0.1.0", lifespan=lifespan)
app.include_router(pairing.router)
app.include_router(state.router)
app.include_router(scheduling.router)
app.include_router(inbox.router)
app.include_router(webui.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
