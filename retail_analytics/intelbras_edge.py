"""Production Frigate connector: durable observations, photos and on-demand media."""

import asyncio
import fcntl
import json
import logging
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import aiomqtt
import httpx
import websockets
from crossing_counter import CrossingCounter
from edge_provisioner import validate_snapshot

log = logging.getLogger(__name__)


class Outbox:
    """Keep metadata and photo bytes until the cloud acknowledges delivery."""

    def __init__(self, path):
        self.path = path
        with self.connect() as c:
            c.executescript("""PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (kind TEXT NOT NULL,key TEXT NOT NULL,payload TEXT NOT NULL,
                body BLOB,created REAL NOT NULL,PRIMARY KEY(kind,key));""")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def bind(self, tid):
        with self.connect() as c:
            old = c.execute("SELECT value FROM meta WHERE name='tenant'").fetchone()
            if old and old[0] != tid:
                raise ValueError("Edge belongs to another tenant")
            c.execute("INSERT OR IGNORE INTO meta VALUES ('tenant',?)", (tid,))

    def cache(self, snapshot=None):
        with self.connect() as c:
            if snapshot is not None:
                clean = json.loads(json.dumps(snapshot))
                for camera in clean["cameras"]:
                    camera.pop("rtsp_url", None)
                c.execute(
                    "INSERT INTO meta VALUES ('snapshot',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (json.dumps(clean),),
                )
                return clean
            row = c.execute("SELECT value FROM meta WHERE name='snapshot'").fetchone()
            return json.loads(row[0]) if row else None

    def put(self, kind, key, payload, body=None):
        with self.connect() as c:
            c.execute(
                """INSERT INTO pending VALUES (?,?,?,?,?) ON CONFLICT(kind,key)
                DO UPDATE SET payload=excluded.payload,body=COALESCE(excluded.body,pending.body)""",
                (kind, key, json.dumps(payload), body, time.time()),
            )

    def defer(self, kind, key):
        """Rotate unavailable media without deleting its durable request."""
        with self.connect() as c:
            c.execute(
                "UPDATE pending SET created=? WHERE kind=? AND key=?",
                (time.time(), kind, key),
            )

    def pending(self, kind, limit=100, ready=None):
        with self.connect() as c:
            return [
                {"key": r[0], "payload": json.loads(r[1]), "body": r[2]}
                for r in c.execute(
                    "SELECT key,payload,body FROM pending WHERE kind=?"
                    + (
                        " AND body IS NULL"
                        if ready is False
                        else " AND body IS NOT NULL"
                        if ready is True
                        else ""
                    )
                    + " ORDER BY created LIMIT ?",
                    (kind, limit),
                )
            ]

    def ack(self, kind, key, expected_payload=None):
        """Do not erase an observation updated while its request was in flight."""
        with self.connect() as c:
            if expected_payload is None:
                c.execute("DELETE FROM pending WHERE kind=? AND key=?", (kind, key))
            else:
                c.execute(
                    "DELETE FROM pending WHERE kind=? AND key=? AND payload=?",
                    (kind, key, json.dumps(expected_payload)),
                )

    def size(self):
        with self.connect() as c:
            return c.execute("SELECT COUNT(*) FROM pending").fetchone()[0]


def normalize_event(message, camera, dimensions):
    """Extract a bounded observation from a genuine Frigate MQTT event."""
    after = message.get("after", {})
    if (
        message.get("type") not in ("new", "update", "end")
        or after.get("camera") != camera["frigate_name"]
    ):
        return None
    if after.get("label") != "person" or after.get("false_positive", True):
        return None
    tracking = after.get("id", "")
    if not isinstance(tracking, str) or not re.fullmatch(
        r"[a-zA-Z0-9_.:-]{1,128}", tracking
    ):
        return None
    width, height = dimensions
    box = after.get("box")
    if not box or len(box) != 4 or not width or not height:
        return None
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
        return None
    x = (box[0] + box[2]) / 2 / width
    y = box[3] / height
    names = {z["zone_id"] for z in camera["analytics"]["settings"]["zones"]}
    return {
        "camera_id": camera["camera_id"],
        "tracking_id": tracking,
        "observed_at": after["frame_time"],
        "x": min(1, max(0, x)),
        "y": min(1, max(0, y)),
        "zones": sorted(set(after.get("current_zones", [])) & names),
        "revision": camera["analytics"]["revision"],
        "ended": message["type"] == "end",
    }


