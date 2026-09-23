"""Visitor persistence tests with generated pixels and disposable databases."""

import io
import tempfile
import unittest
import uuid
from unittest.mock import patch

from browser_store import create_browser_source
from camera_store import init_camera_db
from PIL import Image
from sqlalchemy import create_engine, text
from traffic_store import ingest_crossings, init_traffic_db
from visitor_store import list_visitors, person_photos, save_browser_visitors


class VisitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + self.tmp.name + "/visitors.db")
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY)"))
            conn.execute(text("INSERT INTO tenants VALUES ('one'),('two')"))
        init_camera_db(self.engine)
        init_traffic_db(self.engine)
        self.cid = create_browser_source(self.engine, "one", "Browser")
        self.grant = {"tenant_id": "one", "camera_id": self.cid, "grant_id": "session"}
        out = io.BytesIO()
        Image.new("RGB", (640, 480), "blue").save(out, format="JPEG")
        self.frame = out.getvalue()
        self.tracks = [{"id": 1, "box": [0.1, 0.1, 0.5, 0.9]}]

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def report(self, tenant="one"):
        return list_visitors(self.engine, tenant, 1, 1000)

    def event(self, direction="entry"):
        return {
            "camera_id": self.cid,
            "tracking_id": "session:1",
            "event_id": str(uuid.uuid4()),
            "direction": direction,
            "occurred_at": 110,
            "gate_revision": "a" * 64,
        }

    def test_photo_and_stable_id_survive_repeated_observations(self):
        save_browser_visitors(self.engine, self.grant, self.tracks, self.frame, 100)
        first = self.report()["items"][0]
        save_browser_visitors(self.engine, self.grant, self.tracks, self.frame, 120)
        second = self.report()["items"][0]
        self.assertEqual(self.report()["total"], 1)
        self.assertEqual(first["visitor_id"], second["visitor_id"])
        self.assertEqual(second["first_seen"], 100)
        self.assertEqual(second["last_seen"], 120)
        self.assertEqual(second["photo_at"], 100)
        photo = Image.open(io.BytesIO(second["photo"]))
        self.assertEqual(photo.size, (256, 384))
        self.assertEqual(photo.format, "JPEG")
        self.assertEqual(len(photo.getexif()), 0)
        init_traffic_db(self.engine)
        self.assertEqual(self.report()["with_photo"], 1)

    def test_photo_access_and_source_are_tenant_scoped(self):
        save_browser_visitors(self.engine, self.grant, self.tracks, self.frame, 100)
        self.assertEqual(self.report("two")["items"], [])
        with self.assertRaises(PermissionError):
            save_browser_visitors(
                self.engine,
                {**self.grant, "tenant_id": "two"},
                self.tracks,
                self.frame,
                100,
            )
        self.assertEqual(self.report("two")["total"], 0)

    def test_crossings_are_retry_safe_and_share_the_visitor(self):
        event = self.event()
        ingest_crossings(self.engine, "one", [event])
        self.assertEqual(self.report()["with_photo"], 0)
        save_browser_visitors(self.engine, self.grant, self.tracks, self.frame, 100)
        ingest_crossings(self.engine, "one", [event])
        ingest_crossings(self.engine, "one", [self.event("exit")])
        item = self.report()["items"][0]
        self.assertEqual(self.report()["total"], 1)
        self.assertEqual((item["entries"], item["exits"]), (1, 1))
        self.assertEqual(item["first_seen"], 100)
        self.assertEqual(item["last_seen"], 110)
        self.assertTrue(item["photo"])

    def test_new_session_does_not_claim_known_identity(self):
        save_browser_visitors(self.engine, self.grant, self.tracks, self.frame, 100)
        save_browser_visitors(
            self.engine, {**self.grant, "grant_id": "new"}, self.tracks, self.frame, 120
        )
        self.assertEqual(self.report()["total"], 2)

    def test_invalid_photos_do_not_write_rows(self):
        for body in (b"not-jpeg", b"x" * 160001):
            with self.assertRaises(ValueError):
                save_browser_visitors(self.engine, self.grant, self.tracks, body, 100)
        with self.assertRaises(ValueError):
            person_photos(self.frame, [{"id": 1, "box": [0, 0, float("nan"), 1]}])
        self.assertEqual(self.report()["total"], 0)

    def test_page_and_date_filters(self):
        for i in range(14):
            save_browser_visitors(
                self.engine,
                {**self.grant, "grant_id": str(i)},
                self.tracks,
                self.frame,
                100 + i,
            )
        page = list_visitors(self.engine, "one", 100, 114, offset=12)
        self.assertEqual(page["total"], 14)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(list_visitors(self.engine, "one", 110, 112)["total"], 2)
        self.assertEqual(
            list_visitors(self.engine, "one", 1, 1000, "foreign")["total"], 0
        )

    def test_worker_only_saves_confirmed_tracks(self):
        import browser_worker
        from browser_vision import LineTracker

        tracker = LineTracker(
            self.cid, "session", {"axis": "x", "position": 0.5, "positive_entry": True}
        )

        class Detector:
            def detect(_, body):
                return [self.tracks[0]["box"]]

        with (
            patch.object(browser_worker, "engine", self.engine),
            patch.object(browser_worker.app.state, "detector", Detector(), create=True),
        ):
            browser_worker.persist_frame(self.grant, tracker, self.frame, 100, False)
            browser_worker.persist_frame(self.grant, tracker, self.frame, 100.2, False)
            self.assertEqual(self.report()["total"], 0)
            browser_worker.persist_frame(self.grant, tracker, self.frame, 100.4, False)
            self.assertEqual(self.report()["with_photo"], 1)

    def test_gallery_shows_photo_only_in_authenticated_tenant_session(self):
        from datetime import date

        import db_config
        from streamlit.testing.v1 import AppTest

        save_browser_visitors(self.engine, self.grant, self.tracks, self.frame, 100)
        with patch.object(db_config, "engine", self.engine):
            app = AppTest.from_string(
                'from db_config import engine\nfrom visitor_dashboard import render_visitors\nrender_visitors(engine, "one", development=True)',
                default_timeout=20,
            )
            app.session_state["tenant_id"] = "one"
            app.session_state["visitor_day"] = date(1969, 12, 31)
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.metric[0].value, "1")
            self.assertTrue(
                any("data:image/jpeg;base64," in item.value for item in app.markdown)
            )
            app.session_state["tenant_id"] = "two"
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.metric), 0)
            self.assertFalse(
                any("data:image/jpeg;base64," in item.value for item in app.markdown)
            )


if __name__ == "__main__":
    unittest.main()
