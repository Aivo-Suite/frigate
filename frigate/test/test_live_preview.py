"""Tests for bounded live images and authenticated tenant isolation."""

import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text

os.environ["DATABASE_URL"] = "sqlite://"
import cloud_api
from live_preview import frame_path, load_frame, store_frame

CID = "a" * 32


def jpeg() -> bytes:
    """Make an artificial test image, never capture the user's webcam."""
    stream = io.BytesIO()
    Image.new("RGB", (640, 480), color="blue").save(stream, format="JPEG")
    return stream.getvalue()


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ, {"LIVE_PREVIEW_DIR": self.tmp.name + "/frames"}
        )
        self.env.start()
        self.engine = create_engine("sqlite:///" + self.tmp.name + "/test.db")
        with self.engine.begin() as conn:
            conn.execute(
                text("CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, api_key TEXT)")
            )
            conn.execute(
                text("INSERT INTO tenants VALUES ('one','key-one'),('two','key-two')")
            )
            conn.execute(
                text(
                    "CREATE TABLE traffic_webcam_sources (tenant_id TEXT, camera_id TEXT)"
                )
            )
            conn.execute(
                text("INSERT INTO traffic_webcam_sources VALUES ('one',:cid)"),
                {"cid": CID},
            )
        self.engine_patch = patch.object(cloud_api, "engine", self.engine)
        self.engine_patch.start()
        self.client = TestClient(cloud_api.app)

    def tearDown(self):
        self.client.close()
        self.engine_patch.stop()
        self.env.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    def upload(self, body=None, key="key-one", content_type="image/jpeg"):
        return self.client.put(
            "/api/traffic/webcam-preview/" + CID,
            content=jpeg() if body is None else body,
            headers={"x-api-key": key, "content-type": content_type},
        )

    def test_valid_upload_replaces_only_latest_frame(self):
        self.assertEqual(self.upload().status_code, 204)
        self.assertEqual(self.upload().status_code, 204)
        self.assertEqual(len(list(Path(self.tmp.name + "/frames").iterdir())), 1)
        frame, age = load_frame("one", CID)
        self.assertLess(age, 2)
        self.assertEqual(Image.open(io.BytesIO(frame)).size, (640, 480))

    def test_unauthorized_and_cross_tenant_rejected(self):
        self.assertEqual(self.upload(key="bad").status_code, 401)
        self.assertEqual(self.upload(key="key-two").status_code, 403)
        self.assertEqual(
            self.client.put(
                "/api/traffic/webcam-preview/" + CID, content=jpeg()
            ).status_code,
            422,
        )
        store_frame("one", CID, jpeg())
        self.assertIsNone(load_frame("two", CID))

    def test_public_read_is_not_available(self):
        self.assertEqual(
            self.client.get("/api/traffic/webcam-preview/" + CID).status_code, 405
        )

    def test_invalid_and_oversized_images_rejected(self):
        self.assertEqual(self.upload(b"not-jpeg").status_code, 422)
        self.assertEqual(self.upload(b"X" * 500001).status_code, 413)
        self.assertEqual(self.upload(content_type="text/html").status_code, 415)

    def test_old_image_never_shown_as_live(self):
        store_frame("one", CID, jpeg())
        path = frame_path("one", CID)
        os.utime(path, (time.time() - 20, time.time() - 20))
        self.assertIsNone(load_frame("one", CID))

    def test_image_is_private_and_metadata_free(self):
        store_frame("one", CID, jpeg())
        self.assertEqual(frame_path("one", CID).stat().st_mode & 0o777, 0o600)
        self.assertIsNone(
            Image.open(io.BytesIO(load_frame("one", CID)[0])).getexif().get(0x010E)
        )

    def test_ui_shows_offline_without_recent_image(self):
        from streamlit.testing.v1 import AppTest

        script = (
            "from traffic_dashboard import render_live_preview\nrender_live_preview('one', '"
            + CID
            + "')"
        )
        app = AppTest.from_string(script)
        app.session_state["tenant_id"] = "one"
        app.run()
        self.assertFalse(app.exception)
        self.assertIn("Webcam offline", app.info[0].value)

    def test_ui_renders_private_data_uri_and_blocks_other_session(self):
        from streamlit.testing.v1 import AppTest

        store_frame("one", CID, jpeg())
        script = (
            "from traffic_dashboard import render_live_preview\nrender_live_preview('one', '"
            + CID
            + "')"
        )
        app = AppTest.from_string(script)
        app.session_state["tenant_id"] = "one"
        app.run()
        self.assertFalse(app.exception)
        self.assertIn("data:image/jpeg;base64,", app.markdown[0].value)
        app.session_state["tenant_id"] = "two"
        app.run()
        self.assertEqual(len(app.markdown), 0)


if __name__ == "__main__":
    unittest.main()
