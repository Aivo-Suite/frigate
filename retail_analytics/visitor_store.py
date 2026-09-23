"""Tenant-scoped anonymous visitor observations and bounded person thumbnails."""

import io
import json
import math
import uuid

from PIL import Image, UnidentifiedImageError
from sqlalchemy import text


def init_visitor_db(engine):
    """Add visitor storage without modifying previous traffic observations."""
    binary = "BYTEA" if engine.dialect.name == "postgresql" else "BLOB"
    with engine.begin() as conn:
        conn.execute(
            text(f"""
            CREATE TABLE IF NOT EXISTS traffic_visitors (
                tenant_id VARCHAR NOT NULL REFERENCES tenants(tenant_id),
                visitor_id VARCHAR(36) NOT NULL,
                camera_id VARCHAR(32) NOT NULL,
                tracking_id VARCHAR(128) NOT NULL,
                first_seen DOUBLE PRECISION NOT NULL,
                last_seen DOUBLE PRECISION NOT NULL,
                photo {binary},
                photo_at DOUBLE PRECISION,
                crm_external_id VARCHAR(200),
                PRIMARY KEY (tenant_id, visitor_id),
                UNIQUE (tenant_id, camera_id, tracking_id)
            )
        """)
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_visitors_tenant_seen ON traffic_visitors (tenant_id, first_seen, visitor_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_crossings_visitor ON traffic_crossings (tenant_id, camera_id, tracking_id)"
            )
        )


def observe_visitor(conn, tenant_id, camera_id, tracking_id, seen, photo=None):
    """Upsert a continuous track; caller must have verified source ownership."""
    if (
        not tracking_id
        or len(tracking_id) > 128
        or not math.isfinite(seen)
        or seen <= 0
    ):
        raise ValueError("Invalid observation")
    visitor_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            json.dumps([tenant_id, camera_id, tracking_id], separators=(",", ":")),
        )
    )
    conn.execute(
        text("""
        INSERT INTO traffic_visitors
            (tenant_id, visitor_id, camera_id, tracking_id, first_seen, last_seen, photo, photo_at)
        VALUES (:tid, :vid, :cid, :track, :seen, :seen, :photo, :photo_at)
        ON CONFLICT (tenant_id, camera_id, tracking_id) DO UPDATE SET
            first_seen=CASE WHEN excluded.first_seen < traffic_visitors.first_seen
                THEN excluded.first_seen ELSE traffic_visitors.first_seen END,
            last_seen=CASE WHEN excluded.last_seen > traffic_visitors.last_seen
                THEN excluded.last_seen ELSE traffic_visitors.last_seen END,
            photo=COALESCE(traffic_visitors.photo, excluded.photo),
            photo_at=COALESCE(traffic_visitors.photo_at, excluded.photo_at)
    """),
        {
            "tid": tenant_id,
            "vid": visitor_id,
            "cid": camera_id,
            "track": tracking_id,
            "seen": seen,
            "photo": photo,
            "photo_at": seen if photo else None,
        },
    )
    return visitor_id


def person_photos(body, tracks):
    """Re-encode detected person regions without retaining the scene or EXIF."""
    if not body or len(body) > 160000:
        raise ValueError("Invalid frame")
    try:
        with Image.open(io.BytesIO(body)) as image:
            if (
                image.format != "JPEG"
                or not 32 <= image.width <= 640
                or not 32 <= image.height <= 480
            ):
                raise ValueError("Invalid frame")
            image.load()
            photos = {}
            for track in tracks:
                box = track["box"]
                if len(box) != 4 or any(
                    not math.isfinite(v) or not 0 <= v <= 1 for v in box
                ):
                    raise ValueError("Invalid person box")
                left, top = int(box[0] * image.width), int(box[1] * image.height)
                right, bottom = int(box[2] * image.width), int(box[3] * image.height)
                if right <= left or bottom <= top:
                    raise ValueError("Empty person box")
                crop = image.crop((left, top, right, bottom)).convert("RGB")
                crop.thumbnail((256, 384))
                out = io.BytesIO()
                crop.save(out, format="JPEG", quality=80)
                photos[track["id"]] = out.getvalue()
            return photos
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise ValueError("Invalid image") from error


def save_browser_visitors(engine, grant, tracks, body, now):
    """Save confirmed browser tracks under the authenticated source only."""
    if not tracks:
        return
    photos = person_photos(body, tracks)
    with engine.begin() as conn:
        owned = conn.execute(
            text(
                "SELECT camera_id FROM traffic_webcam_sources WHERE tenant_id=:tid AND camera_id=:cid"
            ),
            {"tid": grant["tenant_id"], "cid": grant["camera_id"]},
        ).first()
        if not owned:
            raise PermissionError("Camera does not belong to tenant")
        for track in tracks:
            observe_visitor(
                conn,
                grant["tenant_id"],
                grant["camera_id"],
                f"{grant['grant_id']}:{track['id']}",
                now,
                photos[track["id"]],
            )


def list_visitors(engine, tenant_id, start, end, camera_id=None, offset=0, limit=12):
    """Return a bounded page and totals scoped to tenant and first observation."""
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Invalid page")
    where = "v.tenant_id=:tid AND v.first_seen>=:start AND v.first_seen<:end"
    if camera_id:
        where += " AND v.camera_id=:cid"
    params = {
        "tid": tenant_id,
        "start": start,
        "end": end,
        "cid": camera_id,
        "offset": offset,
        "limit": limit,
    }
    with engine.connect() as conn:
        totals = (
            conn.execute(
                text(f"""
            SELECT COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN v.photo IS NOT NULL THEN 1 ELSE 0 END),0) AS with_photo
            FROM traffic_visitors v WHERE {where}
        """),
                params,
            )
            .mappings()
            .one()
        )
        rows = (
            conn.execute(
                text(f"""
            SELECT v.visitor_id, v.camera_id, v.tracking_id, v.first_seen, v.last_seen,
                v.photo, v.photo_at, v.crm_external_id,
                (SELECT COUNT(*) FROM traffic_crossings c WHERE c.tenant_id=v.tenant_id
                    AND c.camera_id=v.camera_id AND c.tracking_id=v.tracking_id
                    AND c.direction='entry') AS entries,
                (SELECT COUNT(*) FROM traffic_crossings c WHERE c.tenant_id=v.tenant_id
                    AND c.camera_id=v.camera_id AND c.tracking_id=v.tracking_id
                    AND c.direction='exit') AS exits
            FROM traffic_visitors v WHERE {where}
            ORDER BY v.first_seen DESC, v.visitor_id DESC LIMIT :limit OFFSET :offset
        """),
                params,
            )
            .mappings()
            .all()
        )
    return {**dict(totals), "items": [dict(row) for row in rows]}
