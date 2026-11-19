"""Versioned, atomic on-disk favorites. Never rewrite damaged user data."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.storage.atomic_write import atomic_write_path, read_text_with_retries
from app.utils.validators import validate_target

FAVORITES_SCHEMA_VERSION = 1
MAX_GROUPS = 100
MAX_GROUP_TARGETS = 50


@dataclass
class FavoriteState:
    groups: dict[str, list[str]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)


class FavoritesStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> FavoriteState:
        if not self.path.exists():
            return FavoriteState()
        try:
            payload = json.loads(read_text_with_retries(self.path))
        except (OSError, ValueError) as exc:
            raise ValueError(f"즐겨찾기 파일을 읽을 수 없습니다. 원본 보존: {exc}") from exc
        return _validated_state(payload)

    def save(self, state: FavoriteState) -> None:
        checked = _validated_state({
            "schema_version": FAVORITES_SCHEMA_VERSION,
            "groups": state.groups,
            "aliases": state.aliases,
        })
        encoded = json.dumps({
            "schema_version": FAVORITES_SCHEMA_VERSION,
            "groups": checked.groups,
            "aliases": checked.aliases,
        }, ensure_ascii=False, indent=2) + "\n"
        atomic_write_path(self.path, lambda temporary: temporary.write_text(encoded, encoding="utf-8"))


def _validated_state(payload: object) -> FavoriteState:
    if not isinstance(payload, dict) or payload.get("schema_version") != FAVORITES_SCHEMA_VERSION:
        raise ValueError("지원하지 않는 즐겨찾기 스키마입니다. 원본 파일은 유지됩니다.")
    groups = payload.get("groups")
    aliases = payload.get("aliases")
    if not isinstance(groups, dict) or not isinstance(aliases, dict):
        raise ValueError("그룹 및 별칭 데이터 형식이 잘못되었습니다.")
    if len(groups) > MAX_GROUPS:
        raise ValueError("저장 가능한 그룹 수를 초과했습니다.")
    result_groups: dict[str, list[str]] = {}
    for name, raw_addresses in groups.items():
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
            raise ValueError("그룹 이름이 잘못되었습니다.")
        if not isinstance(raw_addresses, list) or len(raw_addresses) > MAX_GROUP_TARGETS:
            raise ValueError("그룹당 최대 50개의 IPv4만 저장할 수 있습니다.")
        addresses: list[str] = []
        for raw in raw_addresses:
            if not isinstance(raw, str) or not validate_target(raw)[0]:
                raise ValueError("그룹에 유효하지 않은 IPv4 주소가 있습니다.")
            if raw not in addresses:
                addresses.append(raw)
        result_groups[name.strip()] = addresses
    result_aliases: dict[str, str] = {}
    for raw_address, alias in aliases.items():
        if not isinstance(raw_address, str) or not validate_target(raw_address)[0]:
            raise ValueError("별칭에 유효하지 않은 IPv4 주소가 있습니다.")
        if not isinstance(alias, str) or len(alias) > 80:
            raise ValueError("IP 별칭이 너무 길거나 형식이 잘못되었습니다.")
        if alias.strip():
            result_aliases[raw_address] = alias.strip()
    return FavoriteState(groups=result_groups, aliases=result_aliases)


def renamed_group(state: FavoriteState, old_name: str, new_name: str) -> FavoriteState:
    """Return a new state. The caller only adopts it after atomic persistence."""
    new_name = new_name.strip()
    if old_name not in state.groups:
        raise ValueError("변경할 그룹을 찾지 못했습니다.")
    if not new_name or len(new_name) > 64:
        raise ValueError("그룹 이름은 1~64자로 지정하세요.")
    if new_name != old_name and new_name in state.groups:
        raise ValueError("이미 같은 이름의 그룹이 있습니다.")
    groups = {new_name if name == old_name else name: list(addresses)
              for name, addresses in state.groups.items()}
    return FavoriteState(groups=groups, aliases=dict(state.aliases))


def updated_membership(
    state: FavoriteState, address: str, group: str, operation: str
) -> FavoriteState:
    """Add, remove or move an IPv4 across named groups without side effects.

    A move removes the address from all old groups before adding the chosen one.
    An add preserves any previous multi-group memberships.
    """
    if not validate_target(address)[0]:
        raise ValueError("유효한 IPv4 주소가 아닙니다.")
    if group not in state.groups:
        raise ValueError("그룹을 찾지 못했습니다.")
    if operation not in {"add", "remove", "move"}:
        raise ValueError("지원하지 않는 그룹 작업입니다.")
    groups = {name: list(addresses) for name, addresses in state.groups.items()}
    if operation == "move":
        for addresses in groups.values():
            if address in addresses:
                addresses.remove(address)
    elif operation == "remove":
        if address in groups[group]:
            groups[group].remove(address)
    if operation in {"add", "move"} and address not in groups[group]:
        if len(groups[group]) >= MAX_GROUP_TARGETS:
            raise ValueError("한 그룹에는 최대 50개 IP만 저장할 수 있습니다.")
        groups[group].append(address)
    return FavoriteState(groups=groups, aliases=dict(state.aliases))
