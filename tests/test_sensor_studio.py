from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from sensor_studio.controller import RecordingController, StudioState
from sensor_studio.recording import SessionRecorder
from sensor_studio.sources import ImuSource, _ImuZarrWriter, _ZarrV3ArrayWriter


class FakeSource:
    def __init__(self, name: str, ready: bool = True) -> None:
        self.name = name
        self.ready = ready
        self.error = None if ready else "missing"
        self.recovering = False
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.session = None
        self.first_host_monotonic_ns = 0
        self.last_host_monotonic_ns = 0

    def start_recording(self, session):
        self.started += 1
        self.session = session
        self.first_host_monotonic_ns = time.monotonic_ns()
        self.last_host_monotonic_ns = self.first_host_monotonic_ns

    def stop_recording(self):
        self.stopped += 1
        self.last_host_monotonic_ns = time.monotonic_ns()

    def close(self):
        self.closed += 1
        self.ready = False

    def stats(self):
        return {
            "ready": self.ready,
            "started": self.started,
            "stopped": self.stopped,
            "first_host_monotonic_ns": self.first_host_monotonic_ns,
            "last_host_monotonic_ns": self.last_host_monotonic_ns,
        }


class ControllerTests(unittest.TestCase):
    class RecoveringSource(FakeSource):
        def start_recording(self, session):
            super().start_recording(session)
            self.ready = False
            self.recovering = True
            self.error = "reconnecting"

    def make_controller(
        self,
        root: Path,
        sources,
        storage_check=None,
        preroll_seconds=0.0,
    ):
        kwargs = {}
        if storage_check is not None:
            kwargs["storage_check"] = storage_check
        controller = RecordingController(
            sources=sources,
            output_root=root,
            name="unit",
            synthetic=True,
            config={"test": True},
            preroll_seconds=preroll_seconds,
            **kwargs,
        )
        controller.mark_started()
        return controller

    def test_missing_source_blocks_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self.make_controller(
                root,
                [FakeSource("dvs"), FakeSource("cis", ready=False), FakeSource("imu")],
            )
            self.assertFalse(controller.start_recording())
            self.assertEqual(controller.state, StudioState.PREVIEW)
            self.assertIsNone(controller.session)

    def test_space_starts_once_and_q_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [FakeSource("dvs"), FakeSource("cis"), FakeSource("imu")]
            controller = self.make_controller(root, sources)
            self.assertTrue(controller.start_recording())
            session_dir = controller.session.session_dir
            self.assertFalse(controller.start_recording())
            self.assertEqual([source.started for source in sources], [1, 1, 1])
            controller.request_exit()
            self.assertEqual(controller.state, StudioState.EXIT)
            self.assertTrue((session_dir / "manifest.json").is_file())
            manifest = json.loads((session_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertTrue(manifest["synthetic"])
            self.assertEqual(
                manifest["synchronization"]["mode"],
                "software_coordinated_preroll",
            )
            self.assertTrue(manifest["synchronization"]["valid"])
            self.assertTrue((session_dir / "sync" / "window.json").is_file())
            self.assertEqual([source.closed for source in sources], [1, 1, 1])

    def test_preroll_sets_common_start_in_the_future(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [FakeSource("dvs"), FakeSource("cis"), FakeSource("imu")]
            controller = self.make_controller(
                root,
                sources,
                preroll_seconds=0.2,
            )
            before = time.monotonic_ns()
            self.assertTrue(controller.start_recording())
            self.assertGreaterEqual(
                controller.recording_start_monotonic_ns,
                before + 200_000_000,
            )
            controller.request_exit()

    def test_start_allows_source_that_is_recovering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [
                FakeSource("dvs"),
                FakeSource("cis"),
                self.RecoveringSource("imu"),
            ]
            controller = self.make_controller(root, sources)
            self.assertTrue(controller.start_recording())
            self.assertEqual(controller.state, StudioState.RECORDING)
            controller.request_exit()

    def test_source_failure_fails_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [FakeSource("dvs"), FakeSource("cis"), FakeSource("imu")]
            controller = self.make_controller(root, sources)
            self.assertTrue(controller.start_recording())
            session_dir = controller.session.session_dir
            sources[1].ready = False
            sources[1].error = "unplugged"
            controller.tick()
            self.assertEqual(controller.state, StudioState.FAULT)
            manifest = json.loads((session_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("unplugged", manifest["last_error"])

    def test_storage_failure_blocks_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self.make_controller(
                root,
                [FakeSource("dvs"), FakeSource("cis"), FakeSource("imu")],
                storage_check=lambda _: (False, "read only"),
            )
            self.assertFalse(controller.start_recording())
            self.assertEqual(controller.state, StudioState.PREVIEW)
            self.assertIsNone(controller.session)


class RecordingTests(unittest.TestCase):
    def test_time_indexes_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = SessionRecorder.begin(
                Path(tmp), "timestamps", True, {"test": True}
            )
            session.add_cis_frame(1, 2, 3, 4)
            session.add_anchor("dvs", 1, 10, 11, 12)
            session.finish("complete", {"dvs": {"ready": True}})
            assert session.paths is not None
            self.assertEqual(
                session.paths.cis_frames.read_text().splitlines()[1],
                "1,2,3,4",
            )
            self.assertEqual(
                session.paths.clock_anchors.read_text().splitlines()[1],
                "dvs,1,10,11,12",
            )
            manifest = json.loads(session.paths.manifest.read_text())
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["status"], "complete")
            self.assertTrue(session.paths.log.read_text())

    def test_sync_window_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = SessionRecorder.begin(
                Path(tmp), "window", True, {"test": True}
            )
            session.mark_recording_started(
                monotonic_ns_value=100,
                unix_ns_value=200,
            )
            session.set_sync_info(
                {
                    "schema_version": 1,
                    "common_start_monotonic_ns": 100,
                    "common_end_monotonic_ns": 200,
                    "valid": True,
                }
            )
            session.finish("complete")
            assert session.paths is not None
            window = json.loads(session.paths.sync_window.read_text())
            self.assertEqual(window["common_start_monotonic_ns"], 100)
            manifest = json.loads(session.paths.manifest.read_text())
            self.assertTrue(manifest["files"]["sync_window"]["exists"])


class ImuWriterTests(unittest.TestCase):
    class SlowStore:
        def __init__(self):
            self.values = []

        def append(self, value):
            time.sleep(0.2)
            self.values.extend(np.asarray(value).tolist())

        def close(self):
            return

    def test_enqueue_does_not_wait_for_disk_write(self):
        errors = []
        writer = _ImuZarrWriter(
            {"accel": self.SlowStore()},
            {"accel": self.SlowStore()},
            errors.append,
        )
        started = time.perf_counter()
        writer.enqueue({"accel": ([[1, 2, 3]], [123])})
        enqueue_seconds = time.perf_counter() - started
        self.assertLess(enqueue_seconds, 0.1)
        writer.close()
        self.assertEqual(errors, [])

    def test_zarr_v3_writer_metadata_and_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accel"
            writer = _ZarrV3ArrayWriter(
                path,
                trailing_shape=(3,),
                dtype="<i2",
                chunk_rows=4,
            )
            writer.append([[1, 2, 3], [4, 5, 6]])
            writer.close()

            metadata = json.loads((path / "zarr.json").read_text())
            self.assertEqual(metadata["shape"], [2, 3])
            self.assertEqual(metadata["zarr_format"], 3)
            raw = (path / "c" / "0").read_bytes()
            values = np.frombuffer(raw, dtype="<i2").reshape(4, 3)
            self.assertEqual(values[:2].tolist(), [[1, 2, 3], [4, 5, 6]])
            self.assertEqual(values[2:].tolist(), [[0, 0, 0], [0, 0, 0]])

    def test_static_zero_values_still_mark_imu_ready(self):
        source = ImuSource("", synthetic=True)
        source._emit_sample(0x51, (0, 0, 0))
        self.assertTrue(source.ready)
        self.assertEqual(source.latest_values()["accel"], (0, 0, 0))


class NativeDvsTests(unittest.TestCase):
    def test_native_frame_builder(self):
        try:
            from sensor_studio._dvs_fastpath import DvsFrameBuilder
        except ImportError:
            self.skipTest("native DVS extension is not built")

        builder = DvsFrameBuilder(8, 6, 10_000)
        builder.consume_test(
            np.array([0, 7, 3], dtype=np.int32),
            np.array([0, 5, 2], dtype=np.int32),
            np.array([1, 0, 1], dtype=np.int32),
            np.array([20_000, 20_001, 100], dtype=np.int64),
        )
        frame = builder.latest_frame()

        self.assertEqual(frame.shape, (6, 8, 3))
        self.assertEqual(frame[0, 0].tolist(), [255, 255, 255])
        self.assertEqual(frame[5, 7].tolist(), [200, 126, 64])
        self.assertEqual(frame[2, 3].tolist(), [30, 37, 52])

        self.assertEqual(builder.stats()["callback_count"], 1)
        self.assertEqual(builder.stats()["event_count"], 3)
        builder.reset_stats()
        self.assertEqual(builder.stats()["callback_count"], 0)
        self.assertEqual(builder.stats()["event_count"], 0)


if __name__ == "__main__":
    unittest.main()
