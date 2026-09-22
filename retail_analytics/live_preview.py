"""Tenant-scoped, short-lived webcam preview storage in a shared memory volume."""

import hashlib
import io
import os
import re
import tempfile
import time
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import text

MAX_FRAME_BYTES = 500_000
FRAME_TTL_SECONDS = 10


def authorize_source(engine, tenant_id: str, camera_id: str) -> bool:
    """Allow only an enrolled webcam belonging to the authenticated store."""
    if not re.fullmatch(r"[0-9a-f]{32}", camera_id):
        return False
    with engine.connect() as conn:
        return (
            conn.execute(
                text("""
            SELECT camera_id FROM traffic_webcam_sources
            WHERE tenant_id=:tid AND camera_id=:cid
        """),
                {"tid": tenant_id, "cid": camera_id},
            ).first()
            is not None
        )


def frame_path(tenant_id: str, camera_id: str) -> Path:
    """Derive a private namespace without accepting paths from request input."""
    root = os.environ.get("LIVE_PREVIEW_DIR")
    if not root:
        raise RuntimeError("Live preview storage is not configured")
    name = hashlib.sha256((tenant_id + ":" + camera_id).encode()).hexdigest()
    return Path(root) / (name + ".jpg")


def store_frame(tenant_id: str, camera_id: str, body: bytes) -> None:
    """Validate and re-encode one bounded JPEG, replacing the previous frame."""
    if not body or len(body) > MAX_FRAME_BYTES:
        raise ValueError("Invalid frame size")
    try:
        with Image.open(io.BytesIO(body)) as picture:
            if (
                picture.format != "JPEG"
                or not 1 <= picture.width <= 1920
                or not 1 <= picture.height <= 1080
            ):
                raise ValueError("Invalid image format or dimensions")
            picture.load()
            picture.thumbnail((640, 480))
            output = io.BytesIO()
            picture.convert("RGB").save(output, format="JPEG", quality=75)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("Invalid JPEG") from error
    path = frame_path(tenant_id, camera_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".frame-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(output.getvalue())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # The API's writable mount removes expired previews. The Dashboard mounts it read-only.
    for old in path.parent.glob("*.jpg"):
        try:
            if time.time() - old.stat().st_mtime > FRAME_TTL_SECONDS:
                old.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def load_frame(tenant_id: str, camera_id: str) -> tuple[bytes, float] | None:
    """Never display a stale frame as live video or serve it through a public URL."""
    path = frame_path(tenant_id, camera_id)
    try:
        with path.open("rb") as stream:
            age = time.time() - os.fstat(stream.fileno()).st_mtime
            if not 0 <= age <= FRAME_TTL_SECONDS:
                return None
            data = stream.read(MAX_FRAME_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > MAX_FRAME_BYTES:
        return None
    return data, age
