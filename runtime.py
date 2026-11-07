"""Asynchronous audio download, conversion, delivery, and retention runtime."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import subprocess
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import math
import wave
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Awaitable, Callable

from .audio import AudioError, generate_test_audio, probe_audio, transcode_and_segment
from .cover import CoverClient, CoverServiceError
from .environment import EnvironmentManager, get_node_tools
from .paths import data_subdirectory, is_path_link
from .providers import _normalize_track_link
from .storage import MusicStorage, parse_time, utc_now


SendAudioCallback = Callable[[Path], Awaitable[Any]]
_MAX_AUDIO_BYTES = 250 * 1024 * 1024
_COVER_PIPELINE_VERSION = "2"
_COVER_JOB_TIMEOUT_SECONDS = 60 * 60
_COVER_INCREMENTAL_HEARTBEAT_SECONDS = 10.0
_COVER_INCREMENTAL_POLL_SECONDS = 0.35
_COVER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COVER_SEGMENT_PATTERN = re.compile(r"^segment_[0-9]{3,}\.wav$")


async def _cancel_cover_job(client: CoverClient, job_id: str) -> tuple[bool, str]:
    """Best-effort cleanup for an owned job, including while its caller is cancelling."""
    task = asyncio.create_task(client.cancel_job(job_id))
    try:
        response = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:
            pass
        raise
    except Exception as exc:
        return False, str(exc) or exc.__class__.__name__
    return response.get("released") is True and response.get("pending") is not True, ""


async def prepare_node(data_dir: Path) -> dict[str, Any]:
    """Install the bundled Node 22+ runtime on an explicit dependency request."""
    manager = EnvironmentManager(data_dir)
    try:
        node, npm_cli = await asyncio.to_thread(get_node_tools, data_dir)
        return {
            "tool_id": "node",
            "installed": True,
            "path": str(node),
            "version": "available",
            "message": f"Node.js与npm可用：{npm_cli.name}",
        }
    except RuntimeError:
        return await manager.install_tool("node")


class MusicRuntime:
    def __init__(self, data_dir: Path, providers: Any, downloads_dir: Path | None = None):
        self.data_dir = data_subdirectory(Path(data_dir).expanduser())
        self.providers = providers
        self.storage = MusicStorage(self.data_dir, downloads_dir)
        self.environment = EnvironmentManager(self.data_dir, providers)
        self._lock = asyncio.Lock()
        self._pins: dict[str, int] = {}
        self._downloads: set[str] = set()
        self._source_downloads: dict[tuple[str, str], asyncio.Event] = {}
        self._cleanup_task: asyncio.Task[Any] | None = None
        self._cleanup_stop = asyncio.Event()

    async def _resolve(self, platform_name: str, query: str) -> dict[str, Any]:
        method = getattr(self.providers, "resolve", None)
        if not callable(method):
            method = getattr(self.providers, "resolve_download", None)
        if not callable(method):
            raise RuntimeError("音源协议管理器没有提供下载查询入口")
        if inspect.iscoroutinefunction(method):
            result = await method(platform_name, query)
        else:
            result = await self._run_thread_safely(method, platform_name, query)
            if inspect.isawaitable(result):
                result = await result
        if not isinstance(result, dict):
            raise RuntimeError("音源查询没有返回可下载的音频信息")
        return result

    async def _run_thread_safely(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    @staticmethod
    def _flag_is_true(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "trial", "preview", "试听"}
        return bool(value)

    async def download(self, platform: str, query: str) -> dict[str, Any]:
        """Download an already selected candidate, identified by its exact query."""
        return await self.download_selected(platform, query)

    async def download_selected(self, platform: str, query: str) -> dict[str, Any]:
        platform = str(platform)
        query = str(query)
        source_query = self._source_query_key(platform, query)
        if source_query:
            query_id = source_query.partition(":")[2]
            async with self._lock:
                existing = self.storage.find_source_track(platform, query_id)
                if existing is None:
                    existing = self.storage.find_source_query(platform, source_query)
            if existing is not None:
                return {**existing, "reused": True}

        resolved = await self._resolve(platform, query)
        source_track_id = str(
            resolved.get("source_track_id") or resolved.get("track_id") or resolved.get("id") or ""
        ).strip()
        if source_track_id:
            key = (platform, source_track_id)
            while True:
                async with self._lock:
                    existing = self.storage.find_source_track(platform, source_track_id)
                    if existing is not None:
                        return {**existing, "reused": True}
                    event = self._source_downloads.get(key)
                    if event is None:
                        event = asyncio.Event()
                        self._source_downloads[key] = event
                        owner = True
                    else:
                        owner = False
                if owner:
                    try:
                        return await self._download_resolved(
                            platform, query, resolved, source_track_id, source_query
                        )
                    finally:
                        async with self._lock:
                            if self._source_downloads.get(key) is event:
                                self._source_downloads.pop(key, None)
                            event.set()
                await event.wait()
        return await self._download_resolved(platform, query, resolved, "", source_query)

    @staticmethod
    def _source_query_key(platform: str, query: str) -> str:
        link = _normalize_track_link(query, platform)
        if not link:
            return ""
        for key in ("mid", "id", "hash"):
            value = link.get(key)
            if value:
                return f"{key}:{value}"
        return ""

    async def _download_resolved(
        self,
        platform: str,
        query: str,
        resolved: dict[str, Any],
        source_track_id: str,
        source_query: str = "",
    ) -> dict[str, Any]:
        if any(self._flag_is_true(resolved.get(key)) for key in ("trial", "is_trial", "preview", "is_preview", "encrypted", "is_encrypted")):
            raise AudioError("音源返回试听或加密内容，已拒绝下载")
        url = resolved.get("url")
        if not isinstance(url, str) or not self._valid_http_url(url):
            raise AudioError("音源没有返回可直接下载的HTTP(S)完整音频地址")
        headers = resolved.get("headers", {})
        if not isinstance(headers, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()):
            raise AudioError("音源返回了无效的下载请求头")

        song_id = str(uuid.uuid4())
        title = str(resolved.get("title") or query)
        pending = self.storage.pending_dir(song_id, platform)
        final_dir = self.storage.new_song_dir(song_id, platform, title)
        async with self._lock:
            self._downloads.add(song_id)
        try:
            pending.parent.mkdir(parents=True, exist_ok=True)
            pending.mkdir(parents=True, exist_ok=False)
            original = pending / "original.bin"
            download_task = asyncio.create_task(
                asyncio.to_thread(self._download_http, url, headers, original)
            )
            try:
                content_type, byte_count = await asyncio.shield(download_task)
            except asyncio.CancelledError:
                try:
                    await download_task
                except Exception:
                    pass
                raise
            probe = await probe_audio(original, self._required_tool("ffprobe"))
            extension = self._original_audio_extension(probe)
            if extension != ".bin":
                named_original = pending / f"original{extension}"
                original.replace(named_original)
                original = named_original
            expected_duration = resolved.get("duration")
            try:
                expected_duration = float(expected_duration)
            except (TypeError, ValueError):
                expected_duration = 0.0
            if expected_duration > 0:
                tolerance = max(2.0, expected_duration * 0.03)
                if probe["duration"] < expected_duration - tolerance:
                    raise AudioError(
                        "下载音频明显短于音源标注时长，可能是试听片段或不完整文件"
                    )
            converted = await transcode_and_segment(
                original,
                pending,
                self._required_tool("ffmpeg"),
                self._required_tool("ffprobe"),
            )
            finished = utc_now()
            record = {
                "id": song_id,
                "platform": platform,
                "title": title,
                "artist": str(resolved.get("artist") or ""),
                "downloaded_at": finished.isoformat(),
                "duration": converted["duration"],
                "source_duration": probe["duration"],
                "source_format": str(probe.get("format", {}).get("format_name", "")),
                "content_type": content_type,
                "source_size": byte_count,
                "sample_rate": converted["sample_rate"],
                "frames": converted["frames"],
                "files": [
                    {"name": original.name, "size": original.stat().st_size, "kind": "original"},
                    {"name": converted["transcoded"]["name"], "size": converted["transcoded"]["size"], "kind": "transcoded"},
                    *[
                        {"name": item["name"], "size": item["size"], "kind": "segment"}
                        for item in converted["segment_info"]
                    ],
                ],
                "segments": converted["segments"],
            }
            if source_track_id:
                record["source_track_id"] = source_track_id
            if source_query:
                record["source_query"] = source_query
            self.storage.write_pending_record(record, pending)
            pending.replace(final_dir)
            return record
        finally:
            if pending.exists() and not pending.is_symlink():
                shutil.rmtree(pending, ignore_errors=True)
            async with self._lock:
                if final_dir.exists():
                    self._cleanup_locked()
                self._downloads.discard(song_id)

    @staticmethod
    def _valid_http_url(url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _original_audio_extension(probe: dict[str, Any]) -> str:
        format_data = probe.get("format", {})
        format_names = {
            item.strip().lower()
            for item in str(format_data.get("format_name", "")).split(",")
            if item.strip()
        } if isinstance(format_data, dict) else set()
        streams = probe.get("streams", [])
        codecs = {
            str(stream.get("codec_name", "")).lower()
            for stream in streams
            if isinstance(stream, dict)
        }
        if "mp3" in format_names or "mp3" in codecs:
            return ".mp3"
        if "flac" in format_names or "flac" in codecs:
            return ".flac"
        if "wav" in format_names or any(codec.startswith("pcm_") for codec in codecs):
            return ".wav"
        if {"m4a", "mp4", "mov"} & format_names:
            return ".m4a"
        if "aac" in format_names:
            return ".aac"
        if "opus" in codecs:
            return ".opus"
        if "ogg" in format_names or "vorbis" in codecs:
            return ".ogg"
        return ".bin"

    @staticmethod
    def _download_http(url: str, headers: dict[str, str], destination: Path) -> tuple[str, int]:
        request_headers = {"User-Agent": "BetterMusic/0.2.0", **headers}
        request = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(request, timeout=45) as response:
            if not MusicRuntime._valid_http_url(response.geturl()):
                raise AudioError("音频下载跳转到了不支持的地址")
            content_type = str(response.headers.get_content_type() or "application/octet-stream").lower()
            if content_type.startswith("text/") or any(
                marker in content_type for marker in ("html", "json", "xml", "mpegurl", "x-mpegurl")
            ):
                raise AudioError("下载地址返回网页、数据或播放列表，不是音频文件")
            length_header = response.headers.get("Content-Length")
            if length_header and int(length_header) > _MAX_AUDIO_BYTES:
                raise AudioError("音频文件超过250 MiB限制")
            total = 0
            first = response.read(4096)
            sniff = first.lstrip().lower()
            if sniff.startswith((b"<!doctype html", b"<html", b"<?xml", b"{\"", b"[{")):
                raise AudioError("下载内容看起来是网页或文本数据，不是音频")
            with destination.open("wb") as output:
                if first:
                    output.write(first)
                    total += len(first)
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > _MAX_AUDIO_BYTES:
                        raise AudioError("音频文件超过250 MiB限制")
                    output.write(block)
        if total == 0:
            destination.unlink(missing_ok=True)
            raise AudioError("音频下载内容为空")
        return content_type, total

    def _required_tool(self, tool_name: str) -> Path:
        path = self.environment._ffmpeg_path(tool_name)
        if path is None:
            raise RuntimeError(f"缺少{tool_name}，请先在插件环境设置中安装FFmpeg工具")
        return path

    async def send_song(
        self,
        song_id: str,
        send_audio_callback: SendAudioCallback,
        *,
        max_seconds: int = 0,
        gap_seconds: float = 0,
    ) -> dict[str, Any]:
        if not isinstance(max_seconds, int) or isinstance(max_seconds, bool) or max_seconds < 0:
            raise ValueError("max_seconds必须是非负整数")
        try:
            numeric_gap = float(gap_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("gap_seconds必须是有限非负数") from None
        if (
            not isinstance(gap_seconds, (int, float))
            or isinstance(gap_seconds, bool)
            or not math.isfinite(numeric_gap)
            or numeric_gap < 0
        ):
            raise ValueError("gap_seconds必须是有限非负数")
        gap_seconds = numeric_gap
        async with self._lock:
            record = self.storage.get_record(song_id)
            if record is None:
                return {"sent": 0, "total": 0, "failed_segment": None, "message": "歌曲记录不存在"}
            segments = list(record.get("segments", []))
            if not segments:
                return {"sent": 0, "total": 0, "failed_segment": None, "message": "歌曲没有可发送的分段"}
            normalized_id = self.storage.normalize_song_id(song_id)
            self._pins[normalized_id] = self._pins.get(normalized_id, 0) + 1

        sent = 0
        sent_seconds = 0.0
        planned_seconds = 0.0
        capped = False
        temporary_dir: Path | None = None
        try:
            # Keep the legacy path byte-for-byte compatible when no time cap
            # is requested; older stored segments are not always PCM WAVs.
            if max_seconds == 0:
                for index, filename in enumerate(segments, start=1):
                    try:
                        path = self.storage.file_path(song_id, filename, allow_expired=True)
                        outcome = send_audio_callback(path)
                        if inspect.isawaitable(outcome):
                            await outcome
                        sent += 1
                        if gap_seconds and index < len(segments):
                            await asyncio.sleep(gap_seconds)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        return {
                            "sent": sent,
                            "total": len(segments),
                            "failed_segment": filename,
                            "message": f"第{index}段发送失败：{exc}",
                        }
                return {
                    "sent": sent,
                    "total": len(segments),
                    "failed_segment": None,
                    "message": "发送完成",
                }

            # Read duration from PCM frames rather than metadata, which may be
            # missing or stale. Only the final selected segment is rewritten.
            plan: list[tuple[str, Path, int, int, int]] = []
            remaining_seconds = Fraction(max_seconds, 1) if max_seconds else None
            failed = segments[0]
            try:
                for filename in segments:
                    failed = filename
                    path = self.storage.file_path(song_id, filename, allow_expired=True)
                    with wave.open(str(path), "rb") as audio:
                        params = audio.getparams()
                        if params.comptype != "NONE" or params.framerate <= 0:
                            raise ValueError("分段不是有效的PCM WAV音频")
                        frames = params.nframes
                        rate = params.framerate
                    if frames < 0:
                        raise ValueError("WAV采样帧数无效")
                    duration = frames / rate
                    if remaining_seconds is None:
                        selected_frames = frames
                    elif remaining_seconds <= 0:
                        if frames:
                            capped = True
                        break
                    else:
                        selected_frames = min(frames, int(remaining_seconds * rate))
                        if selected_frames < frames:
                            capped = True
                    if selected_frames > 0:
                        plan.append((filename, path, selected_frames, rate, frames))
                        selected_duration = selected_frames / rate
                        planned_seconds += selected_duration
                        if remaining_seconds is not None:
                            remaining_seconds = max(
                                Fraction(0, 1),
                                remaining_seconds - Fraction(selected_frames, rate),
                            )
                    if selected_frames < frames:
                        break
            except (OSError, ValueError, wave.Error, EOFError) as exc:
                return {
                    "sent": 0,
                    "total": len(plan),
                    "failed_segment": failed,
                    "message": f"读取PCM WAV分段失败（{failed}）：{exc}",
                    "sent_seconds": sent_seconds,
                    "planned_seconds": planned_seconds,
                    "capped": capped,
                }

            if not plan:
                return {
                    "sent": 0,
                    "total": 0,
                    "failed_segment": None,
                    "message": "歌曲没有可发送的音频帧",
                    "sent_seconds": sent_seconds,
                    "planned_seconds": planned_seconds,
                    "capped": capped,
                }

            if any(selected_frames < original_frames for _, _, selected_frames, _, original_frames in plan):
                song_dir = self.storage.song_dir(song_id)
                temporary_dir = Path(tempfile.mkdtemp(prefix=".playback-", dir=song_dir))

            prepared: list[tuple[str, Path, float]] = []
            for filename, path, selected_frames, rate, original_frames in plan:
                try:
                    send_path = path
                    if selected_frames < original_frames:
                        if temporary_dir is None:
                            raise RuntimeError("无法创建临时裁切目录")
                        send_path = temporary_dir / filename
                        with wave.open(str(path), "rb") as source:
                            params = source.getparams()
                            samples = source.readframes(selected_frames)
                        frame_width = params.nchannels * params.sampwidth
                        if len(samples) != selected_frames * frame_width:
                            raise ValueError("WAV实际采样帧少于已读取的帧数")
                        with wave.open(str(send_path), "wb") as output:
                            output.setnchannels(params.nchannels)
                            output.setsampwidth(params.sampwidth)
                            output.setframerate(params.framerate)
                            output.writeframes(samples)
                    prepared.append((filename, send_path, selected_frames / rate))
                except (OSError, ValueError, wave.Error, EOFError, RuntimeError) as exc:
                    return {
                        "sent": 0,
                        "total": len(plan),
                        "failed_segment": filename,
                        "message": f"准备第{len(prepared) + 1}段音频失败：{exc}",
                        "sent_seconds": sent_seconds,
                        "planned_seconds": planned_seconds,
                        "capped": capped,
                    }

            for index, (filename, path, duration) in enumerate(prepared, start=1):
                try:
                    outcome = send_audio_callback(path)
                    if inspect.isawaitable(outcome):
                        await outcome
                    sent += 1
                    sent_seconds += duration
                    if gap_seconds and index < len(prepared):
                        await asyncio.sleep(gap_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return {
                        "sent": sent,
                        "total": len(prepared),
                        "failed_segment": filename,
                        "message": f"第{index}段发送失败：{exc}",
                        "sent_seconds": sent_seconds,
                        "planned_seconds": planned_seconds,
                        "capped": capped,
                    }
            return {
                "sent": sent,
                "total": len(prepared),
                "failed_segment": None,
                "message": "发送完成",
                "sent_seconds": sent_seconds,
                "planned_seconds": planned_seconds,
                "capped": capped,
            }
        finally:
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)
            async with self._lock:
                current = self._pins.get(normalized_id, 0)
                if current <= 1:
                    self._pins.pop(normalized_id, None)
                else:
                    self._pins[normalized_id] = current - 1
                self._cleanup_locked()

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _cap_cover_segments(
        directory: Path,
        segment_info: list[dict[str, Any]],
        max_seconds: int,
    ) -> tuple[list[str], list[float], float, bool]:
        segments = []
        full_duration = Fraction(0, 1)
        for item in segment_info:
            name = str(item.get("name") or "")
            if not name or Path(name).name != name or "\\" in name:
                raise AudioError("翻唱服务生成了无效的分段文件名")
            path = directory / name
            with wave.open(str(path), "rb") as audio:
                params = audio.getparams()
                if params.comptype != "NONE" or params.framerate <= 0 or params.nframes < 0:
                    raise AudioError(f"翻唱音频分段无效：{name}")
                frames = params.nframes
                rate = params.framerate
            duration = Fraction(frames, rate)
            full_duration += duration
            segments.append((name, path, params, frames, rate))

        remaining = Fraction(max_seconds, 1) if max_seconds else None
        kept: list[str] = []
        durations: list[float] = []
        planned = Fraction(0, 1)
        capped = bool(max_seconds and full_duration > max_seconds)
        for index, (name, path, params, frames, rate) in enumerate(segments):
            if remaining is not None and remaining <= 0:
                break
            selected = frames if remaining is None else min(frames, int(remaining * rate))
            if selected <= 0:
                break
            if selected < frames:
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    with wave.open(str(path), "rb") as source:
                        source_params = source.getparams()
                        samples = source.readframes(selected)
                    frame_width = source_params.nchannels * source_params.sampwidth
                    if len(samples) != selected * frame_width:
                        raise AudioError(f"翻唱音频分段不完整：{name}")
                    with wave.open(str(temporary), "wb") as output:
                        output.setnchannels(source_params.nchannels)
                        output.setsampwidth(source_params.sampwidth)
                        output.setframerate(source_params.framerate)
                        output.writeframes(samples)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
                capped = True
            kept.append(name)
            duration = Fraction(selected, rate)
            durations.append(float(duration))
            planned += duration
            if remaining is not None:
                remaining = max(Fraction(0, 1), remaining - duration)
            if selected < frames:
                break
        if len(kept) < len(segments):
            capped = capped or bool(max_seconds)
        kept_set = set(kept)
        for name, path, *_ in segments:
            if name not in kept_set:
                path.unlink(missing_ok=True)
        if not kept:
            raise AudioError("翻唱音频没有可发送的采样帧")
        return kept, durations, float(planned), capped

    @staticmethod
    def _read_cover_cache(
        cache_dir: Path,
        *,
        cache_key: str,
        source_sha256: str,
        voice_id: str,
        voice_version: str,
        max_seconds: int,
    ) -> dict[str, Any] | None:
        try:
            if is_path_link(cache_dir) or not cache_dir.is_dir():
                return None
            metadata_path = cache_dir / "cover.json"
            if metadata_path.is_symlink():
                return None
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                not isinstance(metadata, dict)
                or metadata.get("cache_key") != cache_key
                or metadata.get("source_sha256") != source_sha256
                or metadata.get("voice_id") != voice_id
                or metadata.get("voice_version") != voice_version
                or metadata.get("max_seconds") != max_seconds
                or metadata.get("pipeline_version") != _COVER_PIPELINE_VERSION
                or not isinstance(metadata.get("segments"), list)
            ):
                return None
            segments: list[str] = []
            durations: list[float] = []
            for name in metadata["segments"]:
                if not isinstance(name, str) or Path(name).name != name or "\\" in name:
                    return None
                path = cache_dir / name
                if path.is_symlink() or not path.is_file():
                    return None
                with wave.open(str(path), "rb") as audio:
                    params = audio.getparams()
                    if params.comptype != "NONE" or params.framerate <= 0 or params.nframes <= 0:
                        return None
                    durations.append(params.nframes / params.framerate)
                segments.append(name)
            if not segments:
                return None
            return {
                **metadata,
                "segments": segments,
                "segment_durations": durations,
                "planned_seconds": sum(durations),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError, wave.Error, EOFError):
            return None

    @staticmethod
    def _cover_wav_info(path: Path) -> dict[str, Any] | None:
        if is_path_link(path) or not path.is_file():
            return None
        try:
            size = path.stat().st_size
            if size <= 44:
                return None
            with wave.open(str(path), "rb") as audio:
                params = audio.getparams()
                if (
                    params.comptype != "NONE"
                    or params.framerate <= 0
                    or params.nframes <= 0
                    or params.nchannels <= 0
                    or params.sampwidth <= 0
                ):
                    return None
                duration = params.nframes / params.framerate
                return {
                    "size": size,
                    "duration": duration,
                    "sample_rate": params.framerate,
                    "channels": params.nchannels,
                    "sample_width": params.sampwidth,
                    "format": "wav/pcm",
                }
        except (OSError, ValueError, wave.Error, EOFError, ZeroDivisionError):
            return None

    def _read_public_cover(self, song_id: str, cache_dir: Path) -> dict[str, Any] | None:
        """Read only a safe manifest and WAV headers to build public cache metadata."""
        try:
            cover_id = cache_dir.name
            if not _COVER_ID_PATTERN.fullmatch(cover_id):
                return None
            metadata_path = cache_dir / "cover.json"
            if is_path_link(metadata_path) or not metadata_path.is_file():
                return None
            if metadata_path.stat().st_size > 1024 * 1024:
                return None
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                not isinstance(metadata, dict)
                or metadata.get("cache_key") != cover_id
                or not isinstance(metadata.get("voice_id"), str)
                or not metadata.get("voice_id", "").strip()
                or not isinstance(metadata.get("voice_version"), (str, int, float))
                or not str(metadata.get("voice_version", "")).strip()
                or not isinstance(metadata.get("segments"), list)
                or isinstance(metadata.get("max_seconds"), bool)
                or not isinstance(metadata.get("max_seconds"), int)
                or metadata["max_seconds"] < 0
            ):
                return None
            voice_id = metadata["voice_id"].strip()
            voice_name = metadata.get("voice_name")
            if not isinstance(voice_name, str) or not voice_name.strip():
                voice_name = voice_id
            voice_version = str(metadata["voice_version"]).strip()
            if len(voice_id) > 200 or len(voice_name) > 200 or len(voice_version) > 200:
                return None

            segments = metadata["segments"]
            if not segments or any(
                not isinstance(name, str) or not _COVER_SEGMENT_PATTERN.fullmatch(name)
                for name in segments
            ) or len(set(segments)) != len(segments):
                return None

            resolved_dir = cache_dir.resolve()
            actual_dir = self.storage.cover_cache_dir(song_id, cover_id)
            if actual_dir != resolved_dir:
                return None
            files: list[dict[str, Any]] = []
            generated = self._cover_wav_info(cache_dir / "generated.bin")
            if generated is not None:
                files.append({
                    "name": "cover.wav",
                    "kind": "cover",
                    **generated,
                })
            normalized = self._cover_wav_info(cache_dir / "normalized.wav")
            if normalized is not None:
                files.append({
                    "name": "normalized.wav",
                    "kind": "normalized",
                    **normalized,
                })

            segment_files: list[dict[str, Any]] = []
            for name in segments:
                info = self._cover_wav_info(cache_dir / name)
                if info is None:
                    return None
                segment_files.append({"name": name, "kind": "segment", **info})
            files.extend(segment_files)

            pipeline_version = str(metadata.get("pipeline_version") or "1")
            qq_ready = bool(segment_files) and all(
                item["duration"] <= 60.0
                and item["sample_rate"] == 16_000
                and item["channels"] == 1
                and item["sample_width"] == 2
                and item["format"] == "wav/pcm"
                for item in segment_files
            )
            usable = pipeline_version == _COVER_PIPELINE_VERSION and qq_ready
            if pipeline_version != _COVER_PIPELINE_VERSION:
                status_message = "旧版翻唱缓存仅可下载，不能发送；请重新生成翻唱。"
            elif not qq_ready:
                status_message = "翻唱缓存分段不符合 QQ 发送格式，不能发送。"
            else:
                status_message = "翻唱缓存可发送。"

            created = parse_time(metadata.get("created_at"))
            if created is None:
                created = datetime.fromtimestamp(cache_dir.stat().st_mtime, tz=timezone.utc)
            duration = sum(item["duration"] for item in segment_files)
            return {
                "id": cover_id,
                "voice_id": voice_id,
                "voice_name": voice_name.strip(),
                "voice_version": voice_version,
                "pipeline_version": pipeline_version,
                "usable": usable,
                "status_message": status_message,
                "duration": duration,
                "created_at": created.isoformat(),
                "max_seconds": metadata["max_seconds"],
                "segments": [item["name"] for item in segment_files],
                "segment_count": len(segment_files),
                "files": files,
                "capped": metadata.get("capped") is True,
            }
        except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def list_covers(self, song_id: str) -> list[dict[str, Any]]:
        try:
            cache_dirs = self.storage.cover_cache_dirs(song_id)
        except (FileNotFoundError, ValueError, OSError):
            return []
        covers = []
        for cache_dir in cache_dirs:
            cover = self._read_public_cover(song_id, cache_dir)
            if cover is not None:
                covers.append(cover)
        covers.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        return covers

    def cover_file_path(self, song_id: str, cover_id: str, filename: str) -> Path:
        if not isinstance(filename, str):
            raise FileNotFoundError("翻唱缓存文件不存在")
        cache_dir = self.storage.cover_cache_dir(song_id, cover_id)
        cover = self._read_public_cover(song_id, cache_dir)
        if cover is None:
            raise FileNotFoundError("翻唱缓存不可用")
        entry = next((item for item in cover["files"] if item["name"] == filename), None)
        if entry is None and filename == "audio.wav":
            entry = next((item for item in cover["files"] if item["name"] == "cover.wav"), None)
        if entry is None:
            raise FileNotFoundError("该文件不属于翻唱缓存")
        disk_name = "generated.bin" if entry["name"] == "cover.wav" else entry["name"]
        path = cache_dir / disk_name
        if is_path_link(path):
            raise FileNotFoundError("翻唱缓存文件不可用")
        resolved = path.resolve()
        if not resolved.is_relative_to(cache_dir.resolve()) or not resolved.is_file():
            raise FileNotFoundError("翻唱缓存文件不存在")
        return resolved

    async def send_cached_cover(
        self,
        song_id: str,
        cover_id: str,
        send_audio_callback: SendAudioCallback,
    ) -> dict[str, Any]:
        output: dict[str, Any] = {
            "sent": 0,
            "total": 0,
            "failed_segment": None,
            "sent_seconds": 0.0,
            "planned_seconds": 0.0,
            "capped": False,
            "message": "",
            "voice_id": "",
            "voice_name": "",
            "voice_version": "",
            "cover_reused": True,
            "cover_id": cover_id if isinstance(cover_id, str) and _COVER_ID_PATTERN.fullmatch(cover_id) else "",
        }
        normalized_id = ""
        pinned = False
        try:
            async with self._lock:
                record = self.storage.get_record(song_id)
                if record is None:
                    output["message"] = "歌曲记录不存在"
                    return output
                try:
                    normalized_id = self.storage.normalize_song_id(song_id)
                    cache_dir = self.storage.cover_cache_dir(song_id, cover_id)
                    cover = self._read_public_cover(song_id, cache_dir)
                except (ValueError, FileNotFoundError, OSError):
                    cover = None
                if cover is None:
                    output["message"] = "翻唱缓存不存在或不可用"
                    return output
                if cover.get("usable") is not True:
                    output["message"] = str(
                        cover.get("status_message") or "翻唱缓存不能发送，请重新生成翻唱。"
                    )
                    return output
                segment_files = [
                    item for item in cover["files"] if item.get("kind") == "segment"
                ]
                if not segment_files:
                    output["message"] = "翻唱缓存没有可发送的分段"
                    return output
                if any(
                    float(item.get("duration", 0)) > 60.0
                    or item.get("sample_rate") != 16_000
                    or item.get("channels") != 1
                    or item.get("sample_width") != 2
                    or item.get("format") != "wav/pcm"
                    for item in segment_files
                ):
                    output["message"] = "翻唱缓存分段不符合QQ发送格式"
                    return output
                try:
                    paths = [
                        self.cover_file_path(song_id, cover_id, str(item["name"]))
                        for item in segment_files
                    ]
                except (ValueError, FileNotFoundError, OSError):
                    output["message"] = "翻唱缓存分段不可用"
                    return output
                self._pins[normalized_id] = self._pins.get(normalized_id, 0) + 1
                pinned = True
                output.update({
                    "voice_id": cover["voice_id"],
                    "voice_name": cover["voice_name"],
                    "voice_version": cover["voice_version"],
                    "total": len(segment_files),
                    "planned_seconds": float(cover["duration"]),
                    "capped": cover["capped"],
                })

            for index, (item, path) in enumerate(zip(segment_files, paths), start=1):
                name = str(item["name"])
                duration = float(item["duration"])
                try:
                    outcome = send_audio_callback(path)
                    if inspect.isawaitable(outcome):
                        await outcome
                    output["sent"] += 1
                    output["sent_seconds"] += duration
                except asyncio.CancelledError:
                    raise
                except Exception:
                    output["failed_segment"] = name
                    output["message"] = f"第{index}段发送失败"
                    return output
            output["message"] = "发送完成"
            return output
        except asyncio.CancelledError:
            raise
        finally:
            if pinned:
                async with self._lock:
                    current = self._pins.get(normalized_id, 0)
                    if current <= 1:
                        self._pins.pop(normalized_id, None)
                    else:
                        self._pins[normalized_id] = current - 1
                    self._cleanup_locked()

    async def _prepare_cover_cache(
        self,
        client: CoverClient,
        job_id: str,
        song_dir: Path,
        covers_dir: Path,
        cache_dir: Path,
        *,
        cache_key: str,
        source_sha256: str,
        source_duration: float,
        voice: dict[str, Any],
        voice_id: str,
        voice_version: str,
        max_seconds: int,
    ) -> tuple[dict[str, Any], bool]:
        if is_path_link(covers_dir):
            raise AudioError("翻唱缓存目录不能是符号链接")
        covers_dir.mkdir(parents=True, exist_ok=True)
        if song_dir.is_symlink() or not covers_dir.resolve().is_relative_to(song_dir.resolve()):
            raise AudioError("翻唱缓存目录超出歌曲目录")
        cached = self._read_cover_cache(
            cache_dir,
            cache_key=cache_key,
            source_sha256=source_sha256,
            voice_id=voice_id,
            voice_version=voice_version,
            max_seconds=max_seconds,
        )
        if cached is not None:
            return cached, True

        pending_dir = covers_dir / f".pending-{uuid.uuid4().hex}"
        pending_dir.mkdir()
        try:
            generated = pending_dir / "generated.bin"
            await client.download_audio(job_id, generated)
            converted = await transcode_and_segment(
                generated,
                pending_dir,
                self._required_tool("ffmpeg"),
                self._required_tool("ffprobe"),
            )
            segment_names, segment_durations, planned_seconds, output_capped = await self._run_thread_safely(
                self._cap_cover_segments,
                pending_dir,
                converted["segment_info"],
                max_seconds,
            )
            source_capped = bool(max_seconds and source_duration > max_seconds)
            metadata = {
                "cache_key": cache_key,
                "source_sha256": source_sha256,
                "voice_id": voice_id,
                "voice_name": str(voice.get("name") or voice_id),
                "voice_version": voice_version,
                "created_at": utc_now().isoformat(),
                "max_seconds": max_seconds,
                "pipeline_version": _COVER_PIPELINE_VERSION,
                "render_mode": "whole",
                "source_duration": source_duration,
                "planned_seconds": planned_seconds,
                "capped": source_capped or output_capped,
                "segments": segment_names,
            }
            metadata_temp = pending_dir / ".cover.json.tmp"
            metadata_temp.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            metadata_path = pending_dir / "cover.json"
            metadata_temp.replace(metadata_path)
            async with self._lock:
                winner = self._read_cover_cache(
                    cache_dir,
                    cache_key=cache_key,
                    source_sha256=source_sha256,
                    voice_id=voice_id,
                    voice_version=voice_version,
                    max_seconds=max_seconds,
                )
                if winner is not None:
                    return winner, True
                if is_path_link(cache_dir):
                    raise AudioError("翻唱缓存路径不能是符号链接")
                if cache_dir.exists():
                    shutil.rmtree(cache_dir)
                pending_dir.replace(cache_dir)
                pending_dir = None
            entry = self._read_cover_cache(
                cache_dir,
                cache_key=cache_key,
                source_sha256=source_sha256,
                voice_id=voice_id,
                voice_version=voice_version,
                max_seconds=max_seconds,
            )
            if entry is None:
                raise AudioError("翻唱缓存写入后校验失败")
            return entry, False
        finally:
            if pending_dir is not None:
                shutil.rmtree(pending_dir, ignore_errors=True)

    async def _wait_for_cover_job(self, client: CoverClient, job_id: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _COVER_JOB_TIMEOUT_SECONDS
        delay = 0.5
        while loop.time() < deadline:
            payload = await client.job_status(job_id)
            if isinstance(payload.get("job"), dict):
                job = dict(payload["job"])
                if "pipeline_version" not in job and "pipeline_version" in payload:
                    job["pipeline_version"] = payload["pipeline_version"]
            else:
                job = payload
            status = str(job.get("status", "")).lower()
            if status in {"completed", "complete", "succeeded"}:
                result = job.get("result")
                if (
                    str(job.get("pipeline_version", "")) != _COVER_PIPELINE_VERSION
                    or (
                        isinstance(result, dict)
                        and str(result.get("pipeline_version", "")) != _COVER_PIPELINE_VERSION
                    )
                ):
                    raise CoverServiceError(
                        "翻唱服务返回了不兼容的推理结果，请升级至推理管线 v2"
                    )
                return job
            if status in {"failed", "error", "cancelled", "canceled"}:
                detail = job.get("error")
                if not isinstance(detail, str) or not detail.strip():
                    detail = job.get("message")
                if isinstance(detail, str) and detail.strip():
                    raise CoverServiceError(f"翻唱服务处理任务失败：{detail.strip()[:200]}")
                raise CoverServiceError("翻唱服务处理任务失败")
            if status not in {
                "queued",
                "pending",
                "running",
                "processing",
                "separating",
                "converting",
                "mixing",
            }:
                raise CoverServiceError("翻唱服务返回了未知任务状态")
            await asyncio.sleep(min(delay, max(0.0, deadline - loop.time())))
            delay = min(3.5, delay + 0.35)
        raise CoverServiceError("翻唱任务超过 60 分钟")

    @staticmethod
    def _cover_job_fields(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CoverServiceError("翻唱服务返回了无效的任务状态")
        job = payload.get("job")
        if isinstance(job, dict):
            return {**payload, **job}
        return payload

    @staticmethod
    def _incremental_segment_fields(
        value: Any,
        sequence: int,
        *,
        max_duration_seconds: float,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CoverServiceError("翻唱服务返回了无效的分段清单")
        actual_sequence = value.get("sequence")
        start = value.get("start_seconds")
        duration = value.get("duration_seconds")
        size = value.get("size_bytes")
        digest = value.get("sha256")
        released = value.get("released")
        valid_number = lambda number: (
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and math.isfinite(float(number))
        )
        if (
            not isinstance(actual_sequence, int)
            or isinstance(actual_sequence, bool)
            or actual_sequence != sequence
            or not valid_number(start)
            or float(start) < 0
            or not valid_number(duration)
            or float(duration) <= 0
            or float(duration) > max_duration_seconds + 0.001
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 44
            or size > _MAX_AUDIO_BYTES
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(released) is not bool
        ):
            raise CoverServiceError(f"翻唱服务第{sequence}段元数据无效")
        return {
            "sequence": sequence,
            "start_seconds": float(start),
            "duration_seconds": float(duration),
            "size_bytes": size,
            "sha256": digest,
            "released": released,
        }

    @staticmethod
    def _write_wav_prefix(source_path: Path, destination: Path, frames_to_write: int) -> None:
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with wave.open(str(source_path), "rb") as source:
                params = source.getparams()
                if (
                    params.comptype != "NONE"
                    or frames_to_write <= 0
                    or frames_to_write > params.nframes
                    or params.nchannels <= 0
                    or params.sampwidth <= 0
                    or params.framerate <= 0
                ):
                    raise AudioError("翻唱服务分段无法按目标时长安全裁剪")
                with wave.open(str(temporary), "wb") as output:
                    output.setnchannels(params.nchannels)
                    output.setsampwidth(params.sampwidth)
                    output.setframerate(params.framerate)
                    remaining = frames_to_write
                    while remaining:
                        samples = source.readframes(min(remaining, 262_144))
                        if not samples:
                            raise AudioError("翻唱分段采样帧不完整")
                        written = len(samples) // (params.nchannels * params.sampwidth)
                        if written <= 0 or written > remaining:
                            raise AudioError("翻唱分段采样帧无效")
                        output.writeframes(samples)
                        remaining -= written
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _merge_incremental_wavs(
        directory: Path,
        segment_names: list[str],
        source_names: list[str],
    ) -> None:
        normalized = directory / "normalized.wav"
        temporary = directory / ".normalized.wav.tmp"
        try:
            output_params = None
            with wave.open(str(temporary), "wb") as combined:
                for name in segment_names:
                    path = directory / name
                    with wave.open(str(path), "rb") as segment:
                        params = segment.getparams()
                        if (
                            params.comptype != "NONE"
                            or params.nchannels != 1
                            or params.sampwidth != 2
                            or params.framerate != 16_000
                            or params.nframes <= 0
                        ):
                            raise AudioError(f"翻唱缓存分段格式无效：{name}")
                        if output_params is None:
                            output_params = params
                            combined.setnchannels(params.nchannels)
                            combined.setsampwidth(params.sampwidth)
                            combined.setframerate(params.framerate)
                        elif (
                            params.nchannels != output_params.nchannels
                            or params.sampwidth != output_params.sampwidth
                            or params.framerate != output_params.framerate
                        ):
                            raise AudioError("翻唱缓存分段格式不一致")
                        while True:
                            samples = segment.readframes(262_144)
                            if not samples:
                                break
                            combined.writeframes(samples)
            if output_params is None:
                raise AudioError("翻唱结果没有可合并的音频分段")
            temporary.replace(normalized)
            generated_temp = directory / ".generated.bin.tmp"
            try:
                source_dir = directory / ".sources"
                source_params = None
                with wave.open(str(generated_temp), "wb") as generated:
                    for name in source_names:
                        path = source_dir / name
                        with wave.open(str(path), "rb") as segment:
                            params = segment.getparams()
                            if (
                                params.comptype != "NONE"
                                or params.nchannels <= 0
                                or params.sampwidth <= 0
                                or params.framerate <= 0
                                or params.nframes <= 0
                            ):
                                raise AudioError(f"翻唱源音频分段格式无效：{name}")
                            signature = (
                                params.nchannels,
                                params.sampwidth,
                                params.framerate,
                                params.comptype,
                            )
                            if source_params is None:
                                source_params = signature
                                generated.setnchannels(params.nchannels)
                                generated.setsampwidth(params.sampwidth)
                                generated.setframerate(params.framerate)
                            elif source_params != signature:
                                raise AudioError("翻唱源音频分段格式不一致")
                            while True:
                                samples = segment.readframes(131_072)
                                if not samples:
                                    break
                                generated.writeframes(samples)
                if source_params is None:
                    raise AudioError("翻唱结果没有可合并的源音频分段")
                generated_temp.replace(directory / "generated.bin")
            finally:
                generated_temp.unlink(missing_ok=True)
            shutil.rmtree(directory / ".sources", ignore_errors=True)
        finally:
            temporary.unlink(missing_ok=True)

    async def _commit_incremental_cover_cache(
        self,
        pending_dir: Path,
        cache_dir: Path,
        *,
        cache_key: str,
        source_sha256: str,
        voice_id: str,
        voice_version: str,
        max_seconds: int,
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            winner = self._read_cover_cache(
                cache_dir,
                cache_key=cache_key,
                source_sha256=source_sha256,
                voice_id=voice_id,
                voice_version=voice_version,
                max_seconds=max_seconds,
            )
            if winner is not None:
                if pending_dir.is_symlink():
                    raise AudioError("增量翻唱临时目录不能是符号链接")
                shutil.rmtree(pending_dir, ignore_errors=True)
                return winner, True
            if is_path_link(cache_dir):
                raise AudioError("翻唱缓存路径不能是符号链接")
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            pending_dir.replace(cache_dir)
        entry = self._read_cover_cache(
            cache_dir,
            cache_key=cache_key,
            source_sha256=source_sha256,
            voice_id=voice_id,
            voice_version=voice_version,
            max_seconds=max_seconds,
        )
        if entry is None:
            raise AudioError("增量翻唱缓存写入后校验失败")
        return entry, False

    async def _stream_incremental_cover(
        self,
        client: CoverClient,
        job_id: str,
        song_dir: Path,
        covers_dir: Path,
        cache_dir: Path,
        send_audio_callback: SendAudioCallback,
        output: dict[str, Any],
        *,
        cache_key: str,
        source_sha256: str,
        source_duration: float,
        voice: dict[str, Any],
        voice_id: str,
        voice_version: str,
        max_seconds: int,
        gap_seconds: float,
    ) -> tuple[dict[str, Any], bool]:
        if is_path_link(covers_dir):
            raise AudioError("翻唱缓存目录不能是符号链接")
        covers_dir.mkdir(parents=True, exist_ok=True)
        if song_dir.is_symlink() or not covers_dir.resolve().is_relative_to(song_dir.resolve()):
            raise AudioError("翻唱缓存目录超出歌曲目录")
        pending_dir = covers_dir / f".pending-{uuid.uuid4().hex}"
        pending_dir.mkdir()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=2)
        sentinel = object()
        loop = asyncio.get_running_loop()
        seen_segments: dict[int, dict[str, Any]] = {}
        processed_sequence = 0
        total_segments: int | None = None
        segment_duration_limit: float | None = None
        timeline_seconds = 0.0
        selected_seconds = 0.0
        selected_source_frames = 0
        source_signature: tuple[int, int, int, str] | None = None
        output_capped = False
        final_names: list[str] = []
        source_names: list[str] = []
        producer_finished = False
        last_send_succeeded_at: float | None = None
        queue_wait_seconds = 0.0

        async def read_and_validate_status() -> tuple[dict[str, Any], str, list[dict[str, Any]], int | None]:
            nonlocal total_segments, segment_duration_limit
            payload = await client.job_status(job_id)
            job = self._cover_job_fields(payload)
            status = str(job.get("status", "")).strip().lower()
            if (
                str(job.get("pipeline_version", "")) != _COVER_PIPELINE_VERSION
                or job.get("voice_id") != voice_id
                or str(job.get("voice_version", "")) != voice_version
                or job.get("output_mode") != "incremental"
            ):
                raise CoverServiceError("翻唱任务音色、推理管线或增量参数不匹配")
            reported_segment_seconds = job.get("segment_seconds")
            if (
                not isinstance(reported_segment_seconds, (int, float))
                or isinstance(reported_segment_seconds, bool)
                or not math.isfinite(float(reported_segment_seconds))
                or float(reported_segment_seconds) <= 0
                or float(reported_segment_seconds) > 60
            ):
                raise CoverServiceError("翻唱任务分段时长无效")
            if (
                segment_duration_limit is not None
                and abs(segment_duration_limit - float(reported_segment_seconds)) > 0.001
            ):
                raise CoverServiceError("翻唱任务分段时长发生变化")
            segment_duration_limit = float(reported_segment_seconds)
            reported_total = job.get("total_segments")
            if reported_total is not None:
                if (
                    not isinstance(reported_total, int)
                    or isinstance(reported_total, bool)
                    or reported_total < 0
                ):
                    raise CoverServiceError("翻唱任务总分段数无效")
                if reported_total > 0:
                    if total_segments is not None and total_segments != reported_total:
                        raise CoverServiceError("翻唱任务总分段数发生变化")
                    total_segments = reported_total
            raw_segments = job.get("segments", [])
            if not isinstance(raw_segments, list):
                raise CoverServiceError("翻唱任务分段清单无效")
            if total_segments is not None and len(raw_segments) > total_segments:
                raise CoverServiceError("翻唱任务分段清单超过总分段数")
            parsed: list[dict[str, Any]] = []
            expected_start = 0.0
            for index, value in enumerate(raw_segments, start=1):
                item = self._incremental_segment_fields(
                    value,
                    index,
                    max_duration_seconds=segment_duration_limit,
                )
                if abs(item["start_seconds"] - expected_start) > 0.005:
                    raise CoverServiceError(f"翻唱服务第{index}段时间轴不连续")
                expected_start = item["start_seconds"] + item["duration_seconds"]
                previous = seen_segments.get(index)
                if previous is not None:
                    stable_previous = {key: val for key, val in previous.items() if key != "released"}
                    stable_current = {key: val for key, val in item.items() if key != "released"}
                    if stable_current != stable_previous:
                        raise CoverServiceError(f"翻唱服务第{index}段元数据发生变化")
                    if previous["released"] and not item["released"]:
                        raise CoverServiceError(f"翻唱服务第{index}段确认状态回退")
                    seen_segments[index] = item
                elif index <= processed_sequence:
                    raise CoverServiceError(f"翻唱服务第{index}段清单丢失")
                elif item["released"]:
                    raise CoverServiceError(f"翻唱服务第{index}段在客户端处理前已释放")
                parsed.append(item)
            return job, status, parsed, total_segments

        async def put_with_heartbeat(item: Any) -> None:
            nonlocal queue_wait_seconds
            wait_started_at = loop.time()
            put_task = asyncio.create_task(queue.put(item))
            try:
                while not put_task.done():
                    done, _pending = await asyncio.wait(
                        {put_task}, timeout=_COVER_INCREMENTAL_HEARTBEAT_SECONDS
                    )
                    if done:
                        break
                    _heartbeat_job, heartbeat_status, _segments, _total = await read_and_validate_status()
                    if heartbeat_status in {"failed", "error", "cancelled", "canceled"}:
                        raise CoverServiceError("翻唱服务在等待发送队列时终止了任务")
                await put_task
            finally:
                queue_wait_seconds += max(0.0, loop.time() - wait_started_at)
                if not put_task.done():
                    put_task.cancel()
                    await asyncio.gather(put_task, return_exceptions=True)

        async def producer() -> None:
            nonlocal processed_sequence, total_segments, timeline_seconds
            nonlocal selected_seconds, selected_source_frames, source_signature
            nonlocal output_capped, producer_finished
            delay = _COVER_INCREMENTAL_POLL_SECONDS
            deadline = loop.time() + _COVER_JOB_TIMEOUT_SECONDS
            producer_failed_segment: str | None = None
            try:
                while loop.time() < deadline + queue_wait_seconds:
                    job, status, segments, total = await read_and_validate_status()
                    if total is not None:
                        output["total"] = total
                    if status in {"failed", "error", "cancelled", "canceled"}:
                        detail = job.get("error") or job.get("message")
                        if not isinstance(detail, str) or not detail.strip():
                            detail = "翻唱服务处理任务失败"
                        raise CoverServiceError(f"翻唱服务处理任务失败：{detail.strip()[:200]}")

                    while processed_sequence < len(segments):
                        item = segments[processed_sequence]
                        sequence = processed_sequence + 1
                        expected_start = timeline_seconds
                        if abs(item["start_seconds"] - expected_start) > 0.005:
                            raise CoverServiceError(f"翻唱服务第{sequence}段时间轴不连续")
                        if item["released"]:
                            raise CoverServiceError(f"翻唱服务第{sequence}段已释放但本地未完成下载")
                        segment_name = f"segment_{len(final_names) + 1:03d}.wav"
                        producer_failed_segment = segment_name
                        ready_at = loop.time()
                        source_path = pending_dir / f".source-{sequence:04d}.wav"
                        decoded_dir = pending_dir / f".decoded-{sequence:04d}"
                        try:
                            await client.download_segment_audio(job_id, sequence, source_path)
                            actual_size = source_path.stat().st_size
                            if actual_size != item["size_bytes"]:
                                raise CoverServiceError(f"翻唱服务第{sequence}段大小校验失败")
                            actual_hash = await self._run_thread_safely(self._hash_file, source_path)
                            if actual_hash != item["sha256"]:
                                raise CoverServiceError(f"翻唱服务第{sequence}段哈希校验失败")
                            with wave.open(str(source_path), "rb") as source_audio:
                                source_params = source_audio.getparams()
                                source_frames = source_params.nframes
                                source_rate = source_params.framerate
                            if (
                                source_params.comptype != "NONE"
                                or source_params.nchannels <= 0
                                or source_params.sampwidth <= 0
                                or source_rate <= 0
                                or source_frames <= 0
                            ):
                                raise CoverServiceError(f"翻唱服务第{sequence}段源WAV格式无效")
                            source_actual_duration = source_frames / source_rate
                            if (
                                source_actual_duration > float(segment_duration_limit or 0) + 0.001
                                or abs(source_actual_duration - item["duration_seconds"]) > 0.01
                            ):
                                raise CoverServiceError(f"翻唱服务第{sequence}段源时长校验失败")
                            current_source_signature = (
                                source_params.nchannels,
                                source_params.sampwidth,
                                source_rate,
                                source_params.comptype,
                            )
                            if source_signature is None:
                                source_signature = current_source_signature
                            elif source_signature != current_source_signature:
                                raise CoverServiceError("翻唱服务源分段音频格式发生变化")
                            decoded_dir.mkdir()
                            await transcode_and_segment(
                                source_path,
                                decoded_dir,
                                self._required_tool("ffmpeg"),
                                self._required_tool("ffprobe"),
                            )
                            normalized = decoded_dir / "normalized.wav"
                            normalized_info = self._cover_wav_info(normalized)
                            if normalized_info is None:
                                raise AudioError(f"翻唱服务第{sequence}段无法完整解码")
                            actual_duration = float(normalized_info["duration"])
                            if (
                                actual_duration > 60.0
                                or actual_duration > float(segment_duration_limit or 0) + 0.01
                                or abs(actual_duration - item["duration_seconds"]) > 0.01
                            ):
                                raise CoverServiceError(f"翻唱服务第{sequence}段时长校验失败")
                            with wave.open(str(normalized), "rb") as audio:
                                frames = audio.getnframes()
                                rate = audio.getframerate()
                            if frames <= 0 or rate != 16_000:
                                raise AudioError(f"翻唱服务第{sequence}段没有有效采样帧")
                            remaining_normalized_frames = (
                                max(0, max_seconds * rate - int(round(selected_seconds * rate)))
                                if max_seconds
                                else frames
                            )
                            remaining_source_frames = (
                                max(0, max_seconds * source_rate - selected_source_frames)
                                if max_seconds
                                else source_frames
                            )
                            selected_frames = min(frames, remaining_normalized_frames)
                            selected_source_chunk_frames = min(source_frames, remaining_source_frames)
                            if selected_frames < frames or selected_source_chunk_frames < source_frames:
                                output_capped = True
                            if selected_frames > 0 and selected_source_chunk_frames > 0:
                                if selected_frames == frames:
                                    normalized.replace(pending_dir / segment_name)
                                else:
                                    await self._run_thread_safely(
                                        self._write_wav_prefix,
                                        normalized,
                                        pending_dir / segment_name,
                                        selected_frames,
                                    )
                                source_dir = pending_dir / ".sources"
                                source_dir.mkdir(exist_ok=True)
                                source_name = f"source_{len(source_names) + 1:03d}.wav"
                                await self._run_thread_safely(
                                    self._write_wav_prefix,
                                    source_path,
                                    source_dir / source_name,
                                    selected_source_chunk_frames,
                                )
                                segment_duration = selected_frames / rate
                                final_names.append(segment_name)
                                source_names.append(source_name)
                                selected_source_frames += selected_source_chunk_frames
                                selected_seconds += segment_duration
                                output["planned_seconds"] = selected_seconds
                                output["capped"] = bool(output.get("capped")) or output_capped
                            acknowledgement = await client.ack_segment(job_id, sequence)
                            seen_segments[sequence] = {
                                **item,
                                "released": acknowledgement["released"],
                            }
                            processed_sequence += 1
                            timeline_seconds = item["start_seconds"] + item["duration_seconds"]
                            if selected_frames > 0 and selected_source_chunk_frames > 0:
                                await put_with_heartbeat({
                                    "name": segment_name,
                                    "path": pending_dir / segment_name,
                                    "duration": segment_duration,
                                    "ready_at": ready_at,
                                })
                            producer_failed_segment = None
                        finally:
                            source_path.unlink(missing_ok=True)
                            shutil.rmtree(decoded_dir, ignore_errors=True)
                        if loop.time() >= deadline + queue_wait_seconds:
                            raise CoverServiceError("翻唱任务超过 60 分钟")

                    if status in {"completed", "complete", "succeeded"}:
                        if str(job.get("pipeline_version", "")) != _COVER_PIPELINE_VERSION:
                            raise CoverServiceError("翻唱服务返回了不兼容的推理结果，请升级至推理管线 v2")
                        result = job.get("result")
                        if isinstance(result, dict) and str(result.get("pipeline_version", "")) != _COVER_PIPELINE_VERSION:
                            raise CoverServiceError("翻唱服务返回了不兼容的推理结果，请升级至推理管线 v2")
                        if total_segments is None or total_segments <= 0 or processed_sequence != total_segments:
                            raise CoverServiceError("翻唱任务完成时分段数量不完整")
                        if len(segments) != total_segments:
                            raise CoverServiceError("翻唱任务完成时分段清单不完整")
                        await self._run_thread_safely(
                            self._merge_incremental_wavs,
                            pending_dir,
                            final_names,
                            source_names,
                        )
                        metadata = {
                            "cache_key": cache_key,
                            "source_sha256": source_sha256,
                            "voice_id": voice_id,
                            "voice_name": str(voice.get("name") or voice_id),
                            "voice_version": voice_version,
                            "created_at": utc_now().isoformat(),
                            "max_seconds": max_seconds,
                            "pipeline_version": _COVER_PIPELINE_VERSION,
                            "render_mode": "incremental",
                            "source_duration": source_duration,
                            "planned_seconds": selected_seconds,
                            "capped": bool(max_seconds and source_duration > max_seconds) or output_capped,
                            "segments": final_names,
                        }
                        metadata_temp = pending_dir / ".cover.json.tmp"
                        metadata_temp.write_text(
                            json.dumps(metadata, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        metadata_temp.replace(pending_dir / "cover.json")
                        output["total"] = len(final_names)
                        output["planned_seconds"] = selected_seconds
                        output["capped"] = metadata["capped"]
                        producer_finished = True
                        await put_with_heartbeat(sentinel)
                        return
                    if status not in {
                        "queued", "pending", "running", "processing", "separating",
                        "converting", "mixing", "segmenting", "streaming",
                    }:
                        raise CoverServiceError("翻唱服务返回了未知任务状态")
                    await asyncio.sleep(delay)
                    delay = min(2.0, delay + 0.2)
                raise CoverServiceError("翻唱任务超过 60 分钟")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if producer_failed_segment and output.get("failed_segment") is None:
                    output["failed_segment"] = producer_failed_segment
                elif output.get("failed_segment") is None and processed_sequence < (total_segments or processed_sequence):
                    output["failed_segment"] = f"segment_{processed_sequence + 1:03d}.wav"
                raise

        async def consumer() -> None:
            nonlocal last_send_succeeded_at
            while True:
                item = await queue.get()
                if item is sentinel:
                    return
                due_at = item["ready_at"]
                if last_send_succeeded_at is not None:
                    due_at = max(due_at, last_send_succeeded_at + gap_seconds)
                wait_seconds = due_at - loop.time()
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                try:
                    result = send_audio_callback(item["path"])
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = str(exc) or exc.__class__.__name__
                    output["failed_segment"] = item["name"]
                    output["message"] = f"{item['name']}发送失败：{message[:240]}"
                    raise RuntimeError(output["message"]) from exc
                output["sent"] += 1
                output["sent_seconds"] += item["duration"]
                last_send_succeeded_at = loop.time()

        producer_task = asyncio.create_task(producer())
        consumer_task = asyncio.create_task(consumer())
        committed = False
        try:
            done, _pending = await asyncio.wait(
                {producer_task, consumer_task},
                return_when=asyncio.FIRST_EXCEPTION,
            )
            failures = [task for task in done if not task.cancelled() and task.exception() is not None]
            if failures:
                error = failures[0].exception()
                if consumer_task in failures and producer_finished:
                    if not producer_task.done():
                        producer_task.cancel()
                    await asyncio.gather(producer_task, return_exceptions=True)
                    producer_error = (
                        producer_task.exception()
                        if producer_task.done() and not producer_task.cancelled()
                        else None
                    )
                    if producer_error is None:
                        _cache, cache_reused = await self._commit_incremental_cover_cache(
                            pending_dir,
                            cache_dir,
                            cache_key=cache_key,
                            source_sha256=source_sha256,
                            voice_id=voice_id,
                            voice_version=voice_version,
                            max_seconds=max_seconds,
                        )
                        committed = True
                        output["cover_reused"] = cache_reused
                        output["total"] = len(final_names)
                        output["message"] = str(error)[:300]
                        return _cache, cache_reused
                for task in (producer_task, consumer_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(producer_task, consumer_task, return_exceptions=True)
                raise error

            await asyncio.gather(producer_task, consumer_task)
            cache, cache_reused = await self._commit_incremental_cover_cache(
                pending_dir,
                cache_dir,
                cache_key=cache_key,
                source_sha256=source_sha256,
                voice_id=voice_id,
                voice_version=voice_version,
                max_seconds=max_seconds,
            )
            committed = True
            output["cover_reused"] = cache_reused
            output["total"] = len(cache["segments"])
            output["planned_seconds"] = float(cache.get("planned_seconds", 0.0))
            output["capped"] = cache.get("capped") is True
            if output.get("failed_segment") is None:
                output["message"] = "发送完成"
            return cache, cache_reused
        except asyncio.CancelledError:
            for task in (producer_task, consumer_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(producer_task, consumer_task, return_exceptions=True)
            raise
        finally:
            if not committed and pending_dir.exists() and not pending_dir.is_symlink():
                shutil.rmtree(pending_dir, ignore_errors=True)

    async def send_cover(
        self,
        song_id: str,
        send_audio_callback: SendAudioCallback,
        *,
        voice_id: str,
        max_seconds: int = 0,
        gap_seconds: float = 0,
        incremental_output: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(max_seconds, int) or isinstance(max_seconds, bool) or max_seconds < 0:
            raise ValueError("max_seconds 必须是非负整数")
        try:
            numeric_gap = float(gap_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("gap_seconds 必须是有限非负数") from None
        if (
            not isinstance(gap_seconds, (int, float))
            or isinstance(gap_seconds, bool)
            or not math.isfinite(numeric_gap)
            or numeric_gap < 0
        ):
            raise ValueError("gap_seconds 必须是有限非负数")
        if not isinstance(voice_id, str) or not voice_id.strip():
            raise ValueError("请先在插件设置中选择翻唱音色")
        voice_id = voice_id.strip()

        output: dict[str, Any] = {
            "sent": 0,
            "total": 0,
            "failed_segment": None,
            "message": "",
            "sent_seconds": 0.0,
            "planned_seconds": 0.0,
            "capped": False,
            "voice_id": voice_id,
            "voice_name": "",
            "voice_version": "",
            "cover_reused": False,
            "cleanup_confirmed": True,
            "cleanup_warning": "",
        }
        normalized_id = ""
        pinned = False
        client: CoverClient | None = None
        job_id: str | None = None
        incremental_streamed = False
        try:
            async with self._lock:
                record = self.storage.get_record(song_id)
                if record is None:
                    output["message"] = "歌曲记录不存在"
                    return output
                original_name = next(
                    (
                        item.get("name")
                        for item in record.get("files", [])
                        if isinstance(item, dict) and item.get("kind") == "original"
                    ),
                    None,
                )
                if not isinstance(original_name, str):
                    output["message"] = "歌曲记录没有原始音频文件"
                    return output
                try:
                    normalized_id = self.storage.normalize_song_id(song_id)
                    original_path = self.storage.file_path(song_id, original_name, allow_expired=True)
                except (ValueError, FileNotFoundError, OSError):
                    output["message"] = "找不到歌曲原始音频文件"
                    return output
                song_dir = self.storage.song_dir(song_id)
                self._pins[normalized_id] = self._pins.get(normalized_id, 0) + 1
                pinned = True

            settings_snapshot = self.settings()
            client = CoverClient(lambda: settings_snapshot)
            ffprobe = self._required_tool("ffprobe")
            source_probe, source_sha256 = await asyncio.gather(
                probe_audio(original_path, ffprobe),
                self._run_thread_safely(self._hash_file, original_path),
            )
            source_duration = float(source_probe["duration"])
            voices = await client.voices()
            voice = next((item for item in voices if item.get("id") == voice_id), None)
            if voice is None:
                raise CoverServiceError("所选音色已不在服务列表中，请刷新音色后再试")
            voice_name = str(voice.get("name") or voice_id)
            raw_voice_version = voice.get("version")
            if not isinstance(raw_voice_version, (str, int, float)) or not str(raw_voice_version).strip():
                raise CoverServiceError("服务未提供所选音色的版本信息，无法安全复用翻唱缓存")
            voice_version = str(raw_voice_version).strip()
            output.update({"voice_name": voice_name, "voice_version": voice_version})

            cache_key_data = {
                "source_sha256": source_sha256,
                "voice_id": voice_id,
                "voice_version": voice_version,
                "max_seconds": max_seconds,
                "pipeline_version": _COVER_PIPELINE_VERSION,
            }
            cache_key = hashlib.sha256(
                json.dumps(cache_key_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            covers_dir = song_dir / "covers"
            if is_path_link(covers_dir):
                raise AudioError("翻唱缓存目录不能是符号链接")
            covers_dir.mkdir(parents=True, exist_ok=True)
            resolved_song_dir = song_dir.resolve()
            if not covers_dir.resolve().is_relative_to(resolved_song_dir):
                raise AudioError("翻唱缓存目录超出歌曲目录")
            cache_dir = covers_dir / cache_key
            cache = self._read_cover_cache(
                cache_dir,
                cache_key=cache_key,
                source_sha256=source_sha256,
                voice_id=voice_id,
                voice_version=voice_version,
                max_seconds=max_seconds,
            )
            if cache is None:
                health = await client.require_cleanup_support()
                capabilities = health.get("capabilities") if isinstance(health, dict) else None
                use_incremental = (
                    incremental_output is True
                    and isinstance(capabilities, dict)
                    and capabilities.get("incremental_segments") is True
                )
                output["cleanup_confirmed"] = False
                output["cleanup_warning"] = (
                    "翻唱任务提交状态未确认，无法清理未知编号；服务将按其超时策略清理。"
                )
                job_id = await client.submit_job(
                    original_path,
                    voice_id=voice_id,
                    max_seconds=max_seconds,
                    request_id=uuid.uuid4().hex,
                    output_mode="incremental" if use_incremental else None,
                )
                output["cleanup_warning"] = ""
                if use_incremental:
                    incremental_streamed = True
                    cache, cache_reused = await self._stream_incremental_cover(
                        client,
                        job_id,
                        song_dir,
                        covers_dir,
                        cache_dir,
                        send_audio_callback,
                        output,
                        cache_key=cache_key,
                        source_sha256=source_sha256,
                        source_duration=source_duration,
                        voice=voice,
                        voice_id=voice_id,
                        voice_version=voice_version,
                        max_seconds=max_seconds,
                        gap_seconds=numeric_gap,
                    )
                    output["cover_reused"] = cache_reused
                else:
                    completed_job = await self._wait_for_cover_job(client, job_id)
                    if (
                        completed_job.get("voice_id") != voice_id
                        or completed_job.get("voice_version") != voice_version
                    ):
                        raise CoverServiceError("翻唱任务使用的音色或版本与已选音色不一致，未缓存该结果")
                    cache, cache_reused = await self._prepare_cover_cache(
                        client,
                        job_id,
                        song_dir,
                        covers_dir,
                        cache_dir,
                        cache_key=cache_key,
                        source_sha256=source_sha256,
                        source_duration=source_duration,
                        voice=voice,
                        voice_id=voice_id,
                        voice_version=voice_version,
                        max_seconds=max_seconds,
                    )
                    output["cover_reused"] = cache_reused
                try:
                    release = await client.ack_job(job_id)
                    job_id = None
                    output["cleanup_confirmed"] = (
                        release.get("released") is True
                        and release.get("pending") is not True
                    )
                    if not output["cleanup_confirmed"]:
                        output["cleanup_warning"] = (
                            "翻唱任务仍有下载占用，服务会在最后一次下载结束后释放。"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    output["cleanup_confirmed"] = False
                    output["cleanup_warning"] = (
                        f"翻唱任务确认失败：{str(exc)[:160]}；正在尝试取消任务。"
                    )
            else:
                output["cover_reused"] = True
            output["total"] = len(cache["segments"])
            output["planned_seconds"] = float(cache.get("planned_seconds", 0.0))
            output["capped"] = cache.get("capped") is True

            if incremental_streamed:
                token = str((settings_snapshot.get("cover_service") or {}).get("token") or "")
                if token and output.get("message"):
                    output["message"] = str(output["message"]).replace(token, "[hidden]")[:300]
                if not output.get("message"):
                    output["message"] = "发送完成"
                return output

            for index, (filename, duration) in enumerate(
                zip(cache["segments"], cache["segment_durations"]), start=1
            ):
                path = cache_dir / filename
                try:
                    outcome = send_audio_callback(path)
                    if inspect.isawaitable(outcome):
                        await outcome
                    output["sent"] += 1
                    output["sent_seconds"] += float(duration)
                    if numeric_gap and index < output["total"]:
                        await asyncio.sleep(numeric_gap)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = str(exc) or exc.__class__.__name__
                    token = str((settings_snapshot.get("cover_service") or {}).get("token") or "")
                    if token:
                        message = message.replace(token, "[hidden]")
                    output["failed_segment"] = filename
                    output["message"] = f"第{index}段发送失败：{message[:300]}"
                    return output
            output["message"] = "发送完成"
            return output
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            output["message"] = str(exc) or "翻唱处理失败"
            token = ""
            try:
                token = str((settings_snapshot.get("cover_service") or {}).get("token") or "")
            except (UnboundLocalError, AttributeError):
                pass
            if token:
                output["message"] = output["message"].replace(token, "[hidden]")
            output["message"] = output["message"][:300]
            return output
        finally:
            try:
                if job_id is not None and client is not None:
                    try:
                        cleanup_confirmed, cleanup_error = await _cancel_cover_job(client, job_id)
                        output["cleanup_confirmed"] = cleanup_confirmed
                        if cleanup_confirmed:
                            if output["cleanup_warning"]:
                                output["cleanup_warning"] = "任务确认失败，已通过取消接口确认清理。"
                        elif output["cleanup_warning"]:
                            output["cleanup_warning"] = (
                                "翻唱任务确认和取消均未确认；服务将按其超时策略清理。"
                            )
                        else:
                            detail = f"（{cleanup_error[:100]}）" if cleanup_error else ""
                            output["cleanup_warning"] = (
                                f"翻唱任务取消未确认{detail}；服务将按其超时策略清理。"
                            )
                    except asyncio.CancelledError:
                        raise
                if not output["cleanup_confirmed"] and output["cleanup_warning"]:
                    current_message = str(output.get("message") or "翻唱处理失败")
                    output["message"] = (
                        f"{current_message}；{output['cleanup_warning']}"
                    )[:300]
            finally:
                if pinned:
                    async with self._lock:
                        current = self._pins.get(normalized_id, 0)
                        if current <= 1:
                            self._pins.pop(normalized_id, None)
                        else:
                            self._pins[normalized_id] = current - 1
                        self._cleanup_locked()

    def files(self, platform: str | None = None, query: str = "") -> list[dict[str, Any]]:
        records = self.storage.list_records()
        if platform is not None:
            records = [record for record in records if str(record.get("platform", "")) == str(platform)]
        needle = str(query or "").strip().casefold()
        if needle:
            records = [
                record
                for record in records
                if needle in str(record.get("title", "")).casefold()
                or needle in str(record.get("artist", "")).casefold()
            ]
        for record in records:
            record["covers"] = self.list_covers(str(record.get("id", "")))
        return records

    def files_by_platform(self, platform: str | None = None, query: str = "") -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in self.files(platform, query):
            grouped.setdefault(str(record.get("platform", "")), []).append(record)
        return grouped

    def file_path(self, song_id: str, filename: str) -> Path:
        return self.storage.file_path(song_id, filename)

    async def cleanup(self) -> dict[str, int]:
        async with self._lock:
            return self._cleanup_locked()

    def _cleanup_locked(self) -> dict[str, int]:
        removed = 0
        skipped_in_use = 0
        records = self.storage.all_records()
        max_songs = self.environment.settings().get("max_songs", 30)
        oldest_first = sorted(
            records,
            key=lambda item: parse_time(item.get("downloaded_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
        )
        active_ids = set(self._pins) | self._downloads
        remaining = len(records)
        for record in oldest_first:
            if remaining <= max_songs:
                break
            song_id = str(record.get("id", "")).lower()
            if song_id in active_ids:
                skipped_in_use += 1
                # Retention is FIFO: keep newer songs until the oldest pinned
                # song becomes available rather than deleting out of order.
                break
            if self.storage.delete_song(song_id):
                removed += 1
                remaining -= 1

        try:
            platform_dirs = list(self.storage.downloads_dir.iterdir())
        except OSError:
            platform_dirs = []
        for platform_dir in platform_dirs:
            if platform_dir.is_symlink() or not platform_dir.is_dir():
                continue
            try:
                pending_dirs = list(platform_dir.iterdir())
            except OSError:
                continue
            for directory in pending_dirs:
                if directory.is_symlink() or not directory.is_dir() or not directory.name.startswith(".pending-"):
                    continue
                candidate_id = directory.name.removeprefix(".pending-").lower()
                try:
                    self.storage.normalize_song_id(candidate_id)
                    resolved = directory.resolve()
                    if not resolved.is_relative_to(self.storage.downloads_dir.resolve()):
                        continue
                except (OSError, ValueError):
                    continue
                if candidate_id in self._downloads:
                    continue
                shutil.rmtree(resolved, ignore_errors=True)
                removed += 1
        return {"removed": removed, "skipped_in_use": skipped_in_use}

    async def migrate_legacy(self) -> dict[str, int]:
        result = await asyncio.to_thread(self.storage.migrate_legacy)
        retention = await self.cleanup()
        return {**result, **retention}

    def start_cleanup(self, interval_seconds: float = 3600) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_stop.clear()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(max(30.0, interval_seconds)))

    async def _cleanup_loop(self, interval_seconds: float) -> None:
        while not self._cleanup_stop.is_set():
            try:
                await self.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._cleanup_stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        self._cleanup_stop.set()
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        await self.cleanup()

    @staticmethod
    def _clip_cover_test_audio(
        source: Path,
        destination: Path,
        ffmpeg: Path,
        *,
        offset: float,
        duration: float,
    ) -> None:
        command = [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ss",
            f"{offset:.3f}",
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(destination),
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=180,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AudioError("无法裁剪翻唱测试片段") from exc
        if (
            completed.returncode != 0
            or not destination.is_file()
            or destination.stat().st_size <= 44
        ):
            raise AudioError("无法裁剪翻唱测试片段")

    async def test_cover(self, song_id: str, voice_id: str) -> dict[str, Any]:
        """Run a real, uncached cover job and verify QQ-compatible WAV segments."""
        started = time.monotonic()
        result: dict[str, Any] = {
            "ok": False,
            "message": "",
            "voice_id": str(voice_id or "")[:200],
            "voice_name": "",
            "voice_version": "",
            "start_seconds": 0.0,
            "input_duration": 0.0,
            "output_duration": 0.0,
            "format": "",
            "sample_rate": 0,
            "channels": 0,
            "sample_width": 0,
            "segments": [],
            "segment_count": 0,
            "elapsed_seconds": 0.0,
            "cleanup_confirmed": True,
            "cleanup_warning": "",
        }
        stage = "读取原曲"
        normalized_id = ""
        pinned = False
        work: Path | None = None
        client: CoverClient | None = None
        job_id: str | None = None
        try:
            if not isinstance(voice_id, str) or not voice_id.strip():
                stage = "校验音色"
                raise ValueError("invalid voice")
            async with self._lock:
                record = self.storage.get_record(song_id)
                if record is None:
                    raise FileNotFoundError("song unavailable")
                original_name = next(
                    (
                        item.get("name")
                        for item in record.get("files", [])
                        if isinstance(item, dict) and item.get("kind") == "original"
                    ),
                    None,
                )
                if not isinstance(original_name, str):
                    raise FileNotFoundError("source unavailable")
                normalized_id = self.storage.normalize_song_id(song_id)
                original_path = self.storage.file_path(
                    song_id, original_name, allow_expired=True
                )
                self._pins[normalized_id] = self._pins.get(normalized_id, 0) + 1
                pinned = True

            runtime_dir = data_subdirectory(self.data_dir, "runtime")
            work = Path(tempfile.mkdtemp(prefix="cover-test-", dir=runtime_dir))
            ffmpeg = self._required_tool("ffmpeg")
            ffprobe = self._required_tool("ffprobe")
            source_info = await probe_audio(original_path, ffprobe)
            source_duration = float(source_info["duration"])
            if not math.isfinite(source_duration) or source_duration <= 0:
                raise AudioError("source duration unavailable")
            clip_offset = 30.0 if source_duration >= 38.0 else 0.0
            clip_duration = min(8.0, source_duration - clip_offset)
            if clip_duration <= 0:
                clip_offset = 0.0
                clip_duration = min(8.0, source_duration)
            result["start_seconds"] = clip_offset

            stage = "裁剪8秒测试片段"
            test_input = work / "input.wav"
            await self._run_thread_safely(
                self._clip_cover_test_audio,
                original_path,
                test_input,
                ffmpeg,
                offset=clip_offset,
                duration=clip_duration,
            )
            input_info = await probe_audio(test_input, ffprobe)
            result["input_duration"] = float(input_info["duration"])

            stage = "连接翻唱服务"
            settings_snapshot = self.settings()
            client = CoverClient(lambda: settings_snapshot)
            await client.require_cleanup_support()
            voices = await client.voices()
            voice = next(
                (item for item in voices if item.get("id") == voice_id.strip()),
                None,
            )
            if voice is None:
                raise CoverServiceError("voice unavailable")
            voice_name = str(voice.get("name") or voice_id.strip())[:200]
            raw_version = voice.get("version")
            if (
                not isinstance(raw_version, (str, int, float))
                or not str(raw_version).strip()
            ):
                raise CoverServiceError("voice version unavailable")
            voice_version = str(raw_version).strip()[:200]
            result.update({
                "voice_id": voice_id.strip()[:200],
                "voice_name": voice_name,
                "voice_version": voice_version,
            })

            stage = "分离与RVC翻唱推理"
            result["cleanup_confirmed"] = False
            result["cleanup_warning"] = (
                "翻唱任务提交状态未确认，无法清理未知编号；服务将按其超时策略清理。"
            )
            job_id = await client.submit_job(
                test_input,
                voice_id=voice_id.strip(),
                max_seconds=0,
                request_id=uuid.uuid4().hex,
            )
            result["cleanup_warning"] = ""
            completed = await self._wait_for_cover_job(client, job_id)
            if (
                completed.get("voice_id") != voice_id.strip()
                or str(completed.get("voice_version", "")) != voice_version
            ):
                raise CoverServiceError("job voice mismatch")

            stage = "下载与混音输出"
            generated = work / "generated.wav"
            await client.download_audio(job_id, generated)

            stage = "QQ WAV分段格式检查"
            segment_dir = work / "qq-segments"
            segment_dir.mkdir()
            converted = await transcode_and_segment(
                generated,
                segment_dir,
                ffmpeg,
                ffprobe,
            )
            normalized = self._cover_wav_info(segment_dir / "normalized.wav")
            if normalized is None or (
                normalized["sample_rate"] != 16_000
                or normalized["channels"] != 1
                or normalized["sample_width"] != 2
            ):
                raise AudioError("normalized WAV format mismatch")
            segment_details: list[dict[str, Any]] = []
            for item in converted.get("segment_info", []):
                if not isinstance(item, dict):
                    raise AudioError("invalid segment metadata")
                name = item.get("name")
                if not isinstance(name, str) or not _COVER_SEGMENT_PATTERN.fullmatch(name):
                    raise AudioError("invalid segment name")
                info = self._cover_wav_info(segment_dir / name)
                if info is None or (
                    info["sample_rate"] != 16_000
                    or info["channels"] != 1
                    or info["sample_width"] != 2
                    or info["duration"] > 60.0
                ):
                    raise AudioError("QQ segment format mismatch")
                segment_details.append({
                    "name": name,
                    "duration": info["duration"],
                    "size": info["size"],
                })
            if not segment_details:
                raise AudioError("no QQ segments")
            output_duration = sum(item["duration"] for item in segment_details)
            expected_duration = float(result["input_duration"])
            duration_tolerance = max(0.5, expected_duration * 0.05)
            if abs(output_duration - expected_duration) > duration_tolerance:
                raise AudioError("cover output duration mismatch")
            try:
                release = await client.ack_job(job_id)
                job_id = None
                result["cleanup_confirmed"] = (
                    release.get("released") is True
                    and release.get("pending") is not True
                )
                if not result["cleanup_confirmed"]:
                    result["cleanup_warning"] = (
                        "翻唱任务仍有下载占用，服务会在最后一次下载结束后释放。"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result["cleanup_confirmed"] = False
                result["cleanup_warning"] = (
                    f"翻唱任务确认失败：{str(exc)[:160]}；正在尝试取消任务。"
                )
            result.update({
                "ok": True,
                "message": "真实翻唱推理与QQ分段格式检查通过",
                "output_duration": output_duration,
                "format": "WAV PCM signed 16-bit little-endian, mono, 16000 Hz",
                "sample_rate": 16_000,
                "channels": 1,
                "sample_width": 2,
                "segments": segment_details,
                "segment_count": len(segment_details),
            })
        except asyncio.CancelledError:
            raise
        except Exception:
            result["message"] = f"真实翻唱检测失败（{stage}）"
        finally:
            try:
                if job_id is not None and client is not None:
                    try:
                        cleanup_confirmed, cleanup_error = await _cancel_cover_job(client, job_id)
                        result["cleanup_confirmed"] = cleanup_confirmed
                        if cleanup_confirmed:
                            if result["cleanup_warning"]:
                                result["cleanup_warning"] = "任务确认失败，已通过取消接口确认清理。"
                        elif result["cleanup_warning"]:
                            result["cleanup_warning"] = (
                                "翻唱任务确认和取消均未确认；服务将按其超时策略清理。"
                            )
                        else:
                            detail = f"（{cleanup_error[:100]}）" if cleanup_error else ""
                            result["cleanup_warning"] = (
                                f"翻唱任务取消未确认{detail}；服务将按其超时策略清理。"
                            )
                    except asyncio.CancelledError:
                        raise
                if not result["cleanup_confirmed"] and result["cleanup_warning"]:
                    result["message"] = (
                        f"{result['message']}；{result['cleanup_warning']}"
                    )[:300]
            finally:
                if work is not None and not is_path_link(work):
                    shutil.rmtree(work, ignore_errors=True)
                if pinned:
                    async with self._lock:
                        current = self._pins.get(normalized_id, 0)
                        if current <= 1:
                            self._pins.pop(normalized_id, None)
                        else:
                            self._pins[normalized_id] = current - 1
                        self._cleanup_locked()
                elapsed = round(max(0.0, time.monotonic() - started), 3)
                result["elapsed_seconds"] = elapsed
        return result

    async def check_environment(self) -> dict[str, Any]:
        return await self.environment.check_environment()

    async def cover_health(self) -> dict[str, Any]:
        """Check the configured cover service's API v1 health and voices."""
        return await CoverClient(self.settings).check()

    async def install_tool(self, tool_id: str, url: str = "") -> dict[str, Any]:
        if tool_id in {"netease", "kugou"}:
            try:
                await asyncio.to_thread(get_node_tools, self.data_dir)
            except RuntimeError:
                node_result = await self.environment.install_tool("node")
                if not node_result.get("installed"):
                    return {
                        "tool_id": tool_id,
                        "installed": False,
                        "path": "",
                        "version": "",
                        "message": f"{node_result.get('message', 'Node.js安装失败')}；先准备Node.js才能安装该平台依赖",
                    }
        return await self.environment.install_tool(tool_id, url)

    def settings(self) -> dict[str, Any]:
        return self.environment.settings()

    async def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        saved = self.environment.save_settings(values)
        async with self._lock:
            retention = self._cleanup_locked()
        return {**saved, "retention": retention}

    async def test_audio(self, send_audio_callback: SendAudioCallback | None = None) -> dict[str, Any]:
        """Generate and split a temporary 65-second tone; send only on explicit request."""
        temp_root = data_subdirectory(self.data_dir, "runtime", "audio-test-tmp")
        work = Path(tempfile.mkdtemp(prefix="better-music-audio-test-", dir=temp_root))
        try:
            source = work / "test-tone.wav"
            await generate_test_audio(self._required_tool("ffmpeg"), source, duration=65)
            info = await transcode_and_segment(
                source,
                work,
                self._required_tool("ffmpeg"),
                self._required_tool("ffprobe"),
            )
            sent = 0
            failed_segment = None
            if send_audio_callback is not None:
                for name in info["segments"]:
                    try:
                        outcome = send_audio_callback(work / name)
                        if inspect.isawaitable(outcome):
                            await outcome
                        sent += 1
                    except Exception:
                        failed_segment = name
                        break
            return {
                "ok": failed_segment is None,
                "duration": info["duration"],
                "frames": info["frames"],
                "segments": info["segments"],
                "segment_info": info["segment_info"],
                "sent": sent,
                "total": len(info["segments"]),
                "failed_segment": failed_segment,
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)
