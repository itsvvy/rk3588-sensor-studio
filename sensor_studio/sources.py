from __future__ import annotations

import json
import logging
import math
import os
import queue
import struct
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from .recording import SessionRecorder, monotonic_ns, unix_ns


log = logging.getLogger(__name__)


class _ZarrV3ArrayWriter:
    """Minimal uncompressed Zarr v3 array writer for append-only numeric data."""

    def __init__(
        self,
        path: Path,
        *,
        trailing_shape: tuple[int, ...],
        dtype: str,
        chunk_rows: int = 256,
    ) -> None:
        self.path = path
        self.trailing_shape = trailing_shape
        self.dtype = np.dtype(dtype)
        self.chunk_rows = chunk_rows
        self.shape = [0, *trailing_shape]
        self._chunk_index = 0
        self._buffered_rows = 0
        self._buffer = np.zeros(
            (chunk_rows, *trailing_shape),
            dtype=self.dtype,
        )
        (path / "c").mkdir(parents=True, exist_ok=False)
        self._write_metadata()

    def append(self, values: list[Any] | np.ndarray) -> None:
        rows = np.asarray(values, dtype=self.dtype)
        expected_tail = tuple(self.shape[1:])
        if rows.ndim != len(self.trailing_shape) + 1 or rows.shape[1:] != expected_tail:
            raise ValueError(
                f"unexpected array shape {rows.shape}, expected (*, {expected_tail})"
            )
        offset = 0
        while offset < len(rows):
            room = self.chunk_rows - self._buffered_rows
            count = min(room, len(rows) - offset)
            self._buffer[
                self._buffered_rows : self._buffered_rows + count
            ] = rows[offset : offset + count]
            self._buffered_rows += count
            offset += count
            self.shape[0] += count
            if self._buffered_rows == self.chunk_rows:
                self._write_chunk()
                self._chunk_index += 1
                self._buffered_rows = 0
                self._buffer.fill(0)
        self._write_chunk()
        self._write_metadata()

    def close(self) -> None:
        self._write_chunk()
        self._write_metadata()

    def _write_chunk(self) -> None:
        path = self.path / "c" / str(self._chunk_index)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(self._buffer.tobytes(order="C"))
        os.replace(temporary, path)

    def _write_metadata(self) -> None:
        payload = {
            "shape": self.shape,
            "data_type": np.dtype(self.dtype).name,
            "chunk_grid": {
                "name": "regular",
                "configuration": {
                    "chunk_shape": [self.chunk_rows, *self.trailing_shape]
                },
            },
            "chunk_key_encoding": {
                "name": "default",
                "configuration": {"separator": "/"},
            },
            "fill_value": 0,
            "codecs": [
                {
                    "name": "bytes",
                    "configuration": {"endian": "little"},
                }
            ],
            "attributes": {},
            "zarr_format": 3,
            "node_type": "array",
            "storage_transformers": [],
        }
        self._write_json_atomic(self.path / "zarr.json", payload)

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)


class _ImuZarrWriter:
    """Serializes Zarr appends on a writer thread, away from serial sampling."""

    _STOP = object()

    def __init__(
        self,
        stores: dict[str, _ZarrV3ArrayWriter],
        timestamp_stores: dict[str, _ZarrV3ArrayWriter],
        on_error,
    ) -> None:
        self._stores = stores
        self._timestamp_stores = timestamp_stores
        self._on_error = on_error
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="sensor-studio-imu-writer",
            daemon=True,
        )
        self._thread.start()

    def enqueue(
        self,
        batch: dict[str, tuple[list[list[int]], list[int]]],
    ) -> None:
        if self._error is not None:
            raise RuntimeError(f"IMU Zarr writer failed: {self._error}")
        try:
            self._queue.put_nowait(batch)
        except queue.Full as exc:
            raise RuntimeError("IMU Zarr writer queue is full") from exc

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                for name, (rows, timestamps) in item.items():
                    if not rows:
                        continue
                    self._stores[name].append(rows)
                    self._timestamp_stores[name].append(timestamps)
            except Exception as exc:
                self._error = exc
                self._on_error(f"IMU Zarr写入失败: {exc}")
            finally:
                self._queue.task_done()

    def close(self) -> None:
        try:
            self._queue.put(self._STOP, timeout=5.0)
        except queue.Full as exc:
            raise RuntimeError("IMU Zarr writer queue did not drain") from exc
        self._thread.join(timeout=10.0)
        if self._thread.is_alive():
            raise RuntimeError("IMU Zarr writer did not stop within 10 seconds")
        if self._error is not None:
            raise RuntimeError(f"IMU Zarr writer failed: {self._error}")
        for store in (*self._stores.values(), *self._timestamp_stores.values()):
            store.close()


def prewarm_recording_backends() -> None:
    """Recording backends are lightweight; no process-wide import is required."""
    return


class SourceBase:
    name = "unknown"

    def __init__(self) -> None:
        self.ready = False
        self.error: str | None = None
        self.recovering = False
        self._state_lock = threading.RLock()

    def _set_ready(self) -> None:
        with self._state_lock:
            self.ready = True
            self.error = None
            self.recovering = False

    def _set_error(self, message: str) -> None:
        with self._state_lock:
            self.ready = False
            self.error = message
        log.error("%s: %s", self.name, message)

    def poll(self) -> None:
        return


