"""Versioned Intelbras zones, observed trajectories, dwell and in-app alerts."""

import hashlib
import io
import json
import math
import time

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from visitor_store import observe_visitor


class Zone(BaseModel):
    """A normalized polygon; roles describe geometry, not a person's identity."""

    model_config = ConfigDict(extra="forbid")
    zone_id: str = Field(pattern=r"^aivo_[a-z0-9_]{1,32}$")
    name: str = Field(min_length=1, max_length=60)
    role: str = Field(pattern=r"^(outside|inside|area)$")
    points: list[tuple[float, float]] = Field(min_length=3, max_length=20)
    alert_after: int = Field(default=0, ge=0, le=7200)

    @model_validator(mode="after")
    def valid_polygon(self):
        if any(
            not math.isfinite(v) or not 0 <= v <= 1
            for point in self.points
            for v in point
        ):
            raise ValueError("Polygon coordinates must be normalized")
        if len(set(self.points)) != len(self.points):
            raise ValueError("Repeated polygon point")
        area = sum(
            a[0] * b[1] - b[0] * a[1]
            for a, b in zip(self.points, self.points[1:] + self.points[:1])
        )
        if abs(area) < 0.001:
            raise ValueError("Polygon is too small")

        def orient(a, b, c):
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

        edges = list(zip(self.points, self.points[1:] + self.points[:1]))
        for i, (a, b) in enumerate(edges):
            for j, (c, d) in enumerate(edges):
                if j <= i + 1 or (i == 0 and j == len(edges) - 1):
                    continue
                if (
                    orient(a, b, c) * orient(a, b, d) <= 0
                    and orient(c, d, a) * orient(c, d, b) <= 0
                ):
                    raise ValueError("Polygon edges intersect")
        if self.role in ("outside", "inside") and self.alert_after:
            raise ValueError("Counting zones cannot delay presence")
        return self


class CameraProfile(BaseModel):
    """Bounded operating settings editable by the store owner."""

    model_config = ConfigDict(extra="forbid")
    zones: list[Zone] = Field(default_factory=list, max_length=12)
    recording: bool = True
    retention_days: int = Field(default=1, ge=1, le=7)

    @model_validator(mode="after")
    def unique_zones(self):
        if len({z.zone_id for z in self.zones}) != len(self.zones):
            raise ValueError("Zone IDs must be unique")
        roles = [z.role for z in self.zones]
        if roles.count("outside") > 1 or roles.count("inside") > 1:
            raise ValueError("Use one outside and one inside counting zone")
        return self


class Sample(BaseModel):
    """One confirmed Frigate person observation, never an inferred departure."""

    model_config = ConfigDict(extra="forbid")
    camera_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    tracking_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")
    observed_at: float = Field(gt=0, allow_inf_nan=False)
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    zones: list[str] = Field(default_factory=list, max_length=12)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    ended: bool = False

    @model_validator(mode="after")
    def check_time(self):
        if self.observed_at > time.time() + 300:
            raise ValueError("Observation is in the future")
        return self


class Samples(BaseModel):
    samples: list[Sample] = Field(min_length=1, max_length=100)


