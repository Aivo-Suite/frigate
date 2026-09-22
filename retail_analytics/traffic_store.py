"""Tenant-scoped crossing ingestion, health and time-zone-aware reports."""

import time
from datetime import date, datetime, timedelta
from datetime import time as day_time
from zoneinfo import ZoneInfo

from browser_store import init_browser_db
from sqlalchemy import text
from webcam_setup import init_webcam_settings


def init_traffic_db(engine) -> None:
    """Add crossing and counter health tables without changing legacy analytics."""
    with engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS traffic_webcam_sources (
                tenant_id VARCHAR NOT NULL REFERENCES tenants(tenant_id),
                camera_id VARCHAR(32) NOT NULL,
                name VARCHAR(100) NOT NULL,
                PRIMARY KEY (tenant_id, camera_id)
            )
        """)
        )
        conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS traffic_crossings (
                tenant_id VARCHAR NOT NULL REFERENCES tenants(tenant_id),
                event_id VARCHAR(36) NOT NULL,
                camera_id VARCHAR(32) NOT NULL,
                tracking_id VARCHAR(128) NOT NULL,
                direction VARCHAR(5) NOT NULL CHECK (direction IN ('entry', 'exit')),
                occurred_at DOUBLE PRECISION NOT NULL,
                gate_revision VARCHAR(64) NOT NULL,
                received_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (tenant_id, event_id)
            )
        """)
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_crossings_tenant_time ON traffic_crossings (tenant_id, occurred_at)"
            )
        )
        conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS traffic_counter_health (
                tenant_id VARCHAR NOT NULL REFERENCES tenants(tenant_id),
                camera_id VARCHAR(32) NOT NULL,
                received_at DOUBLE PRECISION NOT NULL,
                mqtt_connected BOOLEAN NOT NULL,
                frigate_available BOOLEAN,
                last_person_event DOUBLE PRECISION,
                pending_events INTEGER NOT NULL,
                gate_revision VARCHAR(64) NOT NULL,
                PRIMARY KEY (tenant_id, camera_id)
            )
        """)
        )

    init_webcam_settings(engine)
    init_browser_db(engine)


def ingest_crossings(engine, tenant_id: str, events: list[dict]) -> list[str]:
    """Insert a whole batch atomically; retries never increase the totals."""
    with engine.begin() as conn:
        owned = set(
            conn.execute(
                text("SELECT camera_id FROM cameras WHERE tenant_id=:tid"),
                {"tid": tenant_id},
            ).scalars()
        )
        owned.update(
            conn.execute(
                text(
                    "SELECT camera_id FROM traffic_webcam_sources WHERE tenant_id=:tid"
                ),
                {"tid": tenant_id},
            ).scalars()
        )
        if any(event["camera_id"] not in owned for event in events):
            raise PermissionError("Camera does not belong to this tenant")
        for event in events:
            existing = (
                conn.execute(
                    text("""
                SELECT camera_id, tracking_id, direction, occurred_at, gate_revision
                FROM traffic_crossings WHERE tenant_id=:tid AND event_id=:event_id
            """),
                    {"tid": tenant_id, "event_id": event["event_id"]},
                )
                .mappings()
                .first()
            )
            if existing and any(existing[key] != event[key] for key in existing):
                raise ValueError("Event identity conflicts with stored data")
            conn.execute(
                text("""
                INSERT INTO traffic_crossings
                    (tenant_id, event_id, camera_id, tracking_id, direction, occurred_at, gate_revision, received_at)
                VALUES (:tid, :event_id, :camera_id, :tracking_id, :direction, :occurred_at, :gate_revision, :received_at)
                ON CONFLICT (tenant_id, event_id) DO NOTHING
            """),
                {**event, "tid": tenant_id, "received_at": time.time()},
            )
            # Check again after ON CONFLICT, including concurrent duplicate requests.
            stored = (
                conn.execute(
                    text("""
                SELECT camera_id, tracking_id, direction, occurred_at, gate_revision
                FROM traffic_crossings WHERE tenant_id=:tid AND event_id=:event_id
            """),
                    {"tid": tenant_id, "event_id": event["event_id"]},
                )
                .mappings()
                .one()
            )
            if any(stored[key] != event[key] for key in stored):
                raise ValueError("Event identity conflicts with stored data")
    return [event["event_id"] for event in events]


def record_health(engine, tenant_id: str, payload: dict) -> None:
    """Track counter connectivity separately from successful video detection."""
    with engine.begin() as conn:
        camera = conn.execute(
            text(
                "SELECT camera_id FROM cameras WHERE tenant_id=:tid AND camera_id=:cid"
            ),
            {"tid": tenant_id, "cid": payload["camera_id"]},
        ).first()
        if camera is None:
            camera = conn.execute(
                text(
                    "SELECT camera_id FROM traffic_webcam_sources WHERE tenant_id=:tid AND camera_id=:cid"
                ),
                {"tid": tenant_id, "cid": payload["camera_id"]},
            ).first()
        if camera is None:
            raise PermissionError("Camera does not belong to this tenant")
        conn.execute(
            text("""
            INSERT INTO traffic_counter_health
                (tenant_id, camera_id, received_at, mqtt_connected, frigate_available,
                 last_person_event, pending_events, gate_revision)
            VALUES (:tid, :camera_id, :received_at, :mqtt_connected, :frigate_available,
                    :last_person_event, :pending_events, :gate_revision)
            ON CONFLICT(tenant_id, camera_id) DO UPDATE SET
                received_at=excluded.received_at, mqtt_connected=excluded.mqtt_connected,
                frigate_available=excluded.frigate_available, last_person_event=excluded.last_person_event,
                pending_events=excluded.pending_events, gate_revision=excluded.gate_revision
        """),
            {**payload, "tid": tenant_id, "received_at": time.time()},
        )


def daily_report(
    engine,
    tenant_id: str,
    selected_date: date,
    timezone_name: str = "America/Sao_Paulo",
    camera_id: str | None = None,
) -> dict:
    """Report observed crossings by local date; never equate net flow with occupancy."""
    timezone = ZoneInfo(timezone_name)
    start = datetime.combine(selected_date, day_time.min, timezone).timestamp()
    end = datetime.combine(
        selected_date + timedelta(days=1), day_time.min, timezone
    ).timestamp()
    where_camera = " AND camera_id=:cid" if camera_id else ""
    params = {"tid": tenant_id, "start": start, "end": end, "cid": camera_id}
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
            SELECT camera_id, direction, occurred_at FROM traffic_crossings
            WHERE tenant_id=:tid AND occurred_at>=:start AND occurred_at<:end
        """
                    + where_camera
                    + " ORDER BY occurred_at"
                ),
                params,
            )
            .mappings()
            .all()
        )
        health = [
            dict(row)
            for row in conn.execute(
                text(
                    """
            SELECT * FROM traffic_counter_health WHERE tenant_id=:tid
        """
                    + where_camera
                ),
                params,
            ).mappings()
        ]
    hourly = [
        {"Hora": f"{hour:02d}:00", "Entradas": 0, "Saídas": 0} for hour in range(24)
    ]
    entries, exits = 0, 0
    for row in rows:
        hour = datetime.fromtimestamp(row["occurred_at"], timezone).hour
        if row["direction"] == "entry":
            entries += 1
            hourly[hour]["Entradas"] += 1
        else:
            exits += 1
            hourly[hour]["Saídas"] += 1
    return {
        "entries": entries,
        "exits": exits,
        "net_flow": entries - exits,
        "hourly": hourly,
        "health": health,
        "timezone": timezone_name,
    }


def register_webcam(engine, tenant_id: str, camera_id: str, name: str) -> None:
    """Enroll a local test source without adding a fake RTSP camera to provisioning."""
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO traffic_webcam_sources (tenant_id, camera_id, name)
            VALUES (:tid, :cid, :name)
            ON CONFLICT (tenant_id, camera_id) DO NOTHING
        """),
            {"tid": tenant_id, "cid": camera_id, "name": name},
        )


def list_counting_sources(engine, tenant_id: str) -> list[dict]:
    """Label test sources explicitly alongside registered production cameras."""
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text("""
            SELECT camera_id, name, FALSE AS is_test FROM cameras WHERE tenant_id=:tid
            UNION ALL
            SELECT camera_id, name, TRUE AS is_test FROM traffic_webcam_sources WHERE tenant_id=:tid
        """),
                {"tid": tenant_id},
            ).mappings()
        ]
