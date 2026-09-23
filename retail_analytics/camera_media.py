"""Private, short-lived Edge media jobs and bounded live-video relaying."""

import asyncio
import hashlib
import os
import secrets
import time
import uuid
from contextlib import suppress

import anyio
from db_config import engine
from fastapi import (
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from retail_store import own_camera
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

channels = {}


def init_media_db(db):
    binary = "BYTEA" if db.dialect.name == "postgresql" else "BLOB"
    with db.begin() as c:
        c.execute(
            text(f"""CREATE TABLE IF NOT EXISTS retail_media_jobs (
            job_id VARCHAR(32) PRIMARY KEY, tenant_id VARCHAR NOT NULL, camera_id VARCHAR(32) NOT NULL,
            kind VARCHAR(8) NOT NULL, token_hash VARCHAR(64) NOT NULL UNIQUE,
            status VARCHAR(16) NOT NULL, created_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL, lease_until DOUBLE PRECISION NOT NULL,
            start_at DOUBLE PRECISION, end_at DOUBLE PRECISION, data {binary})""")
        )
        c.execute(
            text("""CREATE TABLE IF NOT EXISTS retail_media_slots (
            tenant_id VARCHAR NOT NULL, camera_id VARCHAR(32) NOT NULL,
            job_id VARCHAR(32) NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(tenant_id,camera_id))""")
        )


def create_job(db, tid, cid, kind, start=None, end=None):
    if kind not in ("live", "clip"):
        raise ValueError("Invalid media kind")
    now = time.time()
    if kind == "clip" and (
        start is None
        or end is None
        or not 0 < end - start <= 30
        or start < now - 7 * 86400
        or end > now
    ):
        raise ValueError("Escolha até 30 segundos dentro dos últimos sete dias.")
    token = secrets.token_urlsafe(32)
    jid = uuid.uuid4().hex
    with db.begin() as c:
        own_camera(c, tid, cid)
        result = c.execute(
            text("""INSERT INTO retail_media_slots VALUES (:t,:c,:j,:exp)
            ON CONFLICT(tenant_id,camera_id) DO UPDATE SET job_id=excluded.job_id,expires_at=excluded.expires_at
            WHERE retail_media_slots.expires_at<:now"""),
            {"t": tid, "c": cid, "j": jid, "exp": now + 300, "now": now},
        )
        if result.rowcount != 1:
            raise ValueError(
                "Já há uma solicitação de vídeo nesta câmera. Pare a reprodução ou aguarde sua conclusão."
            )
        c.execute(
            text(
                """INSERT INTO retail_media_jobs VALUES (:j,:t,:c,:kind,:hash,:status,:now,:exp,0,:start,:end,NULL)"""
            ),
            {
                "j": jid,
                "t": tid,
                "c": cid,
                "kind": kind,
                "hash": hashlib.sha256(token.encode()).hexdigest(),
                "status": "waiting" if kind == "live" else "pending",
                "now": now,
                "exp": now + 300,
                "start": start,
                "end": end,
            },
        )
    return {"job_id": jid, "token": token}


def job_result(db, tid, jid):
    with db.connect() as c:
        row = (
            c.execute(
                text(
                    "SELECT * FROM retail_media_jobs WHERE tenant_id=:t AND job_id=:j AND expires_at>:now"
                ),
                {"t": tid, "j": jid, "now": time.time()},
            )
            .mappings()
            .first()
        )
        if not row:
            return None
        own_camera(c, tid, row["camera_id"])
        return dict(row)


def finish_job(db, tid, jid, status="cancelled", data=None):
    if status not in ("cancelled", "ready", "failed"):
        raise ValueError("Invalid status")
    with db.begin() as c:
        c.execute(
            text("""UPDATE retail_media_jobs SET status=:status,data=:data
            WHERE tenant_id=:t AND job_id=:j AND expires_at>:now
            AND status NOT IN ('ready','cancelled','failed')"""),
            {"t": tid, "j": jid, "status": status, "data": data, "now": time.time()},
        )
        c.execute(
            text("DELETE FROM retail_media_slots WHERE tenant_id=:t AND job_id=:j"),
            {"t": tid, "j": jid},
        )


def claim_viewer(db, token):
    if not isinstance(token, str) or len(token) > 100:
        raise PermissionError("Invalid capability")
    with db.begin() as c:
        p = {"hash": hashlib.sha256(token.encode()).hexdigest(), "now": time.time()}
        row = (
            c.execute(
                text(
                    "SELECT * FROM retail_media_jobs WHERE token_hash=:hash AND kind='live' AND status='waiting' AND expires_at>:now"
                ),
                p,
            )
            .mappings()
            .first()
        )
        if not row:
            raise PermissionError("Expired capability")
        own_camera(c, row["tenant_id"], row["camera_id"])
        updated = c.execute(
            text(
                "UPDATE retail_media_jobs SET status='pending' WHERE job_id=:j AND status='waiting'"
            ),
            {"j": row["job_id"]},
        )
        if updated.rowcount != 1:
            raise PermissionError("Used capability")
        return dict(row)


def pending_jobs(db, tid):
    now = time.time()
    jobs = []
    with db.begin() as c:
        c.execute(
            text(
                "UPDATE retail_media_jobs SET data=NULL,status='expired' WHERE tenant_id=:t AND expires_at<:now"
            ),
            {"t": tid, "now": now},
        )
        c.execute(
            text(
                "DELETE FROM retail_media_jobs WHERE tenant_id=:t AND expires_at<:old"
            ),
            {"t": tid, "old": now - 86400},
        )
        rows = (
            c.execute(
                text("""SELECT j.* FROM retail_media_jobs j JOIN cameras c
            ON c.tenant_id=j.tenant_id AND c.camera_id=j.camera_id AND c.enabled=TRUE
            WHERE j.tenant_id=:t AND j.expires_at>:now
            AND (j.status='pending' OR (j.status='claimed' AND j.lease_until<:now))
            ORDER BY j.created_at LIMIT 4"""),
                {"t": tid, "now": now},
            )
            .mappings()
            .all()
        )
        for row in rows:
            changed = c.execute(
                text("""UPDATE retail_media_jobs SET status='claimed',lease_until=:lease
                WHERE job_id=:j AND (status='pending' OR (status='claimed' AND lease_until<:now))"""),
                {"lease": now + 60, "j": row["job_id"], "now": now},
            )
            if changed.rowcount:
                jobs.append(
                    {
                        k: row[k]
                        for k in ("job_id", "camera_id", "kind", "start_at", "end_at")
                    }
                )
    return jobs


async def first_message(ws):
    raw = await asyncio.wait_for(ws.receive_text(), 8)
    if len(raw) > 4096:
        raise ValueError("Handshake too large")
    import json

    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Invalid handshake")
    return value


def close_dashboard_media(db, state):
    """Revoke the live capability when the authenticated page exits."""
    job = state.pop("retail_live_job", None)
    tid = state.get("tenant_id")
    if job and tid:
        finish_job(db, tid, job["job_id"])


def register_media_api(app, auth):
    @app.on_event("startup")
    async def start_media_cleanup():
        async def clean():
            while True:
                await asyncio.sleep(60)

                def purge():
                    with engine.begin() as c:
                        c.execute(
                            text(
                                "UPDATE retail_media_jobs SET data=NULL,status='expired' WHERE expires_at<:now"
                            ),
                            {"now": time.time()},
                        )
                        c.execute(
                            text("DELETE FROM retail_media_jobs WHERE expires_at<:old"),
                            {"old": time.time() - 86400},
                        )
                        c.execute(
                            text(
                                "DELETE FROM retail_media_slots WHERE expires_at<:now"
                            ),
                            {"now": time.time()},
                        )

                with suppress(SQLAlchemyError):
                    await asyncio.to_thread(purge)

        app.state.media_cleanup = asyncio.create_task(clean())

    @app.on_event("shutdown")
    async def stop_media_cleanup():
        task = getattr(app.state, "media_cleanup", None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @app.get("/api/edge/media-jobs")
    def jobs(tid: str = Depends(auth)):
        return {"jobs": pending_jobs(engine, tid)}

    @app.post("/api/edge/media-jobs/{jid}/failed")
    def failed(jid: str, tid: str = Depends(auth)):
        if not job_result(engine, tid, jid):
            raise HTTPException(404, "Job unavailable")
        finish_job(engine, tid, jid, "failed")
        return {"status": "ok"}

    @app.put("/api/edge/media-jobs/{jid}/clip")
    async def clip(jid: str, request: Request, tid: str = Depends(auth)):
        from retail_api import bounded_body

        job = await asyncio.to_thread(job_result, engine, tid, jid)
        if not job or job["kind"] != "clip" or job["status"] != "claimed":
            raise HTTPException(404, "Job unavailable")
        body = await bounded_body(request, 20_000_000)
        if len(body) < 12 or body[4:8] != b"ftyp":
            raise HTTPException(422, "MP4 required")
        await asyncio.to_thread(finish_job, engine, tid, jid, "ready", body)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.websocket("/api/media/view")
    async def viewer(ws: WebSocket):
        if ws.headers.get("origin") != os.environ.get(
            "BROWSER_ORIGIN", "https://frigate.agenticx.ia.br"
        ):
            await ws.close(code=1008)
            return
        job = None
        tasks = []
        try:
            await ws.accept()
            hello = await first_message(ws)
            job = await asyncio.to_thread(claim_viewer, engine, hello.get("token"))
            queue = asyncio.Queue(maxsize=8)
            channels[job["job_id"]] = {
                "queue": queue,
                "publisher": False,
                "tenant_id": job["tenant_id"],
            }
            await ws.send_json({"status": "waiting"})

            async def forward():
                checked = 0
                while time.time() < job["expires_at"]:
                    if time.monotonic() - checked > 2:
                        current = await asyncio.to_thread(
                            job_result, engine, job["tenant_id"], job["job_id"]
                        )
                        if not current or current["status"] in (
                            "cancelled",
                            "failed",
                            "expired",
                        ):
                            return
                        checked = time.monotonic()
                    chunk = await asyncio.wait_for(queue.get(), 30)
                    if chunk is None:
                        return
                    await ws.send_bytes(chunk)

            async def watch():
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return

            tasks = [asyncio.create_task(forward()), asyncio.create_task(watch())]
            await asyncio.wait(
                tasks,
                timeout=max(1, job["expires_at"] - time.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except (
            ValueError,
            TypeError,
            PermissionError,
            TimeoutError,
            WebSocketDisconnect,
            SQLAlchemyError,
        ):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            with anyio.CancelScope(shield=True):
                if job:
                    channels.pop(job["job_id"], None)
                    await anyio.to_thread.run_sync(
                        finish_job, engine, job["tenant_id"], job["job_id"]
                    )
                with suppress(RuntimeError, WebSocketDisconnect):
                    await ws.close()

    @app.websocket("/api/edge/media-publish")
    async def publisher(ws: WebSocket):
        job = None
        channel = None
        try:
            await ws.accept()
            hello = await first_message(ws)
            key = hello.get("api_key")
            if not isinstance(key, str) or len(key) > 512:
                raise PermissionError("Invalid key")
            tid = await asyncio.to_thread(auth, key)
            job = await asyncio.to_thread(job_result, engine, tid, hello.get("job_id"))
            if not job or job["kind"] != "live" or job["status"] != "claimed":
                raise PermissionError("Invalid job")
            channel = channels.get(job["job_id"])
            if not channel or channel["tenant_id"] != tid or channel["publisher"]:
                raise PermissionError("Viewer unavailable")
            channel["publisher"] = True
            while (
                time.time() < job["expires_at"]
                and channels.get(job["job_id"]) is channel
            ):
                body = await asyncio.wait_for(ws.receive_bytes(), 10)
                if not 0 < len(body) <= 131072:
                    raise ValueError("Invalid fragment")
                await asyncio.wait_for(channel["queue"].put(body), 5)
        except (
            HTTPException,
            ValueError,
            TypeError,
            PermissionError,
            TimeoutError,
            WebSocketDisconnect,
            SQLAlchemyError,
        ):
            pass
        finally:
            if channel:
                with suppress(asyncio.QueueFull):
                    channel["queue"].put_nowait(None)
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close()
