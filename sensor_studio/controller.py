from __future__ import annotations

import enum
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from .recording import RecordingError, SessionRecorder, output_root_is_writable


log = logging.getLogger(__name__)


class StudioState(str, enum.Enum):
    STARTING = "STARTING"
    PREVIEW = "PREVIEW"
    RECORDING = "RECORDING"
    FINALIZING = "FINALIZING"
    FAULT = "FAULT"
    EXIT = "EXIT"


class RecordableSource(Protocol):
    name: str
    ready: bool
    error: str | None
    recovering: bool

    def start_recording(self, session: SessionRecorder) -> None: ...

    def stop_recording(self) -> None: ...

    def close(self) -> None: ...

    def stats(self) -> dict[str, Any]: ...


class RecordingController:
    def __init__(
        self,
        *,
        sources: list[RecordableSource],
        output_root: Path,
        name: str | None,
        synthetic: bool,
        config: dict[str, Any],
        storage_check=output_root_is_writable,
        preroll_seconds: float = 0.5,
    ) -> None:
        self.sources = sources
        self.output_root = output_root
        self.name = name
        self.synthetic = synthetic
        self.config = config
        self.storage_check = storage_check
        self.preroll_seconds = max(0.0, float(preroll_seconds))
        self.state = StudioState.STARTING
        self.session: SessionRecorder | None = None
        self.message = "正在初始化"
        self.recording_start_monotonic_ns: int | None = None
        self._exit_finalized = False
        self._storage_ready = False
        self._storage_message = "存储尚未检查"
        self._last_storage_check_ns = 0

    def mark_started(self) -> None:
        if self.state == StudioState.STARTING:
            self.state = StudioState.PREVIEW
            self.message = "按空格开始录制"
            self.refresh_storage(force=True)

    @property
    def all_sources_ready(self) -> bool:
        return all(source.ready for source in self.sources)

    @property
    def storage_ready(self) -> bool:
        return self._storage_ready

    @property
    def storage_message(self) -> str:
        return self._storage_message

    def refresh_storage(self, force: bool = False) -> None:
        now = time.monotonic_ns()
        if not force and now - self._last_storage_check_ns < 2_000_000_000:
            return
        self._storage_ready, self._storage_message = self.storage_check(self.output_root)
        self._last_storage_check_ns = now

    @property
    def ready(self) -> bool:
        return self.state == StudioState.PREVIEW and self.all_sources_ready and self.storage_ready

    @property
    def elapsed_seconds(self) -> float:
        if self.recording_start_monotonic_ns is None:
            return 0.0
        return max(0.0, (time.monotonic_ns() - self.recording_start_monotonic_ns) / 1e9)

    def source_stats(self) -> dict[str, Any]:
        return {source.name: source.stats() for source in self.sources}

    def start_recording(self) -> bool:
        if self.state == StudioState.RECORDING:
            self.message = "已经在录制"
            return False
        if self.state != StudioState.PREVIEW:
            self.message = f"当前状态不能开始录制: {self.state.value}"
            return False
        if not self.all_sources_ready:
            unavailable = [
                f"{source.name}: {source.error or '未就绪'}"
                for source in self.sources
                if not source.ready
            ]
            self.message = "传感器未就绪: " + " | ".join(unavailable)
            return False

        self.refresh_storage(force=True)
        storage_ok, storage_message = self._storage_ready, self._storage_message
        if not storage_ok:
            self.message = storage_message
            return False

        started_sources: list[RecordableSource] = []
        try:
            self.session = SessionRecorder.begin(
                output_root=self.output_root,
                name=self.name,
                synthetic=self.synthetic,
                config=self.config,
            )
            sources_to_start = sorted(
                self.sources,
                key=lambda source: 0 if source.name == "imu" else 1,
            )
            for source in sources_to_start:
                started_ns = time.monotonic_ns()
                self.session.log(f"starting source: {source.name}")
                source.start_recording(self.session)
                self.session.log(
                    f"started source: {source.name}; "
                    f"elapsed_ms={(time.monotonic_ns() - started_ns) / 1e6:.1f}"
                )
                started_sources.append(source)
            preroll_ns = int(self.preroll_seconds * 1e9)
            self.recording_start_monotonic_ns = time.monotonic_ns() + preroll_ns
            start_unix_ns = time.time_ns() + preroll_ns
            self.session.mark_recording_started(
                monotonic_ns_value=self.recording_start_monotonic_ns,
                unix_ns_value=start_unix_ns,
            )
            self.session.log(
                "all source recorders started; "
                f"preroll_ms={self.preroll_seconds * 1000:.1f}; "
                f"sdk_ready={self.all_sources_ready}"
            )
            unavailable = [
                f"{source.name}: {source.error or '未就绪'}"
                for source in self.sources
                if not source.ready and not source.recovering
            ]
            if unavailable:
                raise RuntimeError(
                    "启动录制期间有传感器退出: " + " | ".join(unavailable)
                )
            recovering = [
                source.name for source in self.sources if source.recovering
            ]
            if recovering:
                self.session.log(
                    "recording started while sources recover: "
                    + ",".join(recovering)
                )
            self.state = StudioState.RECORDING
            self.message = (
                f"正在录制，预热 {self.preroll_seconds:.1f}s 后进入共同起点"
                if self.preroll_seconds > 0
                else "正在录制，按 Q 停止并退出"
            )
            log.info("Recording started in %s", self.session.session_dir)
            return True
        except Exception as exc:
            error = f"启动录制失败: {exc}"
            log.exception(error)
            for source in reversed(started_sources):
                try:
                    source.stop_recording()
                except Exception:
                    log.exception("Stopping %s after start failure failed", source.name)
            if self.session is not None:
                self.session.add_error("session", error)
                self.session.finish(
                    "failed",
                    source_stats=self.source_stats(),
                    error=error,
                )
            self.state = StudioState.FAULT
            self.message = error
            return False

    def finalize(self, requested_status: str = "complete", error: str | None = None) -> None:
        if self._exit_finalized:
            return
        previous_state = self.state
        if previous_state in {StudioState.EXIT}:
            return
        self.state = StudioState.FINALIZING
        self.message = "正在停止并保存数据"
        errors: list[str] = []
        stop_monotonic_ns = time.monotonic_ns()
        stop_unix_ns = time.time_ns()
        if previous_state == StudioState.RECORDING:
            if self.session is not None:
                self.session.mark_recording_stopped(
                    monotonic_ns_value=stop_monotonic_ns,
                    unix_ns_value=stop_unix_ns,
                )
            with ThreadPoolExecutor(
                max_workers=max(1, len(self.sources)),
                thread_name_prefix="sensor-studio-stop",
            ) as executor:
                futures = {}
                for source in self.sources:
                    if self.session is not None:
                        self.session.log(f"stopping source: {source.name}")
                    futures[executor.submit(source.stop_recording)] = source
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        log.exception("Stopping %s failed", source.name)
                        errors.append(f"{source.name}: {exc}")
                    else:
                        if self.session is not None:
                            self.session.log(f"stopped source: {source.name}")

        for source in self.sources:
            try:
                source.close()
            except Exception as exc:
                log.exception("Closing %s failed", source.name)
                errors.append(f"{source.name}: {exc}")

        final_status = requested_status
        final_error = error
        if errors:
            final_status = "failed"
            final_error = "; ".join(errors)

        source_stats = self.source_stats()
        if self.session is not None and self.session.paths is not None:
            sync_info = self._build_sync_info(
                source_stats=source_stats,
                stop_monotonic_ns=stop_monotonic_ns,
                stop_unix_ns=stop_unix_ns,
            )
            self.session.set_sync_info(sync_info)
            self.session.log(
                f"stop requested; previous_state={previous_state.value}"
            )
            for item in errors:
                self.session.add_error("shutdown", item)
            self.session.finish(
                final_status,
                source_stats=source_stats,
                error=final_error,
            )

        self.state = StudioState.FAULT if final_status == "failed" else StudioState.EXIT
        self.message = final_error or "录制完成"
        self._exit_finalized = True
        log.info("Recording finalized with status %s", final_status)

    def _build_sync_info(
        self,
        *,
        source_stats: dict[str, Any],
        stop_monotonic_ns: int,
        stop_unix_ns: int,
    ) -> dict[str, Any]:
        stream_ranges: dict[str, dict[str, int | None]] = {}
        first_values: list[int] = []
        last_values: list[int] = []
        for name, stats in source_stats.items():
            first = stats.get("first_host_monotonic_ns")
            last = stats.get("last_host_monotonic_ns")
            first_int = int(first) if first else None
            last_int = int(last) if last else None
            stream_ranges[name] = {
                "first_host_monotonic_ns": first_int,
                "last_host_monotonic_ns": last_int,
            }
            if first_int is not None:
                first_values.append(first_int)
            if last_int is not None:
                last_values.append(last_int)

        planned_start = (
            int(self.recording_start_monotonic_ns)
            if self.recording_start_monotonic_ns is not None
            else None
        )
        common_start = (
            max([planned_start, *first_values])
            if planned_start is not None and first_values
            else planned_start
        )
        common_end = min([stop_monotonic_ns, *last_values]) if last_values else None
        common_duration_ns = (
            max(0, common_end - common_start)
            if common_start is not None and common_end is not None
            else 0
        )
        uncovered_streams = [
            name
            for name, values in stream_ranges.items()
            if values["first_host_monotonic_ns"] is None
            or values["last_host_monotonic_ns"] is None
            or common_start is None
            or values["first_host_monotonic_ns"] > common_start
            or values["last_host_monotonic_ns"] < common_start
        ]
        valid = bool(
            common_start is not None
            and common_end is not None
            and common_end > common_start
            and not uncovered_streams
        )
        return {
            "schema_version": 1,
            "mode": "software_coordinated_preroll",
            "hardware_trigger": False,
            "preroll_seconds": self.preroll_seconds,
            "planned_start_monotonic_ns": planned_start,
            "stop_request_monotonic_ns": stop_monotonic_ns,
            "stop_request_unix_ns": stop_unix_ns,
            "common_start_monotonic_ns": common_start,
            "common_end_monotonic_ns": common_end,
            "common_duration_ns": common_duration_ns,
            "valid": valid,
            "uncovered_streams": uncovered_streams,
            "stream_ranges": stream_ranges,
        }

    def tick(self) -> None:
        self.refresh_storage()
        for source in self.sources:
            poll = getattr(source, "poll", None)
            if poll is not None:
                poll()
        if self.state != StudioState.RECORDING:
            return
        if (
            self.recording_start_monotonic_ns is not None
            and time.monotonic_ns() >= self.recording_start_monotonic_ns
            and self.message.startswith("正在录制，预热")
        ):
            self.message = "正在录制，按 Q 停止并退出"
        failed = [
            source
            for source in self.sources
            if not source.ready and not getattr(source, "recovering", False)
        ]
        if failed:
            details = " | ".join(
                f"{source.name}: {source.error or '数据流中断'}" for source in failed
            )
            self.fail_recording(details)

    def fail_recording(self, message: str) -> None:
        if self.state != StudioState.RECORDING:
            return
        log.error("Recording failed: %s", message)
        if self.session is not None:
            self.session.add_error("session", message)
        self.finalize(requested_status="failed", error=message)

    def request_exit(self) -> None:
        if self.state in {StudioState.EXIT, StudioState.FINALIZING}:
            return
        if self.state == StudioState.FAULT:
            self.finalize(requested_status="failed", error=self.message)
        elif self.state == StudioState.RECORDING:
            self.finalize(requested_status="complete")
        else:
            self.finalize(requested_status="complete")

