from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Sequence

from .controller import RecordingController, StudioState
from .recording import output_root_is_writable
from .sources import (
    CisSource,
    DualDvsSource,
    ImuSource,
    SyntheticDualDvsSource,
    prewarm_recording_backends,
)


log = logging.getLogger(__name__)


DEFAULT_CIS_DEVICE = (
    "/dev/v4l/by-id/usb-USB_Camera_USB_Camera_01.00.00-video-index0"
)
DEFAULT_IMU_DEVICE = (
    "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
)
DEFAULT_OUTPUT_ROOT = "/home/astri"
DEFAULT_FALLBACK_OUTPUT_ROOT = "/home/astri"
DEFAULT_DVS_MASTER_SERIAL = "2629600159"
DEFAULT_DVS_SLAVE_SERIAL = "2629600155"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unified DVS/CIS/IMU preview and recording application"
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help=(
            "Recording root. When omitted, use the writable SD card, "
            f"otherwise fall back to {DEFAULT_FALLBACK_OUTPUT_ROOT}"
        ),
    )
    parser.add_argument("--name", default=None)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument(
        "--dvs-master-serial",
        default=DEFAULT_DVS_MASTER_SERIAL,
        help="Serial number of the DVS hardware-sync master",
    )
    parser.add_argument(
        "--dvs-slave-serial",
        default=DEFAULT_DVS_SLAVE_SERIAL,
        help="Serial number of the DVS hardware-sync slave",
    )
    parser.add_argument(
        "--dvs-event-rate",
        type=int,
        default=0,
        help="DVS hardware event-rate limit; 0 disables ERC",
    )
    parser.add_argument(
        "--dvs-backend",
        choices=("auto", "native", "python"),
        default="auto",
        help=(
            "DVS preview callback implementation; native keeps event processing "
            "in C++, python keeps the compatibility fallback"
        ),
    )
    parser.add_argument(
        "--dvs-sync-delay-seconds",
        type=float,
        default=1.5,
        help="Delay between starting the DVS slave and opening the master",
    )
    parser.add_argument("--cis-device", default=DEFAULT_CIS_DEVICE)
    parser.add_argument("--cis-fps", type=int, default=25)
    parser.add_argument("--imu-device", default=DEFAULT_IMU_DEVICE)
    parser.add_argument("--imu-baud", type=int, default=115_200)
    parser.add_argument(
        "--sync-preroll-seconds",
        type=float,
        default=0.5,
        help=(
            "Keep an overlap of this length before the declared common "
            "recording start, allowing all streams to be trimmed to one window"
        ),
    )
    parser.add_argument(
        "--cis-preview",
        action="store_true",
        help="Enable the full-size CIS preview; recording is stable without it",
    )
    parser.add_argument(
        "--startup-settle-seconds",
        type=float,
        default=0.5,
        help=(
            "Wait after process/backend initialization before opening USB "
            "devices; stabilizes the shared USB2 dock path"
        ),
    )
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--startup-timeout-seconds",
        type=float,
        default=15.0,
        help="Maximum time to wait for all sources to become ready",
    )
    parser.add_argument(
        "--preview-only-seconds",
        type=float,
        default=0.0,
        help="Preview all sources for this duration without starting recording",
    )
    parser.add_argument(
        "--preview-report",
        type=Path,
        default=None,
        help="Optional JSON path for the preview and DVS synchronization report",
    )
    parser.add_argument(
        "--auto-record-seconds",
        type=float,
        default=0.0,
        help=argparse.SUPPRESS,
    )
    return parser


