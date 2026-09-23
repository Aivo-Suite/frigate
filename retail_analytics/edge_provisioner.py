"""Poll the cloud and transactionally apply tenant cameras to a local Frigate."""

import argparse
import asyncio
import copy
import fcntl
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

logger = logging.getLogger(__name__)
CAMERA_ID = re.compile(r"[0-9a-f]{32}")


def validate_snapshot(payload: dict) -> dict:
    """Reject partial, malformed or inconsistent responses before any local write."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Unsupported snapshot")
    tenant = payload.get("tenant_id")
    cameras = payload.get("cameras")
    if not isinstance(tenant, str) or not tenant or not isinstance(cameras, list):
        raise ValueError("Incomplete snapshot")
    if len(cameras) > 256:
        raise ValueError("Camera limit exceeded")
    seen = set()
    for camera in cameras:
        if not isinstance(camera, dict):
            raise TypeError("Invalid camera")
        cid = camera.get("camera_id", "")
        if not isinstance(cid, str) or not CAMERA_ID.fullmatch(cid) or cid in seen:
            raise ValueError("Invalid camera identity")
        seen.add(cid)
        if camera.get("frigate_name") != "aivo_" + cid:
            raise ValueError("Invalid Frigate identity")
        url = urlsplit(camera.get("rtsp_url", ""))
        if (
            url.scheme != "rtsp"
            or not url.username
            or not url.password
            or url.port != 554
        ):
            raise ValueError("Invalid stream URL")
        address = ipaddress.IPv4Address(url.hostname)
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError("Invalid camera address")
        if url.path != "/cam/realmonitor" or not re.fullmatch(
            r"channel=\d{1,3}&subtype=0", url.query
        ):
            raise ValueError("Invalid Intelbras stream")
        if not 1 <= int(url.query.split("&")[0].split("=")[1]) <= 256 or url.fragment:
            raise ValueError("Invalid Intelbras channel")
    canonical = json.dumps({"tenant_id": tenant, "cameras": cameras}, sort_keys=True)
    if payload.get("revision") != hashlib.sha256(canonical.encode()).hexdigest():
        raise ValueError("Invalid snapshot revision")
    return payload


def merge_config(original: dict, snapshot: dict, state: dict) -> tuple[dict, dict]:
    """Change only cameras owned by this provisioner, preserving local settings."""
    if state.get("tenant_id", snapshot["tenant_id"]) != snapshot["tenant_id"]:
        raise ValueError("Tenant changed; explicit reprovisioning is required")
    config = copy.deepcopy(original)
    if not isinstance(config, dict) or not isinstance(config.get("cameras", {}), dict):
        raise TypeError("Invalid local camera mapping")
    previous = set(state.get("managed_cameras", []))
    desired = {camera["frigate_name"]: camera for camera in snapshot["cameras"]}
    cameras = config.setdefault("cameras", {})
    for name in previous - desired.keys():
        cameras.pop(name, None)
    for name, camera in desired.items():
        if name in cameras and name not in previous:
            raise ValueError("Managed camera conflicts with a local camera")
        if name not in cameras:
            cameras[name] = {
                "enabled": True,
                "ffmpeg": {
                    "inputs": [{"path": camera["rtsp_url"], "roles": ["detect"]}]
                },
                "detect": {"enabled": True, "fps": 5},
                "objects": {"track": ["person"]},
            }
        else:
            inputs = cameras[name]["ffmpeg"]["inputs"]
            detection = [item for item in inputs if "detect" in item.get("roles", [])]
            if len(detection) != 1:
                raise ValueError("Managed detection input is ambiguous")
            detection[0]["path"] = camera["rtsp_url"]
            cameras[name]["enabled"] = True
    # Production profiles are authored in the SaaS and validated before deployment.
    streams = config.get("go2rtc", {}).get("streams", {})
    for name in previous - desired.keys():
        streams.pop(name, None)
    for name, camera in desired.items():
        if "analytics" not in camera:
            continue
        from retail_store import CameraProfile, profile_revision

        analytics = camera["analytics"]
        profile = CameraProfile.model_validate(analytics["settings"]).model_dump(
            mode="json"
        )
        if analytics["revision"] != profile_revision(profile):
            raise ValueError("Invalid analytics revision")
        streams = config.setdefault("go2rtc", {}).setdefault("streams", {})
        if name in streams and name not in previous:
            raise ValueError("Managed stream conflicts with local configuration")
        sub = camera["rtsp_url"].replace("subtype=0", "subtype=1")
        streams[name] = [sub]
        managed = cameras[name]
        inputs = [
            {"path": sub, "input_args": "preset-rtsp-generic", "roles": ["detect"]}
        ]
        if profile["recording"]:
            inputs.append(
                {
                    "path": camera["rtsp_url"],
                    "input_args": "preset-rtsp-generic",
                    "roles": ["record"],
                }
            )
        managed["ffmpeg"]["inputs"] = inputs
        managed["snapshots"] = {
            "enabled": True,
            "retain": {"default": profile["retention_days"]},
        }
        managed["record"] = {
            "enabled": profile["recording"],
            "continuous": {"days": profile["retention_days"]},
            "alerts": {"retain": {"days": profile["retention_days"]}},
            "detections": {"retain": {"days": profile["retention_days"]}},
        }
        local_zones = {
            k: v
            for k, v in managed.get("zones", {}).items()
            if not k.startswith("aivo_")
        }
        for zone in profile["zones"]:
            local_zones[zone["zone_id"]] = {
                "coordinates": ",".join(
                    str(v) for point in zone["points"] for v in point
                ),
                "objects": ["person"],
                "inertia": 3,
            }
        managed["zones"] = local_zones
    if "cameras" not in original and not cameras:
        config.pop("cameras")
    return config, {
        "tenant_id": snapshot["tenant_id"],
        "revision": snapshot["revision"],
        "managed_cameras": sorted(desired),
    }


def atomic_write(path: Path, content: bytes) -> None:
    """Replace a file durably, using a private temporary file in the same directory."""
    fd, temporary = tempfile.mkstemp(prefix=".aivo-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if path.exists():
                stat = path.stat()
                os.fchown(stream.fileno(), stat.st_uid, stat.st_gid)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def command(*args: str, data: bytes | None = None, timeout: int = 60) -> bytes:
    """Run an argument-only command, suppressing output that may contain secrets."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE
        if data is not None
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("Local command timed out") from None
    if process.returncode:
        raise RuntimeError("Local command failed")
    return stdout