def init_retail_db(engine):
    """Create additive tables; historical observations remain immutable."""
    with engine.begin() as c:
        for sql in (
            """CREATE TABLE IF NOT EXISTS retail_profiles (tenant_id VARCHAR NOT NULL,
               camera_id VARCHAR(32) NOT NULL, revision VARCHAR(64) NOT NULL,
               settings TEXT NOT NULL, PRIMARY KEY(tenant_id,camera_id,revision))""",
            """CREATE TABLE IF NOT EXISTS retail_profile_current (tenant_id VARCHAR NOT NULL,
               camera_id VARCHAR(32) NOT NULL, revision VARCHAR(64) NOT NULL,
               PRIMARY KEY(tenant_id,camera_id))""",
            """CREATE TABLE IF NOT EXISTS retail_samples (tenant_id VARCHAR NOT NULL,
               camera_id VARCHAR(32) NOT NULL, tracking_id VARCHAR(128) NOT NULL,
               observed_at DOUBLE PRECISION NOT NULL, x DOUBLE PRECISION NOT NULL,
               y DOUBLE PRECISION NOT NULL, zones TEXT NOT NULL, revision VARCHAR(64) NOT NULL,
               ended BOOLEAN NOT NULL, PRIMARY KEY(tenant_id,camera_id,tracking_id,observed_at))""",
            """CREATE INDEX IF NOT EXISTS ix_retail_samples_time ON retail_samples(tenant_id,camera_id,observed_at)""",
            """CREATE TABLE IF NOT EXISTS retail_alerts (tenant_id VARCHAR NOT NULL,
               camera_id VARCHAR(32) NOT NULL, tracking_id VARCHAR(128) NOT NULL,
               zone_id VARCHAR(40) NOT NULL, revision VARCHAR(64) NOT NULL,
               occurred_at DOUBLE PRECISION NOT NULL, seconds DOUBLE PRECISION NOT NULL,
               reviewed BOOLEAN NOT NULL DEFAULT FALSE,
               PRIMARY KEY(tenant_id,camera_id,tracking_id,zone_id,revision))""",
            """CREATE TABLE IF NOT EXISTS retail_zone_state (tenant_id VARCHAR NOT NULL,
               camera_id VARCHAR(32) NOT NULL, tracking_id VARCHAR(128) NOT NULL,
               zone_id VARCHAR(40) NOT NULL, revision VARCHAR(64) NOT NULL,
               observed_at DOUBLE PRECISION NOT NULL, seconds DOUBLE PRECISION NOT NULL,
               PRIMARY KEY(tenant_id,camera_id,tracking_id,zone_id))""",
            """CREATE TABLE IF NOT EXISTS retail_edge_health (tenant_id VARCHAR NOT NULL,
               camera_id VARCHAR(32) NOT NULL, received_at DOUBLE PRECISION NOT NULL,
               camera_fps DOUBLE PRECISION NOT NULL, revision VARCHAR(64) NOT NULL,
               queue_size INTEGER NOT NULL, PRIMARY KEY(tenant_id,camera_id))""",
        ):
            c.execute(text(sql))


def own_camera(c, tid, cid):
    """Reject unknown, disabled or foreign Intelbras sources."""
    if not c.execute(
        text(
            "SELECT camera_id FROM cameras WHERE tenant_id=:t AND camera_id=:c AND enabled=TRUE"
        ),
        {"t": tid, "c": cid},
    ).first():
        raise PermissionError("Camera unavailable")


def profile_revision(settings):
    return hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def get_profile(engine, tid, cid):
    """Read the active profile, falling back to bounded first-install defaults."""
    with engine.connect() as c:
        own_camera(c, tid, cid)
        row = c.execute(
            text("""SELECT p.settings,p.revision FROM retail_profiles p
          JOIN retail_profile_current a ON a.tenant_id=p.tenant_id AND a.camera_id=p.camera_id AND a.revision=p.revision
          WHERE p.tenant_id=:t AND p.camera_id=:c"""),
            {"t": tid, "c": cid},
        ).first()
    if row:
        return {"settings": json.loads(row[0]), "revision": row[1]}
    settings = CameraProfile().model_dump(mode="json")
    return {"settings": settings, "revision": profile_revision(settings)}


def save_profile(engine, tid, cid, settings, expected_revision):
    """Use compare-and-swap so two open editors cannot overwrite one another."""
    settings = CameraProfile.model_validate(settings).model_dump(mode="json")
    revision = profile_revision(settings)
    default = profile_revision(CameraProfile().model_dump(mode="json"))
    with engine.begin() as c:
        own_camera(c, tid, cid)
        c.execute(
            text("""INSERT INTO retail_profile_current VALUES (:t,:c,:r)
            ON CONFLICT(tenant_id,camera_id) DO NOTHING"""),
            {"t": tid, "c": cid, "r": default},
        )
        updated = c.execute(
            text("""UPDATE retail_profile_current SET revision=:r
            WHERE tenant_id=:t AND camera_id=:c AND revision=:old"""),
            {"t": tid, "c": cid, "r": revision, "old": expected_revision},
        )
        if updated.rowcount != 1:
            raise ValueError("A configuração mudou. Reabra o editor antes de salvar.")
        c.execute(
            text("""INSERT INTO retail_profiles VALUES (:t,:c,:r,:s)
            ON CONFLICT(tenant_id,camera_id,revision) DO NOTHING"""),
            {"t": tid, "c": cid, "r": revision, "s": json.dumps(settings)},
        )
    return revision


def known_profile(c, tid, cid, revision):
    row = c.execute(
        text(
            "SELECT settings FROM retail_profiles WHERE tenant_id=:t AND camera_id=:c AND revision=:r"
        ),
        {"t": tid, "c": cid, "r": revision},
    ).first()
    if row:
        return json.loads(row[0])
    default = CameraProfile().model_dump(mode="json")
    if revision == profile_revision(default):
        return default
    raise ValueError("Unknown calibration revision")


