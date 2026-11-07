"""Audio probing, full decode, and gapless WAV splitting helpers."""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import wave
from pathlib import Path
from typing import Any


class AudioError(RuntimeError):
    pass


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


async def _run(command: list[str], *, timeout: float = 3600) -> tuple[int, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise AudioError(f"无法启动音频工具：{exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise AudioError("音频处理超时") from exc
    return process.returncode or 0, stdout, stderr


def _contains_encryption_marker(data: dict[str, Any]) -> bool:
    markers = ("encryption", "encrypted", "drm", "widevine", "playready", "fairplay", "key_id")
    for stream in data.get("streams", []):
        if not isinstance(stream, dict):
            continue
        if str(stream.get("codec_tag_string", "")).lower() in {"enca", "encv"}:
            return True
        for key in stream:
            if any(marker in str(key).lower() for marker in markers):
                return True
        tags = stream.get("tags", {})
        if isinstance(tags, dict) and any(any(marker in str(key).lower() for marker in markers) for key in tags):
            return True
    fmt = data.get("format", {})
    tags = fmt.get("tags", {}) if isinstance(fmt, dict) else {}
    return isinstance(tags, dict) and any(
        any(marker in str(key).lower() for marker in markers) for key in tags
    )


async def probe_audio(path: Path, ffprobe: Path | str) -> dict[str, Any]:
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    code, stdout, stderr = await _run(command, timeout=90)
    if code != 0:
        reason = stderr.decode("utf-8", "replace").strip()
        raise AudioError("文件不是可解码的完整音频" + (f"：{reason[-400:]}" if reason else ""))
    try:
        metadata = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AudioError("ffprobe未能识别音频文件") from exc
    streams = [
        stream
        for stream in metadata.get("streams", [])
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not streams:
        raise AudioError("下载内容没有音频轨道，可能是网页或播放列表")
    if _contains_encryption_marker(metadata):
        raise AudioError("检测到加密或DRM标记的音频，不会保存或发送")
    fmt = metadata.get("format", {})
    duration_values: list[float] = []
    if isinstance(fmt, dict):
        try:
            duration_values.append(float(fmt.get("duration")))
        except (TypeError, ValueError):
            pass
    for stream in streams:
        try:
            duration_values.append(float(stream.get("duration")))
        except (TypeError, ValueError):
            pass
    valid_durations = [value for value in duration_values if math.isfinite(value) and value > 0]
    if not valid_durations:
        raise AudioError("音频时长无法确认")
    return {"duration": max(valid_durations), "format": fmt, "streams": streams}


async def transcode_and_segment(
    input_path: Path,
    song_dir: Path,
    ffmpeg: Path | str,
    ffprobe: Path | str,
    *,
    sample_rate: int = 16_000,
    max_segment_seconds: float = 60.0,
) -> dict[str, Any]:
    """Decode to mono PCM WAV, then split by sample frames without gaps or overlap."""
    normalized = song_dir / "normalized.wav"
    temporary = song_dir / "normalized.wav.tmp"
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(temporary),
    ]
    code, _, stderr = await _run(command)
    if code != 0 or not temporary.is_file() or temporary.stat().st_size <= 44:
        temporary.unlink(missing_ok=True)
        reason = stderr.decode("utf-8", "replace").strip()
        raise AudioError("完整解码音频失败" + (f"：{reason[-400:]}" if reason else ""))
    temporary.replace(normalized)

    segment_names: list[str] = []
    source_frames = 0
    written_frames = 0
    try:
        with wave.open(str(normalized), "rb") as source:
            params = source.getparams()
            if params.comptype != "NONE" or params.nchannels != 1 or params.sampwidth != 2:
                raise AudioError("转码输出不是预期的16位单声道PCM WAV")
            source_frames = params.nframes
            segment_frames = max(1, int(params.framerate * max_segment_seconds))
            if source_frames <= 0:
                raise AudioError("转码结果没有音频采样帧")
            if params.framerate != sample_rate:
                raise AudioError("转码结果采样率与目标采样率不一致")
            index = 1
            while True:
                samples = source.readframes(segment_frames)
                if not samples:
                    break
                frame_count = len(samples) // params.nchannels // params.sampwidth
                name = f"segment_{index:03d}.wav"
                segment_temp = song_dir / f"{name}.tmp"
                segment_path = song_dir / name
                try:
                    with wave.open(str(segment_temp), "wb") as part:
                        part.setnchannels(params.nchannels)
                        part.setsampwidth(params.sampwidth)
                        part.setframerate(params.framerate)
                        part.writeframes(samples)
                    segment_temp.replace(segment_path)
                except Exception:
                    segment_temp.unlink(missing_ok=True)
                    raise
                segment_names.append(name)
                written_frames += frame_count
                index += 1
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioError(f"切分WAV失败：{exc}") from exc

    if written_frames != source_frames:
        raise AudioError(f"切分校验失败：源帧{source_frames}，分段帧{written_frames}")
    if not segment_names:
        raise AudioError("没有生成可发送的语音分段")

    total_duration = source_frames / sample_rate
    segment_info: list[dict[str, Any]] = []
    for name in segment_names:
        path = song_dir / name
        try:
            with wave.open(str(path), "rb") as part:
                frame_count = part.getnframes()
                duration = frame_count / part.getframerate()
        except (wave.Error, OSError) as exc:
            raise AudioError(f"分段文件校验失败：{name}") from exc
        if duration > 60:
            raise AudioError(f"分段超过60秒：{name} ({duration:.3f}s)")
        segment_info.append({"name": name, "size": path.stat().st_size, "duration": duration})

    return {
        "transcoded": {"name": normalized.name, "size": normalized.stat().st_size},
        "segments": segment_names,
        "segment_info": segment_info,
        "duration": total_duration,
        "frames": source_frames,
        "sample_rate": sample_rate,
    }


async def generate_test_audio(ffmpeg: Path | str, output_path: Path, duration: int = 65) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=44100:duration={duration}",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(output_path),
    ]
    code, _, stderr = await _run(command, timeout=180)
    if code != 0:
        reason = stderr.decode("utf-8", "replace").strip()
        raise AudioError("生成测试音频失败" + (f"：{reason[-400:]}" if reason else ""))
