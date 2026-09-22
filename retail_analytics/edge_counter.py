"""Run the webcam proof-of-concept counter with durable HTTPS delivery."""

import asyncio
import fcntl
import json
import logging
import os
import signal
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiomqtt
import httpx
import yaml
from crossing_counter import CrossingCounter, gate_fingerprint

logger = logging.getLogger(__name__)


async def send_pending(client: httpx.AsyncClient, counter: CrossingCounter) -> int:
    """Retain a batch unless the server acknowledges every submitted identity."""
    events = await asyncio.to_thread(counter.pending)
    if not events:
        return 0
    response = await client.post("/api/traffic/crossings", json={"events": events})
    response.raise_for_status()
    acknowledged = response.json().get("acknowledged")
    expected = {event["event_id"] for event in events}
    if not isinstance(acknowledged, list) or set(acknowledged) != expected:
        raise ValueError("Cloud acknowledgment does not match the submitted batch")
    await asyncio.to_thread(counter.acknowledge, acknowledged)
    logger.info("Cloud acknowledged %d crossings", len(events))
    return len(events)


async def consume(counter: CrossingCounter, health: dict, config_path: Path) -> None:
    """Consume local Frigate events and reset partial tracks after disconnects."""
    host = os.environ.get("MQTT_BROKER", "mqtt")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    prefix = os.environ.get("MQTT_TOPIC_PREFIX", "aivo_webcam")
    while True:
        try:
            await asyncio.to_thread(counter.reset_tracks)
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=os.environ.get("MQTT_USERNAME"),
                password=os.environ.get("MQTT_PASSWORD"),
                identifier="aivo-webcam-counter",
            ) as mqtt:
                await mqtt.subscribe(prefix + "/events")
                await mqtt.subscribe(prefix + "/available")
                health["mqtt_connected"] = True
                logger.info("Connected to the local MQTT broker")
                async for message in mqtt.messages:
                    if str(message.topic) == prefix + "/available":
                        health["frigate_available"] = (
                            bytes(message.payload).decode() == "online"
                        )
                        if not health["frigate_available"]:
                            await asyncio.to_thread(counter.reset_tracks)
                        continue
                    if message.retain:
                        continue
                    try:
                        payload = json.loads(message.payload)
                        # Recheck geometry before every event so a local edit cannot
                        # combine observations from two different calibrations.
                        config = yaml.safe_load(
                            await asyncio.to_thread(config_path.read_text)
                        )
                        camera = config["cameras"][counter.camera_name]
                        if not camera.get("enabled", True) or not camera.get(
                            "detect", {}
                        ).get("enabled", True):
                            await asyncio.to_thread(counter.reset_tracks)
                            continue
                        revision = gate_fingerprint(
                            camera["zones"], counter.outside, counter.inside
                        )
                        counter.gate_revision = revision
                        health["gate_revision"] = revision
                        after = payload.get("after", {})
                        event = await asyncio.to_thread(counter.process, payload)
                        if (
                            after.get("camera") == counter.camera_name
                            and after.get("label") == "person"
                            and type(after.get("frame_time")) in (int, float)
                        ):
                            health["last_person_event"] = after["frame_time"]
                        if event:
                            logger.info("Observed crossing: %s", event["direction"])
                    except (ValueError, TypeError, KeyError, OSError, yaml.YAMLError):
                        await asyncio.to_thread(counter.reset_tracks)
                        logger.warning(
                            "Ignored malformed event or unavailable zone calibration"
                        )
        except aiomqtt.MqttError:
            logger.warning("MQTT disconnected; waiting to reconnect")
        finally:
            health["mqtt_connected"] = False
            health["frigate_available"] = None
        await asyncio.sleep(5)