class DvsSource(SourceBase):
    name = "dvs"

    def __init__(
        self,
        serial: str = "",
        event_rate: int = 0,
        backend: str = "auto",
    ) -> None:
        super().__init__()
        if backend not in {"auto", "native", "python"}:
            raise ValueError(f"unsupported DVS backend: {backend}")
        self.serial = serial
        self.event_rate = int(event_rate)
        self.requested_backend = backend
        self.backend = backend
        self._device = None
        self._event_decoder = None
        self._stream_decoder = None
        self._stream = None
        self._callback_id = None
        self._frame_builder = None
        self._native_stats_snapshot: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._render_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._first_buffer = threading.Event()
        self._recording = False
        self._session: SessionRecorder | None = None
        self._frame = None
        self._latest_frame: np.ndarray | None = None
        self._last_anchor_mono_ns = 0
        self._slice_count = 0
        self._event_count = 0
        self._record_start_ns = 0
        self._record_first_host_ns = 0
        self._record_last_host_ns = 0
        self._last_device_t_us = 0
        self._frame_lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._pending_events: deque = deque(maxlen=128)
        self._record_lock = threading.RLock()

    def open(self) -> None:
        try:
            from metavision_hal import DeviceDiscovery

            self._device = DeviceDiscovery.open(self.serial or "")
            if self._device is None:
                raise RuntimeError("未发现DVS相机")
            geometry = self._device.get_i_geometry()
            if geometry is None:
                raise RuntimeError("DVS设备没有geometry接口")
            height = geometry.get_height()
            width = geometry.get_width()
            if not height or not width:
                raise RuntimeError("OpenEB未返回DVS分辨率")
            self.height = int(height)
            self.width = int(width)
            self._frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            with self._frame_lock:
                self._latest_frame = self._frame.copy()
            self._event_decoder = self._device.get_i_event_cd_decoder()
            self._stream_decoder = self._device.get_i_events_stream_decoder()
            self._stream = self._device.get_i_events_stream()
            if (
                self._event_decoder is None
                or self._stream_decoder is None
                or self._stream is None
            ):
                raise RuntimeError("DVS设备缺少事件流接口")
            erc = self._device.get_i_erc_module()
            if erc is not None and self.event_rate > 0:
                minimum = int(erc.get_min_supported_cd_event_rate())
                maximum = int(erc.get_max_supported_cd_event_rate())
                target = min(max(self.event_rate, minimum), maximum)
                if target != self.event_rate:
                    log.warning(
                        "DVS ERC target clamped from %d to %d events/s",
                        self.event_rate,
                        target,
                    )
                    self.event_rate = target
                if not erc.set_cd_event_rate(self.event_rate):
                    raise RuntimeError("无法设置DVS硬件事件率")
                if not erc.enable(True):
                    raise RuntimeError("无法启用DVS硬件事件率限制")
                log.info(
                    "DVS ERC enabled at %d events/s", self.event_rate
                )
            if self.requested_backend != "python":
                try:
                    from ._dvs_fastpath import DvsFrameBuilder

                    self._frame_builder = DvsFrameBuilder(self.width, self.height)
                    self._callback_id = self._frame_builder.attach(
                        self._event_decoder
                    )
                    self.backend = "native"
                    log.info("DVS native C++ preview callback enabled")
                except Exception as exc:
                    self._frame_builder = None
                    self._callback_id = None
                    if self.requested_backend == "native":
                        raise RuntimeError(
                            f"DVS原生预览路径加载失败: {exc}"
                        ) from exc
                    log.warning(
                        "DVS native preview unavailable, using Python fallback: %s",
                        exc,
                    )
            if self._frame_builder is None:
                self.backend = "python"
                self._callback_id = self._event_decoder.add_event_buffer_callback(
                    self._on_events
                )
            self._stream.start()
            self._thread = threading.Thread(
                target=self._run,
                name="sensor-studio-dvs",
                daemon=True,
            )
            if self.backend == "python":
                self._render_thread = threading.Thread(
                    target=self._render_loop,
                    name="sensor-studio-dvs-preview",
                    daemon=True,
                )
            self._thread.start()
            if self._render_thread is not None:
                self._render_thread.start()
            if not self._first_buffer.wait(timeout=5.0):
                raise TimeoutError("DVS已打开，但5秒内没有USB数据")
            self._set_ready()
        except Exception as exc:
            self._set_error(f"DVS启动失败: {exc}")
            self.close()

    def _on_events(self, events) -> None:
        if events.size == 0 or self._stop.is_set():
            return
        self._slice_count += 1
        self._event_count += int(events.size)
        with self._events_lock:
            self._pending_events.append(events)

    def _render_loop(self) -> None:
        while not self._stop.wait(1.0 / 25.0):
            try:
                with self._events_lock:
                    batches = list(self._pending_events)
                    self._pending_events.clear()
                if not batches:
                    continue
                events = (
                    batches[0]
                    if len(batches) == 1
                    else np.concatenate(batches)
                )
                cutoff_us = self._last_device_t_us - 10_000
                events = events[events["t"] >= cutoff_us]
                x = events["x"].astype(np.intp, copy=False)
                y = events["y"].astype(np.intp, copy=False)
                p = events["p"]
                valid = (
                    (x >= 0)
                    & (x < self.width)
                    & (y >= 0)
                    & (y < self.height)
                )
                positive = valid & (p > 0)
                negative = valid & (p == 0)
                frame = self._frame
                # Metavision Dark palette in BGR.
                frame[:] = (52, 37, 30)
                if positive.any():
                    frame[y[positive], x[positive]] = (255, 255, 255)
                if negative.any():
                    frame[y[negative], x[negative]] = (200, 126, 64)
                with self._frame_lock:
                    self._latest_frame = frame.copy()
            except Exception as exc:
                self._set_error(f"DVS回调处理失败: {exc}")

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                result = self._stream.poll_buffer()
                if result < 0:
                    raise RuntimeError(f"DVS USB读取返回错误: {result}")
                if result > 0:
                    raw_data = self._stream.get_latest_raw_data()
                    if raw_data is not None:
                        self._stream_decoder.decode(raw_data)
                        if self._frame_builder is not None:
                            self._slice_count += 1
                        timestamp_us = int(
                            self._stream_decoder.get_last_timestamp()
                        )
                        if timestamp_us >= 0:
                            self._last_device_t_us = timestamp_us
                            now_mono = monotonic_ns()
                            with self._record_lock:
                                if self._recording and self._session is not None:
                                    self._record_last_host_ns = now_mono
                                    if (
                                        now_mono - self._last_anchor_mono_ns
                                        >= 1_000_000_000
                                    ):
                                        self._session.add_anchor(
                                            "dvs",
                                            self._slice_count,
                                            self._last_device_t_us * 1000,
                                            now_mono,
                                            unix_ns(),
                                        )
                                        self._last_anchor_mono_ns = now_mono
                    self._first_buffer.set()
                else:
                    time.sleep(0.001)
        except Exception as exc:
            if not self._stop.is_set():
                self._set_error(f"DVS数据流异常: {exc}")
        finally:
            self._first_buffer.set()

    def latest_frame(self) -> np.ndarray | None:
        if self._frame_builder is not None:
            return self._frame_builder.latest_frame()
        with self._frame_lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    def start_recording(self, session: SessionRecorder) -> None:
        assert session.paths is not None
        with self._record_lock:
            if self._recording:
                return
            if self._stream is None:
                raise RuntimeError("DVS设备没有事件流接口")
            self._stream.log_raw_data(str(session.paths.dvs))
            self._slice_count = 0
            self._event_count = 0
            now_mono = monotonic_ns()
            now_unix = unix_ns()
            self._record_first_host_ns = now_mono
            self._record_last_host_ns = now_mono
            if self._frame_builder is not None:
                self._frame_builder.reset_stats()
            self._session = session
            self._record_start_ns = now_mono
            self._last_anchor_mono_ns = now_mono
            self._recording = True
            session.add_anchor(
                "dvs",
                0,
                self._last_device_t_us * 1000,
                now_mono,
                now_unix,
            )

    def stop_recording(self) -> None:
        with self._record_lock:
            if not self._recording:
                return
            self._recording = False
            session = self._session
            self._session = None
            last_device_t_us = self._last_device_t_us
            self._record_last_host_ns = monotonic_ns()
            if session is not None:
                session.add_anchor(
                    "dvs",
                    self._slice_count,
                    last_device_t_us * 1000,
                    self._record_last_host_ns,
                    unix_ns(),
                )
        self._stop.set()
        # Stop the USB transfer first. On this USB2 setup,
        # stop_log_raw_data() can otherwise wait indefinitely for in-flight URBs.
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            self._stream.stop_log_raw_data()
        if session is not None:
            session.log("DVS RAW writer stopped")

    def close(self) -> None:
        self.stop_recording()
        self._stop.set()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        if self._render_thread is not None and self._render_thread.is_alive():
            self._render_thread.join(timeout=2.0)
        if self._frame_builder is not None and self._event_decoder is not None:
            self._native_stats_snapshot = self._frame_builder.stats()
            try:
                self._frame_builder.detach(self._event_decoder)
            except Exception:
                pass
        if self._event_decoder is not None and self._callback_id is not None:
            if self._frame_builder is None:
                try:
                    self._event_decoder.remove_callback(self._callback_id)
                except Exception:
                    pass
        self._thread = None
        self._render_thread = None
        self._callback_id = None
        self._frame_builder = None
        self._event_decoder = None
        self._stream_decoder = None
        self._stream = None
        self._device = None

    def stats(self) -> dict[str, Any]:
        native_stats = (
            self._frame_builder.stats()
            if self._frame_builder is not None
            else self._native_stats_snapshot
        )
        return {
            "ready": self.ready,
            "error": self.error,
            "width": getattr(self, "width", None),
            "height": getattr(self, "height", None),
            "event_slices": native_stats.get("callback_count", self._slice_count),
            "events": native_stats.get("event_count", self._event_count),
            "last_device_t_us": self._last_device_t_us,
            "first_host_monotonic_ns": self._record_first_host_ns,
            "last_host_monotonic_ns": self._record_last_host_ns,
            "transport": (
                "openeb_hal_native_callback"
                if self.backend == "native"
                else "openeb_hal_poll"
            ),
            "preview_backend": self.backend,
            "event_rate_limit": self.event_rate,
        }


