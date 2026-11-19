"""Native, read-only Fusion dashboard for the existing MultiPingCheck measuring engine."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.fusion.models import FusionTarget, summarize

TONE = {
    "normal": "#16a34a",
    "warning": "#f59e0b",
    "critical": "#dc2626",
    "paused": "#64748b",
    "waiting": "#64748b",
}


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} ms"


class Sparkline(QWidget):
    """Tiny bounded RTT visualization, no probe or background timer."""
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.points: tuple[float | None, ...] = ()
        self.color = QColor(TONE["normal"])
        self.setMinimumHeight(32)
        self.setMaximumHeight(44)

    def set_data(self, points: tuple[float | None, ...], state: str) -> None:
        color = QColor(TONE.get(state, TONE["waiting"]))
        if self.points == points and self.color == color:
            return
        self.points = points
        self.color = color
        self.update()

    def paintEvent(self, _event) -> None:
        if len(self.points) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.color, 1.8))
        left, top = 2.0, 3.0
        width, height = max(self.width() - 4, 1), max(self.height() - 6, 1)
        valid = [value for value in self.points if value is not None]
        if not valid:
            return
        ceiling = max(max(valid) * 1.1, 1)
        previous = None
        for index, value in enumerate(self.points):
            if value is None:
                previous = None
                continue
            x = left + index * width / (len(self.points) - 1)
            y = top + height * (1.0 - min(value / ceiling, 1.0))
            if previous is not None:
                painter.drawLine(previous[0], previous[1], x, y)
            previous = (x, y)


class TargetCard(QFrame):
    selected = Signal(str)
    pause_requested = Signal(str)
    alias_requested = Signal(str)
    report_requested = Signal(str)
    remove_requested = Signal(str)
    group_requested = Signal(str)

    def __init__(self, address: str, parent=None) -> None:
        super().__init__(parent)
        self.address = address
        self.model: FusionTarget | None = None
        self.setObjectName("fusionCard")
        self.setMinimumWidth(230)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 12, 13, 11)
        layout.setSpacing(5)
        head = QHBoxLayout()
        self.title = QLabel(address)
        self.title.setObjectName("fusionTargetTitle")
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.badge = QLabel("대기")
        head.addWidget(self.title, 1)
        head.addWidget(self.badge)
        layout.addLayout(head)
        self.address_label = QLabel(address)
        self.address_label.setObjectName("fusionMuted")
        self.address_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.address_label)
        metrics = QHBoxLayout()
        self.current = QLabel("-")
        self.average = QLabel("-")
        self.loss = QLabel("-")
        for caption, label in (("현재", self.current), ("평균", self.average), ("최근 손실", self.loss)):
            part = QVBoxLayout()
            tiny = QLabel(caption)
            tiny.setObjectName("fusionMuted")
            part.addWidget(tiny)
            part.addWidget(label)
            metrics.addLayout(part, 1)
        layout.addLayout(metrics)
        self.graph = Sparkline()
        layout.addWidget(self.graph)
        actions = QHBoxLayout()
        for title, signal in (("상세", self.selected), ("이름", self.alias_requested),
                              ("그룹", self.group_requested), ("보고서", self.report_requested)):
            button = QPushButton(title)
            button.clicked.connect(lambda _checked=False, emit=signal: emit.emit(self.address))
            actions.addWidget(button)
        layout.addLayout(actions)
        self.pause_button = QPushButton("일시중지")
        self.pause_button.clicked.connect(lambda: self.pause_requested.emit(self.address))
        self.remove_button = QPushButton("삭제")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.address))
        bottom = QHBoxLayout()
        bottom.addWidget(self.pause_button)
        bottom.addWidget(self.remove_button)
        layout.addLayout(bottom)

    def set_model(self, model: FusionTarget, *, running: bool) -> None:
        if self.model == model and self.pause_button.isEnabled() == running:
            return
        self.model = model
        self.title.setText(model.title)
        self.badge.setText(model.label)
        self.badge.setStyleSheet(
            f"color: {TONE.get(model.state, TONE['waiting'])}; font-weight: bold;"
        )
        self.current.setText(_ms(model.current_ms))
        self.average.setText(_ms(model.average_ms))
        self.loss.setText("-" if model.loss_percent is None else f"{model.loss_percent:.1f}%")
        self.graph.set_data(model.points, model.state)
        self.pause_button.setText("재개" if model.state == "paused" else "일시중지")
        self.pause_button.setEnabled(running)
        self.remove_button.setEnabled(running)


class FusionDashboard(QWidget):
    """Preserves cards across sample updates; filters do not stop measurements."""
    target_selected = Signal(str)
    pause_requested = Signal(str)
    alias_requested = Signal(str)
    report_requested = Signal(str)
    remove_requested = Signal(str)
    group_requested = Signal(str)
    new_group_requested = Signal()
    remove_group_requested = Signal(str)
    load_group_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("fusionDashboard")
        self.targets: list[FusionTarget] = []
        self.cards: dict[str, TargetCard] = {}
        self.groups: dict[str, list[str]] = {}
        self.running = False
        self._last_grid_addresses: tuple[str, ...] = ()
        self._last_columns = 0
        self.theme = "dark"
        self.mode = "cards"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 6)
        layout.setSpacing(8)
        title_row = QHBoxLayout()
        heading = QLabel("Fusion · 통합 모니터링")
        heading.setObjectName("fusionHeading")
        title_row.addWidget(heading)
        title_row.addStretch(1)
        self.card_mode_button = QPushButton("카드")
        self.list_mode_button = QPushButton("목록")
        self.theme_button = QPushButton("라이트 모드")
        self.card_mode_button.clicked.connect(lambda: self.set_mode("cards"))
        self.list_mode_button.clicked.connect(lambda: self.set_mode("list"))
        self.theme_button.clicked.connect(self.toggle_theme)
        for widget in (self.card_mode_button, self.list_mode_button, self.theme_button):
            title_row.addWidget(widget)
        layout.addLayout(title_row)
        kpi_row = QHBoxLayout()
        self.kpis: dict[str, QLabel] = {}
        for key, title in (("total", "전체 대상"), ("normal", "정상"), ("warning", "주의"),
                           ("critical", "장애"), ("paused", "일시중지")):
            block = QFrame()
            block.setObjectName("fusionKpi")
            inner = QVBoxLayout(block)
            inner.setContentsMargins(12, 8, 12, 8)
            caption = QLabel(title)
            caption.setObjectName("fusionMuted")
            number = QLabel("0")
            number.setObjectName("fusionKpiNumber")
            self.kpis[key] = number
            inner.addWidget(caption)
            inner.addWidget(number)
            kpi_row.addWidget(block, 1)
        layout.addLayout(kpi_row)
        workspace = QHBoxLayout()
        sidebar = QFrame()
        sidebar.setObjectName("fusionSidebar")
        sidebar.setMaximumWidth(190)
        sidebar.setMinimumWidth(150)
        side_layout = QVBoxLayout(sidebar)
        side_layout.addWidget(QLabel("대상 그룹"))
        self.group_list = QListWidget()
        self.group_list.currentItemChanged.connect(lambda *_: self._refresh_visible())
        side_layout.addWidget(self.group_list, 1)
        new_group = QPushButton("+ 그룹 생성")
        delete_group = QPushButton("그룹 삭제")
        self.load_group_button = QPushButton("입력창에 불러오기")
        new_group.clicked.connect(self.new_group_requested.emit)
        delete_group.clicked.connect(self._request_group_delete)
        self.load_group_button.clicked.connect(self._request_group_load)
        side_layout.addWidget(new_group)
        side_layout.addWidget(delete_group)
        side_layout.addWidget(self.load_group_button)
        workspace.addWidget(sidebar, 0)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("IP 또는 표시 이름 검색")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh_visible)
        self.state_filter = QComboBox()
        for title, code in (("모든 상태", ""), ("정상", "normal"), ("주의", "warning"),
                            ("장애", "critical"), ("일시중지", "paused"), ("대기", "waiting")):
            self.state_filter.addItem(title, code)
        self.state_filter.currentIndexChanged.connect(self._refresh_visible)
        self.count_label = QLabel("0개 표시")
        self.count_label.setObjectName("fusionMuted")
        filters.addWidget(self.search, 1)
        filters.addWidget(self.state_filter)
        filters.addWidget(self.count_label)
        content_layout.addLayout(filters)
        self.stack = QStackedWidget()
        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.grid_scroll.viewport().installEventFilter(self)
        self.grid_holder = QWidget()
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(3, 3, 5, 3)
        self.grid.setHorizontalSpacing(9)
        self.grid.setVerticalSpacing(9)
        self.grid_scroll.setWidget(self.grid_holder)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["이름", "IPv4", "상태", "현재 RTT", "평균 RTT", "손실 %", "표본"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self._open_table_row)
        self.stack.addWidget(self.grid_scroll)
        self.stack.addWidget(self.table)
        content_layout.addWidget(self.stack, 1)
        workspace.addWidget(content, 1)
        layout.addLayout(workspace, 1)
        event_row = QHBoxLayout()
        event_row.addWidget(QLabel("최근 상태 변경"))
        event_row.addStretch(1)
        self.event_count = QLabel("0건")
        self.event_count.setObjectName("fusionMuted")
        event_row.addWidget(self.event_count)
        layout.addLayout(event_row)
        self.events = QListWidget()
        self.events.setMaximumHeight(116)
        layout.addWidget(self.events)
        self.set_groups({})
        self.set_mode("cards")
        self._apply_theme()

    def eventFilter(self, source, event) -> bool:
        if source == self.grid_scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._reflow()
        return super().eventFilter(source, event)

    def _selected_group(self) -> str | None:
        item = self.group_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item and item.data(Qt.ItemDataRole.UserRole) else None

    def set_groups(self, groups: dict[str, list[str]]) -> None:
        previous = self._selected_group()
        self.groups = {name: list(addresses) for name, addresses in groups.items()}
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for name, key in [("전체 대상", ""), *[(name, name) for name in self.groups]]:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.group_list.addItem(item)
        for index in range(self.group_list.count()):
            item = self.group_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == previous:
                self.group_list.setCurrentRow(index)
                break
        else:
            self.group_list.setCurrentRow(0)
        self.group_list.blockSignals(False)
        self._refresh_visible()

    def _request_group_delete(self) -> None:
        name = self._selected_group()
        if name:
            self.remove_group_requested.emit(name)

    def _request_group_load(self) -> None:
        name = self._selected_group()
        if name:
            self.load_group_requested.emit(name)

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in {"cards", "list"} else "cards"
        self.stack.setCurrentWidget(self.grid_scroll if self.mode == "cards" else self.table)
        self.card_mode_button.setEnabled(self.mode != "cards")
        self.list_mode_button.setEnabled(self.mode != "list")
        self._refresh_visible()

    def toggle_theme(self) -> None:
        self.theme = "light" if self.theme == "dark" else "dark"
        self.theme_button.setText("다크 모드" if self.theme == "light" else "라이트 모드")
        self._apply_theme()

    def _apply_theme(self) -> None:
        dark = self.theme == "dark"
        bg = "#101a2a" if dark else "#f6f8fc"
        tile = "#1b2b42" if dark else "#ffffff"
        border = "#30445e" if dark else "#d8e1ef"
        ink = "#eef5ff" if dark else "#1e293b"
        secondary = "#91a5bc" if dark else "#61728c"
        self.setStyleSheet(f"""
            QWidget#fusionDashboard {{ background: {bg}; color: {ink}; }}
            QFrame#fusionKpi, QFrame#fusionCard, QFrame#fusionSidebar {{
                background: {tile}; border: 1px solid {border}; border-radius: 8px;
            }}
            QLabel#fusionHeading {{ font-size: 17px; font-weight: 700; color: {ink}; }}
            QLabel#fusionTargetTitle {{ font-weight: 700; color: {ink}; }}
            QLabel#fusionKpiNumber {{ font-size: 20px; font-weight: 700; color: {ink}; }}
            QLabel#fusionMuted {{ color: {secondary}; }}
            QLineEdit, QComboBox, QListWidget, QTableWidget {{
                background: {tile}; color: {ink}; border: 1px solid {border}; border-radius: 5px;
            }}
            QPushButton {{
                background: {tile}; color: {ink}; border: 1px solid {border};
                border-radius: 5px; padding: 5px 7px;
            }}
            QPushButton:hover {{ border-color: #4299e1; }}
            QTableWidget::item:selected, QListWidget::item:selected {{
                background: #275a86; color: #ffffff;
            }}
        """)

    def set_running(self, running: bool) -> None:
        self.running = running
        self.load_group_button.setEnabled(not running)
        for card in self.cards.values():
            if card.model is not None:
                card.set_model(card.model, running=running)

    def set_targets(self, targets: list[FusionTarget]) -> None:
        self.targets = list(targets)
        counts = summarize(targets)
        for name, label in self.kpis.items():
            label.setText(str(counts[name]))
        present = {target.address for target in targets}
        for address in list(self.cards):
            if address not in present:
                removed = self.cards.pop(address)
                self.grid.removeWidget(removed)
                removed.deleteLater()
        for target in targets:
            card = self.cards.get(target.address)
            if card is None:
                card = TargetCard(target.address)
                for source, dest in (
                    (card.selected, self.target_selected), (card.pause_requested, self.pause_requested),
                    (card.alias_requested, self.alias_requested), (card.report_requested, self.report_requested),
                    (card.remove_requested, self.remove_requested), (card.group_requested, self.group_requested),
                ):
                    source.connect(dest.emit)
                self.cards[target.address] = card
            card.set_model(target, running=self.running)
        self._refresh_visible()

    def _visible_targets(self) -> list[FusionTarget]:
        allowed = self.groups.get(self._selected_group() or "", None)
        subset = set(allowed) if allowed is not None else None
        text = self.search.text().strip().casefold()
        state = self.state_filter.currentData()
        return [
            target for target in self.targets
            if (subset is None or target.address in subset)
            and (not text or text in (target.alias + " " + target.address).casefold())
            and (not state or target.state == state)
        ]

    def _refresh_visible(self, *_args) -> None:
        visible = self._visible_targets()
        self.count_label.setText(f"{len(visible)} / {len(self.targets)}개 표시")
        addresses = tuple(item.address for item in visible)
        if addresses != self._last_grid_addresses:
            self._last_grid_addresses = addresses
            self._reflow()
        if self.mode == "list":
            self._fill_table(visible)

    def _reflow(self) -> None:
        if not hasattr(self, "grid_scroll"):
            return
        width = max(self.grid_scroll.viewport().width(), 260)
        columns = max(1, min(4, width // 290))
        if columns == self._last_columns and self.grid.count() == len(self._last_grid_addresses):
            return
        self._last_columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for index, address in enumerate(self._last_grid_addresses):
            card = self.cards.get(address)
            if card is not None:
                self.grid.addWidget(card, index // columns, index % columns)
                card.show()
        self.grid.setRowStretch((len(self._last_grid_addresses) + columns - 1) // columns, 1)

    def _fill_table(self, visible: list[FusionTarget]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(visible))
        for row, target in enumerate(visible):
            values = [target.title, target.address, target.label, _ms(target.current_ms),
                      _ms(target.average_ms),
                      "-" if target.loss_percent is None else f"{target.loss_percent:.1f}",
                      str(target.samples)]
            for col, value in enumerate(values):
                item = self.table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, col, item)
                if item.text() != value:
                    item.setText(value)
                item.setData(Qt.ItemDataRole.UserRole, target.address)
                item.setForeground(QColor(TONE[target.state]) if col == 2 else QColor(
                    "#edf5ff" if self.theme == "dark" else "#1e293b"))
        self.table.blockSignals(False)

    def _open_table_row(self, row: int, _column: int) -> None:
        item = self.table.item(row, 1)
        if item is not None:
            self.target_selected.emit(item.text())

    def add_event(self, when: str, address: str, name: str, event: str) -> None:
        item = QListWidgetItem(f"{when}   {name or address} ({address})   {event}")
        self.events.insertItem(0, item)
        while self.events.count() > 200:
            self.events.takeItem(self.events.count() - 1)
        self.event_count.setText(f"{self.events.count()}건")