def ingest_samples(engine, tid, samples):
    """Commit idempotent observations and visitor identities in one transaction."""
    with engine.begin() as c:
        for item in samples:
            item = Sample.model_validate(item).model_dump()
            cid = item["camera_id"]
            own_camera(c, tid, cid)
            profile = known_profile(c, tid, cid, item["revision"])
            if not set(item["zones"]) <= {z["zone_id"] for z in profile["zones"]}:
                raise ValueError("Unknown zone")
            p = {"tid": tid, **item, "zones": json.dumps(sorted(set(item["zones"])))}
            old = (
                c.execute(
                    text("""SELECT x,y,zones,revision FROM retail_samples WHERE tenant_id=:tid
                AND camera_id=:camera_id AND tracking_id=:tracking_id AND observed_at=:observed_at"""),
                    p,
                )
                .mappings()
                .first()
            )
            if old and any(old[k] != p[k] for k in old):
                raise ValueError("Conflicting observation")
            previous = (
                c.execute(
                    text("""SELECT observed_at,zones,revision,ended FROM retail_samples
                WHERE tenant_id=:tid AND camera_id=:camera_id AND tracking_id=:tracking_id
                ORDER BY observed_at DESC LIMIT 1"""),
                    p,
                )
                .mappings()
                .first()
            )
            c.execute(
                text("""INSERT INTO retail_samples VALUES
                (:tid,:camera_id,:tracking_id,:observed_at,:x,:y,:zones,:revision,:ended)
                ON CONFLICT(tenant_id,camera_id,tracking_id,observed_at)
                DO UPDATE SET ended=retail_samples.ended OR excluded.ended"""),
                p,
            )
            observe_visitor(c, tid, cid, item["tracking_id"], item["observed_at"])
            if not old and (
                not previous or previous["observed_at"] < item["observed_at"]
            ):
                advance_alerts(c, tid, item, previous, profile)

    return len(samples)


def advance_alerts(c, tid, item, previous, profile):
    """Produce alerts at ingestion, without requiring an open dashboard."""
    p = {"tid": tid, "camera_id": item["camera_id"], "tracking_id": item["tracking_id"]}
    states = {
        r["zone_id"]: dict(r)
        for r in c.execute(
            text("""SELECT * FROM retail_zone_state
        WHERE tenant_id=:tid AND camera_id=:camera_id AND tracking_id=:tracking_id"""),
            p,
        ).mappings()
    }
    c.execute(
        text("""DELETE FROM retail_zone_state WHERE tenant_id=:tid AND camera_id=:camera_id
        AND tracking_id=:tracking_id"""),
        p,
    )
    gap = item["observed_at"] - previous["observed_at"] if previous else 0
    continuous = (
        previous
        and not previous["ended"]
        and previous["revision"] == item["revision"]
        and 0 < gap <= 75
    )
    previous_zones = set(json.loads(previous["zones"])) if continuous else set()
    zones = {z["zone_id"]: z for z in profile["zones"]}
    for zone in set(item["zones"]):
        state = states.get(zone)
        seconds = 0
        if zone in previous_zones:
            seconds = gap
            if (
                state
                and state["revision"] == item["revision"]
                and state["observed_at"] == previous["observed_at"]
            ):
                seconds += state["seconds"]
        values = {
            **p,
            "zone": zone,
            "revision": item["revision"],
            "now": item["observed_at"],
            "seconds": seconds,
        }
        if not item["ended"]:
            c.execute(
                text("""INSERT INTO retail_zone_state VALUES
                (:tid,:camera_id,:tracking_id,:zone,:revision,:now,:seconds)"""),
                values,
            )
        threshold = zones[zone]["alert_after"]
        if threshold and seconds >= threshold:
            c.execute(
                text("""INSERT INTO retail_alerts VALUES
                (:tid,:camera_id,:tracking_id,:zone,:revision,:now,:seconds,FALSE)
                ON CONFLICT(tenant_id,camera_id,tracking_id,zone_id,revision) DO NOTHING"""),
                values,
            )


def upload_person_photo(engine, tid, cid, tracking, body):
    """Store a bounded, metadata-free Frigate person snapshot on its visitor."""
    if not body or len(body) > 1000000:
        raise ValueError("Invalid photo size")
    with Image.open(io.BytesIO(body)) as im:
        if im.format != "JPEG" or im.width > 4096 or im.height > 4096:
            raise ValueError("Invalid photo")
        im.load()
        crop = im.convert("RGB")
        crop.thumbnail((256, 384))
        out = io.BytesIO()
        crop.save(out, format="JPEG", quality=80)
    with engine.begin() as c:
        own_camera(c, tid, cid)
        result = c.execute(
            text("""UPDATE traffic_visitors SET photo=COALESCE(photo,:photo),photo_at=COALESCE(photo_at,:now)
            WHERE tenant_id=:t AND camera_id=:c AND tracking_id=:track"""),
            {
                "photo": out.getvalue(),
                "now": time.time(),
                "t": tid,
                "c": cid,
                "track": tracking,
            },
        )
        if result.rowcount != 1:
            raise LookupError("Observation must arrive before its photo")


