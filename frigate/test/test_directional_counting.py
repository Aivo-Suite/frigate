"""Behavioral tests for webcam crossings, offline delivery and cloud isolation."""

import os
import tempfile
import time
import unittest
import uuid
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

os.environ["DATABASE_URL"] = "sqlite://"
import cloud_api
from crossing_counter import CrossingCounter, gate_fingerprint
from traffic_store import (
    daily_report,
    ingest_crossings,
    init_traffic_db,
    list_counting_sources,
    register_webcam,
)

CID = "c" * 32
REV = "d" * 64


def observed(side, timestamp, tracking_id="person-1", kind="update"):
    """Build a Frigate-shaped observation without any image or identity data."""
    return {
        "type": kind,
        "after": {
            "camera": "webcam_notebook",
            "label": "person",
            "id": tracking_id,
            "frame_time": timestamp,
            "false_positive": False,
            "current_zones": [side] if side else [],
        },
    }


def crossing(direction="entry", timestamp=None, camera_id=CID):
    """Build a cloud ingestion fixture."""
    return {
        "event_id": str(uuid.uuid4()),
        "camera_id": camera_id,
        "tracking_id": "person-1",
        "direction": direction,
        "occurred_at": timestamp or time.time(),
        "gate_revision": REV,
    }


class CounterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "counter.db"
        self.counter = CrossingCounter(
            self.database, CID, REV, camera_name="webcam_notebook"
        )
        self.now = time.time() - 120

    def tearDown(self):
        self.temp.cleanup()

    def test_entry_exit_and_multiple_people(self):
        c = self.counter
        self.assertIsNone(c.process(observed("aivo_outside", self.now)))
        entry = c.process(observed("aivo_inside", self.now + 3))
        self.assertEqual(entry["direction"], "entry")
        exit_event = c.process(observed("aivo_outside", self.now + 8))
        self.assertEqual(exit_event["direction"], "exit")
        c.process(observed("aivo_inside", self.now, "person-2"))
        self.assertEqual(
            c.process(observed("aivo_outside", self.now + 3, "person-2"))["direction"],
            "exit",
        )
        self.assertEqual(len(c.pending()), 3)

    def test_detection_end_is_not_an_exit(self):
        self.counter.process(observed("aivo_inside", self.now, kind="new"))
        self.counter.process(observed("aivo_outside", self.now + 3, kind="end"))
        self.assertEqual(self.counter.pending(), [])

    def test_end_does_not_reopen_on_replayed_updates(self):
        self.counter.process(observed("aivo_outside", self.now))
        self.counter.process(observed("aivo_outside", self.now, kind="end"))
        self.counter.process(observed("aivo_inside", self.now + 2))
        self.assertEqual(self.counter.pending(), [])

    def test_duplicate_and_out_of_order_observations(self):
        self.counter.process(observed("aivo_outside", self.now))
        event = observed("aivo_inside", self.now + 3)
        self.counter.process(event)
        self.counter.process(event)
        self.counter.process(observed("aivo_outside", self.now + 1))
        self.assertEqual(len(self.counter.pending()), 1)

    def test_jitter_does_not_double_count_entry(self):
        c = self.counter
        c.process(observed("aivo_outside", self.now))
        c.process(observed("aivo_inside", self.now + 3))
        c.process(observed("aivo_outside", self.now + 3.2))
        c.process(observed("aivo_inside", self.now + 7))
        self.assertEqual([e["direction"] for e in c.pending()], ["entry"])

    def test_neutral_gap_and_ambiguous_zones(self):
        c = self.counter
        c.process(observed("aivo_outside", self.now))
        c.process(observed(None, self.now + 1))
        self.assertEqual(
            c.process(observed("aivo_inside", self.now + 3))["direction"], "entry"
        )
        ambiguous = observed("aivo_outside", self.now + 6)
        ambiguous["after"]["current_zones"].append("aivo_inside")
        c.process(ambiguous)
        self.assertIsNone(c.process(observed("aivo_outside", self.now + 9)))
        self.assertEqual(len(c.pending()), 1)

    def test_stale_track_gap_does_not_invent_a_crossing(self):
        self.counter.process(observed("aivo_outside", self.now))
        self.counter.process(observed("aivo_inside", self.now + 60))
        self.assertEqual(self.counter.pending(), [])

    def test_non_person_other_camera_and_false_positive(self):
        for field, value in [
            ("label", "cat"),
            ("camera", "another"),
            ("false_positive", True),
        ]:
            message = observed("aivo_outside", self.now)
            message["after"][field] = value
            self.counter.process(message)
        self.counter.process(observed("aivo_inside", self.now + 3))
        self.assertEqual(self.counter.pending(), [])

    def test_restart_and_replay_preserve_queue_and_idempotency(self):
        self.counter.process(observed("aivo_outside", self.now))
        event = self.counter.process(observed("aivo_inside", self.now + 3))
        restarted = CrossingCounter(
            self.database, CID, REV, camera_name="webcam_notebook"
        )
        restarted.process(observed("aivo_inside", self.now + 3))
        self.assertEqual(restarted.pending(), [event])
        restarted.acknowledge([event["event_id"]])
        self.assertEqual(restarted.pending(), [])

    def test_disconnect_and_calibration_changes_break_partial_tracks(self):
        self.counter.process(observed("aivo_outside", self.now))
        self.counter.reset_tracks()
        self.counter.process(observed("aivo_inside", self.now + 3))
        self.counter.gate_revision = "f" * 64
        self.counter.process(observed("aivo_outside", self.now + 6))
        self.assertEqual(self.counter.pending(), [])

    def test_tenant_binding_rejects_reuse(self):
        self.counter.bind_tenant("one")
        self.counter.bind_tenant("one")
        with self.assertRaises(ValueError):
            self.counter.bind_tenant("two")

    def test_missing_or_delayed_zones_rejected(self):
        with self.assertRaises(ValueError):
            gate_fingerprint({}, "outside", "inside")
        zones = {
            "outside": {"coordinates": "0,0,0.4,0,0.4,1"},
            "inside": {"coordinates": "0.6,0,1,0,1,1", "loitering_time": 5},
        }
        with self.assertRaises(ValueError):
            gate_fingerprint(zones, "outside", "inside")

    def test_pruning_never_deletes_pending_events(self):
        self.counter.process(observed("aivo_outside", self.now - 10 * 86400))
        self.counter.process(observed("aivo_inside", self.now - 10 * 86400 + 3))
        self.counter.prune()
        self.assertEqual(len(self.counter.pending()), 1)


class CloudTrafficTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + self.temp.name + "/cloud.db")
        with self.engine.begin() as conn:
            conn.execute(
                text("CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, api_key TEXT)")
            )
            conn.execute(
                text("INSERT INTO tenants VALUES ('one','key-one'),('two','key-two')")
            )
            conn.execute(
                text(
                    "CREATE TABLE cameras (camera_id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT)"
                )
            )
        init_traffic_db(self.engine)
        register_webcam(self.engine, "one", CID, "Webcam")
        self.patch = patch.object(cloud_api, "engine", self.engine)
        self.patch.start()
        self.client = TestClient(cloud_api.app)

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def post(self, events, tenant="one"):
        return self.client.post(
            "/api/traffic/crossings",
            json={"events": events},
            headers={"x-api-key": "key-" + tenant},
        )

    def test_batch_idempotency(self):
        event = crossing()
        self.assertEqual(self.post([event]).status_code, 200)
        self.assertEqual(self.post([event]).json()["acknowledged"], [event["event_id"]])
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(text("SELECT COUNT(*) FROM traffic_crossings")).scalar(), 1
            )

    def test_tenant_isolation_and_all_or_nothing_batch(self):
        self.assertEqual(self.post([crossing()], "two").status_code, 403)
        self.assertEqual(
            self.post([crossing(), crossing(camera_id="a" * 32)]).status_code, 403
        )
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(text("SELECT COUNT(*) FROM traffic_crossings")).scalar(), 0
            )

    def test_event_identity_conflict(self):
        event = crossing()
        self.post([event])
        changed = {**event, "direction": "exit"}
        self.assertEqual(self.post([changed]).status_code, 409)

    def test_payload_validation(self):
        for field, value in [
            ("direction", "unknown"),
            ("event_id", "not-a-uuid"),
            ("occurred_at", time.time() + 1000),
            ("camera_id", "bad"),
        ]:
            self.assertEqual(self.post([{**crossing(), field: value}]).status_code, 422)
        self.assertEqual(self.post([]).status_code, 422)
        self.assertEqual(
            self.client.post(
                "/api/traffic/crossings", json={"events": [crossing()]}
            ).status_code,
            422,
        )

    def test_local_day_boundaries_and_delayed_delivery(self):
        tz = ZoneInfo("America/Sao_Paulo")
        day = date(2026, 9, 21)
        start = datetime(2026, 9, 21, tzinfo=tz).timestamp()
        events = [
            crossing("entry", start - 1),
            crossing("entry", start),
            crossing("exit", start + 86399),
            crossing("entry", start + 86400),
        ]
        ingest_crossings(self.engine, "one", events)
        report = daily_report(self.engine, "one", day)
        self.assertEqual(
            (report["entries"], report["exits"], report["net_flow"]), (1, 1, 0)
        )
        self.assertEqual(report["hourly"][23]["Saídas"], 1)
        self.assertEqual(daily_report(self.engine, "two", day)["entries"], 0)

    def test_webcam_registration_does_not_create_an_rtsp_camera(self):
        response = self.client.post(
            "/api/traffic/webcam-sources",
            json={"camera_id": "e" * 32, "name": "Test webcam"},
            headers={"x-api-key": "key-one"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_test"])
        sources = list_counting_sources(self.engine, "one")
        self.assertTrue(all(source["is_test"] for source in sources))
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(text("SELECT COUNT(*) FROM cameras")).scalar(), 0
            )

    def test_counter_dashboard_labels_webcam_and_waiting_status(self):
        import db_config
        from streamlit.testing.v1 import AppTest

        with (
            patch.object(db_config, "engine", self.engine),
            patch.object(db_config, "init_db"),
        ):
            app = AppTest.from_file(
                str(Path(cloud_api.__file__).with_name("dashboard.py")),
                default_timeout=30,
            )
            app.session_state["tenant_id"] = "one"
            app.session_state["tenant_name"] = "Test Store"
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.title[0].value, "Entradas e Saídas")
            self.assertTrue(
                any("Webcam de teste" in warning.value for warning in app.warning)
            )
            self.assertEqual(app.metric[0].value, "0")

    def test_heartbeat_is_tenant_scoped(self):
        payload = {
            "camera_id": CID,
            "mqtt_connected": True,
            "frigate_available": True,
            "pending_events": 1,
            "gate_revision": REV,
        }
        self.assertEqual(
            self.client.post(
                "/api/traffic/heartbeat", json=payload, headers={"x-api-key": "key-two"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/traffic/heartbeat", json=payload, headers={"x-api-key": "key-one"}
            ).status_code,
            200,
        )
        self.assertEqual(
            len(
                daily_report(
                    self.engine,
                    "one",
                    datetime.now(ZoneInfo("America/Sao_Paulo")).date(),
                )["health"]
            ),
            1,
        )
        self.assertEqual(
            daily_report(
                self.engine, "two", datetime.now(ZoneInfo("America/Sao_Paulo")).date()
            )["health"],
            [],
        )


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_failure_and_bad_ack_keep_the_queue(self):
        from edge_counter import send_pending

        with tempfile.TemporaryDirectory() as directory:
            counter = CrossingCounter(
                Path(directory) / "counter.db", CID, REV, camera_name="webcam_notebook"
            )
            counter.process(observed("aivo_outside", time.time() - 6))
            counter.process(observed("aivo_inside", time.time() - 3))
            for response in [
                httpx.Response(503),
                httpx.Response(200, json={"acknowledged": []}),
            ]:
                transport = httpx.MockTransport(
                    lambda request, response=response: response
                )
                async with httpx.AsyncClient(
                    base_url="https://example.test", transport=transport
                ) as client:
                    with self.assertRaises((httpx.HTTPStatusError, ValueError)):
                        await send_pending(client, counter)
                self.assertEqual(len(counter.pending()), 1)
            event_id = counter.pending()[0]["event_id"]
            transport = httpx.MockTransport(
                lambda request: httpx.Response(200, json={"acknowledged": [event_id]})
            )
            async with httpx.AsyncClient(
                base_url="https://example.test", transport=transport
            ) as client:
                self.assertEqual(await send_pending(client, counter), 1)
            self.assertEqual(counter.pending(), [])


if __name__ == "__main__":
    unittest.main()
