"""Short-lived, single-use browser grants and tenant-owned camera settings."""

import hashlib
import secrets
import time
import uuid

from sqlalchemy import text


def init_browser_db(engine):
    """Apply additive migrations for browser capture, without changing Edge sources."""
    with engine.begin() as conn:
        conn.execute(
            text("""CREATE TABLE IF NOT EXISTS browser_camera_sources (
            tenant_id VARCHAR NOT NULL, camera_id VARCHAR(32) NOT NULL,
            axis VARCHAR(1) NOT NULL DEFAULT 'x', position DOUBLE PRECISION NOT NULL DEFAULT 0.5,
            positive_entry BOOLEAN NOT NULL DEFAULT TRUE,
            PRIMARY KEY(tenant_id,camera_id),
            FOREIGN KEY(tenant_id,camera_id) REFERENCES traffic_webcam_sources(tenant_id,camera_id)
        )""")
        )
        conn.execute(
            text("""CREATE TABLE IF NOT EXISTS browser_camera_grants (
            grant_id VARCHAR(32) PRIMARY KEY, token_hash VARCHAR(64) NOT NULL UNIQUE,
            tenant_id VARCHAR NOT NULL, camera_id VARCHAR(32) NOT NULL,
            connect_until DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
            claimed BOOLEAN NOT NULL DEFAULT FALSE, revoked BOOLEAN NOT NULL DEFAULT FALSE,
            FOREIGN KEY(tenant_id,camera_id) REFERENCES browser_camera_sources(tenant_id,camera_id)
        )""")
        )


def list_browser_sources(engine, tenant_id):
    """Read only the authenticated store's browser cameras."""
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text("""
            SELECT b.*, s.name FROM browser_camera_sources b JOIN traffic_webcam_sources s
            ON b.tenant_id=s.tenant_id AND b.camera_id=s.camera_id
            WHERE b.tenant_id=:tid ORDER BY s.name
        """),
                {"tid": tenant_id},
            ).mappings()
        ]


def create_browser_source(engine, tenant_id, name):
    """Create a real capture identity; enrollment does not create traffic data."""
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Informe um nome de até 100 caracteres.")
    cid = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO traffic_webcam_sources (tenant_id,camera_id,name) VALUES (:tid,:cid,:name)"
            ),
            {"tid": tenant_id, "cid": cid, "name": name},
        )
        conn.execute(
            text(
                "INSERT INTO browser_camera_sources (tenant_id,camera_id) VALUES (:tid,:cid)"
            ),
            {"tid": tenant_id, "cid": cid},
        )
    return cid


def issue_grant(engine, tenant_id, camera_id):
    """Issue an opaque capability without exposing the tenant API key to JavaScript."""
    token = secrets.token_urlsafe(32)
    gid = uuid.uuid4().hex
    now = time.time()
    with engine.begin() as conn:
        owned = conn.execute(
            text(
                "SELECT camera_id FROM browser_camera_sources WHERE tenant_id=:tid AND camera_id=:cid"
            ),
            {"tid": tenant_id, "cid": camera_id},
        ).first()
        if not owned:
            raise PermissionError("Camera unavailable")
        conn.execute(
            text("DELETE FROM browser_camera_grants WHERE expires_at<:cutoff"),
            {"cutoff": now - 86400},
        )
        conn.execute(
            text("""INSERT INTO browser_camera_grants
            (grant_id,token_hash,tenant_id,camera_id,connect_until,expires_at)
            VALUES (:gid,:digest,:tid,:cid,:connect,:expires)"""),
            {
                "gid": gid,
                "digest": hashlib.sha256(token.encode()).hexdigest(),
                "tid": tenant_id,
                "cid": camera_id,
                "connect": now + 600,
                "expires": now + 8 * 3600,
            },
        )
    return {"grant_id": gid, "token": token, "camera_id": camera_id}


def claim_grant(engine, token):
    """Atomically consume a valid connection capability exactly once."""
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        raise PermissionError("Invalid grant")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with engine.begin() as conn:
        result = conn.execute(
            text("""UPDATE browser_camera_grants SET claimed=TRUE
            WHERE token_hash=:digest AND claimed=FALSE AND revoked=FALSE AND connect_until>:now"""),
            {"digest": digest, "now": time.time()},
        )
        if result.rowcount != 1:
            raise PermissionError("Invalid or expired grant")
        return dict(
            conn.execute(
                text(
                    "SELECT grant_id,tenant_id,camera_id,expires_at FROM browser_camera_grants WHERE token_hash=:digest"
                ),
                {"digest": digest},
            )
            .mappings()
            .one()
        )


def grant_active(engine, grant):
    """Check logout, expiry and revocation during a running capture."""
    with engine.connect() as conn:
        return (
            conn.execute(
                text("""SELECT grant_id FROM browser_camera_grants
            WHERE grant_id=:gid AND tenant_id=:tid AND revoked=FALSE AND expires_at>:now"""),
                {
                    "gid": grant["grant_id"],
                    "tid": grant["tenant_id"],
                    "now": time.time(),
                },
            ).first()
            is not None
        )


def revoke_grant(engine, tenant_id, grant_id):
    """Stop a page's capability on logout or explicit renewal."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE browser_camera_grants SET revoked=TRUE WHERE tenant_id=:tid AND grant_id=:gid"
            ),
            {"tid": tenant_id, "gid": grant_id},
        )


def validate_gate(gate):
    """Bound line calibration before using coordinates in the tracker."""
    if not isinstance(gate, dict) or gate.get("axis") not in ("x", "y"):
        raise ValueError("Invalid axis")
    pos = gate.get("position")
    if (
        type(pos) not in (float, int)
        or not 0.15 <= pos <= 0.85
        or type(gate.get("positive_entry")) is not bool
    ):
        raise ValueError("Invalid counting line")
    return {
        "axis": gate["axis"],
        "position": float(pos),
        "positive_entry": gate["positive_entry"],
    }


def save_gate(engine, grant, gate):
    """Persist calibration only for the granted camera and store."""
    gate = validate_gate(gate)
    with engine.begin() as conn:
        conn.execute(
            text("""UPDATE browser_camera_sources SET axis=:axis,position=:position,positive_entry=:positive_entry
            WHERE tenant_id=:tid AND camera_id=:cid"""),
            {**gate, "tid": grant["tenant_id"], "cid": grant["camera_id"]},
        )
    return gate
