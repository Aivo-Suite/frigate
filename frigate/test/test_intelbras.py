"""Intelbras integration tests using disposable databases and generated media."""

import io
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import camera_media
import retail_api
from camera_store import camera_snapshot, init_camera_db, save_camera
from cryptography.fernet import Fernet
from edge_provisioner import merge_config, validate_snapshot
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from intelbras_edge import Outbox, normalize_event
from intelbras_kit import build_edge_kit
from PIL import Image
from retail_store import (
    CameraProfile,
    Zone,
    get_profile,
    ingest_samples,
    retail_report,
    save_profile,
    upload_person_photo,
)
from sqlalchemy import create_engine, text
from traffic_store import init_traffic_db
from visitor_store import list_visitors


class IntelbrasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = create_engine("sqlite:///" + self.tmp.name + "/test.db")
        self.env = patch.dict(
            os.environ,
            {
                "CAMERA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "LIVE_PREVIEW_DIR": self.tmp.name + "/frames",
            },
        )
        self.env.start()
        with self.db.begin() as c:
            c.execute(text("CREATE TABLE tenants (tenant_id VARCHAR PRIMARY KEY)"))
            c.execute(text("INSERT INTO tenants VALUES ('one'),('two')"))
        init_camera_db(self.db)
        init_traffic_db(self.db)
        self.cid = save_camera(
            self.db,
            "one",
            "Intelbras entry",
            "192.168.0.10",
            "fixture",
            "fixture-secret",
            1,
        )
        self.now = int(time.time()) - 300
        self.settings = {
            "zones": [
                {
                    "zone_id": "aivo_shelf",
                    "name": "Armações",
                    "role": "area",
                    "points": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "alert_after": 5,
                }
            ],
            "recording": True,
            "retention_days": 1,
        }
        self.revision = save_profile(
            self.db,
            "one",
            self.cid,
            self.settings,
            get_profile(self.db, "one", self.cid)["revision"],
        )

    def tearDown(self):
        self.env.stop()
        self.db.dispose()
        self.tmp.cleanup()

    def sample(self, when=0, **kw):
        return {
            "camera_id": self.cid,
            "tracking_id": "fixture.1",
            "observed_at": self.now + when,
            "x": 0.2,
            "y": 0.4,
            "zones": ["aivo_shelf"],
            "revision": self.revision,
            "ended": False,
            **kw,
        }

    def test_profile_validation_and_concurrent_edit(self):
        for points in (
            [[0, 0], [0, 0], [1, 1]],
            [[0, 0], [1, 1], [0, 1], [1, 0]],
            [[0, 0], [2, 0], [1, 1]],
        ):
            with self.assertRaises(ValueError):
                Zone.model_validate({**self.settings["zones"][0], "points": points})
        with self.assertRaises(ValueError):
            CameraProfile.model_validate({**self.settings, "retention_days": 90})
        save_profile(
            self.db,
            "one",
            self.cid,
            {**self.settings, "recording": False},
            self.revision,
        )
        with self.assertRaises(ValueError):
            save_profile(self.db, "one", self.cid, self.settings, self.revision)
        with self.assertRaises(PermissionError):
            get_profile(self.db, "two", self.cid)

    def test_provisioning_includes_substream_zones_recording_and_keeps_local_camera(
        self,
    ):
        snapshot = validate_snapshot(camera_snapshot(self.db, "one"))
        merged, state = merge_config(
            {"cameras": {"unmanaged": {"enabled": False}}}, snapshot, {}
        )
        name = "aivo_" + self.cid
        camera = merged["cameras"][name]
        self.assertIn("unmanaged", merged["cameras"])
        self.assertIn("subtype=1", camera["ffmpeg"]["inputs"][0]["path"])
        self.assertIn("record", camera["ffmpeg"]["inputs"][1]["roles"])
        self.assertIn("aivo_shelf", camera["zones"])
        self.assertTrue(camera["snapshots"]["enabled"])
        self.assertEqual(camera["record"]["continuous"]["days"], 1)
        self.assertIn(name, merged["go2rtc"]["streams"])
        again, _ = merge_config(merged, snapshot, state)
        self.assertEqual(again, merged)
        removed, _ = merge_config(merged, {**snapshot, "cameras": []}, state)
        self.assertNotIn(name, removed["go2rtc"]["streams"])

    def test_dwell_gaps_retries_and_alert_idempotence(self):
        samples = [
            self.sample(0),
            self.sample(10),
            self.sample(100),
            self.sample(110, ended=True),
        ]
        ingest_samples(self.db, "one", samples)
        ingest_samples(self.db, "one", samples)
        with self.db.connect() as c:
            self.assertEqual(
                c.execute(text("SELECT COUNT(*) FROM retail_alerts")).scalar(), 1
            )
        report = retail_report(self.db, "one", self.cid, self.now - 1, self.now + 200)
        self.assertEqual(report["zones"][0]["seconds"], 20)
        self.assertEqual(sum(x["seconds"] for x in report["heat"]), 20)
        self.assertEqual(len(report["alerts"]), 1)
        self.assertEqual(
            len(
                retail_report(self.db, "one", self.cid, self.now - 1, self.now + 200)[
                    "alerts"
                ]
            ),
            1,
        )
        self.assertEqual(
            list_visitors(self.db, "one", self.now - 1, self.now + 200)["total"], 1
        )
        with self.assertRaises(PermissionError):
            ingest_samples(self.db, "two", [self.sample()])
        with self.assertRaises(ValueError):
            ingest_samples(self.db, "one", [self.sample(x=0.8)])

    def test_photo_requires_owned_observation_and_strips_metadata(self):
        out = io.BytesIO()
        Image.new("RGB", (640, 480), "blue").save(out, format="JPEG")
        with self.assertRaises(LookupError):
            upload_person_photo(self.db, "one", self.cid, "fixture.1", out.getvalue())
        ingest_samples(self.db, "one", [self.sample()])
        upload_person_photo(self.db, "one", self.cid, "fixture.1", out.getvalue())
        visitor = list_visitors(self.db, "one", self.now - 1, self.now + 200)["items"][
            0
        ]
        self.assertTrue(visitor["photo"])
        self.assertEqual(
            list_visitors(self.db, "two", self.now - 1, self.now + 200)["items"], []
        )
        with self.assertRaises(PermissionError):
            upload_person_photo(self.db, "two", self.cid, "fixture.1", out.getvalue())

    def test_media_capability_is_single_use_and_foreign_key_cannot_get_job(self):
        job = camera_media.create_job(self.db, "one", self.cid, "live")
        self.assertIsNone(camera_media.job_result(self.db, "two", job["job_id"]))
        with self.assertRaises(ValueError):
            camera_media.create_job(self.db, "one", self.cid, "live")
        camera_media.claim_viewer(self.db, job["token"])
        with self.assertRaises(PermissionError):
            camera_media.claim_viewer(self.db, job["token"])
        self.assertEqual(camera_media.pending_jobs(self.db, "two"), [])
        jobs = camera_media.pending_jobs(self.db, "one")
        self.assertEqual(len(jobs), 1)
        self.assertNotIn("token_hash", jobs[0])
        self.assertEqual(camera_media.pending_jobs(self.db, "one"), [])
        camera_media.finish_job(self.db, "one", job["job_id"])
        camera_media.create_job(
            self.db, "one", self.cid, "clip", self.now, self.now + 20
        )

    def test_clip_limits_and_expiration(self):
        with self.assertRaises(ValueError):
            camera_media.create_job(
                self.db, "one", self.cid, "clip", self.now, self.now + 90
            )
        job = camera_media.create_job(
            self.db, "one", self.cid, "clip", self.now, self.now + 20
        )
        camera_media.pending_jobs(self.db, "one")
        camera_media.finish_job(self.db, "one", job["job_id"], "ready", b"fixture")
        self.assertEqual(
            camera_media.job_result(self.db, "one", job["job_id"])["data"], b"fixture"
        )
        with self.db.begin() as c:
            c.execute(text("UPDATE retail_media_jobs SET expires_at=1"))
        self.assertIsNone(camera_media.job_result(self.db, "one", job["job_id"]))
        camera_media.pending_jobs(self.db, "one")
        with self.db.connect() as c:
            self.assertIsNone(
                c.execute(text("SELECT data FROM retail_media_jobs")).scalar()
            )

    def test_outbox_survives_restart_and_preserves_inflight_update(self):
        path = Path(self.tmp.name) / "outbox.db"
        box = Outbox(path)
        box.bind("one")
        payload = self.sample()
        box.put("sample", "sample", payload)
        saved = box.pending("sample")[0]
        box.put("sample", "sample", {**payload, "ended": True})
        box.ack("sample", "sample", saved["payload"])
        other = Outbox(path)
        self.assertTrue(other.pending("sample")[0]["payload"]["ended"])
        with self.assertRaises(ValueError):
            other.bind("two")

    def test_normalize_filters_unconfirmed_person_and_foreign_camera(self):
        camera = camera_snapshot(self.db, "one")["cameras"][0]
        message = {
            "type": "update",
            "after": {
                "camera": camera["frigate_name"],
                "label": "person",
                "false_positive": False,
                "id": "fixture.1",
                "frame_time": self.now,
                "box": [0, 0, 320, 240],
                "current_zones": ["aivo_shelf"],
            },
        }
        item = normalize_event(message, camera, (640, 480))
        self.assertEqual((item["x"], item["y"]), (0.25, 0.5))
        message["after"]["false_positive"] = True
        self.assertIsNone(normalize_event(message, camera, (640, 480)))
        message["after"]["false_positive"] = False
        message["after"]["camera"] = "other"
        self.assertIsNone(normalize_event(message, camera, (640, 480)))

    def test_kit_has_runtime_sources_and_no_keys(self):
        kit = zipfile.ZipFile(io.BytesIO(build_edge_kit()))
        for name in (
            "intelbras_edge.py",
            "edge_provisioner.py",
            "retail_store.py",
            "visitor_store.py",
            "Dockerfile.edge",
            "compose.yml",
            "install.sh",
            "config/config.yml",
        ):
            self.assertIn(name, kit.namelist())
        self.assertFalse(any(".secrets/" in name for name in kit.namelist()))
        self.assertNotIn("fixture-secret", kit.read("compose.yml").decode())

    def test_authenticated_http_and_websocket_boundary(self):
        def auth(x_api_key: str = Header(...)):
            if x_api_key not in ("key-one", "key-two"):
                raise HTTPException(401)
            return "one" if x_api_key == "key-one" else "two"

        app = FastAPI()
        retail_api.register_retail_api(app, auth)
        camera_media.register_media_api(app, auth)
        with (
            patch.object(retail_api, "engine", self.db),
            patch.object(camera_media, "engine", self.db),
            TestClient(app) as client,
        ):
            self.assertEqual(
                client.post(
                    "/api/edge/observations",
                    json={"samples": [self.sample()]},
                    headers={"x-api-key": "key-two"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/edge/observations",
                    json={"samples": [self.sample()]},
                    headers={"x-api-key": "key-one"},
                ).status_code,
                200,
            )
            job = camera_media.create_job(self.db, "one", self.cid, "live")
            with client.websocket_connect(
                "/api/media/view", headers={"origin": "https://frigate.agenticx.ia.br"}
            ) as viewer:
                viewer.send_json({"token": job["token"]})
                self.assertEqual(viewer.receive_json()["status"], "waiting")
                self.assertEqual(
                    len(
                        client.get(
                            "/api/edge/media-jobs", headers={"x-api-key": "key-one"}
                        ).json()["jobs"]
                    ),
                    1,
                )
                with client.websocket_connect("/api/edge/media-publish") as publisher:
                    publisher.send_json({"api_key": "key-one", "job_id": job["job_id"]})
                    publisher.send_bytes(b"isolated-fragment")
                    self.assertEqual(viewer.receive_bytes(), b"isolated-fragment")
            self.assertEqual(camera_media.channels, {})
            self.assertEqual(
                camera_media.job_result(self.db, "one", job["job_id"])["status"],
                "cancelled",
            )

    def test_customer_pages_render_real_observation_without_public_photo_url(self):
        from streamlit.testing.v1 import AppTest

        ingest_samples(self.db, "one", [self.sample(), self.sample(10)])
        for page in (
            "render_cameras",
            "render_zones",
            "render_behavior",
            "render_history",
        ):
            script = "\n".join(
                [
                    "import streamlit as st",
                    "from sqlalchemy import create_engine",
                    "from retail_dashboard import " + page,
                    "st.session_state['tenant_id']='one'",
                    "engine=create_engine(" + repr(str(self.db.url)) + ")",
                    page + "(engine,'one')",
                ]
            )
            with self.subTest(page=page):
                app = AppTest.from_string(script).run(timeout=20)
                self.assertEqual(list(app.exception), [])
                for item in app.markdown:
                    self.assertNotIn("/media/", item.value)

    def test_disabled_camera_and_logout_revoke_access(self):
        job = camera_media.create_job(self.db, "one", self.cid, "live")
        state = {"tenant_id": "one", "retail_live_job": job}
        camera_media.close_dashboard_media(self.db, state)
        with self.assertRaises(PermissionError):
            camera_media.claim_viewer(self.db, job["token"])
        with self.db.begin() as c:
            c.execute(
                text("UPDATE cameras SET enabled=FALSE WHERE camera_id=:id"),
                {"id": self.cid},
            )
        with self.assertRaises(PermissionError):
            ingest_samples(self.db, "one", [self.sample()])

    def test_photo_retry_rotation_does_not_block_ready_photos(self):
        queue = Outbox(Path(self.tmp.name) / "rotate.db")
        queue.put("photo", "missing", {"tracking_id": "missing"})
        queue.put("photo", "ready", {"tracking_id": "ready"}, b"jpeg")
        self.assertEqual(queue.pending("photo", 1, True)[0]["key"], "ready")
        self.assertEqual(queue.pending("photo", 1, False)[0]["key"], "missing")
        queue.defer("photo", "missing")
        self.assertEqual(queue.size(), 2)


if __name__ == "__main__":
    unittest.main()
