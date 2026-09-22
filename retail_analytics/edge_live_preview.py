"""Send a one-frame-per-second webcam preview only when explicitly started."""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


async def run() -> None:
    """Relay the local Frigate webcam through authenticated HTTPS uploads."""
    cloud = os.environ["CLOUD_API_URL"].rstrip("/")
    parsed = urlsplit(cloud)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("CLOUD_API_URL must be an HTTPS origin")
    camera_id = os.environ["COUNTING_CAMERA_ID"]
    if not re.fullmatch(r"[0-9a-f]{32}", camera_id):
        raise ValueError("Invalid webcam identity")
    key = (
        await asyncio.to_thread(Path(os.environ["TENANT_API_KEY_FILE"]).read_text)
    ).strip()
    async with (
        httpx.AsyncClient(
            base_url="http://frigate:5000", timeout=5, follow_redirects=False
        ) as local,
        httpx.AsyncClient(
            base_url=cloud,
            headers={"x-api-key": key},
            timeout=10,
            follow_redirects=False,
        ) as remote,
    ):
        logger.info("Webcam live preview relay started")
        failures = 0
        while True:
            started = time.monotonic()
            try:
                response = await local.get("/api/stats")
                response.raise_for_status()
                stats = response.json()
                camera = stats.get("cameras", {}).get("webcam_notebook", {})
                if (
                    camera.get("camera_fps", 0) <= 0
                    or camera.get("process_fps", 0) <= 0
                ):
                    raise ValueError("Camera is not processing frames")
                if time.time() - stats.get("service", {}).get("last_updated", 0) > 30:
                    raise ValueError("Camera statistics are stale")
                response = await local.get(
                    "/api/webcam_notebook/latest.jpg", params={"height": 480}
                )
                response.raise_for_status()
                if len(response.content) > 500000 or not response.content.startswith(
                    b"\xff\xd8"
                ):
                    raise ValueError("Invalid camera image")
                uploaded = await remote.put(
                    "/api/traffic/webcam-preview/" + camera_id,
                    content=response.content,
                    headers={"content-type": "image/jpeg"},
                )
                uploaded.raise_for_status()
                if failures:
                    logger.info("Webcam preview transmission resumed")
                failures = 0
            except (httpx.HTTPError, ValueError, TypeError) as error:
                failures += 1
                if failures == 1 or failures % 12 == 0:
                    logger.warning(
                        "Webcam preview unavailable (%s)", type(error).__name__
                    )
            await asyncio.sleep(
                max(0.05, (5 if failures else 1) - (time.monotonic() - started))
            )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
