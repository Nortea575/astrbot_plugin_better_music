"""Plugin-owned filesystem paths and legacy data migration."""

from __future__ import annotations

import os
from pathlib import Path


PLUGIN_NAME = "astrbot_plugin_better_music"


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def is_path_link(path: Path | str) -> bool:
    """Return whether a path is a symbolic link or a Windows directory junction."""
    return _is_link(Path(path))


def _reject_link(path: Path, label: str) -> None:
    if _is_link(path):
        raise RuntimeError(f"{label}不能是符号链接或目录联接：{path}")


def _plugin_root(plugin_root: Path | str) -> Path:
    root = _absolute(plugin_root)
    if root.name != PLUGIN_NAME:
        raise RuntimeError(f"插件目录名称应为 {PLUGIN_NAME}：{root}")
    _reject_link(root, "插件目录")
    if not root.is_dir():
        raise RuntimeError(f"插件目录不存在或不是目录：{root}")
    return root


def plugin_root_from_module(module_file: str) -> Path:
    """Return the installed plugin root without following a plugin symlink."""
    return _plugin_root(Path(module_file).absolute().parent)


def _legacy_data_dir(root: Path) -> Path | None:
    if root.parent.name.casefold() != "plugins" or root.parent.parent.name.casefold() != "data":
        return None
    _reject_link(root.parent, "AstrBot插件目录")
    astrbot_data = root.parent.parent
    plugin_data_root = astrbot_data / "plugin_data"
    _reject_link(plugin_data_root, "旧插件数据目录")
    if plugin_data_root.exists() and not plugin_data_root.is_dir():
        raise RuntimeError(f"旧插件数据路径不是目录：{plugin_data_root}")
    legacy = plugin_data_root / PLUGIN_NAME
    _reject_link(legacy, "旧插件数据目录")
    return legacy


def _has_entries(path: Path) -> bool:
    return any(path.iterdir())


def _assert_inside(base: Path, candidate: Path, label: str) -> None:
    try:
        candidate.resolve().relative_to(base.resolve())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{label}超出插件目录：{candidate}") from exc


def plugin_data_dir(plugin_root: Path | str) -> Path:
    """Migrate StarTools data into <plugin>/data and return that directory.

    A first migration is a same-filesystem rename, preserving all files and
    their metadata. If both locations contain entries, startup stops rather
    than combining or overwriting either tree.
    """
    root = _plugin_root(plugin_root)
    target = root / "data"
    legacy = _legacy_data_dir(root)
    _reject_link(target, "插件数据目录")

    if target.exists() and not target.is_dir():
        raise RuntimeError(f"插件数据路径已存在但不是目录：{target}")
    if legacy is not None and legacy.exists() and not legacy.is_dir():
        raise RuntimeError(f"旧插件数据路径已存在但不是目录：{legacy}")

    if legacy is not None and legacy.exists():
        legacy_has_entries = _has_entries(legacy)
        target_has_entries = target.exists() and _has_entries(target)
        if legacy_has_entries and target_has_entries:
            raise RuntimeError(
                "新旧插件数据目录都含有文件，未覆盖或合并；请先手动整理后重启："
                f"旧目录 {legacy}；新目录 {target}"
            )
        if legacy_has_entries and target.exists():
            try:
                if legacy.stat().st_dev != target.parent.stat().st_dev:
                    raise RuntimeError("新旧插件数据目录不在同一文件系统，无法安全迁移")
                target.rmdir()
            except OSError as exc:
                raise RuntimeError(f"无法准备插件数据迁移目标 {target}：{exc}") from exc
        elif not legacy_has_entries:
            try:
                legacy.rmdir()
            except OSError as exc:
                raise RuntimeError(f"无法清理空的旧插件数据目录 {legacy}：{exc}") from exc

        if legacy.exists():
            try:
                os.replace(legacy, target)
            except OSError as exc:
                raise RuntimeError(
                    f"无法将旧插件数据迁入插件目录；请检查目录权限后重启：{legacy} -> {target} ({exc})"
                ) from exc

    if not target.exists():
        target.mkdir()
    _reject_link(target, "插件数据目录")
    if not target.is_dir():
        raise RuntimeError(f"插件数据路径不是目录：{target}")
    _assert_inside(root, target, "插件数据目录")
    if os.name != "nt":
        target.chmod(0o700)
    return target


def plugin_subdirectory(plugin_root: Path | str, name: str) -> Path:
    """Create a direct plugin-owned directory after checking its boundary."""
    root = _plugin_root(plugin_root)
    component = Path(name)
    if component.is_absolute() or len(component.parts) != 1 or component.name in {".", ".."}:
        raise ValueError("插件子目录名称必须是单一目录名")
    target = root / component
    _reject_link(target, "插件子目录")
    target.mkdir(exist_ok=True)
    if not target.is_dir():
        raise RuntimeError(f"插件子目录不是目录：{target}")
    _assert_inside(root, target, "插件子目录")
    return target


def data_subdirectory(data_dir: Path | str, *parts: str) -> Path:
    """Create a directory below the plugin data root, rejecting link escapes."""
    root = _absolute(data_dir)
    _reject_link(root, "插件数据目录")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise RuntimeError(f"插件数据路径不是目录：{root}")

    current = root
    for part in parts:
        component = Path(part)
        if component.is_absolute() or len(component.parts) != 1 or component.name in {".", ".."}:
            raise ValueError("插件数据子目录名称必须是单一目录名")
        current = current / component
        _reject_link(current, "插件数据子目录")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise RuntimeError(f"插件数据子路径不是目录：{current}")
        _assert_inside(root, current, "插件数据子目录")
    return current
