# Sensor Studio v2

Preview and recording for DVS, stereo CIS, and JY931 IMU.

Two windows are shown:

- `DVS - Sensor Studio`: DVS preview and the shared keyboard controller.
- GStreamer preview: the complete 2560x720 stereo CIS image.

The IMU has no viewer; it is recorded in the background.

## Start

```bash
cd /home/astri
./start_sensor_studio_v2.sh
```

The v2 launcher is independent from `start_sensor_studio.sh`. It activates the
same `myenv` and OpenEB installation, then runs the `sensor_studio_v2` package.
Existing v1 scripts and the `sensor_studio` package are not changed.

Current hardware defaults are aligned with the connected camera module:

- JY931 baud rate: `115200`
- DVS ERC: disabled by default, matching the original viewer
- DVS preview: 320x320 native pixels, 25 fps, 10 ms accumulation,
  Metavision Dark palette
- CIS capture: `2560x720 @ 25 fps`
- CIS decode/encode: `mppjpegdec` and `mpph264enc`

The DVS uses the stable low-level OpenEB HAL stream path. By default, a compiled
C++ callback renders 25 fps preview frames without creating a Python object for
each event batch, while its complete RAW stream is recorded. If the shared USB2 hub
briefly re-enumerates the CH341 serial adapter, the IMU source follows the
stable `/dev/serial/by-id` link and reconnects automatically.

Controls:

- Focus the DVS window.
- `SPACE`: start the single recording session for DVS, CIS, and IMU.
- `Q` or `Esc`: stop all streams, finalize files, and close both windows.
- Closing the DVS window follows the same safe shutdown path.

The default output directory is `/media/astri/SD-FAT32`. If it is not writable,
the application falls back to `/home/astri`. Recording is disabled unless DVS,
CIS, IMU, and the selected output directory are all ready.

Recording uses a software-coordinated preroll. All sources begin recording
first, then the declared common start is placed 0.5 seconds later. This gives
every stream an overlap before the common start and avoids losing frames or
events when source startup takes different amounts of time. The preroll is
configurable with `--sync-preroll-seconds`.

## Synthetic check

No sensor hardware is required:

```bash
./start_sensor_studio_v2.sh --synthetic --output-root /tmp
```

For an automated file-format smoke test:

```bash
./start_sensor_studio_v2.sh \
  --synthetic --headless --output-root /tmp \
  --name smoke --auto-record-seconds 5
```

Synthetic DVS files are not Prophesee RAW files. They are timestamped event
records for pipeline testing and the session manifest is marked
`"synthetic": true`.

## Session layout

```text
<output-root>/<session-id>/
├── manifest.json
├── dvs/events.raw
├── cis/stereo.mkv
├── cis/frames.csv
├── imu/imu.zarr
├── sync/clock_anchors.csv
├── sync/window.json
└── logs/session.log
```

`cis/frames.csv` contains frame PTS and host monotonic/Unix timestamps. The host
timestamp is taken at the camera source pad before JPEG decode, color
conversion, and queue scheduling.
`sync/clock_anchors.csv` contains DVS/IMU device-to-host clock anchors.
`imu/imu.zarr` stores raw int16 XYZ arrays and matching int64 monotonic
nanosecond timestamps. Its append-only chunks are written by a dedicated
thread so serial reception does not wait for storage I/O. The arrays use the
Zarr v3 format with uncompressed little-endian chunks, which avoids the Zarr
Python event-loop dependency on the recording path.

`sync/window.json` and `manifest.json` report the per-stream observed ranges,
the planned common start, the stop request, and the common effective window.
If one source did not cover the common start, `valid` is false and the source
is listed in `uncovered_streams`. Raw files are retained unchanged; consumers
should use this window when exact three-stream overlap is required.

With a static camera module, IMU values may remain zero or nearly constant.
That is not data loss: readiness and overlap are determined from received
packets and their timestamps, not from changes in the accelerometer or angle
values.

## Useful options

```text
--output-root PATH
--name NAME
--synthetic
--dvs-serial SERIAL
--dvs-event-rate EVENTS_PER_SECOND (default: 0, matching the original viewer)
--dvs-backend auto|native|python (default: auto)
--cis-device PATH
--cis-fps FPS (default: 25)
--imu-device PATH
--imu-baud BAUD (default: 115200)
--sync-preroll-seconds SECONDS (default: 0.5)
```

The current SD card must be writable before hardware recording. The application
does not repair, remount, or format storage automatically.