class SyntheticDvsSource(SourceBase):
    name = "dvs"

    def __init__(self, width: int = 320, height: int = 320) -> None:
        super().__init__()
        self.width = width
        self.height = height
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest = np.zeros((height, width, 3), dtype=np.uint8)
        self._lock = threading.Lock()
        self._record_file = None
        self._record_start_mono_ns = 0
        self._record_start_unix_ns = 0
        self._record_first_host_ns = 0
        self._record_last_host_ns = 0
        self._last_anchor_ns = 0
        self._record_count = 0
        self._record_file_lock = threading.Lock()

    def open(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="sensor-studio-synthetic-dvs",
            daemon=True,
        )
        self._thread.start()
        self._set_ready()

    def _run(self) -> None:
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        phase = 0.0
        while not self._stop.wait(0.04):
            phase += 0.12
            frame[:] = (8, 8, 12)
            x = int((math.sin(phase) * 0.35 + 0.5) * (self.width - 1))
            y = int((math.cos(phase * 0.8) * 0.3 + 0.5) * (self.height - 1))
            x0, x1 = max(0, x - 8), min(self.width, x + 9)
            y0, y1 = max(0, y - 8), min(self.height, y + 9)
            frame[y0:y1, x0:x1] = (20, 90, 230)
            frame[::24, ::24] = (80, 80, 80)
            with self._lock:
                self._latest = frame.copy()
            with self._record_file_lock:
                if self._record_file is not None:
                    mono = monotonic_ns()
                    unix = unix_ns()
                    self._record_last_host_ns = mono
                    self._record_file.write(
                        f"{mono},{unix},{x},{y}\n".encode("ascii")
                    )
                    self._record_file.flush()
                    self._record_count += 1

    def latest_frame(self) -> np.ndarray:
        with self._lock:
            return self._latest.copy()

    def start_recording(self, session: SessionRecorder) -> None:
        assert session.paths is not None
        self._record_start_mono_ns = monotonic_ns()
        self._record_start_unix_ns = unix_ns()
        self._record_first_host_ns = self._record_start_mono_ns
        self._record_last_host_ns = self._record_start_mono_ns
        self._last_anchor_ns = 0
        self._record_count = 0
        self._record_file = session.paths.dvs.open("wb")
        self._record_file.write(
            b"SENSOR_STUDIO_SYNTHETIC_DVS_RAW_V1\n"
            + f"start_unix_ns={self._record_start_unix_ns}\n".encode("ascii")
        )
        self._record_file.flush()
        session.add_anchor(
            "dvs",
            0,
            self._record_start_mono_ns,
            self._record_start_mono_ns,
            self._record_start_unix_ns,
        )

    def stop_recording(self) -> None:
        with self._record_file_lock:
            if self._record_file is not None:
                self._record_file.flush()
                self._record_file.close()
                self._record_file = None

    def close(self) -> None:
        self.stop_recording()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def stats(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "error": self.error,
            "width": self.width,
            "height": self.height,
            "synthetic": True,
            "events": self._record_count,
            "first_host_monotonic_ns": self._record_first_host_ns,
            "last_host_monotonic_ns": self._record_last_host_ns,
        }