class Provisioner:
    """Apply snapshots under a single-process lock, with restart recovery."""

    def __init__(self, config: Path, container: str, health_timeout: int = 120):
        self.config = config.resolve()
        self.container = container
        self.health_timeout = health_timeout
        self.state_path = self.config.parent / ".aivo-camera-state.json"
        self.journal_path = self.config.parent / ".aivo-camera-pending.json"
        self.backup_path = self.config.parent / ".aivo-camera-backup.yml"

    async def check_mount(self) -> None:
        """Require a directory bind mount so atomic replacement reaches Frigate."""
        if self.container.startswith("supreme-") or self.container.startswith(
            "aivo-cloud"
        ):
            raise ValueError("Not an Edge Frigate container")
        inspected = json.loads(await command("docker", "inspect", self.container))[0]
        mounts = inspected.get("Mounts", [])
        if not any(
            mount.get("Destination") == "/config"
            and mount.get("Type") == "bind"
            and Path(mount["Source"]).resolve() == self.config.parent
            for mount in mounts
        ):
            raise ValueError("Frigate must bind the configuration directory to /config")
        if any(mount.get("Destination", "").startswith("/config/") for mount in mounts):
            raise ValueError("Nested config mounts prevent atomic provisioning")
        if self.config.name not in ("config.yml", "config.yaml"):
            raise ValueError("Unsupported Frigate config filename")
        if (
            self.config.name == "config.yaml"
            and self.config.with_name("config.yml").exists()
        ):
            raise ValueError("config.yml would shadow config.yaml")
        env = inspected.get("Config", {}).get("Env", [])
        for entry in env:
            if (
                entry.startswith("CONFIG_FILE=")
                and entry.split("=", 1)[1] != "/config/" + self.config.name
            ):
                raise ValueError("Container uses a different config file")

    async def validate(self, candidate: bytes) -> None:
        """Validate against the installed Frigate version before replacing its file."""
        await command(
            "docker",
            "exec",
            "-i",
            self.container,
            "python3",
            "-c",
            "import sys; from frigate.config import FrigateConfig; FrigateConfig.parse(sys.stdin.read())",
            data=candidate,
            timeout=90,
        )

    async def restart(self) -> None:
        """Restart only the explicitly configured local Frigate container."""
        await command("docker", "restart", "--time", "30", self.container, timeout=90)

    async def healthy(self, expected: dict) -> None:
        """Verify the live camera configuration, not merely a running container."""
        deadline = asyncio.get_running_loop().time() + self.health_timeout
        probe = (
            "import json,sys,urllib.request; "
            "expected=json.load(sys.stdin); "
            "actual=json.load(urllib.request.urlopen('http://127.0.0.1:5000/api/config',timeout=4)); "
            "assert set(actual.get('cameras',{})) == set(expected.get('cameras',{})); "
            "assert all(actual['cameras'][name].get('enabled',True) == camera.get('enabled',True) "
            "for name,camera in expected.get('cameras',{}).items())"
        )
        while asyncio.get_running_loop().time() < deadline:
            try:
                await command(
                    "docker",
                    "exec",
                    "-i",
                    self.container,
                    "python3",
                    "-c",
                    probe,
                    data=json.dumps(expected).encode(),
                    timeout=10,
                )
                return
            except RuntimeError:
                await asyncio.sleep(3)
        raise RuntimeError("Frigate did not load the expected camera set")

    async def recover(self) -> None:
        """Restore the last known configuration after a failed or interrupted apply."""
        if not self.journal_path.exists():
            return
        journal = json.loads(await asyncio.to_thread(self.journal_path.read_text))
        original = journal["original"].encode()
        await asyncio.to_thread(atomic_write, self.config, original)
        await self.restart()
        await self.healthy(yaml.safe_load(original))
        await asyncio.to_thread(
            atomic_write, self.state_path, json.dumps(journal["state"]).encode()
        )
        await asyncio.to_thread(self.journal_path.unlink)
        logger.warning("Restored previous Frigate configuration")

    async def apply(self, snapshot: dict, dry_run: bool = False) -> bool:
        """Apply a full snapshot; retain the journal until health checks pass."""
        snapshot = validate_snapshot(snapshot)
        if not dry_run:
            await self.check_mount()
            await self.recover()
        elif self.journal_path.exists():
            raise RuntimeError("An interrupted apply requires recovery")
        state = (
            json.loads(await asyncio.to_thread(self.state_path.read_text))
            if self.state_path.exists()
            else {}
        )
        original = await asyncio.to_thread(self.config.read_bytes)
        old_config = yaml.safe_load(original)
        merged, new_state = merge_config(old_config, snapshot, state)
        changed = merged != old_config
        if dry_run:
            logger.info(
                "Check complete: %d cloud cameras; config change required: %s",
                len(snapshot["cameras"]),
                changed,
            )
            return changed
        if not changed:
            if state != new_state:
                await asyncio.to_thread(
                    atomic_write, self.state_path, json.dumps(new_state).encode()
                )
            return False
        candidate = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True).encode()
        await self.validate(candidate)
        if await asyncio.to_thread(self.config.read_bytes) != original:
            raise RuntimeError("Configuration was edited concurrently; retry later")
        await asyncio.to_thread(atomic_write, self.backup_path, original)
        journal = {"original": original.decode(), "state": state}
        await asyncio.to_thread(
            atomic_write, self.journal_path, json.dumps(journal).encode()
        )
        try:
            await asyncio.to_thread(atomic_write, self.config, candidate)
            await self.restart()
            await self.healthy(merged)
            await asyncio.to_thread(
                atomic_write, self.state_path, json.dumps(new_state).encode()
            )
            await asyncio.to_thread(self.journal_path.unlink)
        except Exception:
            # Recovery must also run for filesystem and subprocess failures.
            await self.recover()
            raise
        logger.info(
            "Applied camera configuration: %d managed cameras", len(snapshot["cameras"])
        )
        return True


