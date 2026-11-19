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
13 tests, 12 passed, 1 skipped
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

CIS-only real hardware recording, 8 seconds:

- 201 timestamp rows in `frames.csv`
- 201 frames decoded from the MKV
- A repeated-test frame-count mismatch was fixed by recording `frames.csv`
  rows from the actual CIS recording branch while using the source-pad timing
  map for each PTS
- A later 6-second boundary check produced 150 CSV rows and 150 MKV frames,
  with identical PTS sets and no one-sided frames

DVS-only real hardware recording, 10 seconds:

- 5,843,543 CD events
- RAW duration 10.000 seconds
- No DVS source error

IMU-only real hardware recording, 6 seconds:

- 1,206 samples per sensor
- No serial reconnects
- P99 timestamp gap: 7.44 ms accel, 7.92 ms gyro, 8.56 ms angle
- No gaps above 20 ms or 50 ms

This removes the periodic 50-100 ms pauses observed with the previous
synchronous Zarr write path.

Two additional IMU-only runs, 10 seconds each:

- 2,009 samples per sensor
- Zero reconnects
- P99 time gap: 7.4-8.6 ms
- No gaps above 20 ms

Full three-source hardware runs, five attempts:

- DVS RAW and CIS video were successfully recorded.
- No attempt formed a valid three-stream common window.
- Depending on the attempt, IMU produced 0-51 samples before disconnect, or was
  not ready within the 8-second startup limit.
- Kernel logs show the Cypress EVKCX3 control device re-enumerating in DFU mode
  while the CH340 is simultaneously disconnected and reattached.
- DVS-only, CIS-only, and IMU-only runs were stable, so the failure is
  correlated with the combined USB hub/power topology rather than any single
  sensor driver in isolation.
- `sync/window.json` correctly marked the common window invalid when IMU did
  not cover the common start.
- A complete three-source hardware synchronization run is still required after
  USB topology or IMU-link stability is addressed.

Native DVS backend check:

- The repository checkout did not contain the OpenEB tree, so initial tests
  automatically used the Python DVS fallback.
- Reusing the existing compiled native extension with identical
  `dvs_fastpath.cpp` allowed a three-source recording to start and produce a
  non-empty intersection.
- IMU still disconnected after about two seconds. The result covered only
  1.33 seconds of the requested 10-second session.
- The synchronization validity check was tightened to reject windows that do
  not cover the stop request within a 0.5-second tolerance.

USB-bandwidth check:

- DVS RAW was approximately 5.1 MB/s (about 41 Mbps).
- CIS H.264 output was approximately 5.4 Mbps; even using the camera's 25 fps
  MJPEG descriptor limit of 147 Mbps, total device traffic remains well below
  the 480 Mbps USB2 link.
- Kernel failures are disconnect/protocol errors (`-32`, `-71`) during DVS
  control-device re-enumeration and CH340 removal, not bandwidth exhaustion.
- The hub reports itself as self-powered. Its downstream device declarations
  total approximately 1.2 A, so external supply quality and the shared hub
  topology remain the primary hardware suspects.

## Replacement Dock Validation

A second VIA Labs USB-C dock was tested on 2026-11-19. The dock exposes both
USB2 and USB3 hubs, but the DVS, camera, and CH340 remain nested under the
original TUSB8041 USB2 branch. The DVS still does not negotiate USB3.

Three consecutive 10-second three-source runs:

- Run 1 disconnected the entire dock after about 0.2 seconds.
- DVS failed with `LIBUSB_ERROR_NO_DEVICE`.
- CIS failed with a V4L2 `No such device` polling error.
- Runs 2 and 3 could not open the camera or DVS within the startup timeout.
- No synchronization window was produced.

Isolated and combined tests with the replacement dock:

- CIS only, 5 seconds: passed, 125 recorded frames.
- DVS only, 5 seconds: passed, 3,195,499 events.
- DVS plus CIS, 5 seconds: the whole USB dock disconnected immediately.
- The replacement dock's USB3 network adapter was disconnected at the same
  time, showing that the failure affects the dock/upstream connection rather
  than only the sensor sub-hub.

The best traffic estimate remains below 200 Mbps on a nominal 480 Mbps USB2
link. Kernel behavior is consistent with an upstream power drop, cable/contact
problem, or dock protection reset. The replacement dock did not improve the
combined hardware path and was less stable than the previous dock.

### Replacement Dock Retest

After the USB dock/cabling was replaced again and the system re-enumerated,
three consecutive 10-second three-source runs completed successfully with the
native DVS callback:

| Run | Common window | DVS events | CIS frames | IMU samples/type | IMU reconnects |
|---|---:|---:|---:|---:|---:|
| 1 | 9.508 s | 5,544,698 | 253 | 2,020-2,021 | 0 |
| 2 | 9.484 s | 5,442,567 | 253 | 2,019 | 0 |
| 3 | 9.478 s | 5,842,356 | 252 | 2,024 | 0 |

All three windows were marked `valid`, all source errors were null, and no
kernel USB disconnect occurred during the recordings. IMU P99 gaps were
7.5-8.6 ms with no gaps above 20 ms. CIS CSV rows and decoded MKV frame counts
matched in every run.

Inside the common window, CIS maintained 238 frames over approximately
9.48-9.51 seconds. P99 frame spacing was about 40.06 ms and maximum spacing was
at most 40.07 ms. The approximately 0.5 second CIS gap observed at pipeline
startup remained in the preroll and was excluded from the common window.

The active device path is still USB2:

```text
VIA USB2 hub -> TUSB8041 USB2 hub -> DVS/camera/CH340
```

DVS did not negotiate USB3, but the replacement upstream dock/cabling made the
combined recording repeatably stable. This further supports USB power,
contact, or upstream-hub behavior rather than sensor bandwidth as the earlier
root cause.

Remaining observation: run 1 reported 304 more DVS events in the manifest than
were decoded from RAW, approximately 0.005%. Runs 2 and 3 matched exactly. RAW
verification itself succeeded in all three runs.

## Stable GUI Iteration

Later GUI tests reproduced complete USB-path disconnects in both v1 and v2
while the full-size 2560x720 CIS preview was active. Headless recordings
remained stable. This showed that the failure was not caused by the v2
synchronization algorithm alone.

Two stability changes were made in v2:

- The CIS Python timestamp callback was moved out of the V4L2 source thread.
  It now runs at the post-decode tee sink, before the recording branch, so the
  PTS-to-host map remains available without blocking camera reads.
- The full-size CIS preview is disabled by default. `--cis-preview` opts back
  into it for workflows that accept the additional USB stability risk.

Final validation with the default v2 launcher:

- GUI stayed online for 27 seconds and all devices remained present afterward.
- A 15-second hardware recording was `complete` and `valid`.
- Common window: 14.549 seconds, exactly equal to the expected window.
- DVS: 13,611,588 events, RAW duration 15.056 seconds.
- CIS: 379 CSV rows and 379 decoded MKV frames.
- IMU: 3,026-3,027 samples per type, zero reconnects.
- IMU P99 spacing: 7.5-8.6 ms, no gaps above 20 ms.

The synchronization design therefore remains unchanged: preroll, common host
timestamps, per-stream coverage, and `sync/window.json` are retained.

## Known Limits

- This is software-coordinated synchronization, not a common hardware trigger.
- CIS frame timing is still limited by UVC/USB transport and camera exposure.
- IMU timestamps remain host reception times; enabling and validating JY931
  `0x50` time packets is a separate experiment.
- `sync/window.json` identifies the effective overlap but does not rewrite the
  raw DVS, CIS, or IMU files.
