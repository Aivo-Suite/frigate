# Intelbras retail integration

The customer uses the cloud dashboard. A Linux computer on the store LAN runs
Frigate 0.18.0 and the downloadable Edge kit. The VPS cannot reach a private camera
IP without this connector (or a separately designed private network). No router
port forwarding is required. The notebook webcam remains a development tool.

## Setup

1. Add the Intelbras in **Configurar Câmeras** using its LAN IP, username,
   password and channel. Credentials are encrypted in the tenant database.
2. In **Câmeras da loja**, download the Edge kit. A technician extracts it into
   `/opt/aivo-edge` on the store Linux host and runs `sudo bash install.sh`.
   Docker Compose, Python venv support and systemd must already be available.
   Enter the store API key locally when prompted; the archive contains no secrets.
3. Confirm the real preview and camera FPS. Use H.264 main and substreams on the
   Intelbras. Main stream is recorded; substream feeds person detection and live
   video. CPU detection is a bootstrap setting: measure real load and latency
   before promising accuracy or adding cameras; supported acceleration can be
   configured locally without changing the tenant-managed camera names.
4. In **Zonas e gravação**, draw non-overlapping outside and inside polygons on
   opposite sides of the entrance, then optional store sectors. The bottom-center
   of the person box determines membership. Click Apply in the drawing before
   saving the area. Record retention defaults to one day, configurable to seven.
5. Walk in and out, including turnarounds, groups and occlusion. Compare observed
   crossings with manual counts; test Edge restart and a network interruption.
   Camera field of view limits behavior and heatmap coverage.

## Data and interface

- Stable crossing IDs and a persistent Edge outbox prevent retries duplicating
  counts. Ending a track never counts as an exit. Net crossings are not occupancy.
- Cropped person snapshots are stored on tenant-scoped visitor records after
  observations arrive. Track IDs do not identify a returning customer; biometric
  matching and CRM synchronization are not part of this implementation.
- Observations carry calibration revisions. Dwell includes consecutive intervals
  up to 75 seconds under the same calibration; longer gaps are excluded. The
  heatmap weights observed positions by this elapsed time, not inferred intent.
- Dwell alerts are generated on ingestion, even with the dashboard closed, and
  shown in **Comportamento**. No external notifications are sent.
- **Histórico** requests up to 30 seconds from the Edge recording; cloud clips
  are limited to 20 MB, expire after five minutes and are purged within another
  minute. Original video remains on the Edge under its retention policy.
- Live video is explicit, silent, limited to 640-pixel width and 10 FPS with a
  five-minute single-use capability. One media job per camera is allowed.
  Closing or hiding the player stops transmission. The API runs one Uvicorn
  process because active relay queues are process-local; multiple workers need
  a dedicated distributed relay design.

## Deployment and operation

Only Aivo application containers are updated. Tables are additive and migrations
are idempotent. Preserve the camera encryption key and existing database. Never
change Supreme services. The only proxy changes belong in `z_frigate.conf`, with
configuration validation and graceful reload. Preserve `/api/browser/ws`.
New WebSocket routes `/api/media/view` and `/api/edge/media-publish` target the
cloud API with Upgrade headers and 330-second timeouts. Clip uploads under
`/api/edge/media-jobs/` need a 21 MB proxy body limit.

The Edge provisioner runs on the host and can restart only the named Frigate
container. The connector container has no Docker socket. Config changes are
atomic and roll back when restart fails. Restarts briefly interrupt store video.
Pending telemetry survives internet outages in `state/`; monitor disk space and
back up state before replacing an Edge. Failed snapshot retrieval rotates in the
queue instead of blocking later visitors. Requests whose footage has expired
remain pending for diagnosis; their photos cannot be reconstructed.

## Validation

Run the existing standalone tests plus `test_intelbras`. Validate generated YAML
against the pinned official Frigate image, build the downloaded kit, and exercise
migrations with disposable PostgreSQL. Tests must never activate a physical
camera or write generated fixtures into production. Shop acceptance remains a
separate required step with the actual Intelbras and store network.
