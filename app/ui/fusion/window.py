"""Drop-in Fusion entry window. Reuses v0.3.0 report window and all existing workers."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from itertools import islice

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QWidget,
)

from app.reporting.data import TargetInfo
from app.storage.fusion_favorites import FavoriteState, FavoritesStore, renamed_group, updated_membership
from app.storage.fusion_status_events import (
    StatusChange,
    StatusEventWriter,
    format_local_time,
    recent_changes,
)
from app.ui.fusion.dashboard import FusionDashboard
from app.ui.fusion.models import FusionTarget, to_fusion_target, transition_event
from app.ui.report_window import ReportMainWindow
from app.utils.app_paths import app_data_directory


class FusionMainWindow(ReportMainWindow):
    """Fusion view augments the released window; classic mode is always available."""
    def __init__(self, worker_factory=None) -> None:
        super().__init__(worker_factory=worker_factory)
        self.favorites_store = FavoritesStore(app_data_directory() / "fusion_favorites.json")
        self._fusion_favorites_readonly = False
        try:
            self.favorites: FavoriteState = self.favorites_store.load()
        except (OSError, ValueError) as exc:
            self.favorites = FavoriteState()
            self._fusion_favorites_readonly = True
            self.status_label.setText(f"즐겨찾기 읽기 실패, 쓰기 잠금: {exc}")
        self.target_aliases.update(self.favorites.aliases)
        self._fusion_last_states: dict[str, str] = {}
        self._fusion_mode = "fusion"
        self.fusion_dashboard = FusionDashboard(self)
        container = self.graph_panel.parentWidget()
        layout = container.layout()
        layout.insertWidget(layout.indexOf(self.graph_panel), self.fusion_dashboard, 8)
        self.fusion_dashboard.set_groups(self.favorites.groups)
        self.fusion_dashboard.target_selected.connect(self._show_fusion_target_detail)
        self.fusion_dashboard.pause_requested.connect(self._fusion_toggle_pause)
        self.fusion_dashboard.alias_requested.connect(self.edit_target_alias)
        self.fusion_dashboard.report_requested.connect(self.open_monitoring_report)
        self.fusion_dashboard.remove_requested.connect(self._fusion_remove_target)
        self.fusion_dashboard.group_requested.connect(self._fusion_assign_to_group)
        self.fusion_dashboard.new_group_requested.connect(self._fusion_create_group)
        self.fusion_dashboard.remove_group_requested.connect(self._fusion_remove_group)
        self.fusion_dashboard.rename_group_requested.connect(self._fusion_rename_group)
        self.fusion_dashboard.load_group_requested.connect(self._fusion_load_group)

        toolbar = QWidget(self)
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(2, 4, 2, 0)
        row.addWidget(QLabel("모니터링 화면"))
        self.fusion_mode_combo = QComboBox()
        self.fusion_mode_combo.setAccessibleName("Fusion 카드 또는 기존 그래프 보기")
        self.fusion_mode_combo.addItem("Fusion 카드 / 목록", "fusion")
        self.fusion_mode_combo.addItem("클래식 그래프", "classic")
        self.fusion_mode_combo.currentIndexChanged.connect(self._fusion_view_mode_changed)
        row.addWidget(self.fusion_mode_combo)
        row.addStretch(1)
        self.controls_panel.layout().addWidget(toolbar)

        self._fusion_event_path = app_data_directory() / "fusion_status_events.sqlite3"
        try:
            for record in reversed(recent_changes(self._fusion_event_path)):
                self.fusion_dashboard.add_event(
                    format_local_time(record.observed_at), record.address, record.alias, record.description
                )
        except (OSError, ValueError) as exc:
            self.status_label.setText(f"기존 상태 이력 읽기 실패: {exc}")
        self._fusion_writer = StatusEventWriter(self._fusion_event_path, self)
        self._fusion_writer.persisted.connect(self._on_fusion_event_persisted)
        self._fusion_writer.failed.connect(self._on_fusion_event_failed)
        self._fusion_writer.start()
        self._sync_fusion_targets()
        self._sync_graph_panel_visibility()

    def _fusion_view_mode_changed(self, *_args) -> None:
        self._fusion_mode = str(self.fusion_mode_combo.currentData() or "fusion")
        self._sync_graph_panel_visibility()
        if self._fusion_mode == "classic":
            self._request_graph_render(force=True)
        else:
            self._sync_fusion_targets()

    def _sync_graph_panel_visibility(self, *, running: bool | None = None) -> None:
        super()._sync_graph_panel_visibility(running=running)
        dashboard = getattr(self, "fusion_dashboard", None)
        if dashboard is None:
            return
        active = getattr(self, "_fusion_mode", "classic") == "fusion"
        self.graph_panel.setVisible(not active and not self.graph_panel.isHidden())
        dashboard.setVisible(active)

    def _set_running(self, running: bool) -> None:
        super()._set_running(running)
        dashboard = getattr(self, "fusion_dashboard", None)
        if dashboard is not None:
            dashboard.set_running(running)
            self._sync_fusion_targets()

    def start_measurement(self) -> None:
        self._fusion_last_states = {}
        super().start_measurement()
        self._sync_fusion_targets()

    def on_measurement_updated(self, *args) -> None:
        super().on_measurement_updated(*args)
        self._sync_fusion_targets(emit_events=True)

    def _sync_fusion_targets(self, *, emit_events: bool = False) -> None:
        dashboard = getattr(self, "fusion_dashboard", None)
        if dashboard is None:
            return
        snapshots = {item.address: item for item in self.target_snapshots if item.address}
        addresses = list(dict.fromkeys([*self.current_targets, *snapshots]))
        models: list[FusionTarget] = []
        for address in addresses:
            snapshot = snapshots.get(address)
            source: deque = self.live_graph_observations_by_address.get(address, deque())
            history = list(islice(reversed(source), 48))
            history.reverse()
            state = self._target_graph_health_state(address, snapshot, history)
            model = to_fusion_target(
                address, snapshot, state, self.target_aliases.get(address, ""), history
            )
            models.append(model)
            if emit_events and self._is_worker_running():
                previous = self._fusion_last_states.get(address)
                description = transition_event(
                    previous, model.state, manually_paused=self._is_target_paused(address, snapshot)
                )
                if description and getattr(self, "_fusion_writer", None) is not None:
                    event = StatusChange(
                        observed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        address=address,
                        alias=model.alias,
                        previous=previous or "",
                        current=model.state,
                        description=description,
                    )
                    self._fusion_writer.submit(event)
                self._fusion_last_states[address] = model.state
        live = set(addresses)
        for address in list(self._fusion_last_states):
            if address not in live:
                self._fusion_last_states.pop(address, None)
        dashboard.set_running(self._is_worker_running())
        dashboard.set_targets(models)

    def _on_fusion_event_persisted(self, record: StatusChange) -> None:
        if getattr(self, "fusion_dashboard", None) is not None:
            self.fusion_dashboard.add_event(
                format_local_time(record.observed_at), record.address, record.alias, record.description
            )

    def _on_fusion_event_failed(self, message: str) -> None:
        self.status_label.setText(message)

    def _show_fusion_target_detail(self, address: str) -> None:
        if not address:
            return
        self.select_summary_target(address)
        self.open_graph_detail()

    def _fusion_toggle_pause(self, address: str) -> None:
        self.toggle_runtime_target_pause(address)
        self._sync_fusion_targets()

    def _fusion_remove_target(self, address: str) -> None:
        if not self._is_worker_running():
            return
        choice = QMessageBox.question(
            self, "측정 IP 삭제", f"{address} 측정을 중단하고 목록에서 제거할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self.remove_runtime_target(address)
            self._sync_fusion_targets()

    def edit_target_alias(self, address: str) -> None:
        original = self.target_aliases.get(address, "")
        super().edit_target_alias(address)
        changed = self.target_aliases.get(address, "")
        if changed != original:
            updated = FavoriteState(
                groups={key: list(values) for key, values in self.favorites.groups.items()},
                aliases=dict(self.favorites.aliases),
            )
            if changed:
                updated.aliases[address] = changed
            else:
                updated.aliases.pop(address, None)
            if not self._save_fusion_favorites(updated):
                # Never pretend a failed disk write preserved the edited alias.
                if original:
                    self.target_aliases[address] = original
                else:
                    self.target_aliases.pop(address, None)
                self._sync_target_graph_alias_label(address)
                self.status_label.setText(f"{address} 별칭 저장에 실패하여 이전 값으로 되돌렸습니다.")
        self._sync_fusion_targets()

    def _save_fusion_favorites(
        self, candidate: FavoriteState | None = None, *, select_group: str | None = None
    ) -> bool:
        if self._fusion_favorites_readonly:
            QMessageBox.warning(
                self, "손상된 즐겨찾기 파일 보호",
                "기존 즐겨찾기 파일이 손상되었거나 읽을 수 없습니다.\n"
                "원본 덮어쓰기를 막았습니다. 먼저 파일을 복구하거나 백업 후 다시 실행하세요.",
            )
            return False
        next_state = candidate if candidate is not None else self.favorites
        try:
            self.favorites_store.save(next_state)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "즐겨찾기 저장 실패", f"기존 파일은 보존되었습니다.\n{exc}")
            return False
        self.favorites = next_state
        self.fusion_dashboard.set_groups(self.favorites.groups)
        if select_group is not None:
            self.fusion_dashboard.select_group(select_group)
        return True

    def _fusion_create_group(self) -> None:
        name, accepted = QInputDialog.getText(self, "그룹 생성", "그룹 이름")
        name = name.strip()
        if not accepted or not name:
            return
        if name in self.favorites.groups:
            QMessageBox.information(self, "중복 그룹", "이미 같은 이름의 그룹이 있습니다.")
            return
        candidate = FavoriteState(
            groups={**self.favorites.groups, name: []},
            aliases=dict(self.favorites.aliases),
        )
        self._save_fusion_favorites(candidate, select_group=name)

    def _fusion_rename_group(self, name: str) -> None:
        if name not in self.favorites.groups:
            return
        new_name, accepted = QInputDialog.getText(
            self, "그룹 이름 변경", "새 그룹 이름", QLineEdit.EchoMode.Normal, name
        )
        if not accepted or new_name.strip() == name:
            return
        try:
            candidate = renamed_group(self.favorites, name, new_name)
        except ValueError as exc:
            QMessageBox.warning(self, "이름 변경 불가", str(exc))
            return
        self._save_fusion_favorites(candidate, select_group=new_name.strip())

    def _fusion_remove_group(self, name: str) -> None:
        if name not in self.favorites.groups:
            return
        choice = QMessageBox.question(
            self, "그룹 삭제", f"'{name}' 그룹을 삭제할까요? IP 측정과 저장된 세션은 삭제되지 않습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        candidate = FavoriteState(
            groups={key: list(values) for key, values in self.favorites.groups.items() if key != name},
            aliases=dict(self.favorites.aliases),
        )
        self._save_fusion_favorites(candidate)

    def _fusion_assign_to_group(self, address: str) -> None:
        if not self.favorites.groups:
            QMessageBox.information(self, "그룹 필요", "먼저 그룹을 생성하세요.")
            return
        members = {name for name, values in self.favorites.groups.items() if address in values}
        options: list[tuple[str, str, str]] = []
        for name in self.favorites.groups:
            if name not in members:
                options.append((f"추가: {name}", "add", name))
            else:
                options.append((f"해제: {name}", "remove", name))
        if members:
            options.extend(
                (f"이동: {name} (기존 그룹 모두 해제)", "move", name)
                for name in self.favorites.groups if name not in members
            )
        label, accepted = QInputDialog.getItem(
            self, "IP 그룹 관리",
            f"{address} 그룹 작업 선택\n이동은 이 IP의 기존 그룹 소속을 모두 해제합니다.",
            [option[0] for option in options], 0, False
        )
        if not accepted:
            return
        selected = next(((op, group) for title, op, group in options if title == label), None)
        if selected is None:
            return
        operation, group = selected
        try:
            candidate = updated_membership(self.favorites, address, group, operation)
        except ValueError as exc:
            QMessageBox.warning(self, "그룹 변경 불가", str(exc))
            return
        if self._save_fusion_favorites(candidate):
            self.status_label.setText(f"{address}: 그룹 '{group}' {operation} 작업을 저장했습니다.")

    def _fusion_load_group(self, name: str) -> None:
        if self._is_worker_running():
            QMessageBox.information(self, "측정 중", "측정 중에는 입력 목록을 변경하지 않습니다.")
            return
        addresses = self.favorites.groups.get(name)
        if not addresses:
            self.status_label.setText(f"{name}: 저장된 IPv4 주소가 없습니다.")
            return
        self.target_input.setPlainText("\n".join(addresses))
        self.status_label.setText(f"{name} 그룹 {len(addresses)}개를 입력했습니다. 시작 버튼을 눌러 측정하세요.")

    def _report_target_info(self) -> list[TargetInfo]:
        """Reports use the exact Fusion/legacy health state even when classic graphs are hidden."""
        by_address = {item.address: item for item in self.fusion_dashboard.targets} if hasattr(self, "fusion_dashboard") else {}
        result = []
        for address in self.current_targets:
            model = by_address.get(address)
            result.append(TargetInfo(
                address=address,
                alias=self.target_aliases.get(address, ""),
                screen_status=model.label if model is not None else "대기",
                interval_seconds=self.target_interval_overrides.get(address, int(self.interval_combo.currentText() or "1")),
            ))
        return result

    def closeEvent(self, event) -> None:
        writer = getattr(self, "_fusion_writer", None)
        if writer is not None and writer.isRunning() and not writer.shutdown():
            self.status_label.setText("Fusion 이벤트 저장 종료를 완료하지 못했습니다. 다시 종료하세요.")
            event.ignore()
            return
        super().closeEvent(event)
