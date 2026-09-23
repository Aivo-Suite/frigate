# Aivo Retail Analytics agent instructions

These instructions apply to `retail_analytics/`. Keep the repository root
`AGENTS.md` for the upstream Frigate project; this directory is a standalone
multi-tenant SaaS and Edge integration. User instructions take precedence.

## Architecture and ownership

- `cloud_api.py` is the FastAPI ingestion and configuration API.
- `dashboard.py` is the tenant-authenticated Streamlit dashboard.
- `camera_store.py` encrypts Intelbras RTSP credentials; `camera_dashboard.py`
  implements the enrollment wizard.
- `edge_provisioner.py` polls camera configuration and updates only cameras it
  owns in the local Frigate configuration, with validation and rollback.
- `crossing_counter.py` and `edge_counter.py` deliver observed directional
  crossings with stable event IDs and retry-safe persistence.
- `traffic_store.py` owns tenant-scoped crossings and reports. A tracking event
  ending is not evidence that a person exited. Net flow is not occupancy.
- `browser_component/` and `browser_dashboard.py` provide webcam capture without
  a client installation. `browser_worker.py` and `browser_vision.py` process the
  frames in a separate resource-limited container on the VPS.
- The browser path uses authenticated WSS over existing HTTPS, independently of
  Frigate Edge. Keep `BROWSER_CAMERA.md`, `WEBCAM_WIZARD.md` and
  `CAMERA_PROVISIONING.md` consistent with implementation changes.

## Production boundaries

The production VPS is `frigate.agenticx.ia.br`. All Aivo cloud maintenance belongs
in `/root/frigate-saas/retail_analytics/`.

- Relevant containers: `aivo-cloud-api`, `aivo-cloud-dashboard`,
  `aivo-browser-worker`, and `aivo-saas-db`.
- Never modify, stop, delete, recreate, or restart any `supreme-*` container.
  The sole allowed proxy operation is the graceful nginx reload described below.
- Never run broad `docker compose down`, Docker prune, or volume deletion on
  this shared host. Do not recreate the database for application updates.
- Host ports 80 and 443 belong to `supreme-nginx-1`. Aivo services must not
  publish host ports. They use the existing Docker networks and reverse proxy.
- If routing changes are necessary, edit only
  `/opt/supreme/infra/nginx/conf.d/z_frigate.conf`. Preserve other Aivo routes,
  including the browser WebSocket route `/api/browser/ws`.
- Validate with `docker exec supreme-nginx-1 nginx -t`, then reload with
  `docker exec supreme-nginx-1 nginx -s reload`. Never restart nginx.
- Keep application and image backups before publication. Build and verify new
  images first. Use healthy preview API/dashboard containers for routing while
  updating their main counterparts with targeted `up -d --no-deps` commands.
  Check health and public endpoints before switching back and stopping previews.
- A browser worker restart interrupts active browser capture. Inspect its usage
  before updating it and make that interruption clear when relevant.
- Database migrations must be additive, repeatable and tenant-safe. Do not
  restore a database as a shortcut for rolling back application code.

## Secrets and tenant isolation

- Never commit or print API keys, RTSP passwords, camera encryption keys,
  production `.env` files, database dumps, snapshots or customer images.
- Compose requires `AIVO_DATABASE_URL` and `AIVO_POSTGRES_PASSWORD`. Use
  `.env.cloud.example` as a template and set the existing deployment values in
  an ignored `.env`. Do not rotate an existing PostgreSQL password just by
  changing its container environment; configure database credentials explicitly.
- Keep the same `.secrets/camera.key` for API and dashboard. Do not regenerate it
  while encrypted camera rows exist. Treat database/key backups as sensitive.
- Scope every read, write, grant and image by the authenticated tenant. Never
  trust a tenant or camera identity supplied by browser JavaScript.
- Browser capture uses short-lived, single-use capabilities, not tenant API
  keys. Do not put tokens in URLs or relax Origin checks.
- Do not weaken webcam permission, preview-only or explicit start/stop behavior.
  Opening a page must never automatically activate a camera or send images.
- Full preview frames stay ephemeral and bounded. The user has authorized saving
  cropped person thumbnails in tenant-scoped `traffic_visitors` records. Do not
  add biometric matching or identify returning people from these photos.
- `visitor_store.py` owns visitor records, separate from login accounts. A visitor
  ID represents a continuous track, not a verified unique identity. CRM sync is
  deferred. Never expose photos publicly or include them in Git/test fixtures.
- No audio or full video recording is enabled in the browser pilot.

## Local hardware and data

- Do not start a physical webcam, Frigate capture or live relay as a side effect
  of tests, a build or a deployment. Respect an instruction to leave capture
  stopped until the user explicitly asks to activate it.
- Notebook webcam resources live under `poc_webcam/`. `.env`, `.secrets`, state,
  local databases and generated media stay ignored.
- The browser pilot supports one simultaneous camera, at most five processed
  frames per second, with a one-CPU/768-MiB worker limit. Preserve these limits
  unless capacity has been measured and the change is intentional.
- Do not invent traffic data or repopulate production with fixtures.
  `mock_traffic.py` is retired. Observed webcam test movements are real
  observations and must not be silently deleted as synthetic data.
- Before requested data cleanup, identify exact records, back them up and scope
  deletion to the relevant tenant. Never broadly delete `test_*` identifiers.

## Validation and publishing changes

Use temporary SQLite databases and synthetic/public fixtures in isolated test
containers. Never run test suites against the production database or a physical
camera. Current standalone tests are:

```sh
PYTHONPATH=retail_analytics:frigate/test python -m unittest \
  test_camera_provisioning test_directional_counting test_live_preview \
  test_camera_wizard test_browser_camera test_visitors test_intelbras -v
```

Use compatible dependencies from the cloud image plus `httpx`, `PyYAML` and
`aiomqtt`, or a disposable environment. Do not modify the host's global Python
packages to resolve test dependency conflicts. Validate changed Compose files
with placeholder environment values, JavaScript with `node --check`, and changed
Python modules with Ruff. Check downloadable kits inside the actual built cloud
image so missing packaged files cannot be hidden by a test source mount.

Download model weights with `python download_browser_model.py`. Verify the pinned
SHA-256; keep weights out of Git and retain `browser_models/YOLOX-LICENSE`. A real
model test may use public reference images in isolation, never camera frames
without authorization.

The standalone SaaS OpenAPI schema is served by `cloud_api.py`; upstream
Frigate's generated API schema is unrelated unless its own API changes.

Before a requested commit/push, inspect the staged diff for secrets and generated
files, check the remote branch, and push without force. Report what was tested,
what was deployed, and whether physical camera validation remains pending.

## Intelbras customer path

See `INTELBRAS.md` and `edge_kit/README.md`. `intelbras_edge.py` is the production
connector; do not enable the legacy facial-matching analytics daemon.
`retail_store.py` owns calibrated observations and dwell metrics;
`camera_media.py` owns short-lived media capabilities. Keep the API single-worker
until live relay state is distributed. Webcam stays under development tools.
