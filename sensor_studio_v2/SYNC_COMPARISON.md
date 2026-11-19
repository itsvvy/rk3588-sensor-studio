# Sensor Studio v1/v2 Synchronization Comparison

This document compares the original `sensor_studio` implementation with the
experimental `sensor_studio_v2` implementation using two real hardware
captures:

- v1 baseline: `2026-11-17_15-22-09`
- v2 example: `2026-11-19_18-42-46`

The captures have different durations, so counts alone are not used as a
performance comparison. The relevant metrics are common overlap, timestamp
spacing, stream tail differences, and whether the session explicitly reports
a valid synchronization window.

## Synchronization mechanism

### v1

1. IMU, DVS, and CIS start recording sequentially.
2. A host monotonic timestamp is written after all three recorders start.
3. DVS device time is mapped to host time with periodic clock anchors.
4. CIS stores PTS and a host timestamp observed after pipeline decoding.
5. IMU timestamps are host times taken when serial packets are parsed.
6. IMU Zarr writes happen synchronously in the serial-processing path.
7. Stop order is DVS, then CIS, then IMU. CIS can spend about two seconds
   finalizing while IMU continues recording.
8. The manifest has session start/end times but no common three-stream window.

The effective overlap must therefore be inferred from each stream's first and
last timestamps.

### v2

1. IMU, DVS, and CIS start recording sequentially with a 0.5-second preroll.
2. The declared common start is placed after all recorders are active.
3. DVS device time is mapped to host time with start, periodic, and stop
   anchors.
4. CIS records frame rows from the actual video recording branch. The PTS is
   mapped to host time through the post-decode timestamp branch, which remains
   separate from the V4L2 source thread.
5. IMU timestamps remain per-packet host receive times, but Zarr writes move
   to a dedicated writer thread with approximately one-second batching.
6. The stop marker is recorded before all sources are stopped concurrently.
7. A common window and per-stream coverage are written to
   `sync/window.json` and `manifest.json`.
8. Windows that do not cover the stop point are explicitly marked invalid.

Both versions remain software-coordinated. Neither uses a shared hardware
trigger.

## Measured comparison

| Metric | v1 `2026-11-17_15-22-09` | v2 `2026-11-19_18-42-46` |
|---|---:|---:|
| Session status | `complete` | `complete` |
| Explicit common-window status | Not available | `valid: true` |
| Common overlap | Approximately 8.504 s | 4.633479 s |
| Expected common duration | Not recorded | 4.634785 s |
| Common-duration error | Not available | Approximately 1.3 ms |
| Common overlap / session interval | Approximately 80.4% | Approximately 99.97% |
| DVS events | 5,746,637 | 3,214,456 |
| DVS RAW duration | 8.520 s | 5.133 s |
| CIS recorded frames | 214 | 129 |
| CIS timestamp rows / decoded frames | Matched | 129 / 129 |
| CIS P99 frame spacing | 40.059 ms | 40.068 ms |
| CIS maximum frame spacing | 40.105 ms | 40.083 ms |
| IMU samples per channel | 2,125 | 1,034-1,035 |
| IMU P99 spacing, accel | 51.13 ms | 9.78 ms |
| IMU P99 spacing, gyro | 52.51 ms | 9.99 ms |
| IMU P99 spacing, angle | 51.09 ms | 10.01 ms |
| IMU maximum spacing | 100.81-105.46 ms | 11.58-13.24 ms |
| IMU gaps above 20 ms | 34-35 per channel | 0 |
| DVS/CIS end versus IMU end | IMU continues about 2.018 s longer | DVS/CIS/IMU tails differ by about 8 ms or less |

The v1 common-overlap ratio uses the inferred three-stream intersection
relative to the manifest session interval. The v2 ratio uses the declared
common window relative to its expected duration.

## Interpretation

### What improved

- v2 exposes an explicit synchronization result instead of requiring consumers
  to infer overlap.
- The 0.5-second preroll removes startup-order loss from the common window.
- IMU timestamp spacing improved from tens of milliseconds and occasional
  hundred-millisecond gaps to roughly 10 ms P99 with no gap above 20 ms.
- CIS frame cadence remains effectively unchanged at 25 fps.
- Concurrent stopping removes roughly two seconds of IMU postroll.
- Invalid or incomplete three-stream coverage is reported rather than treated
  as a valid synchronized recording.

### What did not materially improve

- DVS-to-host mapping is still based on periodic host anchors. A five-second
  v2 capture is too short to make a reliable DVS clock-drift comparison.
- CIS host timing still depends on the GStreamer pipeline and host scheduling.
  Its PTS sequence remains the more stable frame clock.
- v2 remains software synchronized and cannot provide the accuracy of a
  common hardware trigger.

## Practical use

For exact three-stream analysis, use the interval in:

```text
sync/window.json
```

Check these fields before processing:

```json
{
  "valid": true,
  "uncovered_streams": []
}
```

The raw files remain complete, including the preroll. Consumers that require
only the common interval should trim all streams to
`common_start_monotonic_ns` through `common_end_monotonic_ns`.
