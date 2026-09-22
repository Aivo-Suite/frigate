"""Isolated tests for browser grants, trajectories and the cloud frame channel."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
import browser_worker
from browser_store import (
    claim_grant,
    create_browser_source,
    grant_active,
    issue_grant,
    list_browser_sources,
    revoke_grant,
    validate_gate,
)
from browser_vision import LineTracker
from camera_store import init_camera_db
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from starlette.websockets import WebSocketDisconnect
from traffic_store import init_traffic_db

GATE = {"axis": "x", "position": 0.5, "positive_entry": True}


def frame():
    """Create test-only JPEG pixels; never access physical camera hardware."""
    out = io.BytesIO()
    Image.new("RGB", (320, 240), "blue").save(out, format="JPEG")
    return out.getvalue()


def box(x):
    return [x - 0.12, 0.15, x + 0.12, 0.9]


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + self.tmp.name + "/test.db")
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY)"))
            conn.execute(text("INSERT INTO tenants VALUES ('one'),('two')"))
        init_camera_db(self.engine)
        init_traffic_db(self.engine)
        self.cid = create_browser_source(self.engine, "one", "Browser webcam")

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_ticket_is_single_use_and_does_not_expose_api_key(self):
        ticket = issue_grant(self.engine, "one", self.cid)
        claimed = claim_grant(self.engine, ticket["token"])
        self.assertEqual(claimed["tenant_id"], "one")
        self.assertTrue(grant_active(self.engine, claimed))
        with self.assertRaises(PermissionError):
            claim_grant(self.engine, ticket["token"])
        with self.engine.connect() as conn:
            self.assertNotEqual(
                conn.execute(
                    text("SELECT token_hash FROM browser_camera_grants")
                ).scalar_one(),
                ticket["token"],
            )
        revoke_grant(self.engine, "two", ticket["grant_id"])
        self.assertTrue(grant_active(self.engine, claimed))
        revoke_grant(self.engine, "one", ticket["grant_id"])
        self.assertFalse(grant_active(self.engine, claimed))

    def test_foreign_camera_and_expired_tickets_rejected(self):
        with self.assertRaises(PermissionError):
            issue_grant(self.engine, "two", self.cid)
        self.assertEqual(list_browser_sources(self.engine, "two"), [])
        ticket = issue_grant(self.engine, "one", self.cid)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE browser_camera_grants SET connect_until=0"))
        with self.assertRaises(PermissionError):
            claim_grant(self.engine, ticket["token"])

    def test_gate_validation_and_migration_idempotence(self):
        for gate in (
            {},
            None,
            {**GATE, "position": float("nan")},
            {**GATE, "position": 1},
            {**GATE, "positive_entry": "true"},
        ):
            with self.assertRaises(ValueError):
                validate_gate(gate)
        init_traffic_db(self.engine)
        self.assertEqual(len(list_browser_sources(self.engine, "one")), 1)

    def test_browser_source_not_listed_as_linux_device(self):
        from webcam_setup import list_webcams

        self.assertEqual(list_webcams(self.engine, "one"), [])

    def test_stream_uses_granted_tenant_and_stops_cleanly(self):
        class Detector:
            def __init__(self, _):
                pass

            def detect(self, _):
                return []

        ticket = issue_grant(self.engine, "one", self.cid)
        with (
            patch.object(browser_worker, "engine", self.engine),
            patch.object(browser_worker, "PersonDetector", Detector),
            patch.dict(os.environ, {"LIVE_PREVIEW_DIR": self.tmp.name + "/frames"}),
        ):
            with TestClient(browser_worker.app) as client:
                with client.websocket_connect(
                    "/api/browser/ws",
                    headers={"origin": "https://frigate.agenticx.ia.br"},
                ) as ws:
                    ws.send_json({"token": ticket["token"], "gate": GATE})
                    self.assertEqual(ws.receive_json()["type"], "ready")
                    ws.send_bytes(frame())
                    result = ws.receive_json()
                    self.assertEqual(result["entries"], 0)
                    self.assertEqual(result["exits"], 0)
            self.assertFalse(browser_worker.app.state.busy)
            with self.engine.connect() as conn:
                self.assertEqual(
                    conn.execute(
                        text("SELECT count(*) FROM traffic_crossings")
                    ).scalar_one(),
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        text("SELECT tenant_id FROM traffic_counter_health")
                    ).scalar_one(),
                    "one",
                )
            self.assertEqual(list(Path(self.tmp.name + "/frames").glob("*.jpg")), [])

    def test_wrong_origin_rejected_before_consuming_ticket(self):
        class Detector:
            def __init__(self, _):
                pass

        ticket = issue_grant(self.engine, "one", self.cid)
        with (
            patch.object(browser_worker, "engine", self.engine),
            patch.object(browser_worker, "PersonDetector", Detector),
            TestClient(browser_worker.app) as client,
        ):
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect(
                    "/api/browser/ws", headers={"origin": "https://other.example"}
                ):
                    pass
        self.assertEqual(claim_grant(self.engine, ticket["token"])["tenant_id"], "one")

    def test_revoked_session_cannot_submit_frames(self):
        class Detector:
            def __init__(self, _):
                pass

            def detect(self, _):
                raise AssertionError("Inference must not run after revocation")

        ticket = issue_grant(self.engine, "one", self.cid)
        with (
            patch.object(browser_worker, "engine", self.engine),
            patch.object(browser_worker, "PersonDetector", Detector),
            patch.dict(os.environ, {"LIVE_PREVIEW_DIR": self.tmp.name}),
            TestClient(browser_worker.app) as client,
            client.websocket_connect(
                "/api/browser/ws",
                headers={"origin": "https://frigate.agenticx.ia.br"},
            ) as ws,
        ):
            ws.send_json({"token": ticket["token"], "gate": GATE})
            ws.receive_json()
            revoke_grant(self.engine, "one", ticket["grant_id"])
            ws.send_bytes(frame())
            self.assertEqual(ws.receive_json()["type"], "error")


class TrackingTests(unittest.TestCase):
    def run_path(self, path, gate=GATE):
        tracker = LineTracker("a" * 32, "session", gate)
        events = []
        for i, x in enumerate(path):
            events.extend(
                tracker.update([box(x)] if x is not None else [], 100 + i * 0.22)[0]
            )
        return events

    def test_entry_exit_direction_and_no_stationary_duplicates(self):
        path = [
            0.3,
            0.3,
            0.3,
            0.38,
            0.46,
            0.54,
            0.62,
            0.62,
            0.62,
            0.62,
            0.62,
            0.54,
            0.46,
            0.38,
            0.3,
            0.3,
        ]
        self.assertEqual(
            [e["direction"] for e in self.run_path(path)], ["entry", "exit"]
        )
        self.assertEqual(
            [
                e["direction"]
                for e in self.run_path(path, {**GATE, "positive_entry": False})
            ],
            ["exit", "entry"],
        )
        self.assertEqual(self.run_path([0.3] * 20), [])

    def test_disappearance_and_long_gap_never_count_as_exit(self):
        self.assertEqual(self.run_path([0.3] * 4 + [None] * 10 + [0.65] * 4), [])
        self.assertEqual(self.run_path([0.3] * 4 + [None] * 10), [])

    def test_jitter_around_line_does_not_count(self):
        self.assertEqual(self.run_path([0.49, 0.51, 0.49, 0.51] * 5), [])

    def test_duplicate_timestamp_has_no_effect(self):
        tracker = LineTracker("a" * 32, "session", GATE)
        tracker.update([box(0.3)], 100)
        self.assertEqual(tracker.update([box(0.7)], 100), ([], []))


if __name__ == "__main__":
    unittest.main()
