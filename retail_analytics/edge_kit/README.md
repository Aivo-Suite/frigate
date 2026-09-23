# Aivo Intelbras Edge

This kit is for the store technician, not the daily dashboard user. Use a Linux
mini-PC with Docker Compose, Python 3 with venv support, systemd, reliable storage,
and outbound HTTPS access to the Aivo VPS. Camera hardware compatibility and
CPU/GPU capacity must be validated at the store. The CPU detector is a bootstrap
configuration, not a capacity guarantee. This kit pins Frigate 0.18.0.

1. Extract into `/opt/aivo-edge` on the store computer.
2. Run `sudo bash install.sh` there. Enter this store's API key privately.
3. In Aivo, register the camera's local IP, user, password and channel. Verify
   main stream (`subtype=0`) and substream (`subtype=1`) are enabled. Use H.264
   for browser-compatible historical MP4 playback.
4. Open Cameras in Aivo and check the actual image and online state.
5. Draw two distinct, nonoverlapping door regions (outside and inside), then
   draw store sectors if visible. Compare actual crossings with a manual count.
6. Adjust recording retention to available disk space. Default is one day;
   increasing it increases local storage usage. No microphone is enabled.

The connector and provisioner make outbound connections. Do not forward camera,
MQTT, Frigate or go2rtc ports on the store router. The unauthenticated Frigate
port is bound only to the host loopback; internal MQTT is not published. Keep
the kit on a dedicated Edge Docker network. Do not deploy it on the shared SaaS
VPS. The provisioner has Docker control on this host only; its container checks
refuse cloud and supreme containers.

The provisioner manages camera inputs, recording, snapshots, `aivo_*` zones and
the corresponding go2rtc streams. Configuration is validated against installed
Frigate before atomic replacement and restart. An error restores the previous
configuration. Applying camera/zone edits briefly interrupts Edge detection.

Visitor photos and unsent observations are held in the Edge SQLite queue until
acknowledged. Keep state/ and .secrets/ private and persistent. Cloud outages may
prevent live view and clip retrieval but do not erase queued data. Back up the
state directory before replacing the Edge. Do not change tenants on a used Edge;
provision a new state directory for a different store. Camera deletion should be
preceded by queue drain if pending records must be delivered.

Live video is on demand, muted, at 640-pixel width and 10 fps, up to five minutes.
It is transcoded on the Edge and relayed to the authorized browser without public
camera access. Historical clips are requested on demand, at most 30 seconds and
20 MB, and cached privately in the cloud for five minutes. They may be absent
if recording was disabled, retention expired, or the Edge is offline.

Production counting uses Frigate continuous tracks. Disappearance is not an exit.
A new track does not prove a new individual. Zone dwell is estimated only across
consecutive observations within 75 seconds; gaps are excluded. The legacy
analytics_daemon.py facial matching pipeline is not used by this kit.