class CisSource(SourceBase):
    name = "cis"

    def __init__(
        self,
        device: str,
        *,
        synthetic: bool = False,
        width: int = 2560,
        height: int = 720,
        fps: int = 25,
        show_preview: bool = False,
    ) -> None:
        super().__init__()
        self.device = device
        self.synthetic = synthetic
        self.width = width
        self.height = height
        self.fps = fps
        self.show_preview = show_preview
        self._pipeline = None
        self._tee = None
        self._preview_sink = None
        self._timestamp_probe_id = None
        self._timestamp_pad = None
        self._timestamp_drain_pad = None
        self._first_frame = threading.Event()
        self._recording = False
        self._session: SessionRecorder | None = None
        self._record_elements: list[Any] = []
        self._record_tee_pad = None
        self._record_probe_id = None
        self._record_probe_pad = None
        self._record_frame_index = 0
        self._total_frames = 0
        self._last_pts_ns = 0
        self._record_first_host_ns = 0
        self._record_last_host_ns = 0
        self._source_pts_timing: dict[int, tuple[int, int]] = {}
        self._source_pts_order: deque[int] = deque()
        self._stopping = False
        self._record_lock = threading.RLock()
        self._Gst = None

    def open(self) -> None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            self._Gst = Gst
            Gst.init(None)
            self._pipeline = Gst.Pipeline.new("sensor-studio-cis")
            if self.synthetic:
                source = self._make("videotestsrc", "source")
                source.set_property("is-live", True)
                source.set_property("pattern", "smpte")
            else:
                source = self._make("v4l2src", "source")
                source.set_property("device", self.device)
                source.set_property("do-timestamp", True)

            caps = self._make("capsfilter", "source-caps")
            caps.set_property(
                "caps",
                Gst.Caps.from_string(
                    f"video/x-raw,width={self.width},height={self.height},"
                    f"framerate={self.fps}/1"
                )
                if self.synthetic
                else Gst.Caps.from_string(
                    f"image/jpeg,width={self.width},height={self.height},"
                    f"framerate={self.fps}/1"
                ),
            )
            convert = self._make("videoconvert", "convert")
            bgr_caps = self._make("capsfilter", "bgr-caps")
            bgr_caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGR"))
            self._tee = self._make("tee", "tee")
            if self.synthetic:
                elements = [source, caps, convert]
            else:
                decoder = self._Gst.ElementFactory.make(
                    "mppjpegdec", "decoder"
                )
                if decoder is not None:
                    self.decoder_name = "mppjpegdec"
                else:
                    decoder = self._make("jpegdec", "decoder")
                    self.decoder_name = "jpegdec"
                elements = [source, caps, decoder, convert]
            base_elements = [*elements, bgr_caps, self._tee]
            for element in base_elements:
                self._pipeline.add(element)
            for left, right in zip(base_elements[:-1], base_elements[1:]):
                if not left.link(right):
                    raise RuntimeError(f"GStreamer无法连接 {left.get_name()} -> {right.get_name()}")

            # Capture timing at the source pad, before JPEG decode and tee
            # scheduling can add variable processing latency.
            self._timestamp_pad = source.get_static_pad("src")
            if self._timestamp_pad is None:
                raise RuntimeError("CIS采集源没有输出pad")
            self._timestamp_probe_id = self._timestamp_pad.add_probe(
                Gst.PadProbeType.BUFFER, self._on_buffer
            )

            timestamp_queue = self._make("queue", "timestamp-queue")
            timestamp_queue.set_property("max-size-buffers", 1)
            timestamp_queue.set_property("leaky", 2)
            timestamp_sink = self._make("fakesink", "timestamp-sink")
            timestamp_sink.set_property("sync", False)
            timestamp_pad = self._link_tee_branch(
                [timestamp_queue, timestamp_sink], "timestamp"
            )
            self._timestamp_drain_pad = timestamp_pad
            if self.show_preview:
                preview_queue = self._make("queue", "cis-preview-queue")
                preview_queue.set_property("max-size-buffers", 2)
                preview_queue.set_property("leaky", 2)
                preview_convert = self._make(
                    "videoconvert", "cis-preview-convert"
                )
                preview_sink = self._make("autovideosink", "cis-preview")
                preview_sink.set_property("sync", False)
                self._preview_sink = preview_sink
                self._link_tee_branch(
                    [preview_queue, preview_convert, preview_sink],
                    "preview",
                )

            result = self._pipeline.set_state(Gst.State.PLAYING)
            _, state, _ = self._pipeline.get_state(5 * Gst.SECOND)
            if result == Gst.StateChangeReturn.FAILURE or state not in {
                Gst.State.PLAYING,
                Gst.State.PAUSED,
            }:
                raise RuntimeError(f"CIS管线无法进入PLAYING，state={state}")
            if not self._first_frame.wait(timeout=12.0):
                raise TimeoutError("CIS已打开，但12秒内没有视频帧")
            if self.show_preview:
                log.info(
                    "CIS preview attached at %dx%d via autovideosink",
                    self.width,
                    self.height,
                )
            self._set_ready()
        except Exception as exc:
            bus_error = ""
            if self._pipeline is not None and self._Gst is not None:
                message = self._pipeline.get_bus().pop_filtered(
                    self._Gst.MessageType.ERROR
                )
                if message is not None:
                    error, debug = message.parse_error()
                    bus_error = f"; GStreamer: {error}"
                    if debug:
                        bus_error += f" ({debug})"
            self._set_error(f"CIS启动失败: {exc}{bus_error}")
            self.close()

    def _make(self, factory: str, name: str):
        element = self._Gst.ElementFactory.make(factory, name)
        if element is None:
            raise RuntimeError(f"缺少GStreamer element: {factory}")
        return element

    def _link_tee_branch(self, elements: list[Any], name: str):
        for element in elements:
            self._pipeline.add(element)
        tee_pad = self._request_tee_pad()
        if tee_pad is None:
            raise RuntimeError(f"无法创建CIS {name}输出pad")
        if tee_pad.link(elements[0].get_static_pad("sink")) != self._Gst.PadLinkReturn.OK:
            raise RuntimeError(f"无法连接CIS {name}分支")
        for left, right in zip(elements[:-1], elements[1:]):
            if not left.link(right):
                raise RuntimeError(
                    f"无法连接CIS {name}元素 "
                    f"{left.get_name()} -> {right.get_name()}"
                )
        for element in elements:
            element.sync_state_with_parent()
        return tee_pad

    def _on_buffer(self, _pad, info):
        buffer = info.get_buffer()
        if buffer is None:
            return self._Gst.PadProbeReturn.OK
        try:
            host_mono = monotonic_ns()
            host_unix = unix_ns()
            pts = int(buffer.pts)
            if pts == int(self._Gst.CLOCK_TIME_NONE):
                pts = 0
            self._total_frames += 1
            self._last_pts_ns = pts
            self._first_frame.set()
            with self._record_lock:
                self._source_pts_timing[pts] = (host_mono, host_unix)
                self._source_pts_order.append(pts)
                while len(self._source_pts_order) > 512:
                    expired = self._source_pts_order.popleft()
                    self._source_pts_timing.pop(expired, None)
            return self._Gst.PadProbeReturn.OK
        except Exception as exc:
            self._set_error(f"CIS帧处理异常: {exc}")
            return self._Gst.PadProbeReturn.DROP

    def _on_record_buffer(self, _pad, info):
        buffer = info.get_buffer()
        if buffer is None:
            return self._Gst.PadProbeReturn.OK
        try:
            pts = int(buffer.pts)
            if pts == int(self._Gst.CLOCK_TIME_NONE):
                pts = 0
            with self._record_lock:
                if self._recording and self._session is not None:
                    source_mono, source_unix = self._source_pts_timing.get(
                        pts,
                        (monotonic_ns(), unix_ns()),
                    )
                    self._record_frame_index += 1
                    if self._record_first_host_ns == 0:
                        self._record_first_host_ns = source_mono
                    self._record_last_host_ns = source_mono
                    self._session.add_cis_frame(
                        self._record_frame_index,
                        pts,
                        source_mono,
                        source_unix,
                    )
            return self._Gst.PadProbeReturn.OK
        except Exception as exc:
            self._set_error(f"CIS录制帧处理异常: {exc}")
            return self._Gst.PadProbeReturn.DROP

    def latest_frame(self) -> np.ndarray | None:
        return None

    def poll(self) -> None:
        if self._pipeline is None or self._Gst is None:
            return
        bus = self._pipeline.get_bus()
        message = bus.pop_filtered(self._Gst.MessageType.ERROR)
        if message is not None:
            error, debug = message.parse_error()
            self._set_error(f"CIS/GStreamer错误: {error}; {debug or ''}".strip())

    def _request_tee_pad(self):
        if hasattr(self._tee, "request_pad_simple"):
            return self._tee.request_pad_simple("src_%u")
        return self._tee.get_request_pad("src_%u")

    def start_recording(self, session: SessionRecorder) -> None:
        assert session.paths is not None
        with self._record_lock:
            if self._recording:
                return
            queue_element = self._make("queue", "record-queue")
            record_convert = self._make("videoconvert", "record-convert")
            record_caps = self._make("capsfilter", "record-caps")
            record_caps.set_property(
                "caps", self._Gst.Caps.from_string("video/x-raw,format=I420")
            )
            encoder = None
            for factory in ("mpph264enc", "x264enc", "openh264enc"):
                encoder = self._Gst.ElementFactory.make(factory, "record-encoder")
                if encoder is not None:
                    self.encoder_name = factory
                    break
            if encoder is None:
                raise RuntimeError("没有可用的H.264编码器")
            if self.encoder_name == "x264enc":
                encoder.set_property("bitrate", 20_000)
                encoder.set_property("speed-preset", "ultrafast")
                encoder.set_property("tune", "zerolatency")
                encoder.set_property("key-int-max", 50)
                encoder.set_property("bframes", 0)
            elif self.encoder_name == "openh264enc":
                encoder.set_property("bitrate", 20_000_000)
                encoder.set_property("complexity", "low")
            parser = self._make("h264parse", "record-parser")
            muxer = self._make("matroskamux", "record-muxer")
            sink = self._make("filesink", "record-sink")
            sink.set_property("location", str(session.paths.cis))
            sink.set_property("sync", False)
            sink.set_property("async", False)
            elements = [
                queue_element,
                record_convert,
                record_caps,
                encoder,
                parser,
                muxer,
                sink,
            ]
            tee_pad = None
            try:
                for element in elements:
                    self._pipeline.add(element)
                tee_pad = self._request_tee_pad()
                if tee_pad is None:
                    raise RuntimeError("无法从CIS tee取得录制输出pad")
                if tee_pad.link(queue_element.get_static_pad("sink")) != self._Gst.PadLinkReturn.OK:
                    raise RuntimeError("无法连接CIS录制分支")
                for left, right in zip(elements[:-1], elements[1:]):
                    if not left.link(right):
                        raise RuntimeError(
                            f"无法连接录制元素 {left.get_name()} -> {right.get_name()}"
                        )
                self._record_probe_pad = queue_element.get_static_pad("sink")
                if self._record_probe_pad is None:
                    raise RuntimeError("CIS录制queue缺少输入pad")
                self._record_probe_id = self._record_probe_pad.add_probe(
                    self._Gst.PadProbeType.BUFFER,
                    self._on_record_buffer,
                )
                self._record_elements = elements
                self._record_tee_pad = tee_pad
                self._session = session
                self._record_frame_index = 0
                self._record_first_host_ns = 0
                self._record_last_host_ns = 0
                self._source_pts_timing.clear()
                self._source_pts_order.clear()
                self._stopping = False
                self._recording = True
                for element in elements:
                    element.sync_state_with_parent()
            except Exception:
                self._recording = False
                self._record_elements = []
                self._record_tee_pad = None
                self._session = None
                self._record_probe_id = None
                self._record_probe_pad = None
                self._stopping = False
                if tee_pad is not None:
                    try:
                        tee_pad.unlink(queue_element.get_static_pad("sink"))
                    except Exception:
                        pass
                    self._tee.release_request_pad(tee_pad)
                for element in reversed(elements):
                    try:
                        element.set_state(self._Gst.State.NULL)
                        self._pipeline.remove(element)
                    except Exception:
                        pass
                raise

    def stop_recording(self) -> None:
        with self._record_lock:
            if not self._recording or self._stopping:
                return
            self._stopping = True
            tee_pad = self._record_tee_pad
            elements = list(self._record_elements)
            probe_pad = self._record_probe_pad
            probe_id = self._record_probe_id
            error: Exception | None = None
            try:
                if tee_pad is None:
                    raise RuntimeError("CIS录制分支缺少tee pad")
                if not elements[0].send_event(self._Gst.Event.new_eos()):
                    raise RuntimeError("向CIS录制queue发送EOS失败")
                bus = self._pipeline.get_bus()
                got_eos = False
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    message = bus.timed_pop_filtered(
                        200 * self._Gst.MSECOND,
                        self._Gst.MessageType.EOS | self._Gst.MessageType.ERROR,
                    )
                    if message is None:
                        continue
                    if message.type == self._Gst.MessageType.ERROR:
                        gst_error, debug = message.parse_error()
                        raise RuntimeError(f"{gst_error}; {debug or ''}")
                    got_eos = True
                    break
                if not got_eos:
                    # Matroska is finalized when the recording elements are
                    # taken to NULL below. Not every live pipeline forwards
                    # the synthetic EOS back to the application bus.
                    log.warning("CIS pipeline did not report EOS; finalizing to NULL")
            except Exception as exc:
                error = exc
            finally:
                if probe_pad is not None and probe_id is not None:
                    try:
                        probe_pad.remove_probe(probe_id)
                    except Exception:
                        pass
                if tee_pad is not None and elements:
                    try:
                        tee_pad.unlink(elements[0].get_static_pad("sink"))
                    except Exception:
                        pass
                for element in reversed(elements):
                    try:
                        element.set_state(self._Gst.State.NULL)
                        self._pipeline.remove(element)
                    except Exception:
                        pass
                if tee_pad is not None and self._tee is not None:
                    try:
                        self._tee.release_request_pad(tee_pad)
                    except Exception:
                        pass
                self._record_elements = []
                self._record_tee_pad = None
                self._record_probe_id = None
                self._record_probe_pad = None
                self._session = None
                self._recording = False
                self._stopping = False
            if error is not None:
                raise error

    def close(self) -> None:
        try:
            self.stop_recording()
        except Exception:
            log.exception("Stopping CIS recording during close failed")
        if self._pipeline is not None:
            if self._timestamp_pad is not None and self._timestamp_probe_id is not None:
                try:
                    self._timestamp_pad.remove_probe(self._timestamp_probe_id)
                except Exception:
                    pass
            pipeline = self._pipeline
            threading.Thread(
                target=pipeline.set_state,
                args=(self._Gst.State.NULL,),
                name="sensor-studio-cis-close",
                daemon=True,
            ).start()
            self._pipeline = None
        self._preview_sink = None
        self._timestamp_probe_id = None
        self._timestamp_pad = None
        self._timestamp_drain_pad = None
        self._tee = None

    def stats(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "error": self.error,
            "synthetic": self.synthetic,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "preview": self.show_preview,
            "preview_frames": self._total_frames,
            "recorded_frames": self._record_frame_index,
            "last_pts_ns": self._last_pts_ns,
            "first_host_monotonic_ns": self._record_first_host_ns,
            "last_host_monotonic_ns": self._record_last_host_ns,
            "timestamp_point": "source_pad",
            "encoder": getattr(self, "encoder_name", None),
            "decoder": getattr(self, "decoder_name", None),
        }


