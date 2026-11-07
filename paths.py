"""Filesystem boundaries for Better Music's static and persistent data."""

from __future__ import annotations

import os
from pathlib import Path


PLUGIN_NAME = "astrbot_plugin_better_music"


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


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


def _assert_inside(base: Path, candidate: Path, label: str) -> None:
    try:
        candidate.resolve().relative_to(base.resolve())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{label}超出插件数据目录：{candidate}") from exc


def data_subdirectory(data_dir: Path | str, *parts: str) -> Path:
    """Create a private directory under AstrBot's plugin data root."""
    root = _absolute(data_dir)
    _reject_link(root, "插件数据目录")
    root.mkdir(parents=True, exist_ok=True)
    _reject_link(root, "插件数据目录")
    if not root.is_dir():
        raise RuntimeError(f"插件数据路径不是目录：{root}")
    if os.name != "nt":
        root.chmod(0o700)

    current = root
    for part in parts:
        component = Path(part)
        if component.is_absolute() or len(component.parts) != 1 or component.name in {".", ".."}:
            raise ValueError("插件数据子目录名称必须是单一目录名")
        current = current / component
        _reject_link(current, "插件数据子目录")
        current.mkdir(exist_ok=True)
        _reject_link(current, "插件数据子目录")
        if not current.is_dir():
            raise RuntimeError(f"插件数据子路径不是目录：{current}")
        _assert_inside(root, current, "插件数据子目录")
    return current
