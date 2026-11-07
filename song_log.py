"""Best-effort delivery of one summary message for a completed playback task."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger

from .templates import render_template, validate_templates


_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:token|cookie|set-cookie|authorization|password|passwd|secret|api[_-]?key|access[_-]?key)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[a-z]:\\|\\\\)[^\r\n<>\"|?*]+")
_POSIX_PATH = re.compile(r"(?<![\w:/])/(?:[^\s<>\"'|?*]+/)*[^\s<>\"'|?*]+")


def _safe_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "")
    text = _SECRET_VALUE.sub(r"\1[已隐藏]", text)
    text = _BEARER_VALUE.sub("Bearer [已隐藏]", text)
    text = _WINDOWS_PATH.sub("[本地路径]", text)
    text = _POSIX_PATH.sub("[本地路径]", text)
    return text[:limit]


def _short_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _count(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return result if result >= 0 else fallback


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _duration_text(value: float) -> str:
    rounded = round(value, 2)
    if abs(rounded - round(rounded)) < 0.005:
        return f"{int(round(rounded))} 秒"
    return f"{rounded:.2f} 秒"


def _target_umo(platform_id: str, target_type: str, target_id: str) -> str:
    message_type = "GroupMessage" if target_type == "group" else "FriendMessage"
    return f"{platform_id}:{message_type}:{target_id}"


def build_song_log_message(
    settings: dict[str, Any],
    *,
    title: str,
    artist: str,
    platform: str,
    trigger: str,
    voice: str,
    source_session: str,
    sent: Any,
    total: Any,
    sent_seconds: Any = None,
    fallback_seconds: Any = None,
    failure_reason: str = "",
) -> str:
    """Render a sanitized final summary using the persisted song_log template."""
    sent_count = _count(sent)
    total_count = _count(total)
    if total_count == 0 and sent_count:
        total_count = sent_count
    duration = _seconds(sent_seconds)
    if duration is None:
        duration = _seconds(fallback_seconds) or 0.0
    if sent_count > 0 and total_count > 0 and sent_count >= total_count and not failure_reason:
        status = "成功"
    elif sent_count > 0:
        status = "部分成功"
    else:
        status = "失败"

    reason = _safe_text(failure_reason, limit=300).strip()
    values = {
        "时间": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "歌名": _short_text(title, 300) or "未知歌曲",
        "歌手": _short_text(artist, 200) or "未知歌手",
        "平台": _short_text(platform, 100) or "未知平台",
        "触发方式": _short_text(trigger, 80),
        "音色": _short_text(voice, 200) or "原唱",
        "结果": status,
        "已发送段数": sent_count,
        "总段数": total_count,
        "发送时长": _duration_text(duration),
        "来源会话": _short_text(source_session, 240) or "未知",
        "失败原因": reason,
    }
    templates = validate_templates(settings.get("reply_templates"))
    message = render_template("song_log", templates, values)
    return message[:2000]


async def send_song_log(
    context: Any,
    settings: dict[str, Any],
    *,
    title: str,
    artist: str,
    platform: str,
    trigger: str,
    voice: str,
    source_session: str,
    sent: Any,
    total: Any,
    sent_seconds: Any = None,
    fallback_seconds: Any = None,
    failure_reason: str = "",
) -> None:
    """Send one configured log message. All delivery errors are non-fatal."""
    section = settings.get("song_log") if isinstance(settings, dict) else None
    if not isinstance(section, dict) or section.get("enabled") is not True:
        return
    platform_id = section.get("platform_id")
    target_type = section.get("target_type")
    target_id = section.get("target_id")
    if (
        not isinstance(platform_id, str)
        or not platform_id
        or len(platform_id) > 128
        or ":" in platform_id
        or "!" in platform_id
        or target_type not in {"private", "group"}
        or not isinstance(target_id, str)
        or not target_id
        or len(target_id) > 128
        or ":" in target_id
    ):
        _warn_delivery_failure("target")
        return
    try:
        platform_getter = getattr(context, "get_platform_inst", None)
        sender = getattr(context, "send_message", None)
        if not callable(platform_getter) or platform_getter(platform_id) is None:
            raise LookupError("platform instance unavailable")
        if not callable(sender):
            raise LookupError("message sender unavailable")
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain

        message = build_song_log_message(
            settings,
            title=title,
            artist=artist,
            platform=platform,
            trigger=trigger,
            voice=voice,
            source_session=source_session,
            sent=sent,
            total=total,
            sent_seconds=sent_seconds,
            fallback_seconds=fallback_seconds,
            failure_reason=failure_reason,
        )
        chain = MessageChain(chain=[Plain(text=message)])
        result = sender(
            _target_umo(platform_id, target_type, target_id),
            chain,
        )
        if hasattr(result, "__await__"):
            result = await result
        if result is False:
            raise RuntimeError("message send returned false")
    except Exception:
        _warn_delivery_failure("send")


def _warn_delivery_failure(reason: str) -> None:
    warning = getattr(logger, "warning", None)
    if callable(warning):
        try:
            warning(f"点歌日志投递失败（{reason}），不影响音频发送结果。")
        except Exception:
            pass
