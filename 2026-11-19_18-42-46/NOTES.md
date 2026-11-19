# Session 2026-11-19_18-42-46

First retained v2 synchronized-recording validation capture.

## Configuration

- Launcher: `./start_sensor_studio_v2.sh --cis-preview`
- Output root: `/home/astri`, on the system disk
- DVS backend: native C++
- DVS event-rate limit: disabled
- CIS: 2560x720 JPEG at 25 fps, H.264 MKV output
- IMU: JY931 over CH341 serial at 115200 baud
- Synchronization: software-coordinated 0.5-second preroll

## Result

- Session status: `complete`
- Synchronization window: `valid: true`
- Source errors: 0
- IMU reconnects: 0
- DVS RAW: 3,214,456 CD events over 5.132713 seconds
- CIS: 129 recorded frames; 129 timestamp rows and 129 decoded MKV frames
- IMU: 1,035 accel, 1,034 gyro, and 1,035 angle samples
- IMU writer: at most one queued batch, seven written batches

The declared common window is 4.633479 seconds. Its expected duration is
4.634785 seconds, a difference of approximately 1.3 ms. The 0.5-second
preroll contains data from all three streams but is intentionally excluded
from the common window.

## Synchronization quality

- IMU P99 timestamp spacing: 9.78-10.01 ms per channel
- IMU maximum timestamp spacing: 11.58-13.24 ms
- IMU gaps above 20 ms: 0
- CIS P50/P99/max frame spacing: 40.002/40.068/40.083 ms
- DVS manifest event count matches `metavision_file_info`

This remains software synchronization rather than a shared hardware trigger.
The DVS event timestamps are device timestamps mapped to host monotonic time
through `sync/clock_anchors.csv`. CIS and IMU timestamps are host-side
timestamps. Consumers requiring an exact three-stream intersection should use
`sync/window.json`.

## Comparison with 2026-11-17_15-22-09

- The older session did not expose a dedicated common-window file.
- The older session's IMU P99 spacing was approximately 51-52 ms and included
  gaps above 100 ms; this capture has no gap above 20 ms.
- The older session continued IMU capture approximately two seconds after the
  DVS/CIS effective end; this capture stops all sources close to the common
  stop marker.
