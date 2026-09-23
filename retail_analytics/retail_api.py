"""Authenticated Edge uploads for production Intelbras cameras."""

import asyncio
import time

from db_config import engine
from fastapi import Depends, HTTPException, Request, Response
from live_preview import store_frame
from pydantic import BaseModel, Field
from retail_store import (
    Samples,
    ingest_samples,
    known_profile,
    own_camera,
    upload_person_photo,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


class EdgeHealth(BaseModel):
    camera_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    camera_fps: float = Field(ge=0, le=240, allow_inf_nan=False)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    queue_size: int = Field(ge=0, le=10000000)


async def bounded_body(request, limit):
    """Read a bounded binary body without trusting Content-Length."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, "Upload too large")
    return bytes(body)


def register_retail_api(app, auth):
    """Attach routes with the same store key dependency as existing ingestion."""

    @app.post("/api/edge/observations")
    def observations(payload: Samples, tid: str = Depends(auth)):
        try:
            count = ingest_samples(
                engine, tid, [s.model_dump() for s in payload.samples]
            )
            return {"accepted": count}
        except PermissionError:
            raise HTTPException(403, "Camera unavailable") from None
        except ValueError:
            raise HTTPException(409, "Invalid or conflicting observation") from None
        except SQLAlchemyError:
            raise HTTPException(503, "Storage unavailable") from None

    @app.put("/api/edge/photos/{camera_id}/{tracking_id}")
    async def photo(
        camera_id: str, tracking_id: str, request: Request, tid: str = Depends(auth)
    ):
        body = await bounded_body(request, 1000000)
        try:
            await asyncio.to_thread(
                upload_person_photo, engine, tid, camera_id, tracking_id, body
            )
        except PermissionError:
            raise HTTPException(403, "Camera unavailable") from None
        except LookupError:
            raise HTTPException(409, "Observation not yet received") from None
        except (ValueError, OSError):
            raise HTTPException(422, "Invalid JPEG") from None
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.put("/api/edge/preview/{camera_id}")
    async def preview(camera_id: str, request: Request, tid: str = Depends(auth)):
        def owned():
            with engine.connect() as c:
                own_camera(c, tid, camera_id)

        try:
            await asyncio.to_thread(owned)
        except PermissionError:
            raise HTTPException(403, "Camera unavailable") from None
        body = await bounded_body(request, 500000)
        try:
            await asyncio.to_thread(store_frame, tid, camera_id, body)
        except (ValueError, OSError):
            raise HTTPException(422, "Invalid JPEG") from None
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.post("/api/edge/health")
    def health(payload: EdgeHealth, tid: str = Depends(auth)):
        try:
            with engine.begin() as c:
                own_camera(c, tid, payload.camera_id)
                known_profile(c, tid, payload.camera_id, payload.revision)
                c.execute(
                    text("""INSERT INTO retail_edge_health VALUES (:tid,:camera_id,:now,:camera_fps,:revision,:queue_size)
                    ON CONFLICT(tenant_id,camera_id) DO UPDATE SET received_at=excluded.received_at,
                    camera_fps=excluded.camera_fps,revision=excluded.revision,queue_size=excluded.queue_size"""),
                    {"tid": tid, "now": time.time(), **payload.model_dump()},
                )
        except PermissionError:
            raise HTTPException(403, "Camera unavailable") from None
        except ValueError:
            raise HTTPException(409, "Unknown calibration") from None
        return {"status": "ok"}
