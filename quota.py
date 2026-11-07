"""Per-session daily music usage accounting and quota callback bridge."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4


API_VERSION = 2
TIMEZONE = "Asia/Shanghai"
LOCAL_TIMEZONE = timezone(timedelta(hours=8))
CHECKER_TIMEOUT_SECONDS = 5.0
DEFAULT_DENIAL = "已达到今日点歌额度。"
CHECKER_FAILURE = "当前暂时无法确认点歌额度，请稍后重试。"

Checker = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Settlement = Callable[[dict[str, Any]], Awaitable[Any] | Any]


def scope_from_event(event: Any) -> dict[str, str] | None:
    """Return the raw platform and QQ target identifiers from an AstrBot event."""

    def read_id(method_name: str) -> str:
        getter = getattr(event, method_name, None)
        if not callable(getter):
            return ""
        try:
            value = getter()
        except Exception:
            return ""
        return str(value).strip() if value is not None else ""

    platform_id = read_id("get_platform_id")
    if not platform_id:
        return None
    group_id = read_id("get_group_id")
    if group_id:
        return {"platform_id": platform_id, "kind": "group", "target_id": group_id}
    sender_id = read_id("get_sender_id")
    if sender_id:
        return {"platform_id": platform_id, "kind": "private", "target_id": sender_id}
    return None


class UsageUnavailableError(RuntimeError):
    """Raised when stored usage cannot be trusted."""


class _NullLogger:
    @staticmethod
    def warning(*_args: Any, **_kwargs: Any) -> None:
        pass

    @staticmethod
    def exception(*_args: Any, **_kwargs: Any) -> None:
        pass


class QuotaReservation:
    """One admitted playback; it counts on its first successful audio delivery."""

    def __init__(
        self,
        service: MusicQuotaService,
        *,
        request_id: str,
        session_id: str,
        day: str,
        source: str,
        kind: str,
        actor_id: str | None,
        request: dict[str, Any] | None = None,
        on_commit: Settlement | None = None,
        on_release: Settlement | None = None,
    ) -> None:
        self._service = service
        self.request_id = request_id
        self.session_id = session_id
        self.day = day
        self.timezone = TIMEZONE
        self.source = source
        self.kind = kind
        self.actor_id = actor_id
        self._external_request = dict(request) if request is not None else None
        self._on_commit = on_commit
        self._on_release = on_release
        self.confirmed = False
        self.released = False

    async def confirm(self) -> bool:
        """Record delivery once, completing the commit even if the caller is cancelled."""
        task = asyncio.create_task(self._service._confirm(self))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    async def release(self) -> bool:
        """Drop an unconfirmed reservation; confirmed usage remains counted."""
        task = asyncio.create_task(self._service._release(self))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise


class MusicQuotaService:
    """Persistent usage store with an optional policy and settlement bridge."""

    api_version = API_VERSION

    def __init__(self, usage_path: Path | str, *, logger: Any = None) -> None:
        self.path = Path(usage_path)
        self._timezone = LOCAL_TIMEZONE
        self._sessions: dict[str, dict[str, int]] = {}
        self._reservations: dict[str, QuotaReservation] = {}
        self._state_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._checker: tuple[str, str, Checker, Settlement | None, Settlement | None] | None = None
        self._logger = logger or _NullLogger()
        self._available = True
        self._closing = False
        self._load()

    def register_checker(
        self,
        owner: str,
        callback: Checker,
        *,
        on_commit: Settlement | None = None,
        on_release: Settlement | None = None,
    ) -> str:
        """Install the current policy callback and return its reload-safe token."""
        owner_name = str(owner or "").strip()
        if not owner_name:
            raise ValueError("checker owner is required")
        if not callable(callback):
            raise TypeError("checker callback must be callable")
        if on_commit is not None and not callable(on_commit):
            raise TypeError("on_commit callback must be callable")
        if on_release is not None and not callable(on_release):
            raise TypeError("on_release callback must be callable")
        token = uuid4().hex
        self._checker = (token, owner_name, callback, on_commit, on_release)
        return token

    def unregister_checker(self, token: str) -> bool:
        """Remove only the callback represented by this registration token."""
        current = self._checker
        if current is None or current[0] != token:
            return False
        self._checker = None
        return True

    async def get_usage(self, session_id: str, day: str | None = None) -> dict[str, Any]:
        """Return committed and in-flight counts for a session and Beijing day."""
        session = self._session_id(session_id)
        requested_day = self._normalize_day(day)
        async with self._state_lock:
            self._ensure_available()
            return self._usage_locked(session, requested_day)

    async def reserve(
        self,
        session_id: str,
        *,
        source: str,
        kind: str,
        actor_id: str | None = None,
        scope: dict[str, str] | None = None,
    ) -> tuple[QuotaReservation | None, str | None]:
        """Check policy and reserve one playback before its download or inference."""
        session = self._session_id(session_id)
        request_day = self._now().date().isoformat()
        request_id = uuid4().hex
        source_name = str(source or "").strip()
        kind_name = str(kind or "").strip()
        actor = str(actor_id).strip() if actor_id is not None else None
        request_scope = dict(scope) if isinstance(scope, dict) else None
        gate = self._session_locks.setdefault(session, asyncio.Lock())

        async with gate:
            while True:
                checker = self._checker
                async with self._state_lock:
                    if not self._available or self._closing:
                        return None, CHECKER_FAILURE
                    snapshot = self._usage_locked(session, request_day)
                if checker is None:
                    decision = {"allowed": True}
                    request = None
                else:
                    _, owner, callback, on_commit, on_release = checker
                    request = {
                        "request_id": request_id,
                        "session_id": session,
                        "day": request_day,
                        "timezone": TIMEZONE,
                        "used": snapshot["used"],
                        "reserved": snapshot["reserved"],
                        "source": source_name,
                        "kind": kind_name,
                        "actor_id": actor,
                        "scope": request_scope,
                    }
                    try:
                        decision = await asyncio.wait_for(
                            callback(dict(request)), timeout=CHECKER_TIMEOUT_SECONDS
                        )
                    except asyncio.CancelledError:
                        await self._settle_unadmitted(checker, request, None)
                        raise
                    except asyncio.TimeoutError:
                        await self._settle_unadmitted(checker, request, None)
                        self._logger.warning(f"Better Music quota checker timed out: {owner}")
                        return None, CHECKER_FAILURE
                    except Exception:
                        await self._settle_unadmitted(checker, request, None)
                        self._logger.exception(f"Better Music quota checker failed: {owner}")
                        return None, CHECKER_FAILURE

                    if self._checker is not checker:
                        await self._settle_unadmitted(checker, request, decision)
                        request_id = uuid4().hex
                        continue
                    if not isinstance(decision, dict) or type(decision.get("allowed")) is not bool:
                        await self._settle_unadmitted(checker, request, decision)
                        self._logger.warning(f"Better Music quota checker returned an invalid decision: {owner}")
                        return None, CHECKER_FAILURE
                    if not decision["allowed"]:
                        await self._settle_unadmitted(checker, request, decision)
                        reason = decision.get("reason")
                        reason = reason.strip()[:500] if isinstance(reason, str) else ""
                        return None, reason or DEFAULT_DENIAL

                    request = self._settlement_request(request, decision)

                reservation = QuotaReservation(
                    self,
                    request_id=request_id,
                    session_id=session,
                    day=request_day,
                    source=source_name,
                    kind=kind_name,
                    actor_id=actor,
                    request=request,
                    on_commit=checker[3] if checker is not None else None,
                    on_release=checker[4] if checker is not None else None,
                )
                admission_error = False
                checker_changed = False
                try:
                    async with self._state_lock:
                        if not self._available or self._closing:
                            admission_error = True
                        elif self._checker is not checker:
                            checker_changed = True
                        else:
                            self._reservations[request_id] = reservation
                except asyncio.CancelledError:
                    if checker is not None:
                        await self._settle_unadmitted(checker, request, None, prepared=True)
                    raise
                if admission_error or checker_changed:
                    if checker is not None:
                        await self._settle_unadmitted(checker, request, None, prepared=True)
                    if checker_changed:
                        request_id = uuid4().hex
                        continue
                    return None, CHECKER_FAILURE
                return reservation, None

    async def _confirm(self, reservation: QuotaReservation) -> bool:
        settlement = None
        async with self._state_lock:
            current = self._reservations.get(reservation.request_id)
            if current is not reservation or reservation.confirmed or reservation.released:
                return False
            days = self._sessions.setdefault(reservation.session_id, {})
            days[reservation.day] = days.get(reservation.day, 0) + 1
            reservation.confirmed = True
            self._reservations.pop(reservation.request_id, None)
            try:
                self._persist_locked()
            except Exception:
                # Delivery has already succeeded. Keep the in-memory count and never
                # turn a bookkeeping error into a retry of the audio segment.
                self._available = False
                self._logger.exception("Could not persist Better Music usage.")
            if reservation._on_commit is not None and reservation._external_request is not None:
                settlement = (reservation._on_commit, reservation._external_request)
        if settlement is not None:
            await self._invoke_settlement(*settlement, label="commit")
        return True

    async def _release(self, reservation: QuotaReservation) -> bool:
        settlement = None
        async with self._state_lock:
            current = self._reservations.get(reservation.request_id)
            if current is not reservation:
                return False
            self._reservations.pop(reservation.request_id, None)
            reservation.released = True
            if reservation._on_release is not None and reservation._external_request is not None:
                settlement = (reservation._on_release, reservation._external_request)
        if settlement is not None:
            await self._invoke_settlement(*settlement, label="release")
        return True

    async def close(self) -> None:
        """Stop new reservations and release all unconfirmed tickets."""
        async with self._state_lock:
            self._closing = True
            pending = list(self._reservations.values())
        for reservation in pending:
            try:
                await reservation.release()
            except Exception:
                self._logger.exception("Could not release a Better Music quota reservation during unload.")

    async def _settle_unadmitted(
        self,
        checker: tuple[str, str, Checker, Settlement | None, Settlement | None],
        request: dict[str, Any],
        decision: Any,
        *,
        prepared: bool = False,
    ) -> None:
        on_release = checker[4]
        if on_release is None:
            return
        settlement_request = (
            dict(request)
            if prepared
            else self._settlement_request(request, decision)
        )
        await self._invoke_settlement(on_release, settlement_request, label="release")

    @staticmethod
    def _settlement_request(request: dict[str, Any], decision: Any) -> dict[str, Any]:
        result = dict(request)
        if isinstance(decision, dict) and isinstance(decision.get("reservation_id"), str):
            result["reservation_id"] = decision["reservation_id"]
        return result

    async def _invoke_settlement(
        self,
        callback: Settlement,
        request: dict[str, Any],
        *,
        label: str,
    ) -> None:
        async def invoke() -> None:
            try:
                result = callback(dict(request))
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                self._logger.warning(f"Better Music quota {label} callback was cancelled.")
            except Exception:
                self._logger.exception(f"Better Music quota {label} callback failed.")

        task = asyncio.create_task(invoke())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except asyncio.CancelledError:
                self._logger.warning(f"Better Music quota {label} callback was cancelled.")

    def _usage_locked(self, session_id: str, day: str) -> dict[str, Any]:
        reserved = sum(
            1
            for reservation in self._reservations.values()
            if reservation.session_id == session_id and reservation.day == day
        )
        return {
            "api_version": API_VERSION,
            "session_id": session_id,
            "day": day,
            "timezone": TIMEZONE,
            "used": self._sessions.get(session_id, {}).get(day, 0),
            "reserved": reserved,
        }

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            self._available = False
            self._logger.exception("Could not read Better Music usage file; accounting is unavailable.")
            return
        if not isinstance(payload, dict) or payload.get("version") != 1:
            self._available = False
            self._logger.warning("Ignoring an unsupported Better Music usage file.")
            return
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            self._available = False
            return
        for session_id, days in sessions.items():
            if not isinstance(session_id, str) or not isinstance(days, dict):
                self._available = False
                return
            valid_days: dict[str, int] = {}
            for day, used in days.items():
                if not isinstance(day, str) or not self._valid_day(day):
                    self._available = False
                    return
                if type(used) is int and used >= 0:
                    valid_days[day] = used
                else:
                    self._available = False
                    return
            if valid_days:
                self._sessions[session_id] = valid_days

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "sessions": self._sessions},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = temp_file.name
                temp_file.write(payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _now(self) -> datetime:
        return datetime.now(self._timezone)

    def _ensure_available(self) -> None:
        if not self._available:
            raise UsageUnavailableError("stored music usage is unavailable")

    def _normalize_day(self, day: str | None) -> str:
        if day is None:
            return self._now().date().isoformat()
        if not isinstance(day, str) or not self._valid_day(day):
            raise ValueError("day must be an ISO date in YYYY-MM-DD format")
        return day

    @staticmethod
    def _valid_day(value: str) -> bool:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
        except ValueError:
            return False

    @staticmethod
    def _session_id(value: str) -> str:
        session = str(value or "").strip()
        if not session:
            raise ValueError("session_id is required")
        return session
