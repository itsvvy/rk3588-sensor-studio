# Sensor Studio v3

Independent dual-DVS unified preview and recording application for the RK3588
camera module.

## Hardware

- DVS master: `2629600159`
- DVS slave: `2629600155`
- CIS: stereo UVC stream, `2560x720 @ 25 fps`
- IMU: JY931 over CH341 serial, `115200` baud

The two DVS devices are configured and started as one hardware-synchronized
pair. CIS and IMU are joined through the software-coordinated session window.

## Start

```bash
cd /home/astri
./start_sensor_studio_v3.sh
```

The UI shows the DVS slave on the left and master on the right. Pressing SPACE
starts one unified recording session. Q or Escape stops all sources, writes the
session metadata, and exits.

## Preview-only validation

This validates DVS hardware synchronization and activity from all sources
without starting a recording session:

```bash
./start_sensor_studio_v3.sh \
  --headless \
  --preview-only-seconds 5 \
  --preview-report /tmp/sensor_studio_v3_preview.json
```

No RAW, MKV, or Zarr recording is created. The report checks:

- Both DVS roles were configured as slave/master.
- The slave was started before the master.
- Both DVS streams produced positive device timestamps and events.
- The latest DVS device timestamps agree within the configured tolerance.
- CIS produced decoded frames.
- IMU produced accel, gyro, and angle packets.

## Unified recording

```bash
./start_sensor_studio_v3.sh
```

Or a short headless acceptance run:

```bash
./start_sensor_studio_v3.sh \
  --headless \
  --auto-record-seconds 10 \
  --name dual-dvs-check
```

Output:

```text
<session>/
├── dvs/
│   ├── slave.raw
│   └── master.raw
├── cis/
│   ├── stereo.mkv
│   └── frames.csv
├── imu/imu.zarr
├── sync/
│   ├── clock_anchors.csv
│   └── window.json
├── logs/session.log
└── manifest.json
```

The manifest schema is version 3. `sync/window.json` reports `dvs_slave`,
`dvs_master`, `cis`, and `imu`.

## Synchronization semantics

- DVS-to-DVS: OpenEB hardware Master/Slave synchronization.
- DVS/CIS/IMU: software-coordinated preroll and a common host-time window.
- A recording is valid only when all four leaf streams cover the common window.

## Useful options

```text
--dvs-master-serial SERIAL
--dvs-slave-serial SERIAL
--dvs-sync-delay-seconds SECONDS
--dvs-event-rate EVENTS_PER_SECOND
--dvs-backend auto|native|python
--cis-device PATH
--cis-fps FPS
--imu-device PATH
--imu-baud BAUD
--sync-preroll-seconds SECONDS
--startup-timeout-seconds SECONDS
--preview-only-seconds SECONDS
--preview-report PATH
--headless
--auto-record-seconds SECONDS
```
