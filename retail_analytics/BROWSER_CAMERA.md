# Browser webcam pilot

The dashboard offers Webcam no navegador and a browser option in Configurar Câmeras. Clients need HTTPS and a supported browser, not an installed agent. Camera access and preview require an explicit user gesture. Sending begins only after Iniciar monitoramento. No microphone is requested. Stopping, hiding or leaving the capture page releases local tracks; disconnections stop cloud processing. Users must keep the capture page active.

## Architecture

A same-origin Streamlit component captures bounded JPEG frames, sends at most one unacknowledged frame over WSS and displays server-confirmed counts. The isolated aivo-browser-worker performs person detection using the pinned official Apache-2.0 YOLOX-Nano model, then conservative IoU tracking with a dead band and repeated observations before recording a crossing. This browser path is independent of the Frigate Edge runtime and shares the tenant-scoped crossing and live-preview storage.

This release uses authenticated WebSockets over existing HTTPS instead of WebRTC. It caps processing at five frames per second for a single simultaneous camera across this pilot deployment. Docker limits the worker to one CPU and 768 MiB with no published host ports. Short detection gaps, occlusions, crowded scenes and very fast crossings can cause undercounting; calibrate with the actual doorway and compare with manual counts before commercial use.

## Authentication and lifecycle

Dashboard sessions issue random capabilities bound to a tenant and camera. Only SHA-256 token digests are persisted. Tokens must be claimed within ten minutes, can only be claimed once, and active sessions expire after eight hours. The worker requires the exact HTTPS Origin. Tokens travel in the first encrypted WebSocket message, never URLs; tenant API keys are not sent to browser JavaScript. Logout or navigation revokes the current page capability. Background processing completes before cancellation cleanup releases the pilot slot.

No video recording is enabled. The latest preview uses the existing memory volume and expires after ten seconds; orderly disconnect also removes it. Entry and exit events remain stored for reports. Frames are size/dimension bounded, audio is not captured, and the component contains no third-party scripts.

## Build and operations

Run `python download_browser_model.py` before building Dockerfile.browser. The official model URL and SHA-256 are pinned in the script and verified again in the image. The license is stored in browser_models/YOLOX-LICENSE. Downloaded weights are ignored by Git.

Build with `docker compose -f docker-compose.cloud.yml build cloud-api cloud-dashboard browser-worker`. Production rollout must preserve the database and legacy containers. Only `/opt/supreme/infra/nginx/conf.d/z_frigate.conf` changes: `/api/browser/ws` proxies Upgrade requests to `aivo-browser-worker:8001`. Validate and gracefully reload nginx. The root switch_proxy.py has been updated to preserve additional routes when toggling main/preview upstreams. This release also retains backups/browser-20260922/switch_browser_proxy.py.

The additive browser_camera_sources table stores line calibration. browser_camera_grants stores ephemeral authorization metadata. Linux/Frigate webcam sources and their histories are retained. Registering a browser source creates no traffic events.

## Verification

70 isolated unit/integration regressions pass, covering grants, tenant boundaries, expiration, revocation, crossing direction, jitter, disappearance and cleanup on disconnect. A public OpenCV reference image produced two person detections at a median of 29.7 ms per frame across 20 inferences with the worker resource limits. Production WSS upgrade and rejection of an invalid capability were checked without transmitting images. No physical notebook webcam was activated during deployment.

## Visitor photos

Starting browser monitoring now saves a cropped person photo and an anonymous
visitor record after a track is confirmed. Review them in Visitantes after
stopping capture. See VISITORS.md for storage and identity limitations.
