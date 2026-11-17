# Session 2026-11-17_15-22-09

Replacement baseline capture for the RK3588 unified DVS/CIS/IMU launcher.

## Configuration

- DVS event-rate limit: disabled
- DVS backend: native
- CIS: 2560x720 JPEG at 25 fps, H.264 MKV output
- IMU serial: 115200 baud
- Synchronization: software-coordinated start/stop with host timestamps

## Result

The session manifest reports `status: complete`, no errors, and no IMU serial
reconnects.

- Capture session: 2026-11-17 15:22:09 to 15:22:19 HKT
- DVS RAW: 5,746,637 events over 8.520 seconds
- CIS recorded frames: 214
- IMU samples: 2,125 per sensor
- Total archive size: approximately 54 MB

## Timing coverage

- DVS and CIS contain approximately 8.5 seconds of common capture.
- IMU samples span approximately 10.54 seconds because sources are stopped
  sequentially and CIS finalization completes before IMU is stopped.
- The session uses software-coordinated start/stop and host monotonic
  timestamps. It does not use a common hardware trigger or shared hardware
  clock.

## Files

- `manifest.json`: session configuration, stream counts, and final status.
- `dvs/events.raw`: complete GenX320 EVT21 RAW stream.
- `cis/stereo.mkv`: complete 2560x720 H.264 stereo video.
- `cis/frames.csv`: camera frame PTS and host timestamps.
- `imu/imu.zarr`: IMU XYZ arrays and host monotonic timestamps.
- `sync/clock_anchors.csv`: DVS and IMU clock anchors.
- `logs/session.log`: lifecycle and shutdown log.
