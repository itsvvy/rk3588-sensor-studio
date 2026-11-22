from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from sensor_studio_v3.controller import RecordingController
from sensor_studio_v3.recording import SessionRecorder
from sensor_studio_v3.sources import (
    DualDvsSource,
    SyntheticDualDvsSource,
)


class FakeDvsChannel:
    def __init__(self, role: str, serial: str, timestamp_us: int) -> None:
        self.role = role
        self.serial = serial
        self.ready = True
        self.error = None
        self.stream_name = f"dvs_{role}"
        self.sync_mode = role
        self.timestamp_us = timestamp_us

    def preview_sync_stats(self):
        return {
            "role": self.role,
            "serial": self.serial,
            "sync_mode": self.sync_mode,
            "ready": True,
            "events": 100,
            "last_device_t_us": self.timestamp_us,
            "first_positive_device_t_us": self.timestamp_us,
            "first_host_monotonic_ns": 1,
            "last_host_monotonic_ns": 2,
        }

    def stats(self):
        stats = self.preview_sync_stats()
        stats.update(
            {
                "first_host_monotonic_ns": 1_000,
                "last_host_monotonic_ns": 2_000,
                "width": 320,
                "height": 320,
            }
        )
        return stats


class DualDvsV3Tests(unittest.TestCase):
    def test_session_uses_two_dvs_files_and_schema_v3(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = SessionRecorder.begin(
                Path(tmp), "dual", False, {"test": True}
            )
            assert session.paths is not None
            self.assertEqual(session.paths.dvs_slave.name, "slave.raw")
            self.assertEqual(session.paths.dvs_master.name, "master.raw")
            session.finish("complete")
            manifest = json.loads(session.paths.manifest.read_text())
            self.assertEqual(manifest["schema_version"], 3)
            self.assertIn("dvs_slave", manifest["files"])
            self.assertIn("dvs_master", manifest["files"])

    def test_sync_window_expands_dual_dvs_substreams(self):
        source = DualDvsSource("2629600159", "2629600155")
        source.slave = FakeDvsChannel("slave", "2629600155", 5_000_000)
        source.master = FakeDvsChannel("master", "2629600159", 5_000_010)
        source.ready = True
        controller = RecordingController(
            sources=[],
            output_root=Path("/tmp"),
            name=None,
            synthetic=False,
            config={},
        )
        controller.recording_start_monotonic_ns = 1_000
        sync = controller._build_sync_info(
            source_stats={
                "dvs": source.stats(),
                "cis": {
                    "first_host_monotonic_ns": 1_000,
                    "last_host_monotonic_ns": 2_000,
                },
                "imu": {
                    "first_host_monotonic_ns": 1_000,
                    "last_host_monotonic_ns": 2_000,
                },
            },
            stop_monotonic_ns=2_000,
            stop_unix_ns=2_000,
        )
        self.assertTrue(sync["dvs_hardware_sync"])
        self.assertEqual(sync["mode"], "mixed_hardware_software")
        self.assertEqual(
            set(sync["stream_ranges"]),
            {"dvs_slave", "dvs_master", "cis", "imu"},
        )
        self.assertEqual(
            source.preview_sync_stats()["timestamp_delta_us"],
            10,
        )

    def test_synthetic_pair_previews_without_recording(self):
        source = SyntheticDualDvsSource()
        source.open()
        try:
            time.sleep(0.15)
            sync = source.preview_sync_stats()
            self.assertTrue(sync["valid"])
            slave_frame, master_frame = source.latest_frames()
            self.assertEqual(slave_frame.shape, (320, 320, 3))
            self.assertEqual(master_frame.shape, (320, 320, 3))
        finally:
            source.close()

    def test_synthetic_pair_records_two_raw_streams(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = SessionRecorder.begin(
                Path(tmp), "paired", True, {"test": True}
            )
            source = SyntheticDualDvsSource()
            source.open()
            try:
                time.sleep(0.08)
                source.start_recording(session)
                time.sleep(0.12)
                source.stop_recording()
                session.finish("complete", {"dvs": source.stats()})
                assert session.paths is not None
                self.assertTrue(session.paths.dvs_slave.is_file())
                self.assertTrue(session.paths.dvs_master.is_file())
                anchors = session.paths.clock_anchors.read_text()
                self.assertIn("dvs_slave", anchors)
                self.assertIn("dvs_master", anchors)
            finally:
                source.close()