def retail_report(engine, tid, cid, start, end):
    """Estimate observed dwell only between continuous, same-calibration samples."""
    with engine.connect() as c:
        own_camera(c, tid, cid)
        rows = [
            dict(r)
            for r in c.execute(
                text("""SELECT tracking_id,observed_at,x,y,zones,revision,ended
            FROM retail_samples WHERE tenant_id=:t AND camera_id=:c AND observed_at>=:s AND observed_at<:e
            ORDER BY tracking_id,observed_at LIMIT 50001"""),
                {"t": tid, "c": cid, "s": start - 75, "e": end},
            ).mappings()
        ]
        profiles = {
            r[0]: json.loads(r[1])
            for r in c.execute(
                text(
                    "SELECT revision,settings FROM retail_profiles WHERE tenant_id=:t AND camera_id=:c"
                ),
                {"t": tid, "c": cid},
            )
        }
    limited = len(rows) > 50000
    rows = rows[:50000]
    zones = {}
    journeys = {}
    heat = {}
    previous = {}
    alerts = []
    continuous = {}
    for row in rows:
        current = set(json.loads(row["zones"]))
        track = row["tracking_id"]
        now = row["observed_at"]
        settings = profiles.get(row["revision"], {"zones": []})
        byid = {z["zone_id"]: z for z in settings["zones"]}
        prev = previous.get(track)
        if now >= start:
            journeys.setdefault(track, [])
            zone_label = " + ".join(
                byid[z]["name"] for z in sorted(current) if z in byid
            )
            if zone_label and (
                not journeys[track] or journeys[track][-1]["zone"] != zone_label
            ):
                journeys[track].append({"zone": zone_label, "at": now})
            for zone in sorted(current):
                if zone not in byid:
                    continue
                zones.setdefault(
                    zone, {"name": byid[zone]["name"], "seconds": 0.0, "tracks": set()}
                )["tracks"].add(track)
        gap = now - prev["observed_at"] if prev else 0
        valid = (
            prev
            and not prev["ended"]
            and prev["revision"] == row["revision"]
            and 0 < gap <= 75
        )
        shared = current & set(json.loads(prev["zones"])) if valid else set()
        seconds = max(0, now - max(prev["observed_at"], start)) if valid else 0
        for key in list(continuous):
            if key[0] == track and key[1] not in shared:
                continuous.pop(key)
        if seconds and now >= start:
            cell = (min(35, int(prev["x"] * 36)), min(23, int(prev["y"] * 24)))
            heat[cell] = heat.get(cell, 0) + seconds
            for zone in shared:
                if zone not in byid:
                    continue
                z = zones.setdefault(
                    zone, {"name": byid[zone]["name"], "seconds": 0.0, "tracks": set()}
                )
                z["seconds"] += seconds
                z["tracks"].add(track)
                key = (track, zone, row["revision"])
                continuous[key] = continuous.get(key, 0) + seconds
                threshold = byid[zone]["alert_after"]
                if threshold and continuous[key] >= threshold:
                    alerts.append(
                        {
                            "track": track,
                            "zone": zone,
                            "revision": row["revision"],
                            "at": now,
                            "seconds": continuous[key],
                        }
                    )
        previous[track] = row
    with engine.begin() as c:
        for a in alerts:
            c.execute(
                text("""INSERT INTO retail_alerts VALUES (:t,:c,:track,:zone,:revision,:at,:seconds,FALSE)
                ON CONFLICT(tenant_id,camera_id,tracking_id,zone_id,revision) DO NOTHING"""),
                {"t": tid, "c": cid, **a},
            )
        alert_rows = [
            dict(r)
            for r in c.execute(
                text("""SELECT * FROM retail_alerts WHERE tenant_id=:t AND camera_id=:c
            AND occurred_at>=:s AND occurred_at<:e ORDER BY occurred_at DESC LIMIT 100"""),
                {"t": tid, "c": cid, "s": start, "e": end},
            ).mappings()
        ]
    return {
        "zones": [
            {"zone_id": k, **v, "tracks": len(v["tracks"])} for k, v in zones.items()
        ],
        "heat": [{"x": x, "y": y, "seconds": v} for (x, y), v in heat.items()],
        "journeys": journeys,
        "alerts": alert_rows,
        "limited": limited,
    }
