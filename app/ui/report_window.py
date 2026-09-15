"""Small reporting extension; the existing measurement window/worker stay intact."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPushButton, QToolButton, QVBoxLayout,
)

from app.reporting_data import ReportCancelled, ReportRequest, ReportTarget, build_monitoring_report, closed_second
from app.reporting_render import write_monitoring_html, write_monitoring_pdf
from app.storage.atomic_write import atomic_write_path
from app.ui.main_window import (
    MainWindow, TARGET_GRAPH_STATE_LABELS, _datetime_from_qdatetime, _qdatetime_from_datetime,
)
from app.utils.app_paths import user_exports_directory
from app.utils.filename import default_export_path


class MonitoringExportWorker(QThread):
    status_message = Signal(str)
    export_completed = Signal(str)
    error_message = Signal(str)

    def __init__(self, request: ReportRequest, path: Path, kind: str, parent=None):
        super().__init__(parent)
        self.request = request
        self.path = path
        self.kind = kind

    def request_cancel(self) -> None:
        self.requestInterruption()

    def run(self) -> None:
        try:
            self.status_message.emit("선택한 IP의 보고서 데이터를 집계하고 있습니다.")
            report = build_monitoring_report(
                self.request, cancelled=self.isInterruptionRequested, progress=self.status_message.emit,
            )
            if self.kind == "html":
                write_monitoring_html(self.path, report, cancelled=self.isInterruptionRequested)
            elif self.kind == "pdf":
                write_monitoring_pdf(self.path, report, cancelled=self.isInterruptionRequested)
            else:
                raise ValueError("지원하지 않는 보고서 형식입니다.")
        except ReportCancelled:
            self.status_message.emit("보고서 저장 취소됨")
            return
        except Exception as exc:
            logging.getLogger(__name__).exception("Monitoring report export failed")
            self.error_message.emit(f"REPORT_EXPORT_FAILED: {exc}")
            return
        self.export_completed.emit(str(self.path))


class ReportDialog(QDialog):
    def __init__(self, targets: tuple[ReportTarget, ...], selected: list[str], visible_range, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ping 관측 보고서 저장")
        self.resize(640, 620)
        layout = QVBoxLayout(self)
        note = QLabel("선택한 IP의 그래프와 통계를 한 파일로 저장합니다.\n측정은 계속되며 외부 서버로 데이터를 보내지 않습니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(QLabel("저장할 IP (화면 아래에 있는 IP도 모두 선택할 수 있습니다)"))
        self.targets = QListWidget()
        self.targets.setAccessibleName("보고서에 포함할 IP")
        for target in targets:
            label = target.address + (f"  ·  {target.alias}" if target.alias else "")
            if target.paused:
                label += "  [일시중지]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, target.address)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if target.address in selected else Qt.Unchecked)
            self.targets.addItem(item)
        layout.addWidget(self.targets, 1)
        selection = QHBoxLayout()
        for label, checked in (("전체 선택", True), ("선택 해제", False)):
            button = QPushButton(label)
            button.clicked.connect(lambda _value=False, state=checked: self.select_all(state))
            selection.addWidget(button)
        layout.addLayout(selection)
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItem("HTML5 · 단일 오프라인 파일", "html")
        self.kind.addItem("PDF · 페이지별 보고서", "pdf")
        self.scope = QComboBox()
        self.scope.addItem("현재 그래프 구간 (이 창을 연 시점)", "visible")
        self.scope.addItem("측정 전체 (시작부터 저장 시점까지)", "all")
        self.scope.addItem("시작·종료 시각 직접 지정", "custom")
        self.start = QDateTimeEdit(_qdatetime_from_datetime(visible_range[0]))
        self.end = QDateTimeEdit(_qdatetime_from_datetime(visible_range[1]))
        for editor in (self.start, self.end):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            editor.setEnabled(False)
        self.scope.currentIndexChanged.connect(self.sync_range)
        form.addRow("파일 형식", self.kind)
        form.addRow("시간 구간", self.scope)
        form.addRow("시작 시각", self.start)
        form.addRow("종료 시각", self.end)
        layout.addLayout(form)
        precision = QLabel("기존 기록의 초 단위 정밀도 때문에, 저장 요청 시각의 진행 중인 1초는 제외합니다.\n일시중지 IP는 포함하되 중지·미측정 표본은 손실률 계산에서 제외합니다.")
        precision.setWordWrap(True)
        layout.addWidget(precision)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def select_all(self, checked: bool) -> None:
        for index in range(self.targets.count()):
            self.targets.item(index).setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def selected_addresses(self) -> list[str]:
        return [
            str(self.targets.item(index).data(Qt.UserRole))
            for index in range(self.targets.count())
            if self.targets.item(index).checkState() == Qt.Checked
        ]

    def sync_range(self, *_args) -> None:
        custom = self.scope.currentData() == "custom"
        self.start.setEnabled(custom)
        self.end.setEnabled(custom)

    def accept(self) -> None:
        if not self.selected_addresses():
            QMessageBox.information(self, "저장 대상", "IP를 한 개 이상 선택하세요.")
            return
        if self.scope.currentData() == "custom" and self.start.dateTime() > self.end.dateTime():
            QMessageBox.information(self, "시간 구간", "종료 시각은 시작 시각보다 늦어야 합니다.")
            return
        super().accept()


class ReportMainWindow(MainWindow):
    def __init__(self, worker_factory=None):
        super().__init__(worker_factory=worker_factory)
        self.monitor_report_button = QPushButton("보고서 저장")
        self.monitor_report_button.setObjectName("monitorReportButton")
        self.monitor_report_button.setAccessibleName("개별 또는 전체 IP 보고서 저장")
        self.monitor_report_button.setToolTip("선택한 IP 또는 모든 IP의 그래프·통계를 HTML5/PDF로 저장합니다.")
        self.monitor_report_button.clicked.connect(lambda: self.open_monitor_report())
        command_row = self.controls_panel.layout().itemAt(0).layout()
        command_row.insertWidget(max(0, command_row.count() - 2), self.monitor_report_button)
        self._sync_report_enabled(self._has_export_data())

    def _create_target_graph_row(self, address: str, *, use_primary_graph: bool) -> None:
        super()._create_target_graph_row(address, use_primary_graph=use_primary_graph)
        row = self.target_graph_rows[address]
        button = QToolButton(row)
        button.setText("저장")
        button.setObjectName("targetReportButton")
        button.setAccessibleName(f"{address} 그래프 및 보고서 저장")
        button.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(button)
        report_action = menu.addAction("이 IP HTML5 / PDF 보고서")
        report_action.triggered.connect(lambda _checked=False, target=address: self.open_monitor_report([target]))
        png_action = menu.addAction("현재 행 PNG 캡처")
        png_action.triggered.connect(lambda _checked=False, target=address: self.capture_target_png(target))
        button.setMenu(menu)
        row.layout().itemAt(0).layout().itemAt(4).layout().addWidget(button)
        button.setEnabled(self.export_worker is None)

    def _sync_report_enabled(self, enabled: bool) -> None:
        enabled = enabled and self.export_worker is None
        button = getattr(self, "monitor_report_button", None)
        if button is not None:
            button.setEnabled(enabled)
        for button in self.findChildren(QToolButton, "targetReportButton"):
            button.setEnabled(enabled)

    def _set_export_enabled(self, enabled: bool) -> None:
        super()._set_export_enabled(enabled)
        self._sync_report_enabled(enabled)

    def _set_exporting(self, exporting: bool) -> None:
        super()._set_exporting(exporting)
        self._sync_report_enabled(not exporting and self._has_export_data())

    def _report_targets(self) -> tuple[ReportTarget, ...]:
        snapshots = {item.address: item for item in self.target_snapshots if item.address}
        addresses = list(dict.fromkeys(self.current_targets or list(snapshots)))
        base_interval = int(self.interval_combo.currentText() or "1")
        result = []
        for address in addresses:
            snapshot = snapshots.get(address)
            state = self._target_graph_health_state(address, snapshot, [])
            result.append(ReportTarget(
                address, self.target_aliases.get(address, ""),
                self._is_target_paused(address, snapshot),
                self.target_interval_overrides.get(address, base_interval),
                TARGET_GRAPH_STATE_LABELS.get(state, "대기"),
            ))
        return tuple(result)

    def _report_busy(self) -> bool:
        # A finished QThread may still have its finished signal queued.
        return self.export_worker is not None or self.session_archive_worker is not None

    def open_monitor_report(self, selected: list[str] | None = None) -> None:
        if self._report_busy():
            QMessageBox.information(self, "보고서", "이미 저장 작업이 진행 중입니다.")
            return
        targets = self._report_targets()
        if not targets:
            QMessageBox.information(self, "보고서", "먼저 IP를 등록하고 측정을 시작하세요.")
            return
        now = datetime.now()
        visible = self._main_graph_visible_time_range(self._main_graph_data_bounds())
        visible = visible or (now - timedelta(minutes=10), now)
        dialog = ReportDialog(targets, selected if selected is not None else [item.address for item in targets], visible, self)
        if dialog.exec() != QDialog.Accepted:
            return
        chosen = set(dialog.selected_addresses())
        kind = str(dialog.kind.currentData())
        suffix = "html" if kind == "html" else "pdf"
        name = next(iter(chosen)) if len(chosen) == 1 else f"selected_{len(chosen)}_targets"
        default = default_export_path(name, suffix, user_exports_directory())
        try:
            default.parent.mkdir(parents=True, exist_ok=True)
            selected_path, _ = QFileDialog.getSaveFileName(
                self, "보고서 저장", str(default), f"{suffix.upper()} 파일 (*.{suffix})",
                options=QFileDialog.DontUseNativeDialog,
            )
            if not selected_path:
                return
            now = datetime.now()
            cutoff = closed_second(now)
            scope = str(dialog.scope.currentData())
            start = None if scope == "all" else _datetime_from_qdatetime(dialog.start.dateTime()).replace(microsecond=0)
            end = cutoff if scope == "all" else min(cutoff, _datetime_from_qdatetime(dialog.end.dateTime()).replace(microsecond=0))
            request = ReportRequest(
                targets=tuple(item for item in self._report_targets() if item.address in chosen),
                start=start, end=end, generated_at=now.astimezone(),
                session_path=self.session_log_path,
                live_tail=tuple(self.live_graph_observations or self.observations),
                scope_label=str(dialog.scope.currentText()),
                timezone_label="로컬 기록 시각 · " + now.astimezone().strftime("%Z / UTC%z"),
            )
            self.start_monitor_export(request, Path(selected_path).with_suffix(f".{suffix}"), kind)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "보고서 저장 오류", str(exc))

    def start_monitor_export(self, request: ReportRequest, path: Path, kind: str) -> None:
        if self._report_busy():
            raise ValueError("이미 저장 작업이 진행 중입니다.")
        if request.start is not None and request.start > request.end:
            raise ValueError("선택한 구간에 완료된 초 단위 기록이 없습니다.")
        worker = MonitoringExportWorker(request, path, kind, parent=self)
        self.export_worker = worker
        worker.status_message.connect(self.on_export_status)
        worker.export_completed.connect(self.on_export_completed)
        worker.error_message.connect(self.on_export_error)
        worker.finished.connect(self.on_export_finished)
        self._set_exporting(True)
        worker.start()

    def capture_target_png(self, address: str) -> None:
        if self._report_busy() or address not in self.target_graph_rows:
            return
        default = default_export_path(address, "png", user_exports_directory())
        try:
            default.parent.mkdir(parents=True, exist_ok=True)
            selected, _ = QFileDialog.getSaveFileName(self, "이 IP 현재 행 캡처", str(default), "PNG 파일 (*.png)", options=QFileDialog.DontUseNativeDialog)
            if not selected:
                return
            self._request_graph_render(force=True)
            pixmap = self.target_graph_rows[address].grab()
            path = Path(selected).with_suffix(".png")
            def save(temp: Path) -> None:
                if pixmap.isNull() or not pixmap.save(str(temp), "PNG"):
                    raise OSError("PNG 캡처를 저장하지 못했습니다.")
            atomic_write_path(path, save)
            self.status_label.setText(f"PNG 저장 완료: {path}")
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "캡처 오류", str(exc))
