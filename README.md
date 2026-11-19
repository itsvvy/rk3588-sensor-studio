# RK3588 Sensor Studio Experiment Archive

This repository records the RK3588 unified DVS/CIS/IMU sensor launcher and
selected hardware capture sessions. It is an experiment archive, not a
portable software distribution.

## Repository scope

- `start_sensor_studio.sh`: original v1 launcher entry point.
- `sensor_studio/`: original v1 application, preserved from the accepted
  baseline.
- `start_sensor_studio_v2.sh`: independent synchronized-recording launcher.
- `sensor_studio_v2/`: optimized v2 application with preroll, common-window
  validation, source-level CIS timing, and asynchronous IMU storage.
- `tests/`: CPU-only unit tests for controller behavior and the native frame
  builder.
- `environment/requirements.txt`: Python environment snapshot from the RK3588.
- `YYYY-MM-DD_HH-MM-SS/`: selected real capture sessions.

The original standalone scripts and the OpenEB source tree are not included.
The recorded environment expects the existing `/home/astri/myenv` and
`/home/astri/openeb` installations on the RK3588 host.

## Recorded hardware

- Host: Orange Pi 5B / RK3588S, `astri-desktop`, aarch64
- DVS: Prophesee GenX320, EVT21, 320x320
- CIS: UVC stereo camera, 2560x720 JPEG at 25 fps
- IMU: JY931 over CH341 serial, 115200 baud
- DVS event-rate control: disabled
- DVS preview backend: native C++ callback

## Capture sessions

### `2026-11-17_15-22-09`

Current retained baseline capture for this repository.

- Status: `complete`
- Synthetic: `false`
- DVS RAW: 5,746,637 events over 8.520 seconds
- CIS: 2,560x720 H.264, 25 fps, 214 recorded frames
- IMU: 2,125 samples for each of accel, gyro, and angle
- IMU reconnects during session: 0
- Recorded output size: approximately 54 MB

See [the session notes](2026-11-17_15-22-09/NOTES.md), `manifest.json`, and
`logs/session.log` for the complete recorded metadata.

The DVS manifest count matches the event count reported by
`metavision_file_info` for the recorded RAW stream. The launcher resets DVS
counters when recording starts so each manifest describes the recording
session.

### `2026-11-19_18-42-46`

First retained synchronized-recording validation capture from v2.

- Status: `complete`
- Synchronization window: `valid: true`
- DVS RAW: 3,214,456 events over 5.132713 seconds
- CIS: 2,560x720 H.264, 25 fps, 129 recorded and decoded frames
- IMU: 1,035 accel, 1,034 gyro, and 1,035 angle samples
- IMU reconnects: 0
- IMU timestamp gaps above 20 ms: 0
- Common window: approximately 4.633 seconds
- Output size: approximately 22 MB

See [the session notes](2026-11-19_18-42-46/NOTES.md),
`sync/window.json`, and `manifest.json` for synchronization and stream
details. This session remains experimental and does not change the accepted
`main` baseline.

### OpenEB event-tool compatibility analysis

[`openeb_events_analysis_2026-11-17_15-22-09/`](openeb_events_analysis_2026-11-17_15-22-09/)
validates the DVS output from the current capture with OpenEB 5.2.0
(`6b132be062c18828466846de91f8de95bbf73284`).

- The complete RAW file is recognized as EVT21, GenX320MP, 320x320.
- OpenEB decoded all 5,746,637 CD events over 8.520 seconds.
- RAW cutting, CSV/HDF5/DAT conversion, event video generation, and
  Diff/Histo event-frame generation all completed successfully.
- The derived 0-1 second sample contains 163,055 CD events and is used to
  demonstrate every supported output path.

Commands, logs, checksums, representative images, and converted outputs are
documented in the
[analysis README](openeb_events_analysis_2026-11-17_15-22-09/README.md).

## Versioning

`main` contains the currently accepted runnable baseline for this RK3588
module. Experimental changes should use independent `experiment/...` branches
and are merged only after the relevant hardware capture succeeds. Existing
pushed history and branches are not force-rewritten or deleted.

Large capture files are committed directly while they remain below GitHub's
per-file limit. Future sessions may use Git LFS or GitHub Releases if the
archive grows beyond practical Git repository limits.
