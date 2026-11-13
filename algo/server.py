"""FastAPI backend for the GrowwAlgo dashboard.

REST for state and controls, a WebSocket that pushes a fresh snapshot every
second, and the static dashboard served from web/.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import WEB_DIR, Settings
from .engine import TradingEngine

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("growwalgo.server")

engine = TradingEngine(Settings())
engine.load_saved_prefs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine.settings.auto_start:
        engine.start()
    log.info(
        "Dashboard ready - %s mode | prices: %s | orders: %s",
        engine.mode,
        engine.feed.kind,
        engine.executor.kind,
    )
    yield
    engine.stop()


app = FastAPI(title="GrowwAlgo", version="1.0.0", lifespan=lifespan)


class ControlRequest(BaseModel):
    action: str


class SettingsRequest(BaseModel):
    risk_level: str | None = None
    universe: list[str] | None = None
    capital: float | None = None


@app.get("/api/state")
def state() -> JSONResponse:
    return JSONResponse(engine.snapshot())


@app.get("/api/trades")
def trades(limit: int = 60) -> JSONResponse:
    return JSONResponse({"trades": engine.store.trades(limit)})


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "mode": engine.mode,
        "feed": engine.feed.kind,
        "execution": engine.executor.kind,
        "state": engine._state,
        "credentials": engine.settings.has_credentials,
    }


@app.post("/api/control")
def control(body: ControlRequest) -> JSONResponse:
    action = body.action.strip().lower()
    handlers = {
        "start": engine.start,
        "pause": engine.pause,
        "resume": engine.resume,
        "stop": engine.stop,
        "close_all": engine.close_all,
        "reset_paper": engine.reset_paper,
        "flatten": engine.close_all,
    }
    if action in {"mode_paper", "mode_live"}:
        result = engine.switch_mode(action.split("_", 1)[1])
    elif action in handlers:
        result = handlers[action]()
    else:
        return JSONResponse({"ok": False, "message": f"Unknown action {action}"}, status_code=400)
    payload = engine.snapshot()
    payload["result"] = result
    return JSONResponse(payload)


@app.post("/api/scan")
def scan_market() -> JSONResponse:
    result = engine.start_scan()
    payload = engine.snapshot()
    payload["result"] = result
    return JSONResponse(payload)


@app.post("/api/settings")
def update_settings(body: SettingsRequest) -> JSONResponse:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    result = engine.update_settings(patch)
    payload = engine.snapshot()
    payload["result"] = result
    return JSONResponse(payload, status_code=200 if result.get("ok") else 400)


@app.websocket("/ws")
async def live_feed(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(engine.snapshot())
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 - client went away mid-send
        return


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
