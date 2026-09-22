"""Persist directional door crossings and their delivery queue on the Edge."""

import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class CrossingCounter:
    """Count observed transitions between two exclusive zones for one camera."""

    def __init__(
        self,
        database: Path,
        camera_id: str,
        gate_revision: str,
        outside_zone: str = "aivo_outside",
        inside_zone: str = "aivo_inside",
        cooldown_seconds: float = 2.0,
        max_gap_seconds: float = 30.0,
        camera_name: str | None = None,
    ):
        if outside_zone == inside_zone or not outside_zone or not inside_zone:
            raise ValueError("Two distinct zones are required")
        if cooldown_seconds < 0 or max_gap_seconds <= cooldown_seconds:
            raise ValueError("Invalid crossing timing limits")
        self.database = database
        self.camera_id = camera_id
        self.camera_name = camera_name or "aivo_" + camera_id
        self.outside = outside_zone
        self.inside = inside_zone
        self.gate_revision = gate_revision
        self.cooldown = cooldown_seconds
        self.max_gap = max_gap_seconds
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tracks (
                    camera_id TEXT NOT NULL, tracking_id TEXT NOT NULL,
                    gate_revision TEXT NOT NULL, side TEXT,
                    side_seen REAL, frame_time REAL NOT NULL,
                    last_crossing REAL, closed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(camera_id, tracking_id)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, delivered_at REAL
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection safe for calls from worker threads."""
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def bind_tenant(self, tenant_id: str) -> None:
        """Prevent a reused Edge database from mixing stores after a key change."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT value FROM metadata WHERE name='tenant_id'"
            ).fetchone()
            if existing and existing[0] != tenant_id:
                raise ValueError("Counter database belongs to a different tenant")
            conn.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('tenant_id', ?)", (tenant_id,)
            )

    def process(self, message: dict) -> dict | None:
        """Commit track state and a crossing atomically; duplicates have no effect."""
        if not isinstance(message, dict) or message.get("type") not in {
            "new",
            "update",
            "end",
        }:
            return None
        after = message.get("after")
        if not isinstance(after, dict) or after.get("camera") != self.camera_name:
            return None
        if after.get("label") != "person" or after.get("false_positive", False):
            return None
        tracking_id = after.get("id")
        frame_time = after.get("frame_time")
        zones = after.get("current_zones")
        if (
            not isinstance(tracking_id, str)
            or not 1 <= len(tracking_id) <= 128
            or type(frame_time) not in (int, float)
            or not math.isfinite(frame_time)
            or frame_time <= 0
            or not isinstance(zones, list)
            or not all(isinstance(zone, str) for zone in zones)
        ):
            return None
        outside, inside = self.outside in zones, self.inside in zones
        observed_side = (
            "outside"
            if outside and not inside
            else "inside"
            if inside and not outside
            else None
        )
        closed = message["type"] == "end"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tracks WHERE camera_id=? AND tracking_id=?",
                (self.camera_id, tracking_id),
            ).fetchone()
            if row and (row["closed"] or frame_time <= row["frame_time"]):
                # End messages can share their last observed frame timestamp.
                if closed and frame_time >= row["frame_time"]:
                    conn.execute(
                        "UPDATE tracks SET closed=1 WHERE camera_id=? AND tracking_id=?",
                        (self.camera_id, tracking_id),
                    )
                return None
            side, side_seen, last_crossing = None, None, None
            if row and row["gate_revision"] == self.gate_revision:
                side, side_seen, last_crossing = (
                    row["side"],
                    row["side_seen"],
                    row["last_crossing"],
                )
            event = None
            # End means tracking ended, never proof of physical entry or exit.
            if not closed:
                if outside and inside:
                    side, side_seen = None, None
                elif observed_side:
                    recent = (
                        side_seen is not None and frame_time - side_seen <= self.max_gap
                    )
                    if side and side != observed_side and recent:
                        if (
                            last_crossing is None
                            or frame_time - last_crossing >= self.cooldown
                        ):
                            direction = "entry" if observed_side == "inside" else "exit"
                            identity = json.dumps(
                                [
                                    self.camera_id,
                                    tracking_id,
                                    self.gate_revision,
                                    direction,
                                    frame_time,
                                ],
                                separators=(",", ":"),
                            )
                            event = {
                                "event_id": str(
                                    uuid.uuid5(uuid.NAMESPACE_URL, identity)
                                ),
                                "camera_id": self.camera_id,
                                "tracking_id": tracking_id,
                                "direction": direction,
                                "occurred_at": frame_time,
                                "gate_revision": self.gate_revision,
                            }
                            last_crossing = frame_time
                            side, side_seen = observed_side, frame_time
                    else:
                        side, side_seen = observed_side, frame_time
                elif side_seen is not None and frame_time - side_seen > self.max_gap:
                    side, side_seen = None, None
            conn.execute(
                """
                INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(camera_id, tracking_id) DO UPDATE SET
                    gate_revision=excluded.gate_revision, side=excluded.side,
                    side_seen=excluded.side_seen, frame_time=excluded.frame_time,
                    last_crossing=excluded.last_crossing, closed=excluded.closed
            """,
                (
                    self.camera_id,
                    tracking_id,
                    self.gate_revision,
                    side,
                    side_seen,
                    frame_time,
                    last_crossing,
                    int(closed),
                ),
            )
            if event:
                conn.execute(
                    "INSERT OR IGNORE INTO outbox VALUES (?, ?, ?, NULL)",
                    (event["event_id"], json.dumps(event), time.time()),
                )
            return event

    def pending(self, limit: int = 100) -> list[dict]:
        """Read undelivered events without removing them from persistent storage."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM outbox WHERE delivered_at IS NULL ORDER BY created_at, event_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def acknowledge(self, event_ids: list[str]) -> None:
        """Mark events delivered only after an explicit successful cloud response."""
        with self.connect() as conn:
            conn.executemany(
                "UPDATE outbox SET delivered_at=? WHERE event_id=? AND delivered_at IS NULL",
                [(time.time(), event_id) for event_id in event_ids],
            )

    def reset_tracks(self) -> None:
        """Break trajectories across MQTT disconnects without deleting queued events."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE tracks SET side=NULL, side_seen=NULL WHERE camera_id=?",
                (self.camera_id,),
            )

    def prune(self) -> None:
        """Bound acknowledged history while retaining all unsent events."""
        cutoff = time.time() - 7 * 86400
        with self.connect() as conn:
            conn.execute("DELETE FROM tracks WHERE frame_time < ?", (cutoff,))
            conn.execute("DELETE FROM outbox WHERE delivered_at < ?", (cutoff,))


def gate_fingerprint(zones: dict, outside: str, inside: str) -> str:
    """Invalidate partial tracks when the two calibrated zones change."""
    if outside not in zones or inside not in zones or outside == inside:
        raise ValueError("The camera requires calibrated outside and inside zones")
    for name in [outside, inside]:
        if not zones[name].get("coordinates"):
            raise ValueError("Empty counting zone")
        if zones[name].get("loitering_time", 0) != 0:
            raise ValueError("Counting zones must not delay presence with loitering")
        if "objects" in zones[name] and "person" not in zones[name]["objects"]:
            raise ValueError("Counting zones must track persons")
    canonical = json.dumps(
        {"outside": [outside, zones[outside]], "inside": [inside, zones[inside]]},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
