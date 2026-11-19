from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2


class RecordingError(RuntimeError):
    """Raised when a recording session cannot be created or finalized."""


def monotonic_ns() -> int:
    return time.monotonic_ns()


def unix_ns() -> int:
    return time.time_ns()


def _safe_name(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_")
    return cleaned[:64]


def output_root_is_writable(root: Path) -> tuple[bool, str]:
    try:
        root = root.expanduser()
        if not root.exists():
            return False, f"输出目录不存在: {root}"
        if not root.is_dir():
            return False, f"输出路径不是目录: {root}"
        if not os.access(root, os.W_OK):
            return False, f"输出目录不可写: {root}"
        probe = root / f".sensor-studio-write-test-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True, "存储可写"
    except OSError as exc:
        return False, f"输出目录不可写: {root}: {exc}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SessionPaths:
    session_dir: Path
    manifest: Path
    dvs: Path
    cis: Path
    cis_frames: Path
    imu: Path
    clock_anchors: Path
    sync_window: Path
    log: Path


class SessionRecorder:
    """Creates and finalizes one recording session and its timestamp indexes."""

    def __init__(
        self,
        output_root: Path,
        name: str | None,
        synthetic: bool,
        config: dict[str, Any],
    ) -> None:
        self.output_root = output_root.expanduser()
        self.name = name
        self.synthetic = synthetic
        self.config = _json_safe(config)
        self.paths: SessionPaths | None = None
        self.status = "new"
        self.last_error: str | None = None
        self.errors: list[dict[str, Any]] = []
        self.start_monotonic_ns = 0
        self.start_unix_ns = 0
        self.created_monotonic_ns = 0
        self.created_unix_ns = 0
        self.end_monotonic_ns: int | None = None
        self.end_unix_ns: int | None = None
        self.sync_info: dict[str, Any] | None = None
        self._frames_file = None
        self._anchors_file = None
        self._frames_writer: csv.writer | None = None
        self._anchors_writer: csv.writer | None = None
        self._lock = threading.RLock()

    @classmethod
    def begin(
        cls,
        output_root: Path,
        name: str | None,
        synthetic: bool,
        config: dict[str, Any],
    ) -> SessionRecorder:
        instance = cls(output_root, name, synthetic, config)
        instance._begin()
        return instance

    def _begin(self) -> None:
        writable, message = output_root_is_writable(self.output_root)
        if not writable:
            raise RecordingError(message)

        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        suffix = _safe_name(self.name)
        base_id = f"{stamp}_{suffix}" if suffix else stamp
        session_id = base_id
        counter = 1
        while (self.output_root / session_id).exists():
            counter += 1
            session_id = f"{base_id}_{counter:02d}"

        session_dir = self.output_root / session_id
        paths = SessionPaths(
            session_dir=session_dir,
            manifest=session_dir / "manifest.json",
            dvs=session_dir / "dvs" / "events.raw",
            cis=session_dir / "cis" / "stereo.mkv",
            cis_frames=session_dir / "cis" / "frames.csv",
            imu=session_dir / "imu" / "imu.zarr",
            clock_anchors=session_dir / "sync" / "clock_anchors.csv",
            sync_window=session_dir / "sync" / "window.json",
            log=session_dir / "logs" / "session.log",
        )

        try:
            for directory in ("dvs", "cis", "imu", "sync", "logs"):
                (session_dir / directory).mkdir(parents=True, exist_ok=False)
            paths.log.touch(exist_ok=False)
            self._frames_file = paths.cis_frames.open(
                "w", newline="", encoding="utf-8", buffering=1
            )
            self._anchors_file = paths.clock_anchors.open(
                "w", newline="", encoding="utf-8", buffering=1
            )
            self._frames_writer = csv.writer(self._frames_file)
            self._frames_writer.writerow(
                ["frame_index", "pts_ns", "host_monotonic_ns", "host_unix_ns"]
            )
            self._anchors_writer = csv.writer(self._anchors_file)
            self._anchors_writer.writerow(
                [
                    "stream",
                    "sequence",
                    "device_timestamp_ns",
                    "host_monotonic_ns",
                    "host_unix_ns",
                ]
            )
        except OSError as exc:
            raise RecordingError(f"无法创建会话目录或时间戳文件: {exc}") from exc

        self.paths = paths
        self.created_monotonic_ns = monotonic_ns()
        self.created_unix_ns = unix_ns()
        self.start_monotonic_ns = self.created_monotonic_ns
        self.start_unix_ns = self.created_unix_ns
        self.status = "preparing"
        self.log(f"session created; synthetic={self.synthetic}")
        self._write_manifest()

    def mark_recording_started(
        self,
        *,
        monotonic_ns_value: int | None = None,
        unix_ns_value: int | None = None,
    ) -> None:
        with self._lock:
            self.start_monotonic_ns = (
                monotonic_ns() if monotonic_ns_value is None else monotonic_ns_value
            )
            self.start_unix_ns = (
                unix_ns() if unix_ns_value is None else unix_ns_value
            )
            self.status = "recording"
            self.log("recording started")
            self._write_manifest()

    def mark_recording_stopped(
        self,
        *,
        monotonic_ns_value: int,
        unix_ns_value: int,
    ) -> None:
        with self._lock:
            self.end_monotonic_ns = monotonic_ns_value
            self.end_unix_ns = unix_ns_value
            self.log("recording stop requested")
            self._write_manifest()

    def set_sync_info(self, sync_info: dict[str, Any]) -> None:
        with self._lock:
            self.sync_info = _json_safe(sync_info)

    def add_cis_frame(
        self,
        frame_index: int,
        pts_ns: int,
        host_monotonic_ns: int,
        host_unix_ns_value: int,
    ) -> None:
        with self._lock:
            if self._frames_writer is None:
                return
            self._frames_writer.writerow(
                [frame_index, pts_ns, host_monotonic_ns, host_unix_ns_value]
            )

    def add_anchor(
        self,
        stream: str,
        sequence: int,
        device_timestamp_ns: int,
        host_monotonic_ns: int,
        host_unix_ns_value: int,
    ) -> None:
        with self._lock:
            if self._anchors_writer is None:
                return
            self._anchors_writer.writerow(
                [
                    stream,
                    sequence,
                    device_timestamp_ns,
                    host_monotonic_ns,
                    host_unix_ns_value,
                ]
            )

    def add_error(self, stream: str, message: str) -> None:
        with self._lock:
            self.errors.append(
                {
                    "stream": stream,
                    "message": message,
                    "unix_ns": unix_ns(),
                    "monotonic_ns": monotonic_ns(),
                }
            )
            self.last_error = message
            self.log(f"ERROR {stream}: {message}")

    def log(self, message: str) -> None:
        with self._lock:
            if self.paths is None:
                return
            timestamp = datetime.now(timezone.utc).isoformat()
            with self.paths.log.open("a", encoding="utf-8") as handle:
                handle.write(f"{timestamp} {message}\n")

    def _write_manifest(self, source_stats: dict[str, Any] | None = None) -> None:
        if self.paths is None:
            return
        files: dict[str, Any] = {}
        for key in (
            "dvs",
            "cis",
            "cis_frames",
            "imu",
            "clock_anchors",
            "sync_window",
        ):
            path = getattr(self.paths, key)
            files[key] = {
                "path": os.path.relpath(path, self.paths.session_dir),
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() and path.is_file() else None,
            }

        payload = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.paths.session_dir.name,
            "session_dir": str(self.paths.session_dir),
            "synthetic": self.synthetic,
            "name": self.name,
            "status": self.status,
            "created_at_utc": datetime.fromtimestamp(
                self.created_unix_ns / 1e9, tz=timezone.utc
            ).isoformat(),
            "started_at_utc": datetime.fromtimestamp(
                self.start_unix_ns / 1e9, tz=timezone.utc
            ).isoformat(),
            "ended_at_utc": (
                datetime.fromtimestamp(
                    self.end_unix_ns / 1e9, tz=timezone.utc
                ).isoformat()
                if self.end_unix_ns is not None
                else None
            ),
            "start_monotonic_ns": self.start_monotonic_ns,
            "start_unix_ns": self.start_unix_ns,
            "created_monotonic_ns": self.created_monotonic_ns,
            "created_unix_ns": self.created_unix_ns,
            "end_monotonic_ns": self.end_monotonic_ns,
            "end_unix_ns": self.end_unix_ns,
            "config": self.config,
            "files": files,
            "streams": source_stats or {},
            "clock_anchor": {
                "monotonic_ns": self.start_monotonic_ns,
                "unix_ns": self.start_unix_ns,
            },
            "synchronization": self.sync_info or {},
            "errors": self.errors,
            "last_error": self.last_error,
        }
        temporary = self.paths.manifest.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.paths.manifest)

    def finish(
        self,
        status: str,
        source_stats: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            if self.paths is None:
                return
            if status not in {"complete", "failed"}:
                raise ValueError(f"invalid final status: {status}")
            if self.end_monotonic_ns is None:
                self.end_monotonic_ns = monotonic_ns()
                self.end_unix_ns = unix_ns()
            self.status = status
            self.log(f"finalizing with status={status}")
            if error:
                self.last_error = error
            if self.sync_info is not None:
                self.paths.sync_window.write_text(
                    json.dumps(self.sync_info, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            for handle in (self._frames_file, self._anchors_file):
                if handle is not None:
                    handle.flush()
                    os.fsync(handle.fileno())
            self._write_manifest(source_stats)
            for handle in (self._frames_file, self._anchors_file):
                if handle is not None:
                    handle.close()
            self._frames_file = None
            self._anchors_file = None

    @property
    def session_dir(self) -> Path | None:
        return self.paths.session_dir if self.paths is not None else None

