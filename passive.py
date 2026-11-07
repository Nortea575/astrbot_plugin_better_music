"""Persistent, conversation-scoped facts for passive music context."""

from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_STORED_EVENTS = 500
MAX_INJECTED_EVENTS = 10


class PassiveMusicMemory:
    """Store successful and partial song sends outside AstrBot conversation history."""

    def __init__(
        self,
        path: Path,
        *,
        max_events: int = MAX_STORED_EVENTS,
        max_injected: int = MAX_INJECTED_EVENTS,
    ) -> None:
        self.path = Path(path)
        self.max_events = max(1, int(max_events))
        self.max_injected = max(1, int(max_injected))
        self._lock = asyncio.Lock()

    async def record(
        self,
        *,
        umo: str,
        conversation_id: str,
        title: str,
        artist: str,
        platform: str,
        source: str,
        sent: int,
        total: int,
        requested_at: float | int | str | datetime | None = None,
        started_at: float | int | str | datetime | None = None,
        actor_id: str | None = None,
        source_message_id: str | None = None,
        failed_segment: str | None = None,
        voice_id: str | None = None,
        voice_name: str | None = None,
        voice_version: str | None = None,
        sent_seconds: float | int | None = None,
        capped: bool | None = None,
    ) -> dict[str, Any] | None:
        """Persist a sent song event; return None for zero/invalid delivery."""
        umo = str(umo or "").strip()
        conversation_id = str(conversation_id or "").strip()
        try:
            sent_count = int(sent)
            total_count = int(total)
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            not umo
            or not conversation_id
            or total_count <= 0
            or sent_count <= 0
            or sent_count > total_count
            or source not in {"chat", "web", "active"}
        ):
            return None
        try:
            duration = float(sent_seconds) if sent_seconds is not None else None
        except (TypeError, ValueError, OverflowError):
            duration = None
        if duration is not None and (not math.isfinite(duration) or duration < 0):
            duration = None

        outcome = (
            "complete"
            if sent_count == total_count and not failed_segment
            else "partial"
        )
        requested = _format_time(requested_at)
        started = _format_time(started_at)
        completed = _format_time(None)

        async with self._lock:
            state = self._read_state()
            sequence = state["next_sequence"]
            event = {
                "sequence": sequence,
                "umo": umo,
                "conversation_id": conversation_id,
                "requested_at": requested,
                "started_at": started,
                "completed_at": completed,
                "title": _bounded_text(title, 500),
                "artist": _bounded_text(artist, 500),
                "platform": _bounded_text(platform, 100),
                "source": source,
                "actor_id": _bounded_text(actor_id, 200) if actor_id else None,
                "source_message_id": (
                    _bounded_text(source_message_id, 200)
                    if source_message_id
                    else None
                ),
                "sent": sent_count,
                "total": total_count,
                "status": outcome,
                "voice_id": _bounded_text(voice_id, 200) if voice_id else None,
                "voice_name": _bounded_text(voice_name, 200) if voice_name else None,
                "voice_version": _bounded_text(voice_version, 200) if voice_version else None,
                "sent_seconds": duration,
                "capped": capped if type(capped) is bool else None,
            }
            events = state["events"]
            events.append(event)
            state["events"] = events[-self.max_events :]
            state["next_sequence"] = sequence + 1
            self._write_state(state)
            return dict(event)

    async def events_for(
        self,
        umo: str,
        conversation_id: str,
        *,
        limit: int | None = None,
        sources: tuple[str, ...] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the newest matching events in persistent order."""
        umo = str(umo or "").strip()
        conversation_id = str(conversation_id or "").strip()
        if not umo or not conversation_id:
            return []
        selected_limit = self.max_injected if limit is None else max(0, int(limit))
        if selected_limit == 0:
            return []
        async with self._lock:
            events = [
                event
                for event in self._read_state()["events"]
                if event.get("umo") == umo
                and event.get("conversation_id") == conversation_id
                and (sources is None or event.get("source") in sources)
            ]
        return [dict(event) for event in events[-selected_limit:]]

    def _read_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"next_sequence": 1, "events": []}
        if not isinstance(state, dict) or not isinstance(state.get("events"), list):
            return {"next_sequence": 1, "events": []}
        events = [
            event
            for event in state["events"]
            if isinstance(event, dict)
            and isinstance(event.get("sequence"), int)
        ][-self.max_events :]
        next_sequence = state.get("next_sequence")
        if not isinstance(next_sequence, int) or isinstance(next_sequence, bool):
            next_sequence = max((event["sequence"] for event in events), default=0) + 1
        else:
            next_sequence = max(
                next_sequence,
                max((event["sequence"] for event in events), default=0) + 1,
            )
        return {"next_sequence": next_sequence, "events": events}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            output.write(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
        temporary.replace(self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)


async def capture_current_scope(context: Any, umo: str) -> tuple[str, str] | None:
    """Capture the selected conversation, creating one only when none exists."""
    umo = str(umo or "").strip()
    manager = getattr(context, "conversation_manager", None)
    getter = getattr(manager, "get_curr_conversation_id", None)
    if not umo or not callable(getter):
        return None
    conversation_id = await getter(umo)
    if conversation_id:
        return umo, str(conversation_id)

    # Recheck under AstrBot's per-session lock before creating a first CID.
    from astrbot.core.utils.session_lock import session_lock_manager

    async with session_lock_manager.acquire_lock(umo):
        conversation_id = await getter(umo)
        if not conversation_id:
            creator = getattr(manager, "new_conversation", None)
            if not callable(creator):
                return None
            conversation_id = await creator(umo)
            if not conversation_id:
                conversation_id = await getter(umo)
    return (umo, str(conversation_id)) if conversation_id else None


def format_context(events: list[dict[str, Any]]) -> str:
    """Render a compact note containing only facts for the current conversation."""
    facts = [
        {
            "order_in_recent_records": index,
            "requested_at_utc": event["requested_at"],
            "started_at_utc": event["started_at"],
            "completed_at_utc": event["completed_at"],
            "title": event["title"],
            "artist": event["artist"],
            "platform": event["platform"],
            "source": event["source"],
            "actor_id": event["actor_id"],
            "sent_segments": event["sent"],
            "total_segments": event["total"],
            "status": event["status"],
            "voice_id": event.get("voice_id"),
            "voice_name": event.get("voice_name"),
            "voice_version": event.get("voice_version"),
            "sent_seconds": event.get("sent_seconds"),
            "capped": event.get("capped"),
        }
        for index, event in enumerate(events, start=1)
    ]
    return (
        "以下是当前 AstrBot 对话中已记录的真实点歌发送事实，按列表先后排列；"
        "时间为 UTC。status=partial 表示仅部分音频片段发送成功：\n"
        + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    )


def _format_time(value: float | int | str | datetime | None) -> str:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            moment = datetime.now(timezone.utc)
    else:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]