def _wait_until_ready(
    controller: RecordingController,
    timeout: float = 15.0,
    *,
    require_storage: bool = True,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        controller.tick()
        if controller.all_sources_ready and (
            not require_storage or controller.storage_ready
        ):
            return True
        time.sleep(0.05)
    return False


def _preview_report(
    controller: RecordingController,
    *,
    elapsed_seconds: float,
) -> dict:
    stats = controller.source_stats()
    dvs_stats = stats.get("dvs", {})
    cis_stats = stats.get("cis", {})
    imu_stats = stats.get("imu", {})
    dvs_sync = dvs_stats.get("preview_sync", {})
    imu_counts = {
        str(name): int(value)
        for name, value in (imu_stats.get("received_counts") or {}).items()
    }
    cis_frames = int(
        cis_stats.get("preview_frames")
        or cis_stats.get("recorded_frames")
        or 0
    )
    all_imu_active = bool(imu_counts) and all(
        count > 0 for count in imu_counts.values()
    )
    valid = bool(
        controller.all_sources_ready
        and dvs_sync.get("valid")
        and cis_frames > 0
        and all_imu_active
        and controller.session is None
    )
    return {
        "schema_version": 1,
        "mode": "preview_only",
        "elapsed_seconds": elapsed_seconds,
        "valid": valid,
        "recording_started": controller.session is not None,
        "all_sources_ready": controller.all_sources_ready,
        "dvs_sync": dvs_sync,
        "activity": {
            "dvs_events": int(dvs_stats.get("events") or 0),
            "cis_frames": cis_frames,
            "imu_received_counts": imu_counts,
        },
        "sources": stats,
    }


def _choose_output_root(requested: str | None) -> Path:
    if requested:
        return Path(requested).expanduser()
    for candidate in (DEFAULT_OUTPUT_ROOT, DEFAULT_FALLBACK_OUTPUT_ROOT):
        path = Path(candidate)
        if output_root_is_writable(path)[0]:
            return path
    return Path(DEFAULT_OUTPUT_ROOT)


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    output_root = _choose_output_root(args.output_root)
    log.info("Recording output root: %s", output_root)
    prewarm_started = time.monotonic()
    prewarm_recording_backends()
    log.info(
        "Recording backends prewarmed in %.1f ms",
        (time.monotonic() - prewarm_started) * 1000,
    )
    if args.startup_settle_seconds > 0:
        log.info(
            "Waiting %.1f s for USB devices to settle",
            args.startup_settle_seconds,
        )
        time.sleep(args.startup_settle_seconds)
    if args.synthetic:
        sources = [
            SyntheticDualDvsSource(),
            CisSource(
                "",
                synthetic=True,
                fps=args.cis_fps,
                show_preview=not args.headless and args.cis_preview,
            ),
            ImuSource("", args.imu_baud, synthetic=True),
        ]
    else:
        sources = [
            DualDvsSource(
                args.dvs_master_serial,
                args.dvs_slave_serial,
                args.dvs_event_rate,
                backend=args.dvs_backend,
                sync_delay_seconds=args.dvs_sync_delay_seconds,
            ),
            CisSource(
                args.cis_device,
                fps=args.cis_fps,
                show_preview=not args.headless and args.cis_preview,
            ),
            ImuSource(args.imu_device, args.imu_baud),
        ]

    open_order = sorted(
        sources,
        key=lambda source: {"cis": 0, "dvs": 1, "imu": 2}[source.name],
    )
    for source in open_order:
        source.open()

    config = {
        "backend": "synthetic" if args.synthetic else "hardware",
        "dvs_master_serial": args.dvs_master_serial,
        "dvs_slave_serial": args.dvs_slave_serial,
        "dvs_sync_delay_seconds": args.dvs_sync_delay_seconds,
        "dvs_event_rate": args.dvs_event_rate,
        "dvs_backend": args.dvs_backend,
        "cis_device": args.cis_device,
        "cis_mode": f"2560x720 JPEG @ {args.cis_fps}fps -> H.264 MKV",
        "imu_device": args.imu_device,
        "imu_baud": args.imu_baud,
        "sync_preroll_seconds": args.sync_preroll_seconds,
        "cis_preview": args.cis_preview,
        "startup_settle_seconds": args.startup_settle_seconds,
    }
    controller = RecordingController(
        sources=sources,
        output_root=output_root,
        name=args.name,
        synthetic=args.synthetic,
        config=config,
        preroll_seconds=args.sync_preroll_seconds,
    )
    controller.mark_started()

    if args.headless:
        try:
            if args.preview_only_seconds > 0:
                if not _wait_until_ready(
                    controller,
                    timeout=args.startup_timeout_seconds,
                    require_storage=False,
                ):
                    report = _preview_report(controller, elapsed_seconds=0.0)
                    report["valid"] = False
                    report["error"] = "传感器在超时前未全部就绪"
                    if args.preview_report is not None:
                        args.preview_report.write_text(
                            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    print(json.dumps(report, ensure_ascii=False))
                    return 2
                preview_start = time.monotonic()
                while (
                    time.monotonic() - preview_start
                    < args.preview_only_seconds
                ):
                    controller.tick()
                    time.sleep(0.05)
                elapsed = time.monotonic() - preview_start
                report = _preview_report(
                    controller,
                    elapsed_seconds=elapsed,
                )
                if args.preview_report is not None:
                    args.preview_report.write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                print(json.dumps(report, ensure_ascii=False))
                return 0 if report["valid"] else 6
            if not _wait_until_ready(
                controller,
                timeout=args.startup_timeout_seconds,
            ):
                controller.message = "传感器或存储在超时前未就绪"
                log.error(controller.message)
                return 2
            if not controller.start_recording():
                log.error(controller.message)
                return 3
            deadline = time.monotonic() + max(0.1, args.auto_record_seconds)
            while (
                time.monotonic() < deadline
                and controller.state == StudioState.RECORDING
            ):
                controller.tick()
                time.sleep(0.05)
        finally:
            controller.request_exit()
        return 0 if controller.state == StudioState.EXIT else 4

    from .ui import StudioWindow

    StudioWindow(controller, sources).run()
    return 0 if controller.state in {StudioState.EXIT, StudioState.FAULT} else 5


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

