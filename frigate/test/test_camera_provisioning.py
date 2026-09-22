"""Behavior tests for tenant boundaries, UI forms and Edge recovery."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["CAMERA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import cloud_api
from camera_store import (
    build_rtsp_url,
    camera_snapshot,
    delete_camera,
    init_camera_db,
    list_cameras,
    save_camera,
)
from edge_provisioner import (
    Provisioner,
    fetch_snapshot,
    merge_config,
    validate_snapshot,
)


def snapshot(cameras=None, tenant="one"):
    """Create a full signed-by-content fixture."""
    data = {
        "tenant_id": tenant,
        "cameras": cameras
        if cameras is not None
        else [
            {
                "camera_id": "a" * 32,
                "frigate_name": "aivo_" + "a" * 32,
                "name": "Entry",
                "rtsp_url": "rtsp://admin:secret@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0",
            }
        ],
    }
    return {
        **data,
        "schema_version": 1,
        "revision": hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest(),
    }


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + self.temp.name + "/test.db")
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE tenants (tenant_id VARCHAR PRIMARY KEY, api_key VARCHAR, name VARCHAR, username VARCHAR, password_hash VARCHAR)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO tenants (tenant_id,api_key) VALUES ('one','key-one'),('two','key-two')"
                )
            )
        init_camera_db(self.engine)
        from traffic_store import init_traffic_db

        init_traffic_db(self.engine)
        self.api_patch = patch.object(cloud_api, "engine", self.engine)
        self.api_patch.start()
        self.client = TestClient(cloud_api.app)

    def tearDown(self):
        self.client.close()
        self.api_patch.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def add(self, tenant="one", ip="192.168.1.10"):
        return save_camera(self.engine, tenant, "Entry", ip, "ad@min", "p:@/# %ç", 1)

    def test_url_encoding(self):
        self.assertEqual(
            build_rtsp_url("192.168.1.10", "u@", "p:/# %", 2),
            "rtsp://u%40:p%3A%2F%23%20%25@192.168.1.10:554/cam/realmonitor?channel=2&subtype=0",
        )

    def test_invalid_address_and_channel(self):
        for ip, channel in [
            ("localhost", 1),
            ("127.0.0.1", 1),
            ("192.168.1.10/path", 1),
            ("192.168.1.10", 0),
        ]:
            with self.assertRaises(ValueError):
                build_rtsp_url(ip, "u", "p", channel)

    def test_ciphertext_and_tenant_isolation(self):
        cid = self.add()
        self.add("two")
        with self.engine.connect() as conn:
            encrypted = conn.execute(
                text("SELECT rtsp_url_encrypted FROM cameras WHERE camera_id=:cid"),
                {"cid": cid},
            ).scalar()
        self.assertNotIn("rtsp://", encrypted)
        self.assertNotIn("p:@", encrypted)
        self.assertEqual(len(list_cameras(self.engine, "one")), 1)
        self.assertNotIn("rtsp_url_encrypted", list_cameras(self.engine, "one")[0])
        result = self.client.get(
            "/api/config/cameras", headers={"x-api-key": "key-one"}
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(result.json()["cameras"][0]["camera_id"], cid)
        self.assertEqual(len(result.json()["cameras"]), 1)
        validate_snapshot(result.json())

    def test_authentication(self):
        self.assertEqual(self.client.get("/api/config/cameras").status_code, 422)
        self.assertEqual(
            self.client.get(
                "/api/config/cameras", headers={"x-api-key": "bad"}
            ).status_code,
            401,
        )

    def test_foreign_update_and_delete(self):
        cid = self.add()
        with self.assertRaises(ValueError):
            save_camera(
                self.engine,
                "two",
                "Changed",
                "192.168.1.11",
                "u",
                "p",
                1,
                camera_id=cid,
            )
        delete_camera(self.engine, "two", cid)
        self.assertEqual(len(list_cameras(self.engine, "one")), 1)

    def test_blank_edit_password_and_stable_identity(self):
        cid = self.add()
        before = camera_snapshot(self.engine, "one")
        save_camera(
            self.engine,
            "one",
            "Renamed",
            "192.168.1.20",
            "ad@min",
            "",
            2,
            camera_id=cid,
        )
        after = camera_snapshot(self.engine, "one")
        self.assertEqual(after["cameras"][0]["camera_id"], cid)
        self.assertIn(
            "p%3A%40%2F%23%20%25%C3%A7@192.168.1.20", after["cameras"][0]["rtsp_url"]
        )
        self.assertNotEqual(before["revision"], after["revision"])
        self.assertEqual(after, camera_snapshot(self.engine, "one"))

    def test_duplicate_camera_and_valid_empty_snapshot(self):
        cid = self.add()
        with self.assertRaises(IntegrityError):
            self.add()
        save_camera(
            self.engine, "one", "Entry", "192.168.1.10", "ad@min", "", 1, False, cid
        )
        self.assertEqual(camera_snapshot(self.engine, "one")["cameras"], [])
        delete_camera(self.engine, "one", cid)
        self.assertEqual(list_cameras(self.engine, "one"), [])

    def test_missing_key_returns_failure_not_empty(self):
        self.add()
        with patch.dict(os.environ, {"CAMERA_ENCRYPTION_KEY": ""}):
            self.assertEqual(
                self.client.get(
                    "/api/config/cameras", headers={"x-api-key": "key-one"}
                ).status_code,
                503,
            )

    def test_idempotent_migration(self):
        self.add()
        init_camera_db(self.engine)
        self.assertEqual(len(list_cameras(self.engine, "one")), 1)

    def test_dashboard_camera_form_without_traffic(self):
        import db_config
        from streamlit.testing.v1 import AppTest

        with (
            patch.object(db_config, "engine", self.engine),
            patch.object(db_config, "init_db"),
        ):
            app = AppTest.from_file(
                str(Path(cloud_api.__file__).with_name("dashboard.py")),
                default_timeout=20,
            )
            app.session_state["tenant_id"] = "one"
            app.session_state["tenant_name"] = "Test Store"
            app.run()
            app.sidebar.radio[0].set_value("Configurar Câmeras").run()
            self.assertFalse(app.exception)
            next(r for r in app.radio if r.label == "Tipo de câmera").set_value(
                "ip"
            ).run()
            next(
                button for button in app.button if button.label == "Continuar"
            ).click().run()
            fields = {field.label: field for field in app.text_input}
            fields["Nome da câmera"].set_value("Entrada")
            fields["IP Local"].set_value("192.168.1.21")
            fields["Usuário"].set_value("admin")
            fields["Senha"].set_value("test-camera-pass")
            next(
                button for button in app.button if button.label == "Revisar cadastro"
            ).click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(list_cameras(self.engine, "one")), 0)
            next(
                button for button in app.button if button.label == "Confirmar e salvar"
            ).click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(list_cameras(self.engine, "one")), 1)
            self.assertEqual(len(list_cameras(self.engine, "two")), 0)
            self.assertTrue(app.success)


class EdgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.yml"
        self.original = {
            "mqtt": {"host": "mqtt"},
            "detectors": {"cpu": {"type": "cpu"}},
            "cameras": {
                "local": {
                    "enabled": False,
                    "ffmpeg": {
                        "inputs": [{"path": "rtsp://local", "roles": ["detect"]}]
                    },
                }
            },
        }
        self.path.write_text(yaml.safe_dump(self.original))
        self.provisioner = Provisioner(self.path, "frigate")
        for method in ["check_mount", "validate", "restart", "healthy"]:
            setattr(self.provisioner, method, AsyncMock())

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_add_idempotent_update_remove(self):
        p = self.provisioner
        self.assertTrue(await p.apply(snapshot()))
        self.assertEqual(p.restart.await_count, 1)
        self.assertFalse(await p.apply(snapshot()))
        self.assertEqual(p.restart.await_count, 1)
        loaded = yaml.safe_load(self.path.read_text())
        name = snapshot()["cameras"][0]["frigate_name"]
        loaded["cameras"][name]["zones"] = {"entry": {"coordinates": "0,0,1,0,1,1"}}
        self.path.write_text(yaml.safe_dump(loaded))
        changed = copy.deepcopy(snapshot()["cameras"])
        changed[0]["rtsp_url"] = changed[0]["rtsp_url"].replace(
            "secret", "new-password"
        )
        await p.apply(snapshot(changed))
        loaded = yaml.safe_load(self.path.read_text())
        self.assertIn("zones", loaded["cameras"][name])
        self.assertEqual(loaded["detectors"], self.original["detectors"])
        await p.apply(snapshot([]))
        self.assertEqual(yaml.safe_load(self.path.read_text()), self.original)

    async def test_invalid_response_preserves_file(self):
        original = self.path.read_bytes()
        for payload in [
            {},
            {"schema_version": 1, "tenant_id": "one"},
            {**snapshot(), "revision": "wrong"},
        ]:
            with self.assertRaises(ValueError):
                await self.provisioner.apply(payload)
        self.assertEqual(self.path.read_bytes(), original)
        self.provisioner.restart.assert_not_awaited()

    async def test_validation_failure_never_restarts(self):
        original = self.path.read_bytes()
        self.provisioner.validate.side_effect = RuntimeError("bad config")
        with self.assertRaises(RuntimeError):
            await self.provisioner.apply(snapshot())
        self.assertEqual(self.path.read_bytes(), original)
        self.provisioner.restart.assert_not_awaited()

    async def test_failed_health_restores_original_and_state(self):
        original = self.path.read_bytes()
        p = self.provisioner
        p.healthy.side_effect = [RuntimeError("failed"), None]
        with self.assertRaises(RuntimeError):
            await p.apply(snapshot())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(p.restart.await_count, 2)
        self.assertEqual(json.loads(p.state_path.read_text()), {})
        self.assertFalse(p.journal_path.exists())

    async def test_recovery_failure_keeps_journal(self):
        p = self.provisioner
        p.healthy.side_effect = RuntimeError("failed")
        with self.assertRaises(RuntimeError):
            await p.apply(snapshot())
        self.assertTrue(p.journal_path.exists())
        p.healthy.side_effect = None
        await p.recover()
        self.assertFalse(p.journal_path.exists())
        self.assertEqual(yaml.safe_load(self.path.read_text()), self.original)

    async def test_interrupted_apply_recovers_before_next_snapshot(self):
        p = self.provisioner
        original = self.path.read_text()
        p.journal_path.write_text(json.dumps({"original": original, "state": {}}))
        self.path.write_text("cameras: {}")
        await p.apply(snapshot([]))
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(p.restart.await_count, 1)

    async def test_dry_run_never_writes_or_restarts(self):
        original = self.path.read_bytes()
        self.assertTrue(await self.provisioner.apply(snapshot(), dry_run=True))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.provisioner.state_path.exists())
        self.provisioner.restart.assert_not_awaited()

    async def test_tenant_change_and_local_collision_rejected(self):
        await self.provisioner.apply(snapshot())
        with self.assertRaises(ValueError):
            await self.provisioner.apply(snapshot([], tenant="two"))
        with self.assertRaises(ValueError):
            merge_config(yaml.safe_load(self.path.read_text()), snapshot(), {})

    async def test_private_backup_permissions(self):
        await self.provisioner.apply(snapshot())
        for path in [
            self.path,
            self.provisioner.backup_path,
            self.provisioner.state_path,
        ]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_cloud_errors_are_not_empty_snapshots(self):
        for status in [401, 500, 302]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, status=status: httpx.Response(status)
                )
            ) as client:
                with self.assertRaises(httpx.HTTPStatusError):
                    await fetch_snapshot(client, "https://example.test", "key")

    async def test_bound_file_mount_rejected(self):
        p = Provisioner(self.path, "frigate")
        with (
            patch(
                "edge_provisioner.command",
                AsyncMock(
                    return_value=json.dumps(
                        [
                            {
                                "Mounts": [
                                    {
                                        "Type": "bind",
                                        "Source": str(self.path),
                                        "Destination": "/config/config.yml",
                                    }
                                ]
                            }
                        ]
                    ).encode()
                ),
            ),
            self.assertRaises(ValueError),
        ):
            await p.check_mount()


if __name__ == "__main__":
    unittest.main()
