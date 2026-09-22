# Camera enrollment wizard

The authenticated Dashboard > Configurar Câmeras page supports Intelbras IP cameras and Linux webcams. Four steps cover source type, settings, review and connection status. Nothing is persisted before confirmation. Draft passwords are cleared on completion, cancellation, page changes and logout.

IP cameras retain the encrypted RTSP storage and existing polling provisioner. Webcam identities stay in traffic_webcam_sources; the additive webcam_settings table stores device path, pixel format and entrance direction. Old enrolled webcams remain available with /dev/video0 defaults. Editing preserves the camera ID and crossing history. Agent enrollment no longer overwrites a user-selected display name.

## Webcam installation

After enrollment, the dashboard offers a ZIP with a pinned Frigate image, MQTT, crossing counter, optional image relay, configuration and start/stop scripts. Requirements: Linux, Docker Engine and Compose, a supported /dev/videoN device, internet access, an available local port 5000 and the store API key. No credentials are included in the download.

Extract on the webcam computer. Run `bash iniciar.sh` for counts or `bash iniciar.sh --com-imagem` for counts and authenticated live image uploads (one image per second). The script verifies that the supplied key belongs to the configured tenant before starting capture. Run `bash parar.sh` to stop all services, including the relay. Stop first when changing from live mode to counts only. Services do not start automatically after a reboot.

The default capture is 640x480 at 30 fps, person detection at 5 fps on CPU. Choose MJPEG or YUYV according to the hardware. A left/right gate provides initial counting: the person must traverse both zones with their body visible. Reverse entrance direction in the wizard when necessary. Calibrate to the actual doorway before a customer pilot.

Webcam configuration is applied by downloading the updated kit, not by the IP polling endpoint. Stop the old stack and replace files in the SAME directory, preserving `state/` and `.secrets/`. Do not run two installations against the same device. Previously installed notebook POC containers remain separate and stopped until explicitly started.

## Status and data

A saved configuration does not mean connected video. The wizard shows recent counter heartbeats separately from recent webcam frames. The existing entrance dashboard shows real observations from test movements, explicitly labeled as webcam tests, not generated visits or customer counts. No heatmap is fabricated when no visits exist.

The production demo cleanup backs up and deletes only three identified synthetic visit IDs and their 30 heatmap points; nine observed webcam crossings are retained. The old mock sender is retired and has no production credentials.

Validation: 59 isolated regression tests, generated Compose validation, shell syntax validation and Frigate config parsing using the exact installed image. Physical webcam capture stays off during this release, per user instruction.
