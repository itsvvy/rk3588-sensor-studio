from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from sensor_studio.controller import RecordingController, StudioState
from sensor_studio.recording import SessionRecorder


class FakeSource:
    def __init__(self, name: str, ready: bool = True) -> None:
        self.name = name
        self.ready = ready
        self.error = None if ready else "missing"
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.session = None

    def start_recording(self, session):
        self.started += 1
        self.session = session

    def stop_recording(self):
        self.stopped += 1

    def close(self):
        self.closed += 1
        self.ready = False

    def stats(self):
        return {
            "ready": self.ready,
            "started": self.started,
            "stopped": self.stopped,
        }


class ControllerTests(unittest.TestCase):
    def make_controller(self, root: Path, sources, storage_check=None):
        kwargs = {}
        if storage_check is not None:
            kwargs["storage_check"] = storage_check
        controller = RecordingController(
            sources=sources,
            output_root=root,
            name="unit",
            synthetic=True,
            config={"test": True},
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
            self.assertEqual([source.closed for source in sources], [1, 1, 1])

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
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["status"], "complete")
            self.assertTrue(session.paths.log.read_text())


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
