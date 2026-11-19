"""Persist Fusion observation transitions independently of outbound alert actions."""
from __future__ import annotations

import queue
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QThread, Signal


@dataclass(frozen=True)
class StatusChange:
    observed_at: str
    address: str
    alias: str
    previous: str
    current: str
    description: str


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fusion_status_changes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          observed_at TEXT NOT NULL,
          address TEXT NOT NULL,
          alias TEXT NOT NULL,
          previous TEXT NOT NULL,
          current TEXT NOT NULL,
          description TEXT NOT NULL
        )
    """)
    return conn


def recent_changes(path: Path, limit: int = 200) -> list[StatusChange]:
    """Read only when presenting history; corrupt DBs raise instead of appearing empty."""
    if not path.exists():
        return []
    try:
        db = _connect(path)
        try:
            rows = db.execute(
                "SELECT observed_at,address,alias,previous,current,description "
                "FROM fusion_status_changes ORDER BY id DESC LIMIT ?",
                (max(0, min(limit, 2000)),),
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise ValueError(f"상태 이력 DB 손상/잠금: {exc}; 원본은 보존됩니다.") from exc
    return [StatusChange(*row) for row in rows]


def format_local_time(timestamp: str) -> str:
    try:
        date = datetime.fromisoformat(timestamp)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "시간 확인 필요"


class StatusEventWriter(QThread):
    """Bounded queued SQLite writes; GUI thread never waits for disk per observation."""
    persisted = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, parent=None) -> None:
        super().__init__(parent)
        self.path = path
        self._pending: queue.Queue[StatusChange | None] = queue.Queue(maxsize=1000)
        self._closing = False

    def submit(self, event: StatusChange) -> bool:
        if self._closing or not self.isRunning():
            self.failed.emit("Fusion 이벤트 기록 스레드가 중지되어 저장할 수 없습니다.")
            return False
        try:
            self._pending.put_nowait(event)
            return True
        except queue.Full:
            self.failed.emit("Fusion 이벤트 저장 큐 초과: 관측 이력 저장이 지연되었습니다.")
            return False

    def shutdown(self) -> bool:
        self._closing = True
        if not self.isRunning():
            return True
        try:
            self._pending.put(None, timeout=1)
        except queue.Full:
            self.failed.emit("Fusion 이벤트 저장 대기열이 가득 찼습니다.")
            return False
        return self.wait(5000)

    def run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect(self.path)
            while True:
                item = self._pending.get()
                if item is None:
                    return
                try:
                    connection.execute(
                        "INSERT INTO fusion_status_changes "
                        "(observed_at,address,alias,previous,current,description) "
                        "VALUES (?,?,?,?,?,?)",
                        (item.observed_at, item.address, item.alias,
                         item.previous, item.current, item.description),
                    )
                    connection.commit()
                    self.persisted.emit(item)
                except sqlite3.Error as exc:
                    self.failed.emit(f"Fusion 이벤트 저장 오류: {type(exc).__name__}: {exc}")
        except (OSError, sqlite3.Error) as exc:
            self.failed.emit(f"Fusion 이벤트 저장소 초기화 오류: {type(exc).__name__}: {exc}")
        finally:
            if connection is not None:
                connection.close()
