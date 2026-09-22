# Camera provisioning

## Cloud

The authenticated Dashboard offers **Configurar Câmeras**, including creation,
editing, disabling and deletion. Blank passwords on edit retain the existing
password. A unique (tenant, IPv4 address, channel) constraint prevents duplicates.

`GET /api/config/cameras` authenticates with the existing `x-api-key` header and
returns a complete snapshot, including an explicit empty list when appropriate:

```json
{
  "schema_version": 1,
  "tenant_id": "tenant-id",
  "revision": "sha256-of-canonical-tenant-and-cameras",
  "cameras": [
    {
      "camera_id": "32-character-uuid-hex",
      "name": "Entrance",
      "frigate_name": "aivo_<camera_id>",
      "rtsp_url": "rtsp://encoded-user:encoded-password@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0"
    }
  ]
}
```

The revision is a content checksum, not a signature. HTTPS and the tenant key
provide transport protection and authentication. Responses disable caching.
The API intentionally returns stream credentials only to that tenant's Edge.
The Dashboard never displays saved passwords or RTSP URLs.

URLs are encrypted with Fernet using `.secrets/camera.key`, mounted as a Docker
secret. Both cloud services must use the same key. Keep a protected backup of
this key together with database backups. Do not regenerate it while encrypted
camera rows exist. Never commit keys, dumps, local config or production `.env`
files. `.dockerignore` excludes them from build contexts.

`python migrate_cameras.py` creates only the new camera table and tenant index.
It is repeatable and compatible with the previous application. API startup also
ensures this table exists. No existing analytics data is rewritten.

## Edge installation (operator, once per store)

Use a host with a running Frigate container. The container must bind the entire
configuration directory to `/config`, rather than a single YAML file, so atomic
file replacement is visible inside the container. Use the same config filename
that Frigate loads. The provisioner verifies these conditions before applying.

1. Install `edge_provisioner.py` in `/opt/aivo/retail_analytics/`.
2. Create a virtual environment at `/opt/aivo/venv` and install
   `requirements.edge.txt` into it.
3. Copy `edge.env.example` to `/etc/aivo/edge.env`, adjusting the config path and
   exact Frigate container name. Keep this file and the tenant API key file mode
   `0600`. Store the tenant key at `/etc/aivo/tenant_api_key` without quotes.
4. Install `aivo-camera-provisioner.service` in `/etc/systemd/system/`.
5. Load the environment and run `edge_provisioner.py --check` before activation.
   This checks cloud access and computes the proposed change without rewriting
   YAML, validating against Frigate, or restarting containers. It creates only
   a private process lock file beside the config.
6. Start the service with `systemctl enable --now aivo-camera-provisioner` after
   `systemctl daemon-reload`. Inspect `journalctl -u aivo-camera-provisioner`.

The default interval is 120 seconds plus up to 10% jitter. Errors use exponential
backoff capped at 30 minutes. `--once` performs a single actual synchronization.
The service needs permission to write the config directory and access Docker.
The example systemd unit runs as root; Docker access already grants host control.

The initial empty cloud snapshot preserves all pre-existing local cameras. New
cloud cameras use stable names `aivo_<UUID>`, detect persons at 5 fps, and inherit
other global Frigate settings. Camera IP connectivity and available compute
capacity remain requirements of the store's network and Edge hardware.
`analytics_daemon.py` already accepts camera names dynamically from MQTT events.
It still requires its own MQTT and cloud environment configuration; provisioning
does not install or start that analytics service or configure broker credentials.

The provisioner owns only camera names recorded in `.aivo-camera-state.json`.
It rejects collisions and tenant changes. Existing local cameras, MQTT, detectors,
zones and other unrelated settings remain semantically unchanged. Existing
managed cameras retain local zones and settings when their RTSP URL changes.
Removing or disabling a managed camera removes its Frigate stanza; re-enabling
creates a new default stanza. Back up custom per-camera settings when needed.
PyYAML rewriting does not preserve YAML comments or original formatting.

Before applying, the provisioner validates with the installed container's
`FrigateConfig.parse`, writes a private backup and durable recovery journal,
atomically replaces the file, and restarts only the named Frigate container.
Health checks verify the active camera set through the local Frigate API.
They do not verify that every camera produces frames. A failed application
restores the old config and restarts Frigate again. An interrupted apply is
recovered on the next service start, even if the cloud is unavailable.
If recovery fails, its journal stays in place for another attempt.
Do not manually edit the YAML during application. A restart briefly pauses
local video processing; cloud services are unaffected.

## Deployment and rollback on this VPS

The cloud Compose attaches only Aivo API and Dashboard to the existing
`supreme_backend` network. It does not change legacy containers. API and
Dashboard no longer publish host ports. The database remains on its existing
network and volume. `db-seed` requires the explicit `maintenance` profile.

Build the two application images first. For continuous routing during updates,
start the preview Compose with project name `aivo-camera-preview`, check API,
Dashboard and tenant synchronization, and switch only `z_frigate.conf` to the
preview names. Test Nginx configuration before a graceful reload. Update the
regular Aivo containers with `up -d --no-deps cloud-api cloud-dashboard`, verify
health, and switch the proxy back. Streamlit sessions can reconnect during this
process. Never restart Nginx, run a broad Compose down, or recreate the database.

The initial rollout backup lives on the VPS under
`retail_analytics/backups/cameras-20260922/`. It contains a database dump, old
source/Compose, image identifiers and the prior domain proxy configuration.
For application rollback, route to healthy preview containers first, restore
old source and recorded images, and validate before switching the proxy again.
The additive camera table may remain. A database restore is unnecessary for an
application rollback and would overwrite new analytics data.

## Verification

From the repository root, with compatible dependencies installed:

```sh
PYTHONPATH=retail_analytics python -m unittest frigate.test.test_camera_provisioning -v
```

Tests use isolated SQLite databases, temporary files and mocked Frigate restarts.
They cover tenant authorization, encryption, URL encoding, metadata-only lists,
password retention, duplicate cameras, empty snapshots, UI creation, unchanged
polling, update/removal, invalid responses, mount validation and crash recovery.
The cloud OpenAPI schema is available at `/openapi.json`; the main Frigate API
schema is unrelated to this standalone SaaS API.

## Cloud environment configuration

Before running either cloud Compose file, copy `.env.cloud.example` to `.env`
and replace the placeholders with the deployment's database credentials. For an
existing deployment, preserve the current database password and connection URL.
Both `AIVO_POSTGRES_PASSWORD` and `AIVO_DATABASE_URL` are required. URL-encode
reserved characters in the URL password. Keep `.env` outside Git. Changing the
PostgreSQL container environment does not change an existing database password.
