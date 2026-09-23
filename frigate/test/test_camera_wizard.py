"""Regression tests for real-data enrollment and the downloadable webcam kit."""

import io
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import yaml
from camera_store import camera_snapshot, init_camera_db, list_cameras, save_camera
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from streamlit.testing.v1 import AppTest
from traffic_store import init_traffic_db, register_webcam
from webcam_setup import (
    build_webcam_kit,
    list_webcams,
    save_webcam,
    validate_webcam,
    webcam_config,
)


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + self.tmp.name + "/wizard.db")
        self.env = patch.dict(
            os.environ,
            {
                "CAMERA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "LIVE_PREVIEW_DIR": self.tmp.name,
            },
        )
        self.env.start()
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY)"))
            conn.execute(text("INSERT INTO tenants VALUES ('one'),('two')"))
        init_camera_db(self.engine)
        init_traffic_db(self.engine)
        import db_config

        self.db_patch = patch.object(db_config, "engine", self.engine)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.env.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    def app(self):
        app = AppTest.from_string(
            'from db_config import engine\nfrom camera_dashboard import render_camera_settings\nimport streamlit as st\nrender_camera_settings(engine, st.session_state["tenant_id"], allow_webcam=True)',
            default_timeout=20,
        )
        app.session_state["tenant_id"] = "one"
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio[0].value, "ip")
        return app

    def click(self, app, label):
        next(b for b in app.button if b.label == label).click().run()
        self.assertFalse(app.exception)

    def field(self, app, label, value):
        next(field for field in app.text_input if field.label == label).set_value(value)

    def test_webcam_wizard_review_before_save_and_download(self):
        app = self.app()
        app.radio[0].set_value("webcam").run()
        self.click(app, "Continuar")
        self.field(app, "Nome da câmera", "Minha webcam")
        self.click(app, "Revisar cadastro")
        self.assertEqual(list_webcams(self.engine, "one"), [])
        self.click(app, "Confirmar e salvar")
        self.assertEqual(list_webcams(self.engine, "one")[0]["name"], "Minha webcam")
        self.assertEqual(list_webcams(self.engine, "two"), [])
        self.assertTrue(app.get("download_button"))
        self.assertTrue(any("offline" in info.value for info in app.info))
        self.assertEqual(list_cameras(self.engine, "one"), [])
        self.assertNotIn("password", app.session_state["camera_wizard_state"]["draft"])

    def test_invalid_ip_and_cancel_clear_password(self):
        app = self.app()
        app.radio[0].set_value("ip").run()
        self.click(app, "Continuar")
        self.field(app, "Nome da câmera", "Entrada")
        self.field(app, "IP Local", "invalid")
        self.field(app, "Senha", "secret-only-in-draft")
        self.click(app, "Revisar cadastro")
        self.assertTrue(app.error)
        self.assertEqual(list_cameras(self.engine, "one"), [])
        self.click(app, "Cancelar cadastro")
        self.assertNotIn(
            "secret-only-in-draft", repr(app.session_state["camera_wizard_state"])
        )

    def test_ip_review_back_edit_and_preserve_blank_password(self):
        cid = save_camera(self.engine, "one", "Old", "192.168.1.20", "admin", "p@ss", 1)
        app = self.app()
        self.click(app, "Editar configuração")
        self.field(app, "Nome da câmera", "New")
        self.click(app, "Revisar cadastro")
        self.click(app, "Voltar aos dados")
        self.assertEqual(
            next(f.value for f in app.text_input if f.label == "Nome da câmera"), "New"
        )
        self.click(app, "Revisar cadastro")
        self.click(app, "Confirmar e salvar")
        result = camera_snapshot(self.engine, "one")["cameras"][0]
        self.assertEqual(result["camera_id"], cid)
        self.assertEqual(result["name"], "New")
        self.assertIn("p%40ss", result["rtsp_url"])

    def test_tenant_switch_clears_draft(self):
        app = self.app()
        app.radio[0].set_value("ip").run()
        self.click(app, "Continuar")
        self.field(app, "Nome da câmera", "Private")
        self.field(app, "IP Local", "192.168.1.10")
        self.field(app, "Senha", "secret-only-in-draft")
        self.click(app, "Revisar cadastro")
        app.session_state["tenant_id"] = "two"
        app.run()
        self.assertFalse(app.exception)
        self.assertNotIn(
            "secret-only-in-draft", repr(app.session_state["camera_wizard_state"])
        )
        self.assertEqual(app.session_state["camera_wizard_state"]["step"], 1)

    def test_legacy_webcam_edit_preserves_identity_and_name(self):
        cid = "a" * 32
        register_webcam(self.engine, "one", cid, "Legacy")
        init_traffic_db(self.engine)
        save_webcam(
            self.engine, "one", "Custom", "/dev/video1", "yuyv422", "right_to_left", cid
        )
        register_webcam(self.engine, "one", cid, "Webcam do notebook")
        source = list_webcams(self.engine, "one")[0]
        self.assertEqual(source["name"], "Custom")
        self.assertEqual(source["device_path"], "/dev/video1")
        self.assertEqual(source["camera_id"], cid)
        with self.assertRaises(ValueError):
            save_webcam(
                self.engine,
                "two",
                "Foreign",
                "/dev/video0",
                "mjpeg",
                "left_to_right",
                cid,
            )

    def test_duplicate_device_and_unsafe_path_rejected(self):
        save_webcam(
            self.engine, "one", "First", "/dev/video0", "mjpeg", "left_to_right"
        )
        with self.assertRaises(ValueError):
            save_webcam(
                self.engine, "one", "Duplicate", "/dev/video0", "mjpeg", "left_to_right"
            )
        for path in (
            "/etc/passwd",
            "/dev/video0;touch /tmp/oops",
            "/dev/video0\n",
            "/dev/../video0",
        ):
            with self.assertRaises(ValueError):
                validate_webcam("Webcam", path, "mjpeg", "left_to_right")

    def test_download_self_contained_without_keys_and_with_explicit_live_mode(self):
        cid = save_webcam(
            self.engine, "one", "Name\n$bad", "/dev/video2", "yuyv422", "right_to_left"
        )
        source = list_webcams(self.engine, "one")[0]
        with zipfile.ZipFile(io.BytesIO(build_webcam_kit(source, "one"))) as kit:
            names = kit.namelist()
            self.assertNotIn(".secrets/tenant_api_key", names)
            for filename in (
                "edge_counter.py",
                "crossing_counter.py",
                "edge_live_preview.py",
                "verify_setup.py",
            ):
                compile(kit.read(filename), filename, "exec")
            compose = yaml.safe_load(kit.read("compose.yml"))
            self.assertEqual(
                compose["services"]["frigate"]["devices"], ["/dev/video2:/dev/video0"]
            )
            self.assertEqual(compose["services"]["live-preview"]["profiles"], ["live"])
            self.assertEqual(
                compose["services"]["frigate"]["ports"], ["127.0.0.1:5000:5000"]
            )
            config = yaml.safe_load(kit.read("config/config.yml"))
            self.assertIn(
                "yuyv422",
                config["cameras"]["webcam_notebook"]["ffmpeg"]["inputs"][0][
                    "input_args"
                ],
            )
            self.assertFalse(config["record"]["enabled"])
            self.assertIn(cid, kit.read(".env").decode())
            self.assertNotIn("$bad", kit.read(".env").decode())
            self.assertIn("EXPECTED_TENANT_ID=one", kit.read(".env").decode())
            self.assertIn("verify_setup.py", kit.read("iniciar.sh").decode())
            self.assertIn(
                "COPY verify_setup.py", kit.read("Dockerfile.counter").decode()
            )
        with self.assertRaises(ValueError):
            build_webcam_kit(source, "one\nINJECT=yes")

    def test_direction_reversal_swaps_counting_zones(self):
        source = {
            "name": "Webcam",
            "device_path": "/dev/video0",
            "pixel_format": "mjpeg",
            "entry_direction": "left_to_right",
        }
        zones = webcam_config(source)["cameras"]["webcam_notebook"]["zones"]
        reverse = webcam_config({**source, "entry_direction": "right_to_left"})[
            "cameras"
        ]["webcam_notebook"]["zones"]
        self.assertEqual(zones["aivo_inside"], reverse["aivo_outside"])
        self.assertEqual(zones["aivo_outside"], reverse["aivo_inside"])


if __name__ == "__main__":
    unittest.main()
