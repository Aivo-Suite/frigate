"""Customer navigation, setup evidence and missing-data semantics."""

import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import db_config
import test_intelbras as fixtures
from retail_store import get_profile, ingest_samples, save_profile
from sqlalchemy import text
from store_experience import camera_readiness, customer_cameras
from streamlit.testing.v1 import AppTest
from traffic_store import ingest_crossings, record_health, register_webcam


class StoreExperienceTests(unittest.TestCase):
    setUp = fixtures.IntelbrasTests.setUp
    tearDown = fixtures.IntelbrasTests.tearDown
    sample = fixtures.IntelbrasTests.sample

    def ready_camera(self, pending=0):
        settings = {
            **self.settings,
            "zones": [
                {
                    "zone_id": "aivo_out",
                    "name": "Fora",
                    "role": "outside",
                    "points": [[0, 0], [0.4, 0], [0.4, 1], [0, 1]],
                },
                {
                    "zone_id": "aivo_in",
                    "name": "Dentro",
                    "role": "inside",
                    "points": [[0.6, 0], [1, 0], [1, 1], [0.6, 1]],
                },
                *self.settings["zones"],
            ],
        }
        self.revision = save_profile(
            self.db,
            "one",
            self.cid,
            settings,
            get_profile(self.db, "one", self.cid)["revision"],
        )
        with self.db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO retail_edge_health VALUES ('one',:cid,:now,5,:revision,:pending) ON CONFLICT(tenant_id,camera_id) DO UPDATE SET received_at=excluded.received_at,revision=excluded.revision,queue_size=excluded.queue_size"
                ),
                {
                    "cid": self.cid,
                    "now": time.time(),
                    "revision": self.revision,
                    "pending": pending,
                },
            )
        record_health(
            self.db,
            "one",
            {
                "camera_id": self.cid,
                "mqtt_connected": True,
                "frigate_available": True,
                "last_person_event": None,
                "pending_events": pending,
                "gate_revision": self.revision,
            },
        )

    def dashboard(self):
        app = AppTest.from_file(
            str(Path(db_config.__file__).with_name("dashboard.py")), default_timeout=30
        )
        app.session_state["tenant_id"] = "one"
        app.session_state["tenant_name"] = "Fixture store"
        return app

    def test_missing_data_is_not_zero_and_offline_keeps_recorded_counts(self):
        with (
            patch.object(db_config, "engine", self.db),
            patch.object(db_config, "init_db"),
        ):
            app = self.dashboard().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.metric[0].value, "—")
            self.assertTrue(any("Sem dados atualizados" in x.value for x in app.info))
            self.ready_camera()
            app.run()
            self.assertEqual(app.metric[0].value, "0")
            self.assertTrue(any("Online" in x.value for x in app.success))
            ingest_crossings(
                self.db,
                "one",
                [
                    {
                        "event_id": str(uuid.uuid4()),
                        "camera_id": self.cid,
                        "tracking_id": "fixture",
                        "direction": "entry",
                        "occurred_at": time.time(),
                        "gate_revision": self.revision,
                    }
                ],
            )
            with self.db.begin() as c:
                c.execute(text("UPDATE retail_edge_health SET received_at=0"))
            app.run()
            self.assertEqual(app.metric[0].value, "1")
            self.assertTrue(any("incompletos" in x.value for x in app.info))

    def test_calibration_queue_and_tenant_boundaries(self):
        self.assertFalse(camera_readiness(self.db, "one", self.cid)["current"])
        self.ready_camera(pending=2)
        self.assertEqual(
            camera_readiness(self.db, "one", self.cid)["status"], "Sincronizando dados"
        )
        self.assertFalse(camera_readiness(self.db, "one", self.cid)["validated"])
        with self.assertRaises(PermissionError):
            camera_readiness(self.db, "two", self.cid)
        self.assertEqual(customer_cameras(self.db, "two"), [])
        self.ready_camera()
        for direction in ("entry", "exit"):
            ingest_crossings(
                self.db,
                "one",
                [
                    {
                        "event_id": str(uuid.uuid4()),
                        "camera_id": self.cid,
                        "tracking_id": "fixture",
                        "direction": direction,
                        "occurred_at": time.time(),
                        "gate_revision": self.revision,
                    }
                ],
            )
        self.assertTrue(camera_readiness(self.db, "one", self.cid)["validated"])
        p = get_profile(self.db, "one", self.cid)
        save_profile(
            self.db,
            "one",
            self.cid,
            {**p["settings"], "retention_days": 2},
            p["revision"],
        )
        ready = camera_readiness(self.db, "one", self.cid)
        self.assertFalse(ready["validated"])
        self.assertEqual(ready["status"], "Aplicando configuração")

    def test_empty_store_hides_dev_sources_and_routes_to_intelbras_form(self):
        with self.db.begin() as c:
            c.execute(text("DELETE FROM cameras"))
        register_webcam(self.db, "one", "b" * 32, "Developer webcam")
        with (
            patch.object(db_config, "engine", self.db),
            patch.object(db_config, "init_db"),
        ):
            app = self.dashboard().run()
            self.assertFalse(app.exception)
            self.assertEqual(
                app.radio[0].options,
                ["Resumo", "Visitantes", "Análise da loja", "Câmeras"],
            )
            self.assertEqual(app.metric[0].value, "—")
            next(
                b for b in app.button if b.label == "Cadastrar minha Intelbras"
            ).click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["main_page"], "Câmeras")
            self.assertEqual(app.session_state["camera_section"], "Cadastro")
            self.assertIn("IP Local", [field.label for field in app.text_input])
            self.assertFalse(any(r.label == "Tipo de câmera" for r in app.radio))
            self.assertFalse(
                any("Webcam" in option for r in app.radio for option in r.options)
            )

    def test_visitor_details_show_only_scoped_observations(self):
        ingest_samples(self.db, "one", [self.sample(), self.sample(10)])
        with patch.object(db_config, "engine", self.db):
            app = AppTest.from_string(
                'from db_config import engine\nfrom visitor_dashboard import render_visitors\nrender_visitors(engine,"one")',
                default_timeout=30,
            )
            app.session_state["tenant_id"] = "one"
            app.run()
            self.assertFalse(app.exception)
            next(b for b in app.button if b.label == "Ver detalhes").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any(x.value == "Percurso observado" for x in app.subheader))
            self.assertTrue(any(b.label == "Ver trecho da visita" for b in app.button))


if __name__ == "__main__":
    unittest.main()
