# Session 2026-11-09_14-20-38

First retained baseline after enabling the native C++ preview callback for the
GenX320 DVS.

## Configuration

- DVS event-rate limit: disabled
- DVS backend: native
- CIS: 2560x720 JPEG at 25 fps, H.264 MKV output
- IMU serial: 115200 baud

## Result

The session manifest reports `status: complete` and an empty `errors` list.

- Capture interval: 2026-11-09 14:20:39 to 14:20:44 HKT
- DVS RAW: 6,204,229 events over 3.487 seconds
- CIS recorded frames: 88
- IMU samples: 996 per sensor
- IMU reconnects: 2
- Total archive size: approximately 50 MB

## Caveats

- This is a short functional capture, not a long-duration stability test.
- The IMU serial reconnected twice during the session.
- DVS USB link continuity and `TimeHigh discrepancy` behavior should continue
  to be evaluated over longer captures.
- The historical `manifest.json` DVS callback count includes events counted
  before the RAW writer opened. For this archive, use the count reported by
  `metavision_file_info` as the authoritative count of `dvs/events.raw`.
- The launcher has been updated to reset the DVS session counters at recording
  start, so future manifests do not retain the pre-recording count.
- `dvs/events.raw.tmp_index` is retained because it is part of the OpenEB RAW
  output generated for this session.

## Files

- `manifest.json`: session configuration, stream counts, and final status.
- `dvs/events.raw`: complete GenX320 EVT21 RAW stream.
- `cis/stereo.mkv`: complete 2560x720 H.264 stereo video.
- `cis/frames.csv`: camera frame PTS and host timestamps.
- `imu/imu.zarr`: IMU XYZ arrays and host monotonic timestamps.
- `sync/clock_anchors.csv`: DVS and IMU clock anchors.
- `logs/session.log`: lifecycle and shutdown log.