class Edge:
    def __init__(self, cloud, key, data, config_dir):
        self.authorized = False
        self.cloud = cloud
        self.key = key
        self.data = data
        self.config_dir = config_dir
        self.outbox = Outbox(data / "intelbras.db")
        self.snapshot = self.outbox.cache()
        self.cameras = {}
        self.counters = {}
        self.dimensions = {}
        self.mqtt = False
        self.last_samples = {}
        self.photos = set()
        self.tasks = {}
        self.remote = httpx.AsyncClient(
            base_url=cloud,
            headers={"x-api-key": key},
            timeout=25,
            follow_redirects=False,
        )
        self.local = httpx.AsyncClient(
            base_url="http://frigate:5000", timeout=15, follow_redirects=False
        )
        if self.snapshot:
            self.configure(self.snapshot)

    def configure(self, snapshot):
        self.outbox.bind(snapshot["tenant_id"])
        cameras = {c["frigate_name"]: c for c in snapshot["cameras"]}
        for name, camera in cameras.items():
            roles = {
                z["role"]: z["zone_id"]
                for z in camera["analytics"]["settings"]["zones"]
                if z["role"] != "area"
            }
            old = self.cameras.get(name)
            if old and old.get("analytics") == camera.get("analytics"):
                continue
            self.counters.pop(name, None)
            if "outside" in roles and "inside" in roles:
                counter = CrossingCounter(
                    self.data / (camera["camera_id"] + ".db"),
                    camera["camera_id"],
                    camera["analytics"]["revision"],
                    roles["outside"],
                    roles["inside"],
                    max_gap_seconds=75,
                    camera_name=name,
                )
                counter.bind_tenant(snapshot["tenant_id"])
                counter.reset_tracks()
                self.counters[name] = counter
        for name in set(self.counters) - set(cameras):
            self.counters.pop(name)
        self.cameras = cameras
        self.snapshot = snapshot

    async def sync(self):
        while True:
            try:
                response = await self.remote.get("/api/config/cameras")
                response.raise_for_status()
                snapshot = validate_snapshot(response.json())
                await asyncio.to_thread(self.outbox.bind, snapshot["tenant_id"])
                self.authorized = True
                state = json.loads(
                    (self.config_dir / ".aivo-camera-state.json").read_text()
                )
                if state.get("revision") == snapshot["revision"]:
                    await asyncio.to_thread(self.configure, snapshot)
                    await asyncio.to_thread(self.outbox.cache, snapshot)
                config = (await self.local.get("/api/config")).json()
                for name in self.cameras:
                    detect = config.get("cameras", {}).get(name, {}).get("detect", {})
                    self.dimensions[name] = (detect.get("width"), detect.get("height"))
            except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
                log.warning("Waiting for applied camera configuration")
            await asyncio.sleep(10)

    async def consume(self):
        while True:
            try:
                for counter in list(self.counters.values()):
                    await asyncio.to_thread(counter.reset_tracks)
                async with aiomqtt.Client(
                    hostname="mqtt", identifier="aivo-intelbras-connector"
                ) as mqtt:
                    await mqtt.subscribe("aivo_store/events")
                    self.mqtt = True
                    async for event in mqtt.messages:
                        if event.retain:
                            continue
                        try:
                            payload = json.loads(event.payload)
                            after = payload.get("after", {})
                            camera = self.cameras.get(after.get("camera"))
                            if not camera:
                                continue
                            item = normalize_event(
                                payload,
                                camera,
                                self.dimensions.get(
                                    camera["frigate_name"], (None, None)
                                ),
                            )
                            if not item:
                                continue
                            from retail_store import Sample

                            item = Sample.model_validate(item).model_dump()
                            counter = self.counters.get(camera["frigate_name"])
                            if counter:
                                await asyncio.to_thread(counter.process, payload)
                            identity = camera["camera_id"] + ":" + item["tracking_id"]
                            old = self.last_samples.get(identity)
                            if (
                                old
                                and not item["ended"]
                                and item["zones"] == old["zones"]
                                and item["observed_at"] - old["observed_at"] < 1
                            ):
                                continue
                            key = identity + ":" + str(item["observed_at"])
                            await asyncio.to_thread(
                                self.outbox.put, "sample", key, item
                            )
                            self.last_samples[identity] = item
                            if identity not in self.photos:
                                await asyncio.to_thread(
                                    self.outbox.put,
                                    "photo",
                                    identity,
                                    {
                                        "camera_id": camera["camera_id"],
                                        "tracking_id": item["tracking_id"],
                                    },
                                )
                                self.photos.add(identity)
                            if len(self.last_samples) > 10000:
                                cutoff = time.time() - 3600
                                self.last_samples = {
                                    k: v
                                    for k, v in self.last_samples.items()
                                    if v["observed_at"] > cutoff
                                }
                                self.photos.intersection_update(self.last_samples)
                        except (ValueError, KeyError, TypeError):
                            log.warning("Ignored invalid person event")
            except aiomqtt.MqttError:
                log.warning("MQTT unavailable; reconnecting")
            finally:
                self.mqtt = False
            await asyncio.sleep(5)

    async def collect_photos(self):
        while True:
            for job in await asyncio.to_thread(self.outbox.pending, "photo", 30, False):
                if job["body"]:
                    continue
                p = job["payload"]
                try:
                    response = await self.local.get(
                        "/api/events/" + p["tracking_id"] + "/snapshot.jpg",
                        params={
                            "crop": 1,
                            "height": 384,
                            "quality": 80,
                            "timestamp": 0,
                            "bbox": 0,
                        },
                    )
                    response.raise_for_status()
                    if len(
                        response.content
                    ) > 1000000 or not response.content.startswith(b"\xff\xd8"):
                        raise ValueError("Invalid JPEG")
                    await asyncio.to_thread(
                        self.outbox.put, "photo", job["key"], p, response.content
                    )
                except (httpx.HTTPError, ValueError):
                    await asyncio.to_thread(self.outbox.defer, "photo", job["key"])
            await asyncio.sleep(3)

    async def deliver(self):
        while True:
            if not self.authorized:
                await asyncio.sleep(2)
                continue
            try:
                batch = await asyncio.to_thread(self.outbox.pending, "sample")
                if batch:
                    result = await self.remote.post(
                        "/api/edge/observations",
                        json={"samples": [x["payload"] for x in batch]},
                    )
                    result.raise_for_status()
                    if result.json().get("accepted") != len(batch):
                        raise ValueError("Missing acknowledgment")
                    for job in batch:
                        await asyncio.to_thread(
                            self.outbox.ack, "sample", job["key"], job["payload"]
                        )
                from edge_counter import send_pending

                for counter in list(self.counters.values()):
                    await send_pending(self.remote, counter)
                for job in await asyncio.to_thread(
                    self.outbox.pending, "photo", 10, True
                ):
                    if not job["body"]:
                        continue
                    p = job["payload"]
                    result = await self.remote.put(
                        "/api/edge/photos/" + p["camera_id"] + "/" + p["tracking_id"],
                        content=job["body"],
                        headers={"content-type": "image/jpeg"},
                    )
                    if result.status_code == 409:
                        continue
                    result.raise_for_status()
                    await asyncio.to_thread(self.outbox.ack, "photo", job["key"])
            except (httpx.HTTPError, ValueError):
                log.warning("Cloud delivery pending; data retained on Edge")
            await asyncio.sleep(2)

    async def status(self):
        while True:
            if not self.authorized:
                await asyncio.sleep(2)
                continue
            try:
                response = await self.local.get("/api/stats")
                response.raise_for_status()
                stats = response.json()
                fresh = (
                    time.time() - stats.get("service", {}).get("last_updated", 0) < 30
                )
                for name, camera in list(self.cameras.items()):
                    fps = (
                        stats.get("cameras", {}).get(name, {}).get("camera_fps", 0)
                        if fresh
                        else 0
                    )
                    queue = await asyncio.to_thread(self.outbox.size)
                    r = await self.remote.post(
                        "/api/edge/health",
                        json={
                            "camera_id": camera["camera_id"],
                            "camera_fps": fps,
                            "revision": camera["analytics"]["revision"],
                            "queue_size": queue,
                        },
                    )
                    r.raise_for_status()
                    r = await self.remote.post(
                        "/api/traffic/heartbeat",
                        json={
                            "camera_id": camera["camera_id"],
                            "mqtt_connected": self.mqtt,
                            "frigate_available": fps > 0,
                            "last_person_event": None,
                            "pending_events": queue,
                            "gate_revision": camera["analytics"]["revision"],
                        },
                    )
                    r.raise_for_status()
                    if fps > 0:
                        image = await self.local.get(
                            "/api/" + name + "/latest.jpg", params={"height": 480}
                        )
                        image.raise_for_status()
                        if len(image.content) <= 500000:
                            r = await self.remote.put(
                                "/api/edge/preview/" + camera["camera_id"],
                                content=image.content,
                                headers={"content-type": "image/jpeg"},
                            )
                            r.raise_for_status()
            except (httpx.HTTPError, ValueError, TypeError):
                log.warning("Camera health or preview unavailable")
            await asyncio.sleep(5)

    async def media_job(self, job):
        cid = job["camera_id"]
        name = "aivo_" + cid
        if name not in self.cameras:
            return
        try:
            if job["kind"] == "clip":
                url = (
                    f"/api/{name}/start/{job['start_at']}/end/{job['end_at']}/clip.mp4"
                )
                body = bytearray()
                async with self.local.stream("GET", url, timeout=60) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 20_000_000:
                            raise ValueError("Clip exceeds cloud limit")
                result = await self.remote.put(
                    "/api/edge/media-jobs/" + job["job_id"] + "/clip",
                    content=bytes(body),
                    headers={"content-type": "video/mp4"},
                )
                result.raise_for_status()
            else:
                await self.live(job, name)
        except (
            httpx.HTTPError,
            ValueError,
            OSError,
            websockets.WebSocketException,
            TimeoutError,
        ):
            log.warning("Requested camera media is unavailable")
            with __import__("contextlib").suppress(httpx.HTTPError):
                await self.remote.post(
                    "/api/edge/media-jobs/" + job["job_id"] + "/failed"
                )

    async def live(self, job, name):
        process = None
        try:
            async with websockets.connect(
                self.cloud.replace("https://", "wss://", 1) + "/api/edge/media-publish",
                max_size=131072,
                open_timeout=10,
            ) as ws:
                await ws.send(
                    json.dumps({"api_key": self.key, "job_id": job["job_id"]})
                )
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    "rtsp://frigate:8554/" + name,
                    "-an",
                    "-vf",
                    "scale=640:-2",
                    "-r",
                    "10",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-tune",
                    "zerolatency",
                    "-pix_fmt",
                    "yuv420p",
                    "-g",
                    "10",
                    "-movflags",
                    "frag_keyframe+empty_moov+default_base_moof",
                    "-f",
                    "mp4",
                    "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 290
                while time.monotonic() < deadline:
                    chunk = await asyncio.wait_for(process.stdout.read(65536), 10)
                    if not chunk:
                        break
                    await ws.send(chunk)
        finally:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    async def media(self):
        while True:
            if not self.authorized:
                await asyncio.sleep(2)
                continue
            try:
                response = await self.remote.get("/api/edge/media-jobs")
                response.raise_for_status()
                for job in response.json()["jobs"]:
                    if job["job_id"] not in self.tasks:
                        self.tasks[job["job_id"]] = asyncio.create_task(
                            self.media_job(job)
                        )
                for jid, task in list(self.tasks.items()):
                    if task.done():
                        task.result()
                        self.tasks.pop(jid)
            except (httpx.HTTPError, ValueError):
                log.warning("Media requests waiting for connectivity")
            await asyncio.sleep(2)


async def run():
    os.umask(0o077)
    cloud = os.environ["CLOUD_API_URL"].rstrip("/")
    url = urlsplit(cloud)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.path
        or url.query
        or url.fragment
    ):
        raise ValueError("HTTPS origin required")
    key = Path(os.environ["TENANT_API_KEY_FILE"]).read_text().strip()
    data = Path("/data")
    data.mkdir(exist_ok=True)
    with (data / "connector.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        edge = Edge(cloud, key, data, Path("/frigate-config"))
        tasks = [
            asyncio.create_task(fn())
            for fn in (
                edge.sync,
                edge.consume,
                edge.collect_photos,
                edge.deliver,
                edge.status,
                edge.media,
            )
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            for task in edge.tasks.values():
                task.cancel()
            await asyncio.gather(*tasks, *edge.tasks.values(), return_exceptions=True)
            await edge.remote.aclose()
            await edge.local.aclose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, KeyError):
        log.error("Connector configuration unavailable")
        raise SystemExit(1) from None