async def deliver(
    client: httpx.AsyncClient, counter: CrossingCounter, health: dict
) -> None:
    """Retry unavailable cloud services while keeping detection independent."""
    failures = 0
    last_heartbeat = 0.0
    last_prune = 0.0
    while True:
        try:
            await send_pending(client, counter)
            if time.monotonic() - last_heartbeat >= 30:
                pending = await asyncio.to_thread(counter.pending, 100000)
                response = await client.post(
                    "/api/traffic/heartbeat",
                    json={
                        "camera_id": counter.camera_id,
                        **health,
                        "pending_events": len(pending),
                    },
                )
                response.raise_for_status()
                last_heartbeat = time.monotonic()
            if time.monotonic() - last_prune >= 3600:
                await asyncio.to_thread(counter.prune)
                last_prune = time.monotonic()
            failures = 0
        except (httpx.HTTPError, ValueError, TypeError) as error:
            failures += 1
            logger.warning(
                "Cloud delivery pending (%s); events remain on disk",
                type(error).__name__,
            )
        await asyncio.sleep(min(5 * 2 ** min(failures, 4), 60))


async def run() -> None:
    """Enroll this webcam as a test source and run until gracefully stopped."""
    os.umask(0o077)
    base_url = os.environ["CLOUD_API_URL"].rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise ValueError("CLOUD_API_URL must be an HTTPS origin")
    key = (
        await asyncio.to_thread(Path(os.environ["TENANT_API_KEY_FILE"]).read_text)
    ).strip()
    camera_id = os.environ["COUNTING_CAMERA_ID"]
    camera_name = os.environ.get("COUNTING_CAMERA_NAME", "webcam_notebook")
    config_path = Path(os.environ.get("FRIGATE_CONFIG_PATH", "/frigate/config.yml"))
    config = yaml.safe_load(await asyncio.to_thread(config_path.read_text))
    zones = config["cameras"][camera_name]["zones"]
    outside, inside = "aivo_outside", "aivo_inside"
    revision = gate_fingerprint(zones, outside, inside)
    database = Path(os.environ.get("COUNTER_DATABASE", "/data/counter.db"))
    counter = await asyncio.to_thread(
        CrossingCounter,
        database,
        camera_id,
        revision,
        outside,
        inside,
        2.0,
        30.0,
        camera_name,
    )
    with database.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"x-api-key": key},
            timeout=15,
            follow_redirects=False,
        ) as client:
            # Establish tenant ownership before sending any queued observations.
            while True:
                try:
                    response = await client.post(
                        "/api/traffic/webcam-sources",
                        json={"camera_id": camera_id, "name": "Webcam do notebook"},
                    )
                    response.raise_for_status()
                    result = response.json()
                    if (
                        result.get("camera_id") != camera_id
                        or result.get("is_test") is not True
                    ):
                        raise ValueError("Unexpected webcam enrollment response")
                    expected = os.environ.get("EXPECTED_TENANT_ID")
                    if expected and result.get("tenant_id") != expected:
                        raise ValueError("Unexpected tenant identity")
                    await asyncio.to_thread(counter.bind_tenant, result["tenant_id"])
                    break
                except httpx.HTTPError:
                    logger.warning("Waiting for cloud webcam enrollment")
                    await asyncio.sleep(15)
            logger.info("Webcam registered as a test source")
            health = {
                "mqtt_connected": False,
                "frigate_available": None,
                "last_person_event": None,
                "gate_revision": revision,
            }
            stopping = asyncio.Event()
            for sig in [signal.SIGINT, signal.SIGTERM]:
                asyncio.get_running_loop().add_signal_handler(sig, stopping.set)
            tasks = [
                asyncio.create_task(consume(counter, health, config_path)),
                asyncio.create_task(deliver(client, counter, health)),
                asyncio.create_task(stopping.wait()),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                if task not in done:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in done:
                if task.exception():
                    raise task.exception()


def main() -> None:
    """Start the local counter without printing secret-bearing exception details."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        yaml.YAMLError,
    ) as error:
        logger.error("Counter stopped (%s)", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