class ImuSource(SourceBase):
    name = "imu"
    SENSORS = ("accel", "gyro", "angle")

    def __init__(
        self,
        device: str,
        baudrate: int = 921_600,
        *,
        synthetic: bool = False,
    ) -> None:
        super().__init__()
        self.device = device
        self.baudrate = baudrate
        self.synthetic = synthetic
        self._serial = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._buffer = bytearray()
        self._samples: deque[tuple[int, str, tuple[int, int, int]]] = deque(maxlen=2500)
        self._sample_lock = threading.Lock()
        self._latest: dict[str, tuple[int, int, int]] = {}
        self._record_active = False
        self._record_session: SessionRecorder | None = None
        self._record_root = None
        self._record_stores: dict[str, Any] = {}
        self._record_ts_stores: dict[str, Any] = {}
        self._record_writer: _ImuZarrWriter | None = None
        self._pending: dict[str, list[list[int]]] = {
            name: [] for name in self.SENSORS
        }
        self._pending_ts: dict[str, list[int]] = {
            name: [] for name in self.SENSORS
        }
        self._last_flush_ns = 0
        self._counts = {name: 0 for name in self.SENSORS}
        self._last_anchor_ns = 0
        self._record_first_host_ns = 0
        self._record_last_host_ns = 0
        self._reconnect_count = 0

    def open(self) -> None:
        self._thread = threading.Thread(
            target=self._run_synthetic if self.synthetic else self._run_serial,
            name="sensor-studio-imu",
            daemon=True,
        )
        self._thread.start()

    def _emit_sample(self, data_type: int, values: tuple[int, int, int]) -> None:
        names = {0x51: "accel", 0x52: "gyro", 0x53: "angle"}
        name = names.get(data_type)
        if name is None:
            return
        mono = monotonic_ns()
        unix = unix_ns()
        with self._sample_lock:
            self._latest[name] = values
            self._samples.append((mono, name, values))
            if not self.ready or self.recovering:
                self.ready = True
                self.error = None
                self.recovering = False
            if self._record_active and self._record_session is not None:
                self._pending[name].append([int(v) for v in values])
                self._pending_ts[name].append(mono)
                self._counts[name] += 1
                if self._record_first_host_ns == 0:
                    self._record_first_host_ns = mono
                self._record_last_host_ns = mono
                total_pending = sum(len(self._pending[s]) for s in self.SENSORS)
                if total_pending >= 256 or mono - self._last_flush_ns >= 250_000_000:
                    self._enqueue_pending_locked()
                if mono - self._last_anchor_ns >= 1_000_000_000:
                    self._record_session.add_anchor("imu", self._counts[name], mono, mono, unix)
                    self._last_anchor_ns = mono

    def _run_serial(self) -> None:
        import serial

        try:
            while not self._stop.is_set():
                if self._serial is None:
                    try:
                        self._serial = serial.Serial(
                            self.device, self.baudrate, timeout=0.2
                        )
                        self._buffer.clear()
                        log.info("IMU serial connected at %s", self.device)
                    except Exception as exc:
                        with self._state_lock:
                            self.ready = False
                            self.recovering = True
                            self.error = f"IMU等待连接: {exc}"
                        self._stop.wait(0.5)
                        continue
                try:
                    waiting = getattr(self._serial, "in_waiting", 0)
                    chunk = self._serial.read(max(1, waiting))
                    if not chunk:
                        continue
                    self._buffer.extend(chunk)
                    self._parse_buffer()
                except (OSError, serial.SerialException) as exc:
                    if self._stop.is_set():
                        break
                    self._reconnect_count += 1
                    with self._state_lock:
                        self.ready = False
                        self.recovering = True
                        self.error = f"IMU重连中: {exc}"
                    if (
                        self._record_session is not None
                        and self._record_active
                    ):
                        self._record_session.log(
                            f"IMU serial reconnecting after error: {exc}"
                        )
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                    self._serial = None
                    self._stop.wait(0.5)
        except Exception as exc:
            if not self._stop.is_set():
                self._set_error(f"IMU数据流异常: {exc}")

    def _run_synthetic(self) -> None:
        phase = 0.0
        while not self._stop.wait(0.005):
            phase += 0.035
            self._emit_sample(0x51, (int(3000 * math.sin(phase)), int(2500 * math.cos(phase)), 16000))
            self._emit_sample(0x52, (int(10000 * math.cos(phase)), int(8000 * math.sin(phase)), int(3000 * math.sin(phase * 0.5))))
            self._emit_sample(0x53, (int(9000 * math.sin(phase * 0.3)), int(5000 * math.cos(phase * 0.2)), int(12000 * math.sin(phase * 0.1))))

    def _parse_buffer(self) -> None:
        while len(self._buffer) >= 11:
            if self._buffer[0] != 0x55:
                self._buffer.pop(0)
                continue
            packet = bytes(self._buffer[:11])
            if (sum(packet[:10]) & 0xFF) != packet[10]:
                self._buffer.pop(0)
                continue
            del self._buffer[:11]
            data_type = packet[1]
            values = struct.unpack("<hhh", packet[2:8])
            self._emit_sample(data_type, values)

    def get_plot_samples(self) -> list[tuple[int, str, tuple[int, int, int]]]:
        with self._sample_lock:
            return list(self._samples)

    def latest_values(self) -> dict[str, tuple[int, int, int]]:
        with self._sample_lock:
            return dict(self._latest)

    def start_recording(self, session: SessionRecorder) -> None:
        assert session.paths is not None
        with self._sample_lock:
            if self._record_active:
                return

        open_started_ns = monotonic_ns()
        session.paths.imu.mkdir(parents=False, exist_ok=False)
        session.log(
            f"IMU zarr opened; "
            f"elapsed_ms={(monotonic_ns() - open_started_ns) / 1e6:.1f}"
        )
        arrays_started_ns = monotonic_ns()
        stores = {}
        ts_stores = {}
        for name in self.SENSORS:
            stores[name] = _ZarrV3ArrayWriter(
                session.paths.imu / name,
                trailing_shape=(3,),
                dtype="<i2",
            )
            ts_stores[name] = _ZarrV3ArrayWriter(
                session.paths.imu / f"{name}_timestamp_ns",
                trailing_shape=(),
                dtype="<i8",
            )

        with self._sample_lock:
            anchor_mono = monotonic_ns()
            anchor_unix = unix_ns()
            _ZarrV3ArrayWriter._write_json_atomic(
                session.paths.imu / "zarr.json",
                {
                    "attributes": {
                        "sensor": "JY931",
                        "device": self.device,
                        "baudrate": self.baudrate,
                        "synthetic": self.synthetic,
                        "sensor_type": list(self.SENSORS),
                        "data_format": "raw int16 X/Y/Z",
                        "timestamp_format": "host monotonic nanoseconds",
                        "clock_anchor_monotonic_ns": anchor_mono,
                        "clock_anchor_unix_ns": anchor_unix,
                        "accel_scale": "raw/32768*16 g",
                        "gyro_scale": "raw/32768*2000 deg/s",
                        "angle_scale": "raw/32768*180 deg",
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "zarr_format": 3,
                    "node_type": "group",
                },
            )
            self._record_root = session.paths.imu
            self._record_stores = stores
            self._record_ts_stores = ts_stores
            self._record_writer = _ImuZarrWriter(
                stores,
                ts_stores,
                self._set_error,
            )
            self._record_session = session
            self._pending = {name: [] for name in self.SENSORS}
            self._pending_ts = {name: [] for name in self.SENSORS}
            self._counts = {name: 0 for name in self.SENSORS}
            self._record_active = True
            self._record_first_host_ns = 0
            self._record_last_host_ns = 0
            self._last_flush_ns = monotonic_ns()
            self._last_anchor_ns = self._last_flush_ns
            session.log(
                f"IMU zarr initialized; "
                f"elapsed_ms={(monotonic_ns() - arrays_started_ns) / 1e6:.1f}"
            )
            session.add_anchor("imu", 0, anchor_mono, anchor_mono, anchor_unix)

    def _enqueue_pending_locked(self) -> None:
        if not self._record_active or self._record_writer is None:
            return
        batch = {
            name: (
                self._pending[name],
                self._pending_ts[name],
            )
            for name in self.SENSORS
            if self._pending[name]
        }
        if not batch:
            return
        try:
            self._record_writer.enqueue(batch)
        except Exception as exc:
            self._set_error(f"IMU Zarr写入失败: {exc}")
            return
        self._pending = {name: [] for name in self.SENSORS}
        self._pending_ts = {name: [] for name in self.SENSORS}
        self._last_flush_ns = monotonic_ns()

    def _flush_pending_locked(self) -> None:
        """Compatibility name retained for the explicit final flush."""
        self._enqueue_pending_locked()

    def stop_recording(self) -> None:
        with self._sample_lock:
            if not self._record_active:
                return
            self._flush_pending_locked()
            self._record_active = False
            writer = self._record_writer
            session = self._record_session
            self._record_writer = None
            self._record_session = None
        error: Exception | None = None
        if writer is not None:
            try:
                writer.close()
            except Exception as exc:
                error = exc
        if session is not None:
            session.log("IMU Zarr writer stopped")
        if error is not None:
            raise error

    def close(self) -> None:
        self.stop_recording()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def stats(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "error": self.error,
            "synthetic": self.synthetic,
            "device": self.device,
            "baudrate": self.baudrate,
            "counts": dict(self._counts),
            "reconnects": self._reconnect_count,
            "first_host_monotonic_ns": self._record_first_host_ns,
            "last_host_monotonic_ns": self._record_last_host_ns,
            "writer": "threaded_queue",
        }

