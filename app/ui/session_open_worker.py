from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.core.models import HopObservation
from app.core.observation_stats import FocusSnapshotBuilder, FocusSnapshotSet
from app.storage.session_index import (
    SESSION_RECOVERED_WITH_SKIPPED_ROWS_CODE,
    SessionIndexStore,
    TraceSessionRecord,
    session_index_root_for_sample_path,
)
from app.storage.session_log import (
    SessionLogReadSummary,
    read_observations_with_summary,
    session_log_segments,
)
from app.utils.diagnostics import operation_failure

SESSION_OPEN_FAILED_CODE = "SESSION_OPEN_FAILED"
SESSION_OPEN_RECENT_OBSERVATION_LIMIT = 25_000


class _SessionOpenCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionOpenResult:
    record: TraceSessionRecord
    observations: list[HopObservation]
    summary: SessionLogReadSummary
    snapshot_set: FocusSnapshotSet


class SessionOpenWorker(QThread):
    """Read saved-session statistics while retaining only recent UI samples."""

    loaded = Signal(int, object)
    error_message = Signal(int, str)

    def __init__(self, *, request_id: int, record: TraceSessionRecord, parent=None) -> None:
        super().__init__(parent)
        self.request_id = request_id
        self.record = record
        self._cancel_requested = threading.Event()

    def run(self) -> None:
        try:
            if self.record.sample_path.is_dir():
                # Windows reports this as PermissionError while POSIX commonly
                # raises IsADirectoryError. Normalize the public diagnostic so
                # session-open failures stay stable across supported runners.
                raise PermissionError("session sample path points to a directory")
            snapshot_builder = FocusSnapshotBuilder()
            bounds: list[datetime | None] = [None, None]
            observations, summary = read_observations_with_summary(
                self.record.sample_path,
                retained_limit=SESSION_OPEN_RECENT_OBSERVATION_LIMIT,
                on_observation=lambda observation: self._collect_observation(
                    snapshot_builder,
                    observation,
                    bounds,
                ),
            )
            self._raise_if_cancelled()
            snapshot_set = snapshot_builder.build(current_target=self.record.target)
            self._raise_if_cancelled()
            record = self._reconciled_record(summary, bounds)
            self._persist_reconciled_record(record)
            self._raise_if_cancelled()
        except _SessionOpenCancelled:
            return
        except OSError as exc:
            operation_failure(
                SESSION_OPEN_FAILED_CODE,
                "session_open.read",
                exc,
                target=self.record.target,
                session_path=self.record.sample_path,
            )
            self.error_message.emit(self.request_id, f"{SESSION_OPEN_FAILED_CODE}: {type(exc).__name__}")
            return
        except Exception as exc:
            operation_failure(
                SESSION_OPEN_FAILED_CODE,
                "session_open.read",
                exc,
                target=self.record.target,
                session_path=self.record.sample_path,
            )
            self.error_message.emit(self.request_id, f"{SESSION_OPEN_FAILED_CODE}: {type(exc).__name__}")
            return
        self.loaded.emit(
            self.request_id,
            SessionOpenResult(record, observations, summary, snapshot_set),
        )

    def request_cancel(self) -> None:
        self._cancel_requested.set()
        self.requestInterruption()

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested.is_set() or self.isInterruptionRequested():
            raise _SessionOpenCancelled

    def _collect_observation(
        self,
        snapshot_builder: FocusSnapshotBuilder,
        observation: HopObservation,
        bounds: list[datetime | None],
    ) -> None:
        self._raise_if_cancelled()
        snapshot_builder.add(observation)
        bounds[0] = observation.timestamp if bounds[0] is None else min(bounds[0], observation.timestamp)
        bounds[1] = observation.timestamp if bounds[1] is None else max(bounds[1], observation.timestamp)

    def _reconciled_record(
        self,
        summary: SessionLogReadSummary,
        bounds: list[datetime | None],
    ) -> TraceSessionRecord:
        first_timestamp, last_timestamp = bounds
        last_error = self.record.last_error
        if summary.skipped_rows:
            files = ", ".join(path.name for path in summary.skipped_row_files[:3])
            suffix = f"; files={files}" if files else ""
            last_error = f"{SESSION_RECOVERED_WITH_SKIPPED_ROWS_CODE}: skipped_rows={summary.skipped_rows}{suffix}"
        return replace(
            self.record,
            start=min(self.record.start, first_timestamp) if first_timestamp is not None else self.record.start,
            end=(
                max(self.record.end, last_timestamp)
                if self.record.end is not None and last_timestamp is not None
                else last_timestamp or self.record.end
            ),
            samples=summary.rows,
            segments=tuple(session_log_segments(self.record.sample_path)),
            last_error=last_error,
        )

    def _persist_reconciled_record(self, record: TraceSessionRecord) -> None:
        if record.end is None:
            return
        try:
            store = SessionIndexStore.create(session_index_root_for_sample_path(record.sample_path))
            store.finish_session(
                record.session_id,
                state=record.state,
                ended_at=record.end,
                segments=list(record.segments),
                last_error=record.last_error,
                samples=record.samples,
            )
        except Exception as exc:
            operation_failure(
                "SESSION_INDEX_WRITE_FAILED",
                "session_open.reconcile_index",
                exc,
                target=record.target,
                session_path=record.sample_path,
            )
