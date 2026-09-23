"""A resource-limited HTTPS frame receiver for one browser camera pilot."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import anyio
from browser_store import claim_grant, grant_active, revoke_grant, save_gate
from browser_vision import LineTracker, PersonDetector
from db_config import engine
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from live_preview import frame_path, store_frame
from sqlalchemy.exc import SQLAlchemyError
from traffic_store import ingest_crossings, record_health
from visitor_store import save_browser_visitors

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    """Load once; a missing detector keeps the worker unhealthy rather than faking counts."""
    app.state.detector = await asyncio.to_thread(
        PersonDetector, os.environ.get("PERSON_MODEL", "/models/yolox_nano.onnx")
    )
    app.state.busy = False
    yield


app = FastAPI(title="Aivo browser camera worker", lifespan=lifespan)


@app.get("/health")
def health():
    """Report model availability independently of camera activity."""
    return {"ready": True, "capacity": 1}


def persist_frame(grant, tracker, body, now, heartbeat):
    """Run synchronous inference and acknowledge crossings only after their DB commit."""
    boxes = app.state.detector.detect(body)
    events, tracks = tracker.update(boxes, now)
    known = getattr(tracker, "saved_visitors", set())
    visible = {t["id"] for t in tracks}
    if tracks and (heartbeat or events or visible - known):
        save_browser_visitors(engine, grant, tracks, body, now)
        tracker.saved_visitors = visible
    if events:
        ingest_crossings(engine, grant["tenant_id"], events)
    if heartbeat:
        record_health(
            engine,
            grant["tenant_id"],
            {
                "camera_id": grant["camera_id"],
                "mqtt_connected": True,
                "frigate_available": True,
                "last_person_event": now if boxes else None,
                "pending_events": 0,
                "gate_revision": tracker.revision,
            },
        )
        store_frame(grant["tenant_id"], grant["camera_id"], body)
    return events, tracks


def finish(grant, revision):
    """Revoke the session and clear its live preview on any disconnect."""
    revoke_grant(engine, grant["tenant_id"], grant["grant_id"])
    record_health(
        engine,
        grant["tenant_id"],
        {
            "camera_id": grant["camera_id"],
            "mqtt_connected": False,
            "frigate_available": False,
            "last_person_event": None,
            "pending_events": 0,
            "gate_revision": revision,
        },
    )
    frame_path(grant["tenant_id"], grant["camera_id"]).unlink(missing_ok=True)


@app.websocket("/api/browser/ws")
async def webcam_socket(ws: WebSocket):
    """Authenticate in the first frame; never accept a key or camera ID from the URL."""
    if ws.headers.get("origin") != os.environ.get(
        "BROWSER_ORIGIN", "https://frigate.agenticx.ia.br"
    ):
        await ws.close(code=1008)
        return
    if app.state.busy:
        await ws.accept()
        await ws.send_json(
            {
                "type": "error",
                "message": "O piloto já tem uma câmera em análise. Pare a outra sessão e tente novamente.",
            }
        )
        await ws.close(code=1013)
        return
    app.state.busy = True
    grant = None
    revision = "0" * 64
    try:
        await ws.accept()
        initial = await asyncio.wait_for(ws.receive_json(), timeout=8)
        if not isinstance(initial, dict):
            raise TypeError("Invalid handshake")
        grant = await asyncio.to_thread(claim_grant, engine, initial.get("token"))
        gate = await asyncio.to_thread(save_gate, engine, grant, initial.get("gate"))
        tracker = LineTracker(grant["camera_id"], grant["grant_id"], gate)
        revision = tracker.revision
        await ws.send_json({"type": "ready"})
        last_health = 0
        entries = exits = 0
        while True:
            message = await asyncio.wait_for(ws.receive(), timeout=8)
            if message["type"] == "websocket.disconnect":
                break
            body = message.get("bytes")
            if body is None or len(body) > 160000:
                raise ValueError("Invalid frame")
            started = time.monotonic()
            now = time.time()
            heartbeat = now - last_health >= 1
            if heartbeat and not await asyncio.to_thread(grant_active, engine, grant):
                raise PermissionError("Session expired")
            events, tracks = await anyio.to_thread.run_sync(
                persist_frame, grant, tracker, body, now, heartbeat
            )
            if heartbeat:
                last_health = now
            entries += sum(e["direction"] == "entry" for e in events)
            exits += sum(e["direction"] == "exit" for e in events)
            await asyncio.sleep(max(0, 0.2 - (time.monotonic() - started)))
            await ws.send_json(
                {
                    "type": "result",
                    "tracks": tracks,
                    "entries": entries,
                    "exits": exits,
                    "processing_ms": round((time.monotonic() - started) * 1000),
                }
            )
    except WebSocketDisconnect:
        pass
    except (
        PermissionError,
        ValueError,
        TypeError,
        TimeoutError,
        SQLAlchemyError,
        OSError,
    ) as error:
        logger.warning("Browser session stopped (%s)", type(error).__name__)
        try:
            await ws.send_json(
                {
                    "type": "error",
                    "message": "Monitoramento interrompido. Confira a conexão e prepare uma nova sessão.",
                }
            )
            await ws.close(code=1008)
        except (RuntimeError, WebSocketDisconnect, OSError):
            pass
    finally:
        # ASGI can cancel a disconnected task while its final DB write is pending.
        # Shield cleanup so a stopped session cannot keep the single pilot slot busy.
        with anyio.CancelScope(shield=True):
            try:
                if grant:
                    await anyio.to_thread.run_sync(finish, grant, revision)
            except (SQLAlchemyError, OSError, RuntimeError):
                logger.warning("Browser session cleanup needs expiry fallback")
            finally:
                app.state.busy = False