async def fetch_snapshot(
    client: httpx.AsyncClient, base_url: str, api_key: str
) -> dict:
    """Fetch over verified HTTPS without forwarding credentials through redirects."""
    response = await client.get(
        base_url.rstrip("/") + "/api/config/cameras", headers={"x-api-key": api_key}
    )
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise ValueError("Snapshot too large")
    return validate_snapshot(response.json())


async def run(args: argparse.Namespace) -> None:
    """Poll with bounded backoff; failures never become empty camera snapshots."""
    base_url = os.environ.get("CLOUD_API_URL", "")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("CLOUD_API_URL must be an HTTPS origin")
    key_file = os.environ.get("TENANT_API_KEY_FILE")
    api_key = (
        (await asyncio.to_thread(Path(key_file).read_text)).strip()
        if key_file
        else os.environ.get("TENANT_API_KEY", "")
    )
    if not api_key:
        raise ValueError("A tenant API key is required")
    interval = int(os.environ.get("CAMERA_POLL_SECONDS", "120"))
    if interval < 10:
        raise ValueError("Polling interval must be at least 10 seconds")
    config = Path(os.environ.get("FRIGATE_CONFIG_PATH", "config/config.yml"))
    provisioner = Provisioner(config, os.environ.get("FRIGATE_CONTAINER", "frigate"))
    lock_path = config.parent / ".aivo-camera.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.check and provisioner.journal_path.exists():
            await provisioner.check_mount()
            await provisioner.recover()
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            failures = 0
            while True:
                try:
                    snapshot = await fetch_snapshot(client, base_url, api_key)
                    await provisioner.apply(snapshot, dry_run=args.check)
                    failures = 0
                except (
                    httpx.HTTPError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    yaml.YAMLError,
                ) as error:
                    # Error text and tracebacks may contain credentials or stream URLs.
                    logger.error(
                        "Camera synchronization failed (%s)", type(error).__name__
                    )
                    failures += 1
                    if args.once or args.check:
                        raise RuntimeError("Camera synchronization failed") from None
                if args.once or args.check:
                    return
                delay = min(interval * (2 ** min(failures, 4)), 1800)
                await asyncio.sleep(delay + random.uniform(0, delay * 0.1))


def main() -> None:
    """Run the Edge service or a one-shot, read-only configuration preview."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fetch and compare without writing config or restarting",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (
        httpx.HTTPError,
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        yaml.YAMLError,
    ) as error:
        logger.error("Provisioner stopped (%s)", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
