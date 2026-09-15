"""Reporting UI extension; the existing measurement and graph engine is unchanged."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDateTime, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
    QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from app.reporting.data import ReportCancelled, ReportRequest, TargetInfo, build_report
from app.reporting.render import png_bytes, write_html, write_pdf
from app.storage.atomic_write import atomic_write_path
from app.storage.session_log import iter_observations, session_log_bounds
from app.ui.main_window import MainWindow
from app.utils.app_paths import user_exports_directory
from app.utils.filename import default_export_path


class MonitoringReportWorker(QThread):
    status_message = Signal(str)
    export_completed = Signal(str)
    error_message = Signal(str)

    def __init__(self, request: ReportRequest, path: Path, kind: str, parent=None) -> None:
        super().__init__(parent)
        self.request = request
        self.path = path
        self.kind = kind

    def request_cancel(self) -> None:
        self.requestInterruption()

    def _check_cancel(self) -> None:
        if self.isInterruptionRequested():
            raise ReportCancelled("보고서 저장을 취소했습니다.")

    def run(self) -> None:
        try:
            self._check_cancel()
            self.status_message.emit("보고서 집계 중 (측정은 계속됩니다)...")
            request = self.request
            source_start = request.measurement_start
            disk = ()
            if request.session_path is not None:
                if not request.session_path.is_file():
                    raise OSError("원본 세션 로그를 찾을 수 없습니다. 전체 기록으로 위장해 저장하지 않았습니다.")
                if request.start is None and source_start is None:
                    bounds = session_log_bounds(request.session_path)
                    source_start = bounds[0] if bounds else None
                disk = iter_observations(request.session_path, strict=True)
            report = build_report(request, disk, source_start=source_start, check_cancel=self._check_cancel)
            self.status_message.emit(f"{self.kind.upper()} 보고서 작성 중...")
            if self.kind == "html":
                write_html(self.path, report, self._check_cancel)
            elif self.kind == "pdf":
                write_pdf(self.path, report, self._check_cancel)
            else:
                raise ValueError("지원하지 않는 보고서 형식입니다.")
            # Successful atomic replacement is the commit point. Do not call a
            # late cancellation a failure after a complete file was published.
            self.export_completed.emit(str(self.path))
        except ReportCancelled:
            self.status_message.emit("보고서 저장 취소: 기존 파일은 변경하지 않았습니다.")
        except Exception as exc:
            self.error_message.emit(f"REPORT_EXPORT_FAILED: {type(exc).__name__}: {exc}")


class ReportOptionsDialog(QDialog):
    def __init__(self, targets: list[TargetInfo], visible_range: tuple[datetime, datetime], selected: str | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("모니터링 보고서 저장")
        self.resize(580, 610)
        layout = QVBoxLayout(self)
        note = QLabel("체크한 IP만 저장합니다. 전체 IP와 전체 측정 시간은 별개입니다.\n저장 중에도 Ping 측정은 계속됩니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.targets = QListWidget()
        self.targets.setAccessibleName("보고서에 포함할 IP")
        for target in targets:
            item = QListWidgetItem(f"{target.address}  {target.alias}")
            item.setData(Qt.ItemDataRole.UserRole, target.address)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if selected is None or selected == target.address else Qt.CheckState.Unchecked)
            self.targets.addItem(item)
        layout.addWidget(self.targets)
        row = QHBoxLayout()
        for label, checked in (("전체 선택", True), ("전체 해제", False)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=checked: self._check_all(value))
            row.addWidget(button)
        layout.addLayout(row)
        self.scope = QComboBox()
        self.scope.addItems(["현재 그래프 구간", "측정 전체", "직접 지정"])
        layout.addWidget(QLabel("저장할 시간 구간"))
        layout.addWidget(self.scope)
        self.start = QDateTimeEdit(QDateTime(visible_range[0]))
        self.end = QDateTimeEdit(QDateTime(visible_range[1]))
        for widget in (self.start, self.end):
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            widget.setCalendarPopup(True)
            widget.setEnabled(False)
            layout.addWidget(widget)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.kind = QComboBox()
        self.kind.addItem("HTML5 단일 파일 (*.html)", "html")
        self.kind.addItem("PDF 보고서 (*.pdf)", "pdf")
        layout.addWidget(QLabel("저장 형식"))
        layout.addWidget(self.kind)
        privacy = QLabel("IP·이름·측정 정보가 파일에 포함됩니다. 외부 공유 전 확인하세요.\n보고서 생성은 로컬에서만 처리하며 외부 서버로 업로드하지 않습니다.")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _check_all(self, checked: bool) -> None:
        for index in range(self.targets.count()):
            self.targets.item(index).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _scope_changed(self, index: int) -> None:
        self.start.setEnabled(index == 2)
        self.end.setEnabled(index == 2)

    def selected_addresses(self) -> tuple[str, ...]:
        return tuple(str(self.targets.item(index).data(Qt.ItemDataRole.UserRole)) for index in range(self.targets.count()) if self.targets.item(index).checkState() == Qt.CheckState.Checked)

    def _validate(self) -> None:
        if not self.selected_addresses():
            QMessageBox.information(self, "대상 선택", "저장할 IP를 한 개 이상 선택하세요.")
            return
        if self.scope.currentIndex() == 2 and self.end.dateTime() < self.start.dateTime():
            QMessageBox.information(self, "시간 구간", "종료 시각은 시작 시각 이후여야 합니다.")
            return
        self.accept()


class ReportMainWindow(MainWindow):
    def __init__(self, worker_factory=None) -> None:
        super().__init__(worker_factory=worker_factory)
        toolbar = QWidget()
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(0, 6, 0, 0)
        self.monitoring_report_button = QPushButton("전체 / 선택 IP 보고서 저장")
        self.monitoring_report_button.setAccessibleName("HTML5 또는 PDF 모니터링 보고서 저장")
        self.monitoring_report_button.clicked.connect(lambda: self.open_monitoring_report())
        self.cancel_report_button = QPushButton("저장 취소")
        self.cancel_report_button.clicked.connect(self.cancel_monitoring_report)
        self.cancel_report_button.setEnabled(False)
        row.addWidget(self.monitoring_report_button)
        row.addWidget(self.cancel_report_button)
        row.addStretch(1)
        self.controls_panel.layout().addWidget(toolbar)
        self._sync_monitoring_report_controls()

    def _create_target_graph_row(self, address: str, *, use_primary_graph: bool) -> None:
        super()._create_target_graph_row(address, use_primary_graph=use_primary_graph)
        row = self.target_graph_rows[address]
        button = QToolButton(row)
        button.setObjectName("monitoringReportButton")
        button.setText("저장")
        button.setAccessibleName(f"{address} 보고서 또는 PNG 저장")
        menu = QMenu(button)
        menu.addAction("HTML5 / PDF 보고서", lambda: self.open_monitoring_report(address))
        menu.addAction("이 IP 화면 PNG 캡처", lambda: self.capture_target_png(address))
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        row.layout().addWidget(button)

    def _set_export_enabled(self, enabled: bool) -> None:
        super()._set_export_enabled(enabled)
        self._sync_monitoring_report_controls()

    def _set_exporting(self, exporting: bool) -> None:
        super()._set_exporting(exporting)
        self._sync_monitoring_report_controls()

    def on_export_finished(self) -> None:
        super().on_export_finished()
        self._sync_monitoring_report_controls()

    def _sync_monitoring_report_controls(self) -> None:
        busy = self.export_worker is not None
        if hasattr(self, "monitoring_report_button"):
            self.monitoring_report_button.setEnabled(bool(self.current_targets) and not busy)
            self.cancel_report_button.setEnabled(isinstance(self.export_worker, MonitoringReportWorker) and busy)
        for frame in self.target_graph_rows.values():
            button = frame.findChild(QToolButton, "monitoringReportButton")
            if button is not None:
                button.setEnabled(not busy)

    def cancel_monitoring_report(self) -> None:
        if isinstance(self.export_worker, MonitoringReportWorker):
            self.export_worker.request_cancel()
            self.cancel_report_button.setEnabled(False)

    def _report_target_info(self) -> list[TargetInfo]:
        targets = list(self.current_targets)
        result = []
        for address in targets:
            status_label = self.target_graph_status_labels.get(address)
            result.append(TargetInfo(
                address=address,
                alias=self.target_aliases.get(address, ""),
                screen_status=status_label.text() if status_label is not None else "대기",
                interval_seconds=self.target_interval_overrides.get(address, int(self.interval_combo.currentText() or "1")),
            ))
        return result

    def _report_visible_range(self, target: str | None = None) -> tuple[datetime, datetime]:
        graph = self.target_graph_widgets.get(target, self.graph)
        visible = graph.visible_datetime_range()
        if visible is not None:
            return visible
        end = self.main_graph_live_end_time or self.live_graph_latest_timestamp or datetime.now()
        return end - timedelta(seconds=self.main_graph_range_seconds), end

    def open_monitoring_report(self, target: str | None = None) -> None:
        if self.export_worker is not None or (self.session_archive_worker and self.session_archive_worker.isRunning()):
            QMessageBox.information(self, "저장 진행 중", "이미 저장 작업이 진행 중입니다.")
            return
        targets = self._report_target_info()
        if not targets:
            return
        dialog = ReportOptionsDialog(targets, self._report_visible_range(target), target, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        kind = str(dialog.kind.currentData())
        addresses = dialog.selected_addresses()
        default = default_export_path(addresses[0] if len(addresses) == 1 else "all_targets", kind, user_exports_directory())
        path_text, _filter = QFileDialog.getSaveFileName(self, "보고서 저장", str(default), f"{kind.upper()} (*.{kind})")
        if not path_text:
            return
        path = Path(path_text).with_suffix(f".{kind}")
        if str(path) != path_text and path.exists():
            if QMessageBox.question(self, "덮어쓰기", f"{path.name} 파일을 덮어쓸까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        captured = datetime.now()
        mode = dialog.scope.currentIndex()
        start, end = self._report_visible_range(target)
        if mode == 1:
            start, end = None, captured
        elif mode == 2:
            start, end = dialog.start.dateTime().toPython(), dialog.end.dateTime().toPython()
            if start > captured or end > captured:
                QMessageBox.information(self, "시간 구간", "미래 시각은 저장 구간으로 선택할 수 없습니다.")
                return
        # Dialogs may run while targets are added/removed. Resolve against the
        # current inventory again and never silently include a removed target.
        current = {info.address: info for info in self._report_target_info()}
        if any(address not in current for address in addresses):
            QMessageBox.information(self, "대상 변경", "선택 도중 대상 목록이 변경되었습니다. 다시 선택하세요.")
            return
        request = ReportRequest(
            targets=tuple(current[address] for address in addresses),
            captured_at=captured, start=start, end=end, scope=dialog.scope.currentText(),
            session_path=self.session_log_path,
            live=tuple(self.live_graph_observations or self.observations),
            measurement_start=self.live_graph_measurement_start,
            running=self._is_worker_running(), font_family=QApplication.font().family(),
        )
        worker = MonitoringReportWorker(request, path, kind, self)
        self.export_worker = worker
        worker.status_message.connect(self.on_export_status)
        worker.export_completed.connect(self.on_export_completed)
        worker.error_message.connect(self.on_export_error)
        worker.finished.connect(self.on_export_finished)
        self._set_exporting(True)
        worker.start()

    def capture_target_png(self, address: str) -> None:
        row = self.target_graph_rows.get(address)
        if row is None:
            return
        default = default_export_path(address, "png", user_exports_directory())
        selected, _filter = QFileDialog.getSaveFileName(self, "IP 화면 PNG 캡처", str(default), "PNG (*.png)")
        if not selected:
            return
        row = self.target_graph_rows.get(address)
        if row is None:
            return
        path = Path(selected).with_suffix(".png")
        if str(path) != selected and path.exists():
            if QMessageBox.question(self, "덮어쓰기", f"{path.name} 파일을 덮어쓸까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        # QWidget capture must happen on the GUI thread, not the export thread.
        try:
            image = row.grab().toImage()
            if image.isNull():
                raise OSError("IP 화면을 캡처할 수 없습니다.")
            data = png_bytes(image)
            atomic_write_path(path, lambda temporary: temporary.write_bytes(data))
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "캡처 오류", str(exc))
            return
        self.status_label.setText(f"PNG 저장 완료: {path}")
