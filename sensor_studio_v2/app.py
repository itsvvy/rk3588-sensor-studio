from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Sequence

from .controller import RecordingController, StudioState
from .recording import output_root_is_writable
from .sources import (
    CisSource,
    DvsSource,
    ImuSource,
    SyntheticDvsSource,
    prewarm_recording_backends,
)


log = logging.getLogger(__name__)


DEFAULT_CIS_DEVICE = (
    "/dev/v4l/by-id/usb-USB_Camera_USB_Camera_01.00.00-video-index0"
)
DEFAULT_IMU_DEVICE = (
    "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
)
DEFAULT_OUTPUT_ROOT = "/media/astri/SD-FAT32"
DEFAULT_FALLBACK_OUTPUT_ROOT = "/home/astri"


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
    parser.add_argument("--dvs-serial", default="")
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
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--auto-record-seconds",
        type=float,
        default=0.0,
        help=argparse.SUPPRESS,
    )
    return parser


def _wait_until_ready(controller: RecordingController, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        controller.tick()
        if controller.all_sources_ready and controller.storage_ready:
            return True
        time.sleep(0.05)
    return False


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
    if args.synthetic:
        sources = [
            SyntheticDvsSource(),
            CisSource(
                "",
                synthetic=True,
                fps=args.cis_fps,
                show_preview=not args.headless,
            ),
            ImuSource("", args.imu_baud, synthetic=True),
        ]
    else:
        sources = [
            DvsSource(
                args.dvs_serial,
                args.dvs_event_rate,
                backend=args.dvs_backend,
            ),
            CisSource(
                args.cis_device,
                fps=args.cis_fps,
                show_preview=not args.headless,
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
        "dvs_serial": args.dvs_serial,
        "dvs_event_rate": args.dvs_event_rate,
        "dvs_backend": args.dvs_backend,
        "cis_device": args.cis_device,
        "cis_mode": f"2560x720 JPEG @ {args.cis_fps}fps -> H.264 MKV",
        "imu_device": args.imu_device,
        "imu_baud": args.imu_baud,
        "sync_preroll_seconds": args.sync_preroll_seconds,
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
            if not _wait_until_ready(controller):
                controller.message = "传感器或存储在8秒内未就绪"
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

