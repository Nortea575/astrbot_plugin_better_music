"""Path-safe persistence for plugin-local song files and their metadata."""

from __future__ import annotations

import json
import filecmp
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import is_path_link


_SONG_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FOLDER_SONG_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_COVER_ID = re.compile(r"^[0-9a-f]{64}$")
_COMPONENT = re.compile(r"[^\w.-]+", re.UNICODE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


class MusicStorage:
    """Store each managed song under downloads/<platform>/<safe-title>-<uuid>."""

    def __init__(self, data_dir: Path, downloads_dir: Path | None = None):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.downloads_dir = Path(downloads_dir or self.data_dir / "downloads").expanduser().absolute()
        if is_path_link(self.downloads_dir):
            raise ValueError("歌曲下载目录不能是符号链接或目录联接")
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        if is_path_link(self.downloads_dir):
            raise ValueError("歌曲下载目录不能是符号链接或目录联接")
        if not self.downloads_dir.is_dir():
            raise ValueError("歌曲下载路径不是目录")
        # Kept as an alias for the runtime's pending-directory bookkeeping.
        self.songs_dir = self.downloads_dir
        self.legacy_songs_dir = self.data_dir / "songs"

    @staticmethod
    def normalize_song_id(song_id: str) -> str:
        value = str(song_id).lower()
        if not _SONG_ID.fullmatch(value):
            raise ValueError("无效的歌曲记录编号")
        return value

    @staticmethod
    def safe_component(value: Any, fallback: str = "unknown", limit: int = 72) -> str:
        text = _COMPONENT.sub("_", str(value or "").strip()).strip("._-")[:limit].strip("._-")
        return text or fallback

    def platform_dir(self, platform: str) -> Path:
        name = self.safe_component(platform, "unknown", 48)
        raw_candidate = self.downloads_dir / name
        if raw_candidate.is_symlink():
            raise ValueError("平台目录不能是符号链接")
        candidate = raw_candidate.resolve()
        if not candidate.is_relative_to(self.downloads_dir.resolve()):
            raise ValueError("平台目录超出歌曲下载目录")
        return candidate

    def new_song_dir(self, song_id: str, platform: str, title: str) -> Path:
        normalized = self.normalize_song_id(song_id)
        folder = f"{self.safe_component(title, 'song')}-{normalized}"
        candidate = (self.platform_dir(platform) / folder).resolve()
        if not candidate.is_relative_to(self.downloads_dir.resolve()):
            raise ValueError("歌曲目录超出歌曲下载目录")
        return candidate

    def pending_dir(self, song_id: str, platform: str) -> Path:
        normalized = self.normalize_song_id(song_id)
        candidate = (self.platform_dir(platform) / f".pending-{normalized}").resolve()
        if not candidate.is_relative_to(self.downloads_dir.resolve()):
            raise ValueError("临时歌曲目录超出歌曲下载目录")
        return candidate

    def song_dir(self, song_id: str) -> Path:
        """Find an existing song directory by its stable record UUID."""
        normalized = self.normalize_song_id(song_id)
        for directory in self._song_directories():
            record = self._read_record(directory)
            if record and str(record.get("id", "")).lower() == normalized:
                return directory.resolve()
        return self.platform_dir("unknown") / f"song-{normalized}"

    def _song_directories(self) -> list[Path]:
        try:
            platform_dirs = sorted(self.downloads_dir.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            return []
        result: list[Path] = []
        for platform_dir in platform_dirs:
            if platform_dir.is_symlink() or not platform_dir.is_dir():
                continue
            try:
                children = sorted(platform_dir.iterdir(), key=lambda path: path.name.casefold())
            except OSError:
                continue
            result.extend(
                child
                for child in children
                if not child.name.startswith(".") and not child.is_symlink() and child.is_dir()
            )
        return result

    def write_record(self, record: dict[str, Any], directory: Path | None = None) -> None:
        song_id = self.normalize_song_id(record.get("id", ""))
        target = Path(directory) if directory is not None else self.new_song_dir(
            song_id, str(record.get("platform", "unknown")), str(record.get("title", "song"))
        )
        self._validate_song_directory(target)
        target.mkdir(parents=True, exist_ok=True)
        self._write_record_file(record, target)

    def write_pending_record(self, record: dict[str, Any], pending_dir: Path) -> None:
        song_id = self.normalize_song_id(record.get("id", ""))
        pending_dir = Path(pending_dir)
        expected = f".pending-{song_id}"
        if (
            pending_dir.name != expected
            or pending_dir.is_symlink()
            or not pending_dir.is_dir()
            or pending_dir.parent != self.platform_dir(str(record.get("platform", "unknown")))
        ):
            raise ValueError("临时歌曲目录与记录编号不匹配")
        self._validate_song_directory(pending_dir)
        self._write_record_file(record, pending_dir.resolve())

    def _validate_song_directory(self, directory: Path) -> Path:
        directory = Path(directory)
        if directory.is_symlink():
            raise ValueError("歌曲目录不能是符号链接")
        resolved = directory.resolve()
        root = self.downloads_dir.resolve()
        if not resolved.is_relative_to(root) or resolved.parent.parent != root:
            raise ValueError("歌曲目录必须位于平台目录中")
        return resolved

    @staticmethod
    def _write_record_file(record: dict[str, Any], directory: Path) -> None:
        target = directory / "metadata.json"
        temporary = directory / "metadata.json.tmp"
        payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)

    def _read_record(
        self, directory: Path, *, require_complete_audio: bool = False
    ) -> dict[str, Any] | None:
        try:
            directory = self._validate_song_directory(directory)
            metadata_path = directory / "metadata.json"
            if metadata_path.is_symlink():
                return None
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            song_id = self.normalize_song_id(data.get("id", ""))
            folder_match = _FOLDER_SONG_ID.search(directory.name)
            if folder_match is None:
                return None
            folder_id = self.normalize_song_id(folder_match.group(1))
            if song_id != folder_id:
                return None
            if require_complete_audio and not self._has_complete_audio(data, directory):
                return None
            safe_files: list[dict[str, Any]] = []
            seen: set[str] = set()
            items = data.get("files", [])
            if not isinstance(items, list):
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not self._valid_filename(name) or name in seen:
                    continue
                path = (directory / name).resolve()
                if not path.is_relative_to(directory) or not path.is_file():
                    continue
                seen.add(name)
                safe_files.append({"name": name, "size": path.stat().st_size, "kind": str(item.get("kind", "audio"))})
            data["files"] = safe_files
            segments = data.get("segments", [])
            data["segments"] = [item for item in segments if isinstance(item, str) and item in seen] if isinstance(segments, list) else []
            return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _has_complete_audio(self, data: dict[str, Any], directory: Path) -> bool:
        items = data.get("files")
        segments = data.get("segments")
        if not isinstance(items, list) or not isinstance(segments, list) or not segments:
            return False

        declared: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not self._valid_filename(name) or name in declared:
                continue
            kind = str(item.get("kind", "audio"))
            path = directory / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                actual_size = path.stat().st_size
            except OSError:
                continue
            if actual_size <= 0:
                continue
            declared_size = item.get("size")
            if declared_size is not None and (
                isinstance(declared_size, bool)
                or not isinstance(declared_size, int)
                or declared_size != actual_size
            ):
                continue
            declared[name] = kind

        if sum(kind == "original" for kind in declared.values()) != 1:
            return False
        if sum(kind == "transcoded" for kind in declared.values()) != 1:
            return False
        segment_names = [item for item in segments if isinstance(item, str)]
        if len(segment_names) != len(segments) or len(set(segment_names)) != len(segment_names):
            return False
        declared_segments = {name for name, kind in declared.items() if kind == "segment"}
        return bool(declared_segments) and set(segment_names) == declared_segments

    @staticmethod
    def _valid_filename(filename: str) -> bool:
        return (
            filename not in ("", ".", "..", "metadata.json.tmp")
            and Path(filename).name == filename
            and "/" not in filename
            and "\\" not in filename
            and not Path(filename).is_absolute()
        )

    def get_record(self, song_id: str) -> dict[str, Any] | None:
        try:
            return self._read_record(self.song_dir(song_id))
        except (OSError, ValueError):
            return None

    def all_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for directory in self._song_directories():
            record = self._read_record(directory)
            if not record:
                continue
            normalized = str(record.get("id", "")).lower()
            if normalized in seen_ids:
                continue
            seen_ids.add(normalized)
            records.append(record)
        records.sort(key=lambda item: parse_time(item.get("downloaded_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return records

    def list_records(self) -> list[dict[str, Any]]:
        return self.all_records()

    def find_source_track(self, platform: str, source_track_id: str) -> dict[str, Any] | None:
        wanted = str(source_track_id)
        for directory in self._song_directories():
            record = self._read_record(directory, require_complete_audio=True)
            if not record:
                continue
            if str(record.get("platform", "")) == str(platform) and str(record.get("source_track_id", "")) == wanted:
                return record
        return None

    def find_source_query(self, platform: str, source_query: str) -> dict[str, Any] | None:
        wanted = str(source_query)
        for directory in self._song_directories():
            record = self._read_record(directory, require_complete_audio=True)
            if not record:
                continue
            if str(record.get("platform", "")) == str(platform) and str(record.get("source_query", "")) == wanted:
                return record
        return None

    def file_path(self, song_id: str, filename: str, *, allow_expired: bool = False) -> Path:
        del allow_expired  # Old callers may still pass this; expiry is no longer a storage rule.
        if not isinstance(filename, str) or not self._valid_filename(filename):
            raise ValueError("无效的文件名")
        record = self.get_record(song_id)
        if record is None:
            raise FileNotFoundError("歌曲记录不存在")
        allowed = {item["name"] for item in record.get("files", [])}
        allowed.add("metadata.json")
        if filename not in allowed:
            raise FileNotFoundError("该文件不属于歌曲记录")
        directory = self.song_dir(song_id)
        path = (directory / filename).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            raise FileNotFoundError("歌曲文件不存在")
        return path

    def cover_cache_dirs(self, song_id: str) -> list[Path]:
        """Return valid cache directory candidates belonging to one source song."""
        record = self.get_record(song_id)
        if record is None:
            raise FileNotFoundError("歌曲记录不存在")
        song_dir = self.song_dir(song_id)
        covers_dir = song_dir / "covers"
        if is_path_link(song_dir) or is_path_link(covers_dir):
            return []
        try:
            resolved_song = song_dir.resolve()
            resolved_covers = covers_dir.resolve()
            if not resolved_covers.is_relative_to(resolved_song) or not covers_dir.is_dir():
                return []
            children = list(covers_dir.iterdir())
        except OSError:
            return []
        result: list[Path] = []
        for child in children:
            if not _COVER_ID.fullmatch(child.name) or is_path_link(child) or not child.is_dir():
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved.parent == resolved_covers and resolved.is_relative_to(resolved_song):
                result.append(resolved)
        return sorted(result, key=lambda path: path.name)

    def cover_cache_dir(self, song_id: str, cover_id: str) -> Path:
        """Resolve a cache only by its validated SHA-256 directory identifier."""
        if not isinstance(cover_id, str) or not _COVER_ID.fullmatch(cover_id):
            raise ValueError("无效的翻唱缓存编号")
        for directory in self.cover_cache_dirs(song_id):
            if directory.name == cover_id:
                return directory
        raise FileNotFoundError("翻唱缓存不存在")

    def delete_song(self, song_id: str) -> bool:
        try:
            directory = self.song_dir(song_id)
        except (ValueError, OSError):
            return False
        if directory.is_symlink():
            return False
        shutil.rmtree(directory)
        return True

    def migrate_legacy(self) -> dict[str, int]:
        """Copy valid legacy song folders before removing their originals.

        Expiry metadata is intentionally ignored. Unrecognized directories and
        conflicting destinations are left untouched.
        """
        source_root = self.legacy_songs_dir
        if source_root.is_symlink() or not source_root.is_dir():
            return {"migrated": 0, "skipped": 0}
        try:
            sources = sorted(source_root.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            return {"migrated": 0, "skipped": 0}

        migrated = 0
        skipped = 0
        for source in sources:
            record = self._read_legacy_record(source, source_root)
            if record is None:
                continue
            try:
                target = self.new_song_dir(
                    str(record["id"]), str(record["platform"]), str(record["title"])
                )
                if target.exists() or target.is_symlink():
                    existing = self._read_record(target)
                    if (
                        existing is not None
                        and str(existing.get("id", "")).lower() == str(record["id"]).lower()
                        and self._same_tree_contents(source, target)
                    ):
                        shutil.rmtree(source)
                        migrated += 1
                        continue
                    skipped += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                stage = target.parent / f".migrating-{self.normalize_song_id(record['id'])}"
                if stage.exists() or stage.is_symlink():
                    skipped += 1
                    continue
                stage.mkdir()
                try:
                    shutil.copytree(source, stage, symlinks=True, dirs_exist_ok=True)
                except OSError:
                    shutil.rmtree(stage, ignore_errors=True)
                    raise
                copied = self._read_record(stage)
                if copied is None or str(copied.get("id", "")).lower() != str(record["id"]).lower():
                    shutil.rmtree(stage, ignore_errors=True)
                    skipped += 1
                    continue
                stage.rename(target)
                # The source remains until the complete copied record is readable.
                shutil.rmtree(source)
                migrated += 1
            except (OSError, ValueError, TypeError):
                skipped += 1
        try:
            source_root.rmdir()
        except OSError:
            pass
        return {"migrated": migrated, "skipped": skipped}

    @staticmethod
    def _same_tree_contents(first: Path, second: Path) -> bool:
        try:
            first_paths = sorted((path.relative_to(first) for path in first.rglob("*")), key=lambda path: path.as_posix())
            second_paths = sorted((path.relative_to(second) for path in second.rglob("*")), key=lambda path: path.as_posix())
            if first_paths != second_paths:
                return False
            for relative in first_paths:
                left, right = first / relative, second / relative
                if left.is_symlink() or right.is_symlink():
                    if not (
                        left.is_symlink()
                        and right.is_symlink()
                        and left.readlink().as_posix() == right.readlink().as_posix()
                    ):
                        return False
                elif left.is_dir() or right.is_dir():
                    if not (left.is_dir() and right.is_dir()):
                        return False
                elif left.is_file() and right.is_file():
                    if not filecmp.cmp(left, right, shallow=False):
                        return False
                else:
                    return False
            return True
        except OSError:
            return False

    def _read_legacy_record(self, directory: Path, source_root: Path) -> dict[str, Any] | None:
        if directory.is_symlink() or not directory.is_dir():
            return None
        try:
            resolved = directory.resolve()
            if resolved.parent != source_root.resolve():
                return None
            song_id = self.normalize_song_id(directory.name)
            metadata_path = directory / "metadata.json"
            if metadata_path.is_symlink():
                return None
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or self.normalize_song_id(data.get("id", "")) != song_id:
                return None
            if not isinstance(data.get("platform"), str) or not data["platform"].strip():
                return None
            if not isinstance(data.get("title"), str) or not data["title"].strip():
                return None
            if not isinstance(data.get("files"), list):
                return None
            # Ensure the record has at least one safe, in-folder file reference.
            if not any(
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and self._valid_filename(item["name"])
                and (resolved / item["name"]).resolve().is_relative_to(resolved)
                and (resolved / item["name"]).is_file()
                for item in data["files"]
            ):
                return None
            return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
