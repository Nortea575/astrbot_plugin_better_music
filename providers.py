"""Account and audio providers for Better Music.

Credentials are kept in the plugin data directory. Platform SDKs and Node
dependencies are loaded only when a provider action needs them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

from .environment import node_process_environment
from .paths import data_subdirectory


PLATFORMS = (
    ("netease", "网易云音乐"),
    ("qq", "QQ音乐"),
    ("kugou", "酷狗音乐"),
)

_PLATFORM_NAMES = dict(PLATFORMS)
_NCM_NPM_URL = "https://registry.npmjs.org/@neteasecloudmusicapienhanced/api/-/api-4.41.1.tgz"
_KUGOU_ARCHIVE_URL = "https://github.com/MakcRe/KuGouMusicApi/archive/refs/tags/v1.6.3.tar.gz"
_LOGIN_TTL = 180.0
_CHAT_ACCOUNT_STATUS_TTL = 30.0
_HTTP_TIMEOUT = 15.0
_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus"}


class ProviderError(RuntimeError):
    """A user-facing provider or dependency error."""


def dependency_tools() -> dict[str, dict[str, Any]]:
    """Return the dependency source shown by the environment UI."""
    return {
        "qq": {
            "id": "qq",
            "name": "QQ音乐",
            "kind": "python",
            "packages": ["qqmusic-api-python==0.8.2", "qrcode[pil]==8.2"],
            "url": "",
        },
        "netease": {
            "id": "netease",
            "name": "网易云音乐",
            "kind": "npm",
            "version": "4.41.1",
            "url": _NCM_NPM_URL,
        },
        "kugou": {
            "id": "kugou",
            "name": "酷狗音乐",
            "kind": "github-archive",
            "version": "v1.6.3",
            "url": _KUGOU_ARCHIVE_URL,
        },
    }


def _obj(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_obj(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _obj(dump(mode="json"))
        except TypeError:
            return _obj(dump())
    if dataclasses.is_dataclass(value):
        return _obj(dataclasses.asdict(value))
    fields = getattr(value, "__dict__", None)
    if isinstance(fields, dict):
        return {str(k): _obj(v) for k, v in fields.items() if not str(k).startswith("_")}
    return str(value)


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        lowered = {str(k).lower(): v for k, v in value.items()}
        for name in names:
            if name.lower() in lowered and lowered[name.lower()] is not None:
                return lowered[name.lower()]
    else:
        for name in names:
            found = getattr(value, name, None)
            if found is not None:
                return found
    return default


def _exact_field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _walk(value: Any):
    value = _obj(value)
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _find(value: Any, *keys: str) -> Any:
    want = {key.lower() for key in keys}
    for item in _walk(value):
        for key, child in item.items():
            if key.lower() in want and child not in (None, "", [], {}):
                return child
    return None


def _parse_expiry(value: Any) -> str | None:
    if value in (None, "", 0, "0"):
        return None
    compact = str(value).strip()
    if compact.isdigit() and len(compact) in {8, 14}:
        from datetime import datetime, timezone

        pattern = "%Y%m%d" if len(compact) == 8 else "%Y%m%d%H%M%S"
        try:
            return datetime.strptime(compact, pattern).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    try:
        numeric = float(value)
        if 19_000_101 <= numeric <= 99_991_231:
            from datetime import datetime, timezone

            return datetime.strptime(str(int(numeric)), "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        if numeric > 1_000_000_000:
            from datetime import datetime, timezone

            return datetime.fromtimestamp(numeric, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    if isinstance(value, str):
        from datetime import datetime, timezone

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            pass
        return value
    return None


def _expiry_is_past(value: Any) -> bool:
    if value in (None, "", 0, "0"):
        return False
    compact = str(value).strip()
    if compact.isdigit() and len(compact) in {8, 14}:
        from datetime import datetime, timezone

        pattern = "%Y%m%d" if len(compact) == 8 else "%Y%m%d%H%M%S"
        try:
            return datetime.strptime(compact, pattern).replace(tzinfo=timezone.utc).timestamp() < time.time()
        except ValueError:
            pass
    try:
        numeric = float(value)
        if 19_000_101 <= numeric <= 99_991_231:
            from datetime import datetime, timezone

            numeric = datetime.strptime(str(int(numeric)), "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        if numeric > 1_000_000_000:
            return numeric < time.time()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    if isinstance(value, str):
        from datetime import datetime, timezone

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp() < time.time()
        except (ValueError, OverflowError, OSError):
            return False
    return False


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "active", "vip", "on"}:
            return True
        if lowered in {"0", "false", "no", "inactive", "none", "off", "expired"}:
            return False
    return None


def _membership(platform: str, source: Any) -> dict[str, Any]:
    data = _obj(source)
    if platform == "kugou":
        return {
            "status": "unknown",
            "label": "未知（上游会员信息为概念版范围）",
            "expires_at": None,
            "scope": "concept",
            "message": "上游此会员接口查询酷狗概念版权益，不能确认标准版会员状态。",
        }
    if not data:
        return {"status": "unknown", "label": "未知", "expires_at": None}

    if platform == "qq":
        identity = _find(data, "identity")
        if isinstance(identity, dict):
            vip = _truth(_field(identity, "vip"))
            svip = _truth(_field(identity, "svip"))
            expiry_value = _field(identity, "overdate")
            level = _field(identity, "level")
            flags = [value for value in (vip, svip) if value is not None]
            active = any(flags) if flags else None
            if active is None:
                return {
                    "status": "unknown",
                    "label": "未知",
                    "expires_at": _parse_expiry(expiry_value),
                    "message": "QQ音乐会员接口的identity未返回vip/svip状态。",
                }
            if _expiry_is_past(expiry_value):
                active = False
            if active:
                label = "QQ音乐超级会员" if svip else "QQ音乐绿钻会员"
                if level not in (None, "", 0, "0"):
                    label += f"（等级{level}）"
                return {"status": "active", "label": label, "expires_at": _parse_expiry(expiry_value)}
            return {"status": "inactive", "label": "非会员", "expires_at": _parse_expiry(expiry_value)}
        # qqmusic-api-python also exposes vip_flag on its typed VIP response.
        vip_flag = _truth(_field(data, "vip_flag"))
        if vip_flag is not None:
            expiry_value = _field(data, "overdate", "expire_time", "vip_expire_time")
            return {
                "status": "inactive" if not vip_flag or _expiry_is_past(expiry_value) else "active",
                "label": "QQ音乐绿钻会员" if vip_flag and not _expiry_is_past(expiry_value) else "非会员",
                "expires_at": _parse_expiry(expiry_value),
            }
        return {
            "status": "unknown",
            "label": "未知",
            "expires_at": None,
            "message": "QQ音乐会员接口未返回可确认的identity字段。",
        }

    if platform == "netease":
        vip_response = _field(data, "vip", default=data)
        body = _field(vip_response, "body", default=vip_response)
        vip_data = _field(body, "data", default=body)
        profile = _find(data, "profile")
        vip_type = _truth(_field(profile, "vipType", "vip_type")) if isinstance(profile, dict) else None
        profile_inactive = False
        if vip_type is not None:
            try:
                numeric_type = int(_field(profile, "vipType", "vip_type"))
            except (TypeError, ValueError):
                numeric_type = 0
            if numeric_type in {10, 11}:
                expiry_value = _field(profile, "vipExpireTime", "vip_expire_time", "expireTime")
                if _expiry_is_past(expiry_value):
                    return {"status": "inactive", "label": "会员已过期", "expires_at": _parse_expiry(expiry_value)}
                return {
                    "status": "active",
                    "label": "网易云黑胶VIP" if numeric_type == 11 else "网易云会员",
                    "expires_at": _parse_expiry(expiry_value),
                }
            if numeric_type == 0:
                profile_inactive = True

        active_labels: list[str] = []
        expiries: list[Any] = []
        checked: list[bool] = []
        for key, label in (("associator", "黑胶VIP"), ("musicPackage", "音乐包VIP"), ("redplus", "黑胶SVIP")):
            item = _field(vip_data, key)
            if not isinstance(item, dict):
                continue
            rights = _truth(_field(item, "rights"))
            code = _field(item, "vipCode")
            expires = _field(item, "expireTime", "expires_at", "expire_time")
            if rights is not None:
                checked.append(rights and not _expiry_is_past(expires))
            elif code is not None and expires is not None:
                checked.append(not _expiry_is_past(expires) and str(code) not in {"", "0"})
            if (rights is True or (rights is None and code is not None and str(code) not in {"", "0"})) and not _expiry_is_past(expires):
                active_labels.append(label)
                if expires not in (None, "", 0, "0"):
                    expiries.append(expires)
        if active_labels:
            expiry = expiries[0] if expiries else None
            return {
                "status": "active",
                "label": "网易云" + "、".join(active_labels),
                "expires_at": _parse_expiry(expiry),
            }
        if checked and len(checked) >= 2 and not any(checked):
            return {"status": "inactive", "label": "非会员", "expires_at": None}
        if profile_inactive:
            return {"status": "inactive", "label": "非会员", "expires_at": None}
        return {
            "status": "unknown",
            "label": "未知",
            "expires_at": None,
            "message": "网易云VIP接口未返回可确认的associator或musicPackage权益。",
        }

    expiry_value = _find(
        data,
        "expires_at",
        "expire_time",
        "vip_expire_time",
        "vip_end_time",
        "end_time",
        "vip_expire",
        "vipExpireTime",
        "vipEndTime",
        "deadline",
    )
    expires_at = _parse_expiry(expiry_value)
    expired = _expiry_is_past(expiry_value)

    explicit_status = _find(data, "is_vip", "isvip", "vip_status", "vipStatus", "active", "is_active")
    raw_type = _find(data, "vip_type", "vipType", "member_type", "memberType", "vip_level")
    raw_label = _find(data, "vip_name", "vipName", "member_name", "memberName", "level_name", "title")
    label = str(raw_label).strip() if raw_label not in (None, "", 0) else ""

    active: bool | None = None
    if isinstance(explicit_status, bool):
        active = explicit_status
    elif explicit_status is not None:
        try:
            active = int(explicit_status) > 0
        except (TypeError, ValueError):
            if str(explicit_status).lower() in {"active", "vip", "valid", "on"}:
                active = True
            elif str(explicit_status).lower() in {"inactive", "none", "off", "expired"}:
                active = False
    if active is None and raw_type is not None:
        try:
            type_number = int(raw_type)
            if platform == "netease":
                active = type_number in {10, 11}
            else:
                active = type_number > 0
        except (TypeError, ValueError):
            active = str(raw_type).strip().lower() not in {"", "none", "0", "free", "normal"}
    if active is None and label:
        active = any(word in label.lower() for word in ("vip", "会员", "绿钻", "黑胶", "svip"))
    if expired:
        active = False

    if active is True:
        status = "active"
        if not label:
            type_suffix = f"（{raw_type}）" if raw_type not in (None, "", 0) else ""
            label = f"{_PLATFORM_NAMES.get(platform, platform)}会员{type_suffix}"
    elif active is False:
        status = "inactive"
        if not label:
            label = "非会员"
    else:
        status = "unknown"
        label = label or "未知"
    return {"status": status, "label": label, "expires_at": expires_at}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _as_data_url(value: Any, mime: str = "image/png") -> str | None:
    if not value:
        return None
    if isinstance(value, bytes):
        raw = value
    else:
        text = str(value).strip()
        if text.startswith("data:"):
            return text
        if text.startswith(("http://", "https://")):
            return None
        if text.startswith("base64,"):
            text = text.split(",", 1)[1]
        try:
            raw = base64.b64decode(text, validate=False)
        except (ValueError, TypeError):
            return None
    if not raw:
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _netease_qr_state(value: Any) -> tuple[str, str]:
    body = _field(value, "body", default=value)
    data = _field(body, "data", default=body)
    code = _field(data, "code", default=_field(body, "code"))
    message = str(_field(data, "message", "msg", default=_field(body, "message", "msg", default="")) or "")
    try:
        number = int(code)
    except (TypeError, ValueError):
        number = -1
    return {
        800: ("expired", message or "二维码已过期，请重新发起登录。"),
        801: ("waiting", message or "等待扫码。"),
        802: ("scanned", message or "已扫码，请在网易云音乐客户端确认登录。"),
        803: ("success", message or "登录成功。"),
    }.get(number, ("", message))


def _netease_account_identity(value: Any) -> tuple[Any, Any, str]:
    body = _field(value, "body", default=value)
    data = _field(body, "data", default=body)
    profile = _field(data, "profile", default={})
    account = _field(data, "account", default={})
    user_id = _field(profile, "userId", "user_id")
    if user_id in (None, "", 0, "0"):
        user_id = _field(account, "id", "userId", "user_id")
    nickname = _field(profile, "nickname", "nickName") or _field(
        account,
        "userName",
        "nickname",
        "nickName",
        default="",
    )
    return profile, user_id, str(nickname or "").strip()


def _duration_seconds(value: Any, *, unit: str = "seconds") -> float | None:
    if value in (None, ""):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if unit == "milliseconds":
        return duration / 1000.0
    if unit != "seconds":
        raise ValueError(f"Unsupported duration unit: {unit}")
    return duration


def _kugou_duration_seconds(value: Any) -> float | None:
    duration = _exact_field(value, "Duration")
    if duration not in (None, ""):
        return _duration_seconds(duration, unit="seconds")
    duration_ms = _exact_field(value, "timelength")
    if duration_ms in (None, ""):
        duration_ms = _exact_field(value, "duration")
    return _duration_seconds(duration_ms, unit="milliseconds")


def _song_names(value: Any) -> str:
    if value in (None, ""):
        return "未知歌手"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, dict):
        return str(_field(value, "name", "title", "singername", default="未知歌手"))
    if isinstance(value, (list, tuple)):
        names = [
            str(_field(item, "name", "title", "singername", default=item))
            for item in value
            if item not in (None, "")
        ]
        return ", ".join(names) or "未知歌手"
    return str(_field(value, "name", "title", "singername", default="未知歌手"))


def _album_name(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    name = _field(value, "name", "title", "albumname", default=None)
    if name not in (None, ""):
        return str(name)
    return str(value)


def _candidate_query(platform: str, link: dict[str, str]) -> str:
    if platform == "netease":
        return link["id"]
    if platform == "qq":
        return link.get("mid") or link["id"]
    values = {"hash": link["hash"]}
    if link.get("album_id"):
        values["album_id"] = link["album_id"]
    if link.get("album_audio_id"):
        values["album_audio_id"] = link["album_audio_id"]
    return "https://www.kugou.com/song/?" + urllib.parse.urlencode(values)


def _kugou_qr_state(value: Any) -> tuple[str, str]:
    body = _field(value, "body", default=value)
    data = _field(body, "data", default=body)
    status = _field(data, "status", default=_field(body, "status"))
    message = str(_field(data, "message", "msg", default=_field(body, "message", "msg", default="")) or "")
    try:
        number = int(status)
    except (TypeError, ValueError):
        number = -1
    return {
        0: ("expired", message or "二维码已过期，请重新发起登录。"),
        1: ("waiting", message or "等待扫码。"),
        2: ("scanned", message or "已扫码，请在酷狗音乐客户端确认登录。"),
        4: ("success", message or "登录成功。"),
    }.get(number, ("", message))


def _host_matches_domain(host: str, domains: tuple[str, ...]) -> bool:
    lowered = host.rstrip(".").lower()
    return any(lowered == domain or lowered.endswith("." + domain) for domain in domains)


def _expand_share_link_sync(query: str, domains: tuple[str, ...]) -> str:
    class _ProviderRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, file_pointer, code, message, headers, new_url):
            target = urllib.parse.urlparse(urllib.parse.urljoin(request.full_url, new_url))
            host = (target.hostname or "").lower()
            try:
                allowed_port = target.port in (None, 80, 443)
            except ValueError:
                allowed_port = False
            if target.scheme != "https" or not allowed_port or not _host_matches_domain(host, domains):
                raise urllib.error.HTTPError(new_url, code, "分享链接重定向超出平台域名", headers, file_pointer)
            return super().redirect_request(request, file_pointer, code, message, headers, new_url)

    opener = urllib.request.build_opener(_ProviderRedirectHandler())
    with opener.open(query, timeout=8) as response:
        return response.geturl()


def _normalize_track_link(query: str, platform: str) -> dict[str, str] | None:
    text = str(query or "").strip()
    if not text:
        return None
    raw = text
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        host = (parsed.hostname or "").lower()
        host_platform = None
        if any(host == part or host.endswith("." + part) for part in ("163.com", "163cn.tv", "music.126.net")):
            host_platform = "netease"
        elif host == "qq.com" or host.endswith(".qq.com"):
            host_platform = "qq"
        elif host == "kugou.com" or host.endswith(".kugou.com"):
            host_platform = "kugou"
        if host_platform and host_platform != platform:
            raise ProviderError(f"这是{_PLATFORM_NAMES[host_platform]}链接，请选择对应平台。")
        if not host_platform:
            return None
        query_values = urllib.parse.parse_qs(parsed.query)
        fragment_values = urllib.parse.parse_qs(parsed.fragment)
        all_values = {**fragment_values, **query_values}
        path_parts = [part for part in parsed.path.split("/") if part]
        if platform == "netease":
            song_id = next((all_values.get(key, [None])[0] for key in ("id", "songId", "songid") if all_values.get(key)), None)
            if not song_id:
                for part in reversed(path_parts):
                    if part.isdigit():
                        song_id = part
                        break
            if song_id and str(song_id).isdigit():
                return {"id": str(song_id)}
        elif platform == "qq":
            song_mid = next((all_values.get(key, [None])[0] for key in ("mid", "songmid", "song_mid") if all_values.get(key)), None)
            if not song_mid:
                song_mid = next((part for part in reversed(path_parts) if re.fullmatch(r"[A-Za-z0-9]{14}", part)), None)
            song_id = next((all_values.get(key, [None])[0] for key in ("id", "songid") if all_values.get(key)), None)
            if song_mid:
                return {"mid": str(song_mid)}
            if song_id and str(song_id).isdigit():
                return {"id": str(song_id)}
        else:
            song_hash = next((all_values.get(key, [None])[0] for key in ("hash", "song_hash", "songhash") if all_values.get(key)), None)
            album_id = next((all_values.get(key, [None])[0] for key in ("album_id", "albumid") if all_values.get(key)), None)
            album_audio_id = next((all_values.get(key, [None])[0] for key in ("album_audio_id", "audio_id") if all_values.get(key)), None)
            if song_hash and re.fullmatch(r"[a-fA-F0-9]{32}", str(song_hash)):
                values = {"hash": str(song_hash).upper()}
                if album_id:
                    values["album_id"] = str(album_id)
                if album_audio_id:
                    values["album_audio_id"] = str(album_audio_id)
                return values
        return None
    if platform == "netease" and text.isdigit():
        return {"id": text}
    if platform == "qq":
        if re.fullmatch(r"[A-Za-z0-9]{14}", text):
            return {"mid": text}
        if text.isdigit():
            return {"id": text}
    if platform == "kugou" and re.fullmatch(r"[a-fA-F0-9]{32}", text):
        return {"hash": text.upper()}
    return None


def _audio_format(url: str, fallback: str | None = None) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in _AUDIO_EXTENSIONS:
        return suffix[1:]
    if fallback:
        return fallback
    raise ProviderError("平台返回的地址不是可识别的完整音频格式。")


def _has_audio_url(url: Any) -> bool:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix in _AUDIO_EXTENSIONS


def _secure_directory(path: Path, data_dir: Path) -> Path:
    root = Path(os.path.abspath(Path(data_dir).expanduser()))
    target = Path(os.path.abspath(Path(path).expanduser()))
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"插件数据目录超出数据根目录：{target}") from exc
    target = data_subdirectory(root, *relative.parts)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o700)
    return target


def _blocking_json_request(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = _HTTP_TIMEOUT,
    cookie_jar: CookieJar | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[Any, dict[str, str]]:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    full_url = url + ("&" if "?" in url else "?") + query if query else url
    request_headers = {"User-Agent": "BetterMusic/0.2.0", "Accept": "application/json", **(headers or {})}
    request = urllib.request.Request(full_url, headers=request_headers)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar)) if cookie_jar is not None else urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        if not raw:
            raise ProviderError(f"上游接口返回 HTTP {exc.code}。") from exc
    try:
        return json.loads(raw.decode("utf-8-sig")), response_headers
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        text = raw.decode("utf-8", "replace")[:240]
        raise ProviderError(f"上游接口返回了非 JSON 内容：{text}") from exc


async def _json_request(*args, **kwargs) -> tuple[Any, dict[str, str]]:
    return await asyncio.to_thread(_blocking_json_request, *args, **kwargs)


@dataclasses.dataclass
class _LoginSession:
    platform: str
    state: str
    message: str
    qr_image: str | None
    started_at: float
    expires_at: float
    task: asyncio.Task | None = None
    private: dict[str, Any] = dataclasses.field(default_factory=dict)
    account: dict[str, Any] | None = None


class ProviderManager:
    """Manage the three first-party provider adapters for one plugin instance."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(os.path.abspath(Path(data_dir).expanduser()))
        self.accounts_dir = self.data_dir / "accounts"
        self.providers_dir = self.data_dir / "providers"
        self._sessions: dict[str, _LoginSession] = {}
        self._kugou_process: asyncio.subprocess.Process | None = None
        self._kugou_base_url: str | None = None
        self._kugou_cookie_jar = CookieJar()
        self._ncm_process: asyncio.subprocess.Process | None = None
        self._ncm_lock = asyncio.Lock()
        self._kugou_lock = asyncio.Lock()
        self._npm_lock = asyncio.Lock()
        self._closed = False
        self._chat_account_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def dependency_tools() -> dict[str, dict[str, Any]]:
        return dependency_tools()

    def _check_platform(self, platform: str) -> None:
        if platform not in _PLATFORM_NAMES:
            raise ValueError(f"不支持的平台：{platform}")

    def _credential_path(self, platform: str) -> Path:
        return self.accounts_dir / f"{platform}.json"

    def _qq_device_path(self) -> str:
        _secure_directory(self.accounts_dir, self.data_dir)
        return str(self.accounts_dir / "qq_device.json")

    def _read_account(self, platform: str) -> dict[str, Any] | None:
        path = self._credential_path(platform)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return {"platform": platform, "message": "本地凭证文件无法读取，请重新登录。", "credential": {}}

    def _write_account(self, platform: str, value: dict[str, Any]) -> None:
        _secure_directory(self.accounts_dir, self.data_dir)
        target = self._credential_path(platform)
        temp = self.accounts_dir / f".{platform}.{uuid.uuid4().hex}.tmp"
        try:
            with temp.open("w", encoding="utf-8") as handle:
                os.chmod(temp, 0o600)
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            with contextlib.suppress(OSError):
                os.chmod(target, 0o600)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()
        self._chat_account_status_cache.pop(platform, None)

    def _account_public(self, platform: str, account: dict[str, Any] | None) -> dict[str, Any]:
        result = {
            "platform": platform,
            "name": _PLATFORM_NAMES[platform],
            "logged_in": bool(account and account.get("credential")),
            "nickname": None,
            "user_id": None,
            "membership": {"status": "unknown", "label": "未知", "expires_at": None},
            "message": "未登录。" if not account else str(account.get("message") or "已读取本地账号信息。"),
        }
        if not account:
            return result
        result["nickname"] = account.get("nickname")
        result["user_id"] = account.get("user_id")
        membership = account.get("membership")
        if isinstance(membership, dict):
            result["membership"] = {
                "status": membership.get("status", "unknown"),
                "label": membership.get("label", "未知"),
                "expires_at": membership.get("expires_at"),
            }
            if membership.get("scope"):
                result["membership"]["scope"] = membership["scope"]
            if membership.get("message"):
                result["membership"]["message"] = membership["message"]
        if not result["logged_in"]:
            result["message"] = account.get("message") or "未登录。"
        return result

    async def prepare_dependencies(self, platform: str, url: str = "") -> dict[str, Any]:
        """Prepare only the named provider after an explicit environment action."""
        self._check_platform(platform)
        if platform == "qq":
            try:
                self._qq_imports()
            except Exception as exc:
                return {
                    "platform": platform,
                    "installed": False,
                    "status": "pending",
                    "path": None,
                    "version": "0.8.2",
                    "message": f"缺少QQ音乐依赖。AstrBot应从插件requirements.txt安装qqmusic-api-python==0.8.2和qrcode[pil]==8.2。({exc})",
                }
            return {
                "platform": platform,
                "installed": True,
                "status": "pass",
                "path": "AstrBot Python环境",
                "version": "qqmusic-api-python 0.8.2",
                "message": "QQ音乐Python依赖已就绪。",
            }

        custom_url = str(url or "").strip()
        if custom_url:
            parsed_url = urllib.parse.urlparse(custom_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError("依赖来源必须是有效的 HTTP 或 HTTPS URL。")
        try:
            node, npm_cli = await self._prepare_node_tools()
        except Exception as exc:
            return {
                "platform": platform,
                "installed": False,
                "status": "pending",
                "path": None,
                "version": None,
                "message": str(exc),
            }

        target_dir = self.providers_dir / platform
        package_spec = custom_url or (_NCM_NPM_URL if platform == "netease" else _KUGOU_ARCHIVE_URL)
        if self._dependency_installed(platform):
            version = self._installed_version(platform)
            if not custom_url or self._installed_source(platform) == package_spec:
                return {
                    "platform": platform,
                    "installed": True,
                    "status": "pass",
                    "path": str(target_dir),
                    "version": version,
                    "message": f"{_PLATFORM_NAMES[platform]}依赖已就绪。",
                }

        async with self._npm_lock:
            _secure_directory(target_dir, self.data_dir)
            marker = self._install_marker_path(platform)
            with contextlib.suppress(FileNotFoundError):
                marker.unlink()
            env = os.environ.copy()
            env.update(node_process_environment(self.data_dir, "npm-tmp"))
            if platform == "kugou":
                env["platform"] = "standard"
            command = [
                str(node),
                str(npm_cli),
                "install",
                "--prefix",
                str(target_dir),
                "--no-save",
                "--no-package-lock",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                package_spec,
            ]
            try:
                process = None
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=str(target_dir),
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    output, _ = await asyncio.wait_for(process.communicate(), timeout=600)
                except asyncio.CancelledError:
                    await self._kill_and_reap(process)
                    raise
                except asyncio.TimeoutError as exc:
                    await self._kill_and_reap(process)
                    raise ProviderError(f"{_PLATFORM_NAMES[platform]}依赖安装超过10分钟，已停止。") from exc
                except OSError as exc:
                    raise ProviderError(f"无法启动npm安装：{exc}") from exc
                if process.returncode != 0:
                    detail = output.decode("utf-8", "replace")[-1200:].strip()
                    raise ProviderError(f"{_PLATFORM_NAMES[platform]}依赖安装失败。{detail}")
                await self._verify_node_package(platform, node)
                self._write_install_source(platform, package_spec)
                self._write_install_marker(platform, package_spec)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    marker.unlink()
                raise

        if not self._dependency_installed(platform):
            raise ProviderError(f"npm安装已结束，但未找到{_PLATFORM_NAMES[platform]}运行文件。")
        return {
            "platform": platform,
            "installed": True,
            "status": "pass",
            "path": str(target_dir),
            "version": self._installed_version(platform),
            "message": f"{_PLATFORM_NAMES[platform]}依赖安装完成。",
        }

    async def _prepare_node_tools(self) -> tuple[Path, Path]:
        try:
            from .environment import get_node_tools, prepare_node
        except Exception as exc:
            raise ProviderError("环境模块尚未提供Node.js准备功能，请先安装Node.js环境工具。") from exc
        await prepare_node(self.data_dir)
        try:
            node, npm_cli = get_node_tools(self.data_dir)
        except Exception as exc:
            raise ProviderError(f"Node.js/npm仍不可用：{exc}") from exc
        return Path(node), Path(npm_cli)

    def _write_install_source(self, platform: str, source: str) -> None:
        target = self.providers_dir / platform / ".better-music-source.json"
        _secure_directory(target.parent, self.data_dir)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps({"source": source}, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    def _install_marker_path(self, platform: str) -> Path:
        return self.providers_dir / platform / ".better-music-installed.json"

    def _write_install_marker(self, platform: str, source: str) -> None:
        target = self._install_marker_path(platform)
        payload = {
            "platform": platform,
            "source": source,
            "version": self._installed_version(platform),
            "verified": True,
            "verified_at": _now_iso(),
        }
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    async def _verify_node_package(self, platform: str, node: Path) -> None:
        package_path = self._package_path(platform)
        if platform == "netease":
            script = (
                "const api=require(process.argv[1]); "
                "for (const name of ['song_detail','song_url_v1','login_qr_key']) "
                "if (typeof api[name] !== 'function') throw new Error('missing export: '+name);"
            )
            entry = package_path / "main.js"
        else:
            script = (
                "const path=require('node:path'); "
                "const server=require(path.join(process.argv[1],'server.js')); "
                "if (typeof server.startService !== 'function') throw new Error('missing startService');"
            )
            entry = package_path
        if not entry.exists():
            raise ProviderError(f"{_PLATFORM_NAMES[platform]}安装目录缺少核心入口文件。")
        try:
            process = await asyncio.create_subprocess_exec(
                str(node),
                "-e",
                script,
                str(entry),
                cwd=str(entry.parent if entry.is_file() else entry),
                env={**os.environ, **node_process_environment(self.data_dir)},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=45)
        except asyncio.CancelledError:
            await self._kill_and_reap(process if "process" in locals() else None)
            raise
        except asyncio.TimeoutError as exc:
            await self._kill_and_reap(process if "process" in locals() else None)
            raise ProviderError(f"{_PLATFORM_NAMES[platform]}核心模块加载检查超时。") from exc
        except OSError as exc:
            raise ProviderError(f"无法运行{_PLATFORM_NAMES[platform]}模块检查：{exc}") from exc
        if process.returncode != 0:
            detail = output.decode("utf-8", "replace")[-1200:].strip()
            raise ProviderError(f"{_PLATFORM_NAMES[platform]}核心模块加载失败。{detail}")

    @staticmethod
    async def _kill_and_reap(process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    def _installed_source(self, platform: str) -> str | None:
        path = self.providers_dir / platform / ".better-music-source.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value.get("source")
        except (OSError, json.JSONDecodeError, AttributeError):
            return None

    def _dependency_installed(self, platform: str) -> bool:
        try:
            marker = json.loads(self._install_marker_path(platform).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(marker, dict) or marker.get("platform") != platform or marker.get("verified") is not True:
            return False
        root = self.providers_dir / platform / "node_modules"
        if platform == "netease":
            return (root / "@neteasecloudmusicapienhanced" / "api" / "main.js").is_file()
        if platform == "kugou":
            default_app = root / "kugoumusicapi" / "app.js"
            if default_app.is_file():
                return True
            if root.is_dir():
                with contextlib.suppress(OSError):
                    return any((item / "app.js").is_file() for item in root.iterdir() if item.is_dir())
        return False

    def _package_path(self, platform: str) -> Path:
        node_modules = self.providers_dir / platform / "node_modules"
        if platform == "netease":
            return node_modules / "@neteasecloudmusicapienhanced" / "api"
        default = node_modules / "kugoumusicapi"
        if (default / "app.js").is_file():
            return default
        if node_modules.is_dir():
            for child in node_modules.iterdir():
                if child.is_dir() and (child / "app.js").is_file():
                    return child
        return default

    def _installed_version(self, platform: str) -> str | None:
        try:
            info = json.loads((self._package_path(platform) / "package.json").read_text(encoding="utf-8"))
            return str(info.get("version") or "unknown")
        except (OSError, json.JSONDecodeError):
            return None

    def _ensure_prepared(self, platform: str) -> None:
        if platform == "qq":
            self._qq_imports()
            return
        if not self._dependency_installed(platform):
            raise ProviderError(
                f"{_PLATFORM_NAMES[platform]}运行依赖尚未安装，请在环境页明确安装该平台依赖后再试。"
            )

    @staticmethod
    def _qq_imports():
        try:
            import qrcode  # noqa: F401
            from qqmusic_api import Client, Credential  # noqa: F401
            from qqmusic_api.models.login import QRCodeLoginEvents, QRLoginType  # noqa: F401
            from qqmusic_api.modules.login_utils import QRCodeLoginSession  # noqa: F401
            from qqmusic_api.modules.song import SongFileInfo, SongFileType, SongQueryInfo  # noqa: F401
        except ImportError as exc:
            raise ProviderError(
                "缺少QQ音乐Python依赖；请通过AstrBot安装插件requirements.txt中的qqmusic-api-python==0.8.2与qrcode[pil]==8.2。"
            ) from exc
        return Client, Credential, QRCodeLoginEvents, QRLoginType, QRCodeLoginSession, SongFileInfo, SongFileType, SongQueryInfo

    async def start_login(self, platform: str) -> dict[str, Any]:
        self._check_platform(platform)
        if self._closed:
            raise ProviderError("音乐提供方已关闭。")
        existing = self._sessions.get(platform)
        if existing and existing.state in {"waiting", "scanned"} and existing.task and not existing.task.done():
            return self._login_public(existing)
        if existing:
            self._release_netease_login(existing)
        try:
            self._ensure_prepared(platform)
            if platform == "qq":
                session = await self._start_qq_login()
            elif platform == "netease":
                session = await self._start_netease_login()
            else:
                session = await self._start_kugou_login()
        except Exception as exc:
            message = self._clean_error(exc)
            failed = _LoginSession(platform, "error", message, None, time.monotonic(), time.monotonic())
            self._sessions[platform] = failed
            return self._login_public(failed)
        self._sessions[platform] = session
        return self._login_public(session)

    def _login_public(self, session: _LoginSession) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": session.state,
            "status": session.state,
            "message": session.message,
        }
        if session.qr_image:
            payload["qr_image"] = session.qr_image
        if session.state == "success" and session.account:
            public = self._account_public(session.platform, session.account)
            payload["account"] = {
                "nickname": public["nickname"],
                "user_id": public["user_id"],
                "membership": public["membership"],
            }
        return payload

    async def login_status(self, platform: str) -> dict[str, Any]:
        self._check_platform(platform)
        session = self._sessions.get(platform)
        if not session:
            return {"state": "idle", "status": "idle", "message": "当前没有进行中的登录。"}
        if session.state in {"waiting", "scanned"} and time.monotonic() >= session.expires_at:
            await self._finish_session(session, "expired", "二维码已超时，请重新发起登录。")
        return self._login_public(session)

    async def cancel_login(self, platform: str) -> dict[str, Any]:
        self._check_platform(platform)
        session = self._sessions.get(platform)
        if session and session.task and not session.task.done():
            session.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await session.task
        if session:
            self._release_netease_login(session)
        self._sessions.pop(platform, None)
        return {"state": "idle", "status": "idle", "message": "已取消登录。"}

    async def logout(self, platform: str) -> dict[str, Any]:
        self._check_platform(platform)
        await self.cancel_login(platform)
        with contextlib.suppress(FileNotFoundError):
            self._credential_path(platform).unlink()
        self._chat_account_status_cache.pop(platform, None)
        return {"platform": platform, "logged_in": False, "message": "已退出并移除本地凭证。"}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = []
        for session in self._sessions.values():
            if session.task and not session.task.done():
                session.task.cancel()
                tasks.append(session.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session in self._sessions.values():
            self._release_netease_login(session)
        await self._stop_ncm_bridge()
        await self._stop_kugou_server()

    @staticmethod
    def _release_netease_login(session: _LoginSession) -> None:
        client = session.private.pop("login_client", None)
        if client is not None:
            client.close()

    async def accounts(self, refresh: bool = False) -> list[dict[str, Any]]:
        result = []
        for platform, _ in PLATFORMS:
            account = self._read_account(platform)
            if refresh and account and account.get("credential"):
                try:
                    account = await self._refresh_account(platform, account)
                except Exception as exc:
                    account["message"] = f"已保留本地账号信息；刷新失败：{self._clean_error(exc)}"
            result.append(self._account_public(platform, account))
        return result

    async def chat_account_status(self, platform: str) -> dict[str, Any]:
        """Check one account for command use without refreshing the web account cache.

        The returned status is ``valid``, ``invalid``, ``not_logged_in`` or
        ``unknown``. Network/dependency failures remain unknown; they are never
        presented as an expired account or a non-member account.
        """
        self._check_platform(platform)
        now = time.monotonic()
        cached = self._chat_account_status_cache.get(platform)
        if cached and now - cached[0] < _CHAT_ACCOUNT_STATUS_TTL:
            return dict(cached[1])

        account = self._read_account(platform)
        credential = (account or {}).get("credential") or {}
        if not isinstance(credential, dict) or not credential:
            result = {
                "platform": platform,
                "status": "not_logged_in",
                "account": self._account_public(platform, account),
                "message": "尚未登录。",
            }
            self._chat_account_status_cache[platform] = (now, result)
            return dict(result)

        try:
            valid = await self._check_chat_credential(platform, credential)
            status = "valid" if valid is True else "invalid" if valid is False else "unknown"
            message = {
                "valid": "账号在线验证通过。",
                "invalid": "账号凭证已失效，请重新登录。",
                "unknown": "暂时无法确认账号状态。",
            }[status]
        except Exception as exc:
            if self._is_auth_rejection(exc):
                status = "invalid"
                message = "账号凭证已失效，请重新登录。"
            else:
                status = "unknown"
                message = f"暂时无法确认账号状态：{self._clean_error(exc)}"

        result = {
            "platform": platform,
            "status": status,
            "account": self._account_public(platform, account),
            "message": message,
        }
        self._chat_account_status_cache[platform] = (time.monotonic(), result)
        return dict(result)

    async def _check_chat_credential(
        self,
        platform: str,
        credential: dict[str, Any],
    ) -> bool | None:
        """Return True/False for a confirmed credential, None when inconclusive."""
        if platform == "netease":
            cookie = credential.get("cookie")
            if not isinstance(cookie, str) or not cookie.strip():
                return False
            self._ensure_prepared(platform)
            response = await self._ncm_call("login_status", {"cookie": cookie})
            body = _field(response, "body", default=response)
            data = _field(body, "data", default=body)
            code = _field(data, "code", default=_field(body, "code"))
            profile = _field(data, "profile", default={})
            user_id = _field(profile, "userId", "user_id", "id")
            if user_id not in (None, "", 0, "0"):
                if code is None:
                    return True
                try:
                    code_value = int(code)
                    if code_value == 200:
                        return True
                    if code_value in {301, 302, 401, 403}:
                        return False
                    return None
                except (TypeError, ValueError):
                    return None
            if code is not None:
                try:
                    code_value = int(code)
                    if code_value in {301, 302, 401, 403}:
                        return False
                    if code_value == 200:
                        return False
                except (TypeError, ValueError):
                    return None
            return None

        if platform == "qq":
            self._ensure_prepared(platform)
            Client, Credential, *_ = self._qq_imports()
            qq_credential = Credential(**credential)
            async with Client(credential=qq_credential, device_path=self._qq_device_path()) as client:
                response = await client.user.get_vip_info(credential=qq_credential)
            return self._qq_chat_response_state(response)

        cookie = credential.get("cookie")
        if not isinstance(cookie, str) or not cookie.strip():
            return False
        kugou_auth = self._kugou_chat_auth_fields(cookie)
        if kugou_auth is None:
            return False
        self._ensure_prepared(platform)
        response, _ = await self._kugou_request(
            "/user/detail",
            {"cookie": cookie, **kugou_auth},
            authenticated=False,
        )
        if self._payload_auth_rejection(response):
            return False
        response_data = _obj(response)
        if not isinstance(response_data, dict):
            return None
        try:
            error_code = int(_field(response_data, "error_code", "errorCode", default=-1))
            status = int(_field(response_data, "status", default=-1))
            if error_code in {401, 403, 1001, 1002, 2001, 20018} or status in {401, 403}:
                return False
        except (TypeError, ValueError):
            return None
        if status != 1 or error_code != 0:
            return None
        profile = _field(response_data, "data")
        if not isinstance(profile, dict) or not profile:
            return None
        if not any(
            key in profile
            for key in ("nickname", "nick_name", "username", "vip_type", "vipType", "user_name")
        ):
            return None
        return True

    @staticmethod
    def _kugou_chat_auth_fields(cookie: str) -> dict[str, str] | None:
        """Extract the explicit auth query fields expected by Kugou user detail."""
        values = {}
        for item in cookie.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key.strip() and value.strip():
                values[key.strip().casefold()] = value.strip()
        token = values.get("token") or values.get("clienttoken") or values.get("client_token")
        user_id = (
            values.get("userid")
            or values.get("user_id")
            or values.get("useridstr")
            or values.get("kugouid")
        )
        if not token or not user_id:
            return None
        return {"token": token, "userid": user_id}

    @staticmethod
    def _is_auth_rejection(error: BaseException) -> bool:
        for name in ("status_code", "status", "code"):
            value = getattr(error, name, None)
            try:
                if int(value) in {301, 302, 401, 403, 1001, 1002, 2001}:
                    return True
            except (TypeError, ValueError):
                pass
        message = str(error).casefold()
        return any(
            marker in message
            for marker in (
                "unauthorized",
                "login required",
                "not logged in",
                "invalid credential",
                "invalid cookie",
                "invalid token",
                "token expired",
                "cookie expired",
                "expired credential",
                "login state expired",
                "登录已失效",
                "登录态失效",
                "登录状态失效",
                "登录已过期",
                "凭证已失效",
                "凭证无效",
                "请先登录",
            )
        )

    @classmethod
    def _payload_auth_rejection(cls, payload: Any) -> bool:
        data = _obj(payload)
        if not isinstance(data, dict):
            return False
        code = _find(data, "code", "status", "errcode", "error_code")
        try:
            if int(code) in {401, 403, 1001, 1002, 2001, 301, 302}:
                return True
        except (TypeError, ValueError):
            pass
        message = str(_find(data, "message", "msg", "error") or "")
        return cls._is_auth_rejection(ProviderError(message)) if message else False

    @staticmethod
    def _payload_has_error_code(payload: Any) -> bool:
        data = _obj(payload)
        if not isinstance(data, dict):
            return False
        value = _find(data, "code", "errcode", "error_code")
        if value is None:
            value = _find(data, "status")
            if value is None:
                return False
            status_text = str(value).strip().casefold()
            if status_text in {"error", "failed", "fail"}:
                return True
            try:
                return int(value) >= 400
            except (TypeError, ValueError):
                return False
        normalized = str(value).strip().casefold()
        if normalized in {"0", "200", "ok", "success", "true"}:
            return False
        try:
            return int(value) not in {0, 200}
        except (TypeError, ValueError):
            return True

    @classmethod
    def _qq_chat_response_state(cls, response: Any) -> bool | None:
        """Trust a typed successful SDK response or explicit VIP identity only."""
        if response is None:
            return None
        if cls._payload_auth_rejection(response):
            return False
        if cls._payload_has_error_code(response):
            return None
        if _membership("qq", response).get("status") in {"active", "inactive"}:
            return True
        try:
            from qqmusic_api.models.user import UserVipInfoResponse

            if isinstance(response, UserVipInfoResponse):
                return True
        except ImportError:
            pass
        return None

    async def _refresh_account(self, platform: str, account: dict[str, Any]) -> dict[str, Any]:
        self._ensure_prepared(platform)
        if platform == "qq":
            return await self._refresh_qq(account)
        if platform == "netease":
            return await self._refresh_netease(account)
        return await self._refresh_kugou(account)

    async def _track_link(self, platform: str, query: str) -> dict[str, str] | None:
        link = _normalize_track_link(query, platform)
        if link:
            return link
        expanded = await self._expand_share_link(query, platform)
        if expanded != query:
            return _normalize_track_link(expanded, platform)
        return None

    @staticmethod
    def _candidate(
        platform: str,
        song_id: str,
        title: Any,
        artist: Any,
        album: Any,
        duration: Any,
        query: str,
    ) -> dict[str, Any]:
        return {
            "platform": platform,
            "id": str(song_id),
            "title": str(title or "未知歌曲"),
            "artist": _song_names(artist),
            "album": _album_name(album),
            "duration": duration,
            "query": query,
        }

    async def search(self, platform: str, query: str, limit: int = 10, page: int = 1) -> list[dict[str, Any]]:
        """Search a catalog without requesting any audio URL or downloading audio."""
        self._check_platform(platform)
        if self._closed:
            raise ProviderError("音乐提供方已关闭。")
        text = str(query or "").strip()
        if not text:
            raise ProviderError("请输入歌曲关键词、平台分享链接或歌曲ID。")
        if isinstance(limit, bool) or isinstance(page, bool):
            raise ProviderError("搜索条数和页码必须是整数。")
        try:
            limit = int(limit)
            page = int(page)
        except (TypeError, ValueError) as exc:
            raise ProviderError("搜索条数和页码必须是整数。") from exc
        if page < 1:
            raise ProviderError("搜索页码必须是大于等于1的整数。")
        limit = max(1, min(limit, 50))

        link = await self._track_link(platform, text)
        if not link:
            parsed = urllib.parse.urlparse(text)
            if parsed.scheme in {"http", "https"}:
                raise ProviderError("分享链接中没有识别到歌曲ID，请提供歌曲链接或ID。")
        self._ensure_prepared(platform)

        if link:
            if platform == "netease":
                account = self._read_account("netease")
                cookie = ((account or {}).get("credential") or {}).get("cookie")
                params = {"ids": link["id"]}
                if cookie:
                    params["cookie"] = cookie
                detail = await self._ncm_call("song_detail", params)
                body = _field(detail, "body", default=detail)
                data = _field(body, "data", default=body)
                songs = _field(data, "songs", default=[]) or []
                if isinstance(songs, dict):
                    songs = [songs]
                song = next((item for item in songs if str(_field(item, "id")) == link["id"]), None)
                if song is None:
                    raise ProviderError("网易云音乐歌曲详情接口没有返回匹配的歌曲。")
                return [self._candidate(
                    platform,
                    link["id"],
                    _field(song, "name", "title"),
                    _field(song, "artists", "ar", "artist"),
                    _field(song, "album", "al", "album_name"),
                    _duration_seconds(_field(song, "dt", "duration", "interval"), unit="milliseconds"),
                    _candidate_query(platform, link),
                )]
            if platform == "qq":
                Client, _, _, _, _, _, _, SongQueryInfo = self._qq_imports()
                async with Client(device_path=self._qq_device_path()) as client:
                    if link.get("mid"):
                        request = SongQueryInfo(mid=str(link["mid"]))
                    else:
                        request = SongQueryInfo(id=int(link["id"]))
                    response = await client.song.query_song([request])
                    tracks = _field(response, "tracks", default=[]) or []
                track = next(
                    (item for item in tracks if str(_field(item, "mid", "id", default="")) in {link.get("mid"), link.get("id")}),
                    tracks[0] if tracks else None,
                )
                if track is None:
                    raise ProviderError("QQ音乐没有返回匹配歌曲详情。")
                track_id = str(_field(track, "mid", "id", default=link.get("mid") or link.get("id") or ""))
                if not track_id:
                    raise ProviderError("QQ音乐歌曲详情缺少MID或歌曲ID。")
                return [self._candidate(
                    platform,
                    track_id,
                    _field(track, "title", "name"),
                    _field(track, "singer", "singers", "artists"),
                    _field(track, "album", "album_name"),
                    _duration_seconds(_field(track, "interval", "duration"), unit="seconds"),
                    _candidate_query(platform, {"mid": track_id}) if not track_id.isdigit() else _candidate_query(platform, {"id": track_id}),
                )]

            params = {"hash": link["hash"], "qualities": "128"}
            if link.get("album_id"):
                params["album_id"] = link["album_id"]
            metadata_response, _ = await self._kugou_request("/privilege/lite", params, authenticated=False)
            rows = _find(metadata_response, "quality_vip") or []
            if isinstance(rows, dict):
                rows = [rows]
            metadata = next(
                (item for item in rows if str(_field(item, "hash", default="")).upper() == link["hash"].upper()),
                rows[0] if rows else None,
            )
            if metadata is None:
                raise ProviderError("酷狗音乐没有返回匹配歌曲详情。")
            return [self._candidate(
                platform,
                link["hash"],
                _field(metadata, "name", "songname"),
                _field(metadata, "singername", "singer_name"),
                _field(metadata, "album_name", "albumname", "album"),
                _kugou_duration_seconds(metadata),
                _candidate_query(platform, link),
            )]

        if platform == "netease":
            account = self._read_account("netease")
            cookie = ((account or {}).get("credential") or {}).get("cookie")
            params: dict[str, Any] = {
                "keywords": text,
                "limit": limit,
                "offset": (page - 1) * limit,
                "type": 1,
            }
            if cookie:
                params["cookie"] = cookie
            result = await self._ncm_call("cloudsearch", params)
            body = _field(result, "body", default=result)
            rows = _field(_field(body, "result", default={}), "songs", default=[]) or []
            candidates = []
            for song in rows:
                song_id = _field(song, "id")
                if song_id in (None, ""):
                    continue
                song_id = str(song_id)
                candidates.append(self._candidate(
                    platform,
                    song_id,
                    _field(song, "name", "title"),
                    _field(song, "artists", "ar", "artist"),
                    _field(song, "album", "al", "album_name"),
                    _duration_seconds(_field(song, "dt", "duration", "interval"), unit="milliseconds"),
                    song_id,
                ))
            if not candidates:
                raise ProviderError("网易云音乐没有找到匹配歌曲。")
            return candidates[:limit]

        if platform == "qq":
            Client, _, _, _, _, _, _, _ = self._qq_imports()
            async with Client(device_path=self._qq_device_path()) as client:
                response = await client.search.search_by_type(text, num=limit, page=page)
            rows = _field(response, "song", default=[]) or []
            candidates = []
            for song in rows:
                song_id = _field(song, "mid")
                id_query = str(song_id) if song_id else _field(song, "id")
                if id_query in (None, ""):
                    continue
                id_query = str(id_query)
                link_data = {"mid": id_query} if song_id else {"id": id_query}
                candidates.append(self._candidate(
                    platform,
                    id_query,
                    _field(song, "title", "name"),
                    _field(song, "singer", "singers", "artists"),
                    _field(song, "album", "album_name"),
                    _duration_seconds(_field(song, "interval", "duration"), unit="seconds"),
                    _candidate_query(platform, link_data),
                ))
            if not candidates:
                raise ProviderError("QQ音乐没有找到匹配歌曲。")
            return candidates[:limit]

        account = self._read_account("kugou")
        cookie = ((account or {}).get("credential") or {}).get("cookie")
        if not cookie:
            raise ProviderError("酷狗音乐关键词搜索需要先登录酷狗账号。")
        result, _ = await self._kugou_request(
            "/search",
            {"keywords": text, "type": "song", "page": page, "pagesize": limit, "cookie": cookie},
            authenticated=False,
        )
        rows = _find(result, "lists", "songs", "list") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        candidates = []
        for song in rows:
            song_hash = _field(song, "FileHash", "hash", "filehash", "Hash")
            if not song_hash:
                continue
            song_hash = str(song_hash).upper()
            link_data = {"hash": song_hash}
            album_id = _field(song, "AlbumID", "album_id", "albumid")
            album_audio_id = _field(song, "AudioID", "audio_id", "album_audio_id")
            if album_id:
                link_data["album_id"] = str(album_id)
            if album_audio_id:
                link_data["album_audio_id"] = str(album_audio_id)
            candidates.append(self._candidate(
                platform,
                song_hash,
                _field(song, "SongName", "songname", "name", "audio_name"),
                _field(song, "SingerName", "singername", "author_name", "artist"),
                _field(song, "AlbumName", "albumname", "album_name", "album"),
                _kugou_duration_seconds(song),
                _candidate_query(platform, link_data),
            ))
        if not candidates:
            raise ProviderError("酷狗音乐没有找到匹配歌曲。")
        return candidates[:limit]

    async def resolve(self, platform: str, query: str) -> dict[str, Any]:
        self._check_platform(platform)
        if self._closed:
            raise ProviderError("音乐提供方已关闭。")
        text = str(query or "").strip()
        if not text:
            raise ProviderError("请输入歌曲关键词、平台分享链接或歌曲ID。")
        if not await self._track_link(platform, text):
            parsed = urllib.parse.urlparse(text)
            if parsed.scheme in {"http", "https"}:
                raise ProviderError("分享链接中没有识别到歌曲ID，请先搜索并选择歌曲。")
            raise ProviderError("关键词不能直接解析；请先搜索并选择一首歌曲。")
        self._ensure_prepared(platform)
        if platform == "qq":
            return await self._resolve_qq(text)
        if platform == "netease":
            return await self._resolve_netease(text)
        return await self._resolve_kugou(text)

    async def resolve_download(self, platform: str, query: str) -> dict[str, Any]:
        return await self.resolve(platform, query)

    async def probe(self, platform: str | None = None) -> dict[str, Any]:
        if platform is not None:
            self._check_platform(platform)
            return await self._probe_one(platform)
        checks = []
        for key, label in PLATFORMS:
            item = await self._probe_one(key)
            status = {"pass": "ok", "pending": "warning", "fail": "error"}[item["status"]]
            checks.append({
                "id": key,
                "label": label,
                "status": status,
                "dependency_installed": bool(item.get("dependency_installed")),
                "message": item["message"],
            })
        if all(check["status"] == "ok" for check in checks):
            status = "pass"
            message = "三家音乐平台依赖和轻量接口检查均已通过。"
        elif any(check["status"] == "error" for check in checks):
            status = "fail"
            message = "部分音乐平台依赖或接口检查失败。"
        else:
            status = "pending"
            message = "部分音乐平台尚未准备依赖或账号。"
        return {"status": status, "message": message, "checks": checks}

    async def _probe_one(self, platform: str) -> dict[str, Any]:
        account = self._read_account(platform)
        if platform == "qq":
            try:
                Client, *_ = self._qq_imports()
                async with Client(device_path=self._qq_device_path()) as client:
                    response = await client.search.search_by_type("音乐", num=1, page=1)
                songs = _field(response, "song", default=[]) or []
                if not songs:
                    return {"status": "pending", "dependency_installed": True, "message": "QQ音乐SDK可用，但轻量搜索未返回歌曲。"}
                if account and account.get("credential"):
                    return {"status": "pass", "dependency_installed": True, "message": "QQ音乐SDK和轻量搜索可用；本地已保存账号。"}
                return {"status": "pass", "dependency_installed": True, "message": "QQ音乐SDK和轻量搜索可用；尚未登录账号。"}
            except Exception as exc:
                try:
                    self._qq_imports()
                    installed = True
                except Exception:
                    installed = False
                return {"status": "fail", "dependency_installed": installed, "message": self._clean_error(exc)}
        if not self._dependency_installed(platform):
            return {"status": "pending", "dependency_installed": False, "message": f"{_PLATFORM_NAMES[platform]}依赖未准备；请在环境页安装。"}
        try:
            if platform == "netease":
                response = await self._ncm_call("search_hot", {})
                if not _field(response, "body", default=None):
                    return {"status": "fail", "dependency_installed": True, "message": "网易云音乐轻量接口未返回有效内容。"}
            else:
                await self._ensure_kugou_server()
                response, _ = await self._kugou_request("/search/hot", {}, authenticated=False)
                if not response:
                    return {"status": "fail", "dependency_installed": True, "message": "酷狗轻量接口未返回有效内容。"}
            if account and account.get("credential"):
                return {"status": "pass", "dependency_installed": True, "message": f"{_PLATFORM_NAMES[platform]}依赖和轻量接口可用；本地已保存账号。"}
            return {"status": "pass", "dependency_installed": True, "message": f"{_PLATFORM_NAMES[platform]}依赖和轻量接口可用；尚未登录账号。"}
        except Exception as exc:
            return {"status": "fail", "dependency_installed": True, "message": self._clean_error(exc)}

    async def _start_qq_login(self) -> _LoginSession:
        Client, _, _, QRLoginType, QRCodeLoginSession, *_ = self._qq_imports()
        client = Client(device_path=self._qq_device_path())
        await client.__aenter__()
        try:
            login = QRCodeLoginSession(client.login, QRLoginType.MOBILE, interval=1.5, timeout_seconds=_LOGIN_TTL)
            qr = await login.get_qrcode()
            qr_image = _as_data_url(qr.data, getattr(qr, "mimetype", "image/png"))
            if not qr_image:
                raise ProviderError("QQ音乐SDK未返回可展示的二维码图片。")
        except Exception:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
            raise
        session = _LoginSession("qq", "waiting", "请使用QQ音乐手机客户端扫码并确认登录。", qr_image, time.monotonic(), time.monotonic() + _LOGIN_TTL)
        session.private.update(client=client, login=login)
        session.task = asyncio.create_task(self._poll_qq_login(session), name="better-music-qq-login")
        return session

    async def _poll_qq_login(self, session: _LoginSession) -> None:
        client = session.private["client"]
        login = session.private["login"]
        _, _, events, *_ = self._qq_imports()
        try:
            async for result in login.iter_events():
                event = _field(result, "event")
                event_name = getattr(event, "name", str(event)).upper()
                if event == events.SCAN or event_name.endswith("SCAN"):
                    session.state = "scanned"
                    session.message = "已扫码，请在QQ音乐客户端确认登录。"
                elif event == events.CONF or event_name.endswith("CONF"):
                    session.state = "scanned"
                    session.message = "已确认登录，正在读取QQ音乐账号信息。"
                elif event == events.DONE or event_name.endswith("DONE"):
                    credential = _field(result, "credential")
                    if credential is None:
                        await self._finish_session(session, "error", "QQ音乐登录成功事件缺少凭证。")
                        return
                    account = await self._qq_account_record(client, credential)
                    self._write_account("qq", account)
                    session.account = account
                    await self._finish_session(session, "success", "QQ音乐登录成功。")
                    return
                elif event == events.REFUSE or event_name.endswith("REFUSE"):
                    await self._finish_session(session, "error", "已拒绝QQ音乐登录。")
                    return
                elif event == events.TIMEOUT or event_name.endswith("TIMEOUT"):
                    await self._finish_session(session, "expired", "QQ音乐二维码已过期，请重新发起登录。")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._finish_session(session, "error", self._clean_error(exc))
        finally:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)

    async def _qq_account_record(self, client: Any, credential: Any) -> dict[str, Any]:
        safe_credential = _obj(credential)
        if not isinstance(safe_credential, dict):
            safe_credential = {}
        user_id = str(_field(credential, "musicid", default="") or "")
        nickname = str(_find(safe_credential, "nickname", "nick_name", "nick") or "").strip()
        vip_data: Any = None
        vip_error = None
        try:
            vip_data = await client.user.get_vip_info(credential=credential)
        except Exception as exc:
            vip_error = self._clean_error(exc)
        membership = _membership("qq", vip_data)
        if vip_error and membership["status"] == "unknown":
            membership["message"] = f"会员信息读取失败：{vip_error}"
        if not nickname:
            nickname = f"QQ音乐账号 {user_id}" if user_id else "QQ音乐账号"
        return {
            "platform": "qq",
            "nickname": nickname,
            "user_id": user_id,
            "membership": membership,
            "credential": safe_credential,
            "updated_at": _now_iso(),
            "message": "账号信息已保存到本机。",
        }

    async def _refresh_qq(self, account: dict[str, Any]) -> dict[str, Any]:
        Client, Credential, *_ = self._qq_imports()
        credential_data = account.get("credential") or {}
        credential = Credential(**credential_data)
        async with Client(credential=credential, device_path=self._qq_device_path()) as client:
            updated = await self._qq_account_record(client, credential)
        self._write_account("qq", updated)
        return updated

    async def _resolve_qq(self, query: str) -> dict[str, Any]:
        account = self._read_account("qq")
        credential_data = (account or {}).get("credential")
        if not credential_data:
            raise ProviderError("请先登录QQ音乐账号，再解析歌曲。")
        Client, Credential, _, _, _, SongFileInfo, SongFileType, SongQueryInfo = self._qq_imports()
        credential = Credential(**credential_data)
        link = await self._track_link("qq", query)
        if not link:
            raise ProviderError("关键词不能直接解析；请先搜索并选择一首歌曲。")
        async with Client(credential=credential, device_path=self._qq_device_path()) as client:
            if link and link.get("mid"):
                mid = link["mid"]
            elif link and link.get("id"):
                mid = link["id"]
            else:
                raise ProviderError("QQ音乐歌曲链接没有有效的MID或ID。")
            if link.get("id"):
                track_info = await client.song.query_song([SongQueryInfo(id=int(link["id"]))])
            else:
                track_info = await client.song.query_song([SongQueryInfo(mid=str(mid))])
            tracks = _field(track_info, "tracks", default=[]) or []
            if not tracks:
                raise ProviderError("QQ音乐没有返回歌曲详情。")
            track = tracks[0]
            mid = str(_field(track, "mid", default=mid or ""))
            if not mid:
                raise ProviderError("QQ音乐歌曲详情缺少歌曲MID。")
            title = str(_field(track, "title", "name", default="未知歌曲"))
            singers = _field(track, "singer", "singers", default=[]) or []
            artist = ", ".join(str(_field(item, "name", "title", default=item)) for item in singers) or "未知歌手"
            duration = _field(track, "interval", "duration")
            file_info = _field(track, "file")
            media_mid = _field(file_info, "media_mid")
            response = await client.song.get_song_urls(
                [SongFileInfo(mid=mid, media_mid=media_mid)],
                file_type=SongFileType.MP3_128,
            )
            rows = _field(response, "data", default=[]) or []
            if not rows:
                raise ProviderError("QQ音乐没有提供该歌曲的完整播放文件。")
            row = rows[0]
            result_code = _field(row, "result")
            purl = _field(row, "purl")
            if result_code not in (None, 0, "0") or not purl:
                raise ProviderError("QQ音乐当前账号没有该歌曲的完整播放权限；未使用试听音源。")
            dispatch = await client.song.get_cdn_dispatch()
            cdns = _field(dispatch, "sip", default=[]) or []
            if not cdns:
                raise ProviderError("QQ音乐未返回可用的音频CDN地址。")
            url = str(purl) if str(purl).startswith(("http://", "https://")) else str(cdns[0]).rstrip("/") + "/" + str(purl).lstrip("/")
            if not _has_audio_url(url):
                raise ProviderError("QQ音乐返回的歌曲地址不是可识别的完整MP3文件。")
            return {
                "id": mid,
                "title": title,
                "artist": artist,
                "url": url,
                "headers": {},
                "duration": _duration_seconds(duration, unit="seconds"),
                "trial": False,
                "format": _audio_format(url),
            }

    async def _netease_weapi_encrypt(self, data: dict[str, Any]) -> dict[str, str]:
        value = await self._ncm_call("weapi_encrypt", {"data": data})
        if not isinstance(value, dict):
            raise ProviderError("网易云扫码登录请求加密失败。")
        return value

    async def _start_netease_login(self) -> _LoginSession:
        from ._netease_login import NeteaseQrLogin

        client = NeteaseQrLogin(self._netease_weapi_encrypt, timeout=_HTTP_TIMEOUT)
        try:
            key = await client.create_key()
            qr_url = "https://music.163.com/login?codekey=" + urllib.parse.quote(key, safe="")
            import qrcode
            import io

            image = qrcode.make(qr_url)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            qr_image = _as_data_url(buffer.getvalue(), "image/png")
            if not qr_image:
                raise ProviderError("网易云音乐二维码生成失败。")
        except asyncio.CancelledError:
            client.close()
            raise
        except Exception:
            client.close()
            raise
        session = _LoginSession("netease", "waiting", "请使用网易云音乐客户端扫码并确认登录。", qr_image, time.monotonic(), time.monotonic() + _LOGIN_TTL)
        session.private.update(key=key, login_client=client)
        session.task = asyncio.create_task(self._poll_netease_login(session), name="better-music-netease-login")
        return session

    async def _poll_netease_login(self, session: _LoginSession) -> None:
        client = session.private["login_client"]
        key = session.private["key"]
        try:
            while time.monotonic() < session.expires_at:
                await asyncio.sleep(1.5)
                result = await client.poll(key)
                state, message = _netease_qr_state(result)
                if state == "waiting":
                    session.state, session.message = "waiting", message
                elif state == "scanned":
                    session.state, session.message = "scanned", message
                elif state == "expired":
                    await self._finish_session(session, "expired", message)
                    return
                elif state == "success":
                    if not client.music_u():
                        raise ProviderError("网易云扫码状态返回成功，但会话中缺少 MUSIC_U Cookie。")
                    account_result = await client.verify_account()
                    if not client.music_u():
                        raise ProviderError("网易云账号验证后会话中缺少 MUSIC_U Cookie。")
                    cookie = client.cookie_header()
                    account = await self._netease_account_record(cookie, verified_account=account_result)
                    self._write_account("netease", account)
                    session.account = account
                    await self._finish_session(session, "success", "网易云音乐登录成功。")
                    return
                else:
                    status = _field(_field(result, "body", default=result), "code")
                    detail = message or f"网易云扫码状态接口返回未知状态（code={status!r}）。"
                    raise ProviderError(detail)
            await self._finish_session(session, "expired", "网易云音乐二维码已超时，请重新发起登录。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._finish_session(session, "error", self._clean_error(exc))
        finally:
            self._release_netease_login(session)

    async def _netease_account_record(
        self,
        cookie: str,
        *,
        verified_account: Any = None,
    ) -> dict[str, Any]:
        profile: Any = {}
        uid = None
        nickname = ""
        vip: Any = None
        vip_error = False
        errors: list[str] = []
        if verified_account is not None:
            body = _field(verified_account, "body", default=verified_account)
            data = _field(body, "data", default=body)
            code = _field(data, "code", default=_field(body, "code"))
            try:
                valid_response = int(code) == 200
            except (TypeError, ValueError):
                valid_response = False
            if not valid_response:
                raise ProviderError("网易云账号验证接口没有确认登录状态。")
            profile, uid, nickname = _netease_account_identity(verified_account)
            uid_text = str(uid or "").strip()
            if not uid_text.isdigit() or int(uid_text) <= 0 or not nickname:
                raise ProviderError("网易云账号验证接口未返回有效的用户ID和昵称。")
        else:
            try:
                status = await self._ncm_call("login_status", {"cookie": cookie})
                account = _field(_field(status, "body", default={}), "data", default={})
                profile = _field(account, "profile", default={})
                uid = _field(profile, "userId", "user_id", default=_find(status, "userId", "user_id"))
                nickname = str(_field(profile, "nickname", "nickName", default="") or "")
            except Exception as exc:
                errors.append(f"账号资料读取失败：{self._clean_error(exc)}")
        if uid:
            try:
                vip = await self._ncm_call("vip_info", {"uid": uid, "cookie": cookie})
                vip_body = _field(vip, "body", default=vip)
                vip_code = _field(vip_body, "code")
                vip_http_status = _field(vip, "status")
                try:
                    failed_status = vip_http_status is not None and int(vip_http_status) >= 400
                except (TypeError, ValueError):
                    failed_status = False
                if failed_status or (vip_code is not None and str(vip_code) != "200"):
                    vip_error = True
                    message = _find(vip_body, "message", "msg", "error") or "上游未确认会员信息"
                    errors.append(f"会员信息读取失败：{message}")
            except Exception as exc:
                vip_error = True
                errors.append(f"会员信息读取失败：{self._clean_error(exc)}")
        elif not uid:
            vip_error = True
            errors.append("会员信息读取失败：账号资料没有返回用户ID。")
        membership_source = {"vip": vip if vip and not vip_error else {}}
        if not vip_error:
            membership_source["profile"] = profile
        membership = _membership("netease", membership_source)
        if errors and membership["status"] == "unknown":
            membership["message"] = "；".join(errors)
        return {
            "platform": "netease",
            "nickname": str(nickname or f"网易云账号 {uid or ''}").strip(),
            "user_id": str(uid) if uid is not None else None,
            "membership": membership,
            "credential": {"cookie": cookie},
            "updated_at": _now_iso(),
            "message": "账号凭证已保存到本机。" + (f" {errors[0]}" if errors else ""),
        }

    async def _refresh_netease(self, account: dict[str, Any]) -> dict[str, Any]:
        cookie = (account.get("credential") or {}).get("cookie")
        if not cookie:
            raise ProviderError("本地网易云凭证缺少Cookie。")
        updated = await self._netease_account_record(cookie)
        self._write_account("netease", updated)
        return updated

    async def _resolve_netease(self, query: str) -> dict[str, Any]:
        account = self._read_account("netease")
        cookie = (account or {}).get("credential", {}).get("cookie")
        if not cookie:
            raise ProviderError("请先登录网易云音乐账号，再解析歌曲。")
        link = await self._track_link("netease", query)
        if not link or not link.get("id"):
            raise ProviderError("关键词不能直接解析；请先搜索并选择一首歌曲。")
        song_id = link["id"]
        detail = await self._ncm_call("song_detail", {"ids": str(song_id), "cookie": cookie})
        detail_body = _field(detail, "body", default=detail)
        detail_data = _field(detail_body, "data", default=detail_body)
        detail_rows = _field(detail_data, "songs", default=[]) or []
        if isinstance(detail_rows, dict):
            detail_rows = [detail_rows]
        song = next((item for item in detail_rows if str(_field(item, "id")) == str(song_id)), None)
        if song is None:
            raise ProviderError("网易云音乐歌曲详情接口没有返回匹配的歌曲元数据。")
        title = str(_field(song, "name", "title", default="未知歌曲"))
        artists = _field(song, "artists", "ar", default=[]) if song is not None else []
        artist = ", ".join(str(_field(item, "name", default=item)) for item in (artists or [])) or "未知歌手"
        duration_ms = _field(song, "dt", "duration") if song is not None else None
        duration = _duration_seconds(duration_ms, unit="milliseconds")
        response = await self._ncm_call("song_url_v1", {"id": song_id, "level": "standard", "unblock": "false", "cookie": cookie})
        body = _field(response, "body", default={})
        rows = _field(body, "data", default=[]) or []
        item = rows[0] if rows else {}
        url = _field(item, "url")
        trial_info = _field(item, "freeTrialInfo", "free_trial_info")
        if trial_info not in (None, "", {}, [], "null"):
            raise ProviderError("网易云音乐只提供试听片段；没有返回完整歌曲。")
        if not _has_audio_url(url):
            message = _find(response, "message", "msg")
            raise ProviderError(str(message or "网易云音乐未提供可解析的完整歌曲地址；没有使用其他平台音源。"))
        return {
            "id": str(song_id),
            "title": title,
            "artist": artist,
            "url": str(url),
            "headers": {},
            "duration": duration,
            "trial": False,
            "format": _audio_format(str(url)),
        }

    async def _start_kugou_login(self) -> _LoginSession:
        await self._ensure_kugou_server()
        key_response, _ = await self._kugou_request("/login/qr/key", {"timestamp": int(time.time() * 1000)}, authenticated=False)
        key = _find(key_response, "key", "qr_key", "qrkey", "qrcode")
        if not key:
            raise ProviderError("酷狗音乐没有返回二维码登录Key。")
        qr_response, _ = await self._kugou_request(
            "/login/qr/create",
            {"key": key, "qrimg": 1, "timestamp": int(time.time() * 1000)},
            authenticated=False,
        )
        qr_image_value = _find(qr_response, "base64", "qrimg", "qrcode", "qr_image", "image")
        qr_image = _as_data_url(qr_image_value, "image/png")
        if not qr_image:
            qr_url = _find(qr_response, "qrurl", "url")
            if qr_url:
                import qrcode
                import io

                image = qrcode.make(str(qr_url))
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                qr_image = _as_data_url(buffer.getvalue(), "image/png")
        if not qr_image:
            raise ProviderError("酷狗音乐没有返回可展示的二维码图片。")
        session = _LoginSession("kugou", "waiting", "请使用酷狗音乐客户端扫码并确认登录。", qr_image, time.monotonic(), time.monotonic() + _LOGIN_TTL)
        session.private.update(key=str(key))
        session.task = asyncio.create_task(self._poll_kugou_login(session), name="better-music-kugou-login")
        return session

    async def _poll_kugou_login(self, session: _LoginSession) -> None:
        key = session.private["key"]
        try:
            while time.monotonic() < session.expires_at:
                await asyncio.sleep(1.5)
                result, _ = await self._kugou_request(
                    "/login/qr/check",
                    {"key": key, "timestamp": int(time.time() * 1000)},
                    authenticated=False,
                )
                state, message = _kugou_qr_state(result)
                if state == "waiting":
                    session.state, session.message = "waiting", message
                    continue
                if state == "scanned":
                    session.state, session.message = "scanned", message
                    continue
                if state == "expired":
                    await self._finish_session(session, "expired", message)
                    return
                if state == "success" or self._kugou_login_token(result):
                    cookie = self._kugou_cookie_from_result(result)
                    if not cookie:
                        await self._finish_session(session, "error", "酷狗登录成功，但上游没有返回可用凭证。")
                        return
                    account = await self._kugou_account_record(cookie)
                    self._write_account("kugou", account)
                    session.account = account
                    await self._finish_session(session, "success", "酷狗音乐登录成功。")
                    return
                if message:
                    session.message = message
            await self._finish_session(session, "expired", "酷狗音乐二维码已超时，请重新发起登录。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._finish_session(session, "error", self._clean_error(exc))

    @staticmethod
    def _kugou_login_token(value: Any) -> bool:
        return bool(_find(value, "token", "userid", "user_id", "useridstr"))

    def _kugou_cookie_from_result(self, value: Any) -> str | None:
        cookie_value = _find(value, "cookie")
        if isinstance(cookie_value, dict):
            pieces = [f"{key}={val}" for key, val in cookie_value.items() if val not in (None, "")]
            cookie = "; ".join(pieces)
        elif isinstance(cookie_value, (list, tuple)):
            pieces = [str(item).split(";", 1)[0].strip() for item in cookie_value]
            cookie = "; ".join(item for item in pieces if item)
        else:
            cookie = str(cookie_value or "").strip()
        data = _find(value, "data") or value
        token = _find(data, "token", "clienttoken", "client_token")
        user_id = _find(data, "userid", "user_id", "useridstr", "kugouid")
        if token and user_id:
            cookie = "; ".join(part for part in (cookie, f"token={token}", f"userid={user_id}") if part)
        jar_cookie = self._kugou_cookie_header()
        cookie = "; ".join(part for part in (cookie, jar_cookie) if part)
        return cookie or None

    def _kugou_cookie_header(self) -> str:
        cookies = []
        for cookie in self._kugou_cookie_jar:
            if cookie.domain and cookie.domain.lstrip(".") in {"127.0.0.1", "localhost"}:
                cookies.append(f"{cookie.name}={cookie.value}")
        return "; ".join(cookies)

    async def _kugou_account_record(self, cookie: str) -> dict[str, Any]:
        user: Any = None
        vip: Any = None
        user_error = None
        vip_error = None
        try:
            user, _ = await self._kugou_request("/user/detail", {"cookie": cookie}, authenticated=False)
        except Exception as exc:
            user_error = self._clean_error(exc)
        try:
            vip, _ = await self._kugou_request("/user/vip/detail", {"cookie": cookie}, authenticated=False)
        except Exception as exc:
            vip_error = self._clean_error(exc)
        source = _find(user, "data") or user or {}
        user_id = _find(source, "userid", "user_id", "useridstr", "kugouid") or _find(vip, "userid", "user_id")
        nickname = _find(source, "nickname", "nick_name", "username", "user_name")
        membership = _membership("kugou", vip)
        if vip_error:
            membership["message"] = f"概念版会员信息读取失败：{vip_error}"
        message = "账号凭证已保存到本机。"
        if user_error:
            message += f" 账号资料读取失败：{user_error}"
        return {
            "platform": "kugou",
            "nickname": str(nickname or f"酷狗账号 {user_id or ''}").strip(),
            "user_id": str(user_id) if user_id is not None else None,
            "membership": membership,
            "credential": {"cookie": cookie},
            "updated_at": _now_iso(),
            "message": message,
        }

    async def _refresh_kugou(self, account: dict[str, Any]) -> dict[str, Any]:
        cookie = (account.get("credential") or {}).get("cookie")
        if not cookie:
            raise ProviderError("本地酷狗凭证缺少Cookie。")
        updated = await self._kugou_account_record(cookie)
        self._write_account("kugou", updated)
        return updated

    async def _resolve_kugou(self, query: str) -> dict[str, Any]:
        account = self._read_account("kugou")
        credential = (account or {}).get("credential") or {}
        cookie = credential.get("cookie")
        if not cookie:
            raise ProviderError("请先登录酷狗音乐账号，再解析歌曲。")
        link = await self._track_link("kugou", query)
        if not link or not link.get("hash"):
            raise ProviderError("关键词不能直接解析；请先搜索并选择一首歌曲。")
        song_hash = link["hash"]
        params = {"hash": song_hash, "quality": "128", "cookie": cookie, "timestamp": int(time.time() * 1000)}
        if link.get("album_id"):
            params["album_id"] = link["album_id"]
        if link.get("album_audio_id"):
            params["album_audio_id"] = link["album_audio_id"]
        response, _ = await self._kugou_request("/song/url", params, authenticated=False)
        audio_item = _find(response, "data") or response
        url = _find(audio_item, "url", "play_url", "playUrl")
        if isinstance(url, list):
            url = next((item for item in url if _has_audio_url(item)), None)
        if not _has_audio_url(url):
            raise ProviderError("酷狗音乐未提供该歌曲的完整MP3地址；没有使用试听或加密音源。")
        try:
            metadata_response, _ = await self._kugou_request(
                "/privilege/lite",
                {"hash": song_hash, "qualities": "128", "album_id": link.get("album_id", "")},
                authenticated=False,
            )
            metadata_rows = _find(metadata_response, "quality_vip") or []
            if isinstance(metadata_rows, dict):
                metadata_rows = [metadata_rows]
            metadata = next(
                (item for item in metadata_rows if str(_field(item, "hash", default="")).upper() == song_hash.upper()),
                metadata_rows[0] if metadata_rows else {},
            )
            title = _field(metadata, "name", "songname", default="未知歌曲")
            artist = _field(metadata, "singername", "singer_name", default="未知歌手")
            duration = _kugou_duration_seconds(metadata)
        except Exception as exc:
            raise ProviderError(f"酷狗歌曲元数据读取失败：{self._clean_error(exc)}") from exc
        if title == "未知歌曲" or artist == "未知歌手":
            raise ProviderError("酷狗歌曲详情未返回歌曲名或歌手信息。")
        return {
            "id": song_hash,
            "title": str(title),
            "artist": str(artist),
            "url": str(url),
            "headers": {},
            "duration": duration,
            "trial": False,
            "format": _audio_format(str(url), "mp3"),
        }

    async def _kugou_request(self, route: str, params: dict[str, Any], *, authenticated: bool) -> tuple[Any, dict[str, str]]:
        await self._ensure_kugou_server()
        assert self._kugou_base_url
        base_url = self._kugou_base_url
        if authenticated and not params.get("cookie"):
            account = self._read_account("kugou")
            cookie = (account or {}).get("credential", {}).get("cookie")
            if cookie:
                params = {**params, "cookie": cookie}
        return await _json_request(f"{base_url}{route}", params, timeout=_HTTP_TIMEOUT, cookie_jar=self._kugou_cookie_jar)

    async def _ensure_kugou_server(self) -> None:
        if self._kugou_process and self._kugou_process.returncode is None and self._kugou_base_url:
            return
        if self._kugou_process and self._kugou_process.returncode is not None:
            with contextlib.suppress(Exception):
                await self._kugou_process.wait()
            self._kugou_process = None
            self._kugou_base_url = None
        self._ensure_prepared("kugou")
        async with self._kugou_lock:
            if self._kugou_process and self._kugou_process.returncode is None and self._kugou_base_url:
                return
            node, _ = self._node_tools()
            app_path = self._package_path("kugou") / "app.js"
            if not app_path.is_file():
                raise ProviderError("未找到酷狗音乐API入口文件，请重新准备酷狗依赖。")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = os.environ.copy()
            env.update(node_process_environment(self.data_dir, "kugou-tmp"))
            env.update({"HOST": "127.0.0.1", "PORT": str(port), "platform": "standard"})
            self._kugou_process = await asyncio.create_subprocess_exec(
                str(node),
                str(app_path),
                cwd=str(app_path.parent),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._kugou_base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if self._kugou_process.returncode is not None:
                    code = self._kugou_process.returncode
                    self._kugou_process = None
                    self._kugou_base_url = None
                    raise ProviderError(f"酷狗音乐API本地进程启动失败（退出码 {code}）。")
                try:
                    await _json_request(self._kugou_base_url, timeout=1.5, cookie_jar=self._kugou_cookie_jar)
                    return
                except ProviderError as exc:
                    if "非 JSON" not in str(exc):
                        await asyncio.sleep(0.25)
                        continue
                    return
                except Exception:
                    await asyncio.sleep(0.25)
            await self._stop_kugou_server()
            raise ProviderError("酷狗音乐API本地服务启动超时。")

    def _node_tools(self) -> tuple[Path, Path]:
        try:
            from .environment import get_node_tools

            node, npm_cli = get_node_tools(self.data_dir)
            return Path(node), Path(npm_cli)
        except Exception as exc:
            raise ProviderError(f"Node.js环境未就绪：{exc}") from exc

    async def _stop_kugou_server(self) -> None:
        process = self._kugou_process
        self._kugou_process = None
        self._kugou_base_url = None
        if process:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    async def _ncm_call(self, method: str, params: dict[str, Any]) -> Any:
        self._ensure_prepared("netease")
        async with self._ncm_lock:
            await self._ensure_ncm_bridge()
            if not self._ncm_process or not self._ncm_process.stdin or not self._ncm_process.stdout:
                raise ProviderError("网易云音乐Node适配进程不可用。")
            proc = self._ncm_process
            if proc.returncode is not None:
                self._ncm_process = None
                raise ProviderError("网易云音乐Node适配进程已退出，请重试。")
            request_id = uuid.uuid4().hex
            request = json.dumps({"id": request_id, "method": method, "params": params}, ensure_ascii=False)
            try:
                proc.stdin.write((request + "\n").encode("utf-8"))
                await proc.stdin.drain()
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=45)
            except asyncio.CancelledError:
                await self._stop_ncm_bridge()
                raise
            except (BrokenPipeError, ConnectionError, asyncio.TimeoutError) as exc:
                await self._stop_ncm_bridge()
                raise ProviderError(f"网易云音乐Node调用失败：{exc}") from exc
            if not line:
                error = await self._ncm_stderr()
                await self._stop_ncm_bridge()
                raise ProviderError(f"网易云音乐Node适配进程无响应。{error}")
            try:
                response = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                error = await self._ncm_stderr()
                await self._stop_ncm_bridge()
                detail = f" {error}" if error else ""
                raise ProviderError(f"网易云音乐Node适配进程返回了无法解析的数据。{detail}") from exc
            if not isinstance(response, dict) or response.get("id") != request_id:
                await self._stop_ncm_bridge()
                raise ProviderError("网易云音乐Node适配进程响应编号不匹配，已重启适配进程。")
            if not response.get("ok"):
                message = self._clean_error(Exception(str(response.get("error") or "网易云音乐接口调用失败。")))
                raise ProviderError(message)
            return response.get("value")

    async def _ensure_ncm_bridge(self) -> None:
        if self._ncm_process and self._ncm_process.returncode is None:
            return
        if self._ncm_process and self._ncm_process.returncode is not None:
            with contextlib.suppress(Exception):
                await self._ncm_process.wait()
            self._ncm_process = None
        node, _ = self._node_tools()
        package_main = self._package_path("netease") / "main.js"
        bridge = Path(__file__).resolve().with_name("_providers_ncm_bridge.js")
        if not package_main.is_file() or not bridge.is_file():
            raise ProviderError("网易云音乐适配文件不完整，请重新准备平台依赖。")
        ncm_environment = os.environ.copy()
        ncm_environment.update(node_process_environment(self.data_dir, "netease-tmp"))
        self._ncm_process = await asyncio.create_subprocess_exec(
            str(node),
            str(bridge),
            str(package_main),
            cwd=str(package_main.parent),
            env=ncm_environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(0.1)
        if self._ncm_process.returncode is not None:
            message = await self._ncm_stderr()
            self._ncm_process = None
            raise ProviderError(f"网易云音乐Node适配进程启动失败。{message}")

    async def _ncm_stderr(self) -> str:
        if not self._ncm_process or not self._ncm_process.stderr:
            return ""
        try:
            data = await asyncio.wait_for(self._ncm_process.stderr.read(4096), timeout=0.1)
            return data.decode("utf-8", "replace")[-800:].strip()
        except Exception:
            return ""

    async def _stop_ncm_bridge(self) -> None:
        process = self._ncm_process
        self._ncm_process = None
        if process:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    async def _finish_session(self, session: _LoginSession, state: str, message: str) -> None:
        session.state = state
        session.message = message
        if state in {"success", "expired", "error"}:
            session.qr_image = None

    async def _expand_share_link(self, query: str, platform: str) -> str:
        parsed = urllib.parse.urlparse(str(query).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return query
        allowed = {
            "netease": ("163.com", "163cn.tv", "music.126.net"),
            "qq": ("qq.com",),
            "kugou": ("kugou.com",),
        }[platform]
        try:
            allowed_port = parsed.port in (None, 80, 443)
        except ValueError:
            allowed_port = False
        if not allowed_port or not _host_matches_domain(parsed.hostname, allowed):
            return query
        try:
            return await asyncio.to_thread(_expand_share_link_sync, query, allowed)
        except Exception:
            return query

    @staticmethod
    def _clean_error(exc: Exception) -> str:
        text = str(exc).strip()
        if not text:
            text = type(exc).__name__
        # Never expose upstream cookie/token values if an SDK includes them in an error.
        text = re.sub(r"(?i)(cookie|token|musickey|access_token|refresh_token)\s*[:=]\s*[^\s,;]+", r"\1=[已隐藏]", text)
        text = re.sub(r"(?i)(MUSIC_U|MUSIC_A|__csrf|csrf_token|sid|userId|userid)\s*[:=]\s*[^\s,;]+", r"\1=[已隐藏]", text)
        return text[:500]
