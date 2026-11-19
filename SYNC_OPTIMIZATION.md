# Synchronized Recording Optimization

Status: experimental. Do not merge into `main` without another hardware-review
decision.

## Implemented

- Added a software preroll before the declared common recording start.
- Added `sync/window.json` with per-stream observed ranges, the planned start,
  the stop request, and the common effective window.
- Raised the session manifest schema to version 2.
- Moved CIS host timestamps to the camera source pad, before JPEG decoding and
  queue scheduling.
- Moved IMU array writes off the serial thread through a bounded queue and a
  dedicated writer thread.
- Replaced the Zarr Python write path with a lightweight append-only Zarr v3
  writer using uncompressed little-endian chunks. This avoids the current
  Zarr local-store event-loop deadlock while retaining standard Zarr metadata.
- Requested all source stops concurrently, while retaining the stop marker as
  the common end boundary.
- Added DVS start/end anchors and per-stream start/end host timestamps.
- Allowed a source that is actively recovering to finish the preroll instead of
  failing immediately.
- Static IMU values may remain zero; packet reception, not value change,
  determines readiness.

## Validation

CPU tests:

```text
12 tests, 11 passed, 1 skipped
```

The skipped native DVS test requires the native extension, which was not built
in this checkout.

Synthetic headless recording:

- Manifest status: `complete`
- Common window: valid
- Common duration for a 2-second nominal run: approximately 1.49 seconds after
  the 0.5-second preroll
- CIS: 51 recorded frames
- IMU: 388 samples per sensor in the final run

IMU-only real hardware recording, 6 seconds:

- 1,206 samples per sensor
- No serial reconnects
- P99 timestamp gap: 7.44 ms accel, 7.92 ms gyro, 8.56 ms angle
- No gaps above 20 ms or 50 ms

This removes the periodic 50-100 ms pauses observed with the previous
synchronous Zarr write path.

Full three-source hardware runs:

- DVS RAW and CIS video were successfully recorded.
- The CH340/IMU path intermittently disconnected when DVS and CIS started.
- `sync/window.json` correctly marked the common window invalid when IMU did
  not cover the common start.
- A complete three-source hardware synchronization run is still required after
  USB topology or IMU-link stability is addressed.

## Known Limits

- This is software-coordinated synchronization, not a common hardware trigger.
- CIS frame timing is still limited by UVC/USB transport and camera exposure.
- IMU timestamps remain host reception times; enabling and validating JY931
  `0x50` time packets is a separate experiment.
- `sync/window.json` identifies the effective overlap but does not rewrite the
  raw DVS, CIS, or IMU files.
