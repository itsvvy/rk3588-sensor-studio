# RK3588 Sensor Studio Experiment Archive

This repository records the RK3588 unified DVS/CIS/IMU sensor launcher and
selected hardware capture sessions. It is an experiment archive, not a
portable software distribution.

## Repository scope

- `start_sensor_studio.sh`: unified launcher entry point.
- `sensor_studio/`: preview, recording, session management, IMU Zarr logging,
  CIS GStreamer pipeline, and the native DVS preview callback.
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

### `2026-11-09_14-20-38`

First retained baseline capture for this repository.

- Status: `complete`
- Synthetic: `false`
- DVS RAW: 6,204,229 events over 3.487 seconds
- CIS: 2,560x720 H.264, 25 fps, 88 recorded frames
- IMU: 996 samples for each of accel, gyro, and angle
- IMU reconnects during session: 2
- Recorded output size: approximately 50 MB

See [the session notes](2026-11-09_14-20-38/NOTES.md), `manifest.json`, and
`logs/session.log` for the complete recorded metadata.

The DVS count in that historical `manifest.json` includes callback statistics
from before the RAW file was opened. The count reported by
`metavision_file_info` for `events.raw` is authoritative for the data file.
The launcher now resets DVS counters when recording starts so future manifests
use the recording-session boundary.

## Versioning

`main` contains the currently accepted runnable baseline for this RK3588
module. Experimental changes should use independent `experiment/...` branches
and are merged only after the relevant hardware capture succeeds. Existing
pushed history and branches are not force-rewritten or deleted.

Large capture files are committed directly while they remain below GitHub's
per-file limit. Future sessions may use Git LFS or GitHub Releases if the
archive grows beyond practical Git repository limits.
