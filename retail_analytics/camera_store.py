"""Tenant-scoped camera persistence and encrypted Intelbras credentials."""

import hashlib
import ipaddress
import json
import os
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from cryptography.fernet import Fernet
from sqlalchemy import text


def camera_cipher() -> Fernet:
    """Load the shared cloud key without exposing it in application output."""
    key_file = os.environ.get("CAMERA_ENCRYPTION_KEY_FILE")
    key = (
        Path(key_file).read_text().strip()
        if key_file
        else os.environ.get("CAMERA_ENCRYPTION_KEY", "")
    )
    if not key:
        raise RuntimeError("Camera encryption key is not configured")
    return Fernet(key.encode())


def init_camera_db(engine) -> None:
    """Apply an additive, repeatable migration after the tenants table exists."""
    with engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS cameras (
                camera_id VARCHAR(32) PRIMARY KEY,
                tenant_id VARCHAR NOT NULL REFERENCES tenants(tenant_id),
                name VARCHAR(100) NOT NULL,
                local_ip VARCHAR(45) NOT NULL,
                username VARCHAR(128) NOT NULL,
                channel INTEGER NOT NULL CHECK (channel BETWEEN 1 AND 256),
                rtsp_url_encrypted TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, local_ip, channel)
            )
        """)
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_cameras_tenant ON cameras (tenant_id)")
        )


def build_rtsp_url(local_ip: str, username: str, password: str, channel: int) -> str:
    """Build an Intelbras URL, encoding both user-info components."""
    try:
        address = ipaddress.IPv4Address(local_ip.strip())
    except ipaddress.AddressValueError:
        raise ValueError("Informe um endereço IPv4 válido.") from None
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError("Informe o IP da câmera na rede da loja.")
    if not username or len(username) > 128 or not password or len(password) > 1024:
        raise ValueError("Informe usuário e senha válidos.")
    if type(channel) is not int or not 1 <= channel <= 256:
        raise ValueError("O canal deve estar entre 1 e 256.")
    return f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}@{address}:554/cam/realmonitor?channel={channel}&subtype=0"


def list_cameras(engine, tenant_id: str) -> list[dict]:
    """Return display metadata only, never passwords or URLs."""
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text("""
            SELECT camera_id, name, local_ip, username, channel, enabled
            FROM cameras WHERE tenant_id=:tid ORDER BY name, camera_id
        """),
                {"tid": tenant_id},
            ).mappings()
        ]


def save_camera(
    engine,
    tenant_id: str,
    name: str,
    local_ip: str,
    username: str,
    password: str,
    channel: int,
    enabled: bool = True,
    camera_id: str | None = None,
) -> str:
    """Create or update an owned camera; a blank edit password keeps its value."""
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Informe um nome de até 100 caracteres.")
    cipher = camera_cipher()
    with engine.begin() as conn:
        if camera_id:
            row = conn.execute(
                text("""
                SELECT rtsp_url_encrypted FROM cameras
                WHERE tenant_id=:tid AND camera_id=:cid
            """),
                {"tid": tenant_id, "cid": camera_id},
            ).first()
            if row is None:
                raise ValueError("Câmera não encontrada.")
            if not password:
                password = unquote(
                    urlsplit(cipher.decrypt(row[0].encode()).decode()).password or ""
                )
        rtsp = build_rtsp_url(local_ip, username, password, channel)
        values = {
            "cid": camera_id or uuid.uuid4().hex,
            "tid": tenant_id,
            "name": name,
            "ip": local_ip.strip(),
            "user": username,
            "channel": channel,
            "url": cipher.encrypt(rtsp.encode()).decode(),
            "enabled": enabled,
        }
        if camera_id:
            conn.execute(
                text("""
                UPDATE cameras SET name=:name, local_ip=:ip, username=:user,
                channel=:channel, rtsp_url_encrypted=:url, enabled=:enabled,
                updated_at=CURRENT_TIMESTAMP WHERE tenant_id=:tid AND camera_id=:cid
            """),
                values,
            )
        else:
            conn.execute(
                text("""
                INSERT INTO cameras (camera_id, tenant_id, name, local_ip, username,
                    channel, rtsp_url_encrypted, enabled)
                VALUES (:cid, :tid, :name, :ip, :user, :channel, :url, :enabled)
            """),
                values,
            )
    return values["cid"]


def delete_camera(engine, tenant_id: str, camera_id: str) -> None:
    """Delete only a camera belonging to the authenticated tenant."""
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM cameras WHERE tenant_id=:tid AND camera_id=:cid"),
            {"tid": tenant_id, "cid": camera_id},
        )


def camera_snapshot(engine, tenant_id: str) -> dict:
    """Return the complete desired set with a deterministic content revision."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text("""
            SELECT camera_id, name, rtsp_url_encrypted FROM cameras
            WHERE tenant_id=:tid AND enabled=TRUE ORDER BY camera_id
        """),
                {"tid": tenant_id},
            )
            .mappings()
            .all()
        )
    cipher = camera_cipher()
    cameras = [
        {
            "camera_id": row["camera_id"],
            "name": row["name"],
            "frigate_name": "aivo_" + row["camera_id"],
            "rtsp_url": cipher.decrypt(row["rtsp_url_encrypted"].encode()).decode(),
        }
        for row in rows
    ]
    canonical = json.dumps({"tenant_id": tenant_id, "cameras": cameras}, sort_keys=True)
    return {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "revision": hashlib.sha256(canonical.encode()).hexdigest(),
        "cameras": cameras,
    }
