"""Runtime environment checks and plugin-local tool installation."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .audio import generate_test_audio, probe_audio, transcode_and_segment
from .paths import data_subdirectory
from .templates import REPLY_ENABLED_DEFAULTS, REPLY_ENABLED_KEYS, validate_templates

_SETTINGS_NAME = "settings.json"
_TOOL_IDS = ("ffmpeg", "node", "netease", "qq", "kugou")
_MAX_ARCHIVE_BYTES = 600 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
DEFAULT_SONG_LOG = {
    "enabled": False,
    "platform_id": "",
    "target_type": "private",
    "target_id": "",
}
_SONG_LOG_KEYS = frozenset(DEFAULT_SONG_LOG)


def validate_song_log_settings(
    values: Any,
    *,
    base: dict[str, Any] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Deep-merge and validate the independent song-log delivery target."""
    current = dict(DEFAULT_SONG_LOG)
    if isinstance(base, dict):
        for key in _SONG_LOG_KEYS:
            value = base.get(key)
            if key == "enabled" and type(value) is bool:
                current[key] = value
            elif key == "target_type" and isinstance(value, str) and value in {"private", "group"}:
                current[key] = value
            elif key == "platform_id" and isinstance(value, str):
                current[key] = value.strip()
            elif key == "target_id" and isinstance(value, str):
                current[key] = value.strip()

    if not isinstance(values, dict):
        if strict:
            raise ValueError("song_log 必须是对象")
        return current
    unknown = set(values) - _SONG_LOG_KEYS
    if unknown and strict:
        raise ValueError(f"song_log 含未知设置字段：{', '.join(sorted(map(str, unknown)))}")

    for key, value in values.items():
        if key not in _SONG_LOG_KEYS:
            continue
        if key == "enabled":
            if type(value) is not bool:
                if strict:
                    raise ValueError("song_log.enabled 必须是布尔值")
                continue
            current[key] = value
        elif key == "target_type":
            if not isinstance(value, str) or value not in {"private", "group"}:
                if strict:
                    raise ValueError("song_log.target_type 必须是 private 或 group")
                continue
            current[key] = value
        elif key == "platform_id":
            if not isinstance(value, str):
                if strict:
                    raise ValueError("song_log.platform_id 必须是字符串")
                continue
            normalized = value.strip()
            if len(normalized) > 128 or any(char in normalized for char in ":!\x00"):
                if strict:
                    raise ValueError("song_log.platform_id 必须是不超过128字符的有效平台实例 ID")
                continue
            current[key] = normalized
        elif key == "target_id":
            if not isinstance(value, str):
                if strict:
                    raise ValueError("song_log.target_id 必须是字符串")
                continue
            normalized = value.strip()
            if len(normalized) > 128 or any(char in normalized for char in ":\x00"):
                if strict:
                    raise ValueError("song_log.target_id 必须是不超过128字符的有效目标 ID")
                continue
            current[key] = normalized

    if current["enabled"] and (
        not current["platform_id"]
        or not current["target_id"]
        or current["target_type"] not in {"private", "group"}
    ):
        if strict:
            raise ValueError("启用点歌日志时必须选择平台实例、私聊/群聊类型和目标 ID")
        return dict(DEFAULT_SONG_LOG)
    return current


def _is_supported_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except (TypeError, ValueError):
        return False


def _node_binary_name() -> str:
    return "node.exe" if os.name == "nt" else "node"


def _ffmpeg_binary_name(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def node_process_environment(data_dir: Path, temp_name: str = "node-tmp") -> dict[str, str]:
    """Keep Node/npm temporary files and npm's cache under plugin data."""
    temp_dir = data_subdirectory(data_dir, "runtime", temp_name)
    npm_cache = data_subdirectory(data_dir, "providers", ".npm-cache")
    return {
        "TMPDIR": str(temp_dir),
        "TMP": str(temp_dir),
        "TEMP": str(temp_dir),
        "npm_config_cache": str(npm_cache),
    }


def _node_cli_from_executable(node: Path) -> Path | None:
    parent = node.resolve().parent
    candidates = [
        parent / "npm" / "bin" / "npm-cli.js",
        parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
        parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
    ]
    npm_command = shutil.which("npm")
    if npm_command:
        npm_path = Path(npm_command)
        candidates.extend(
            [
                npm_path.resolve(),
                npm_path.resolve().parent / "npm-cli.js",
                npm_path.resolve().parent.parent / "npm" / "bin" / "npm-cli.js",
            ]
        )
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.suffix.lower() == ".js":
                return candidate.resolve()
        except OSError:
            continue
    return None


def get_node_tools(data_dir: Path) -> tuple[Path, Path]:
    """Return a Node 22+ executable and npm CLI JavaScript file.

    Plugin-local tools are preferred. The npm CLI is returned as a JS path so
    callers can start it through Node without relying on platform shell shims.
    """
    process_env = node_process_environment(data_dir)
    local_root = Path(data_dir).expanduser().absolute() / "tools" / "node"
    local_node = local_root / "bin" / _node_binary_name()
    local_npm = local_root / "npm" / "bin" / "npm-cli.js"
    if local_node.is_file() and local_npm.is_file():
        node, npm_cli = local_node, local_npm
    else:
        node_value = shutil.which("node")
        if not node_value:
            raise RuntimeError("未找到Node.js；请在插件环境设置中安装Node.js 22+")
        node = Path(node_value).resolve()
        npm_cli = _node_cli_from_executable(node)
        if npm_cli is None:
            raise RuntimeError("已找到Node.js，但未找到npm CLI；请安装插件自带Node.js工具")
    try:
        result = subprocess.run(
            [str(node), "--version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env={**os.environ, **process_env},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Node.js无法运行：{exc}") from exc
    match = re.search(r"v?(\d+)\.", result.stdout or result.stderr)
    if result.returncode != 0 or not match or int(match.group(1)) < 22:
        raise RuntimeError("需要Node.js 22或更高版本")
    return node, npm_cli


async def prepare_node(data_dir: Path, url: str = "") -> dict[str, Any]:
    """Make Node available for an explicit platform-dependency installation."""
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
        return await manager.install_tool("node", url)


class EnvironmentManager:
    def __init__(self, data_dir: Path, providers: Any = None):
        self.data_dir = Path(os.path.abspath(Path(data_dir).expanduser()))
        self.tools_dir = data_subdirectory(self.data_dir, "tools")
        self.providers = providers
        self.settings_path = self.data_dir / _SETTINGS_NAME
        self._audio_check_cache: dict[tuple[Any, ...], dict[str, str]] = {}
        self._audio_check_lock = asyncio.Lock()

    def settings(self) -> dict[str, Any]:
        defaults = {
            "tool_urls": {tool_id: "" for tool_id in _TOOL_IDS},
            "max_songs": 30,
            "active_llm": False,
            "passive_llm": False,
            "reply_templates": validate_templates({}),
            "reply_enabled": dict(REPLY_ENABLED_DEFAULTS),
            "passive_playback": {"max_seconds": 0, "gap_seconds": 0},
            "active_playback": {"voice_id": "", "max_seconds": 60, "gap_seconds": 0},
            "cover_service": {"url": "", "token": ""},
            "song_log": dict(DEFAULT_SONG_LOG),
        }
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults
        if not isinstance(data, dict):
            return defaults
        stored = data.get("tool_urls", {})
        if not isinstance(stored, dict):
            stored = {}
        for tool_id in _TOOL_IDS:
            value = stored.get(tool_id, "")
            if isinstance(value, str) and (not value or _is_supported_url(value)):
                defaults["tool_urls"][tool_id] = value
        max_songs = data.get("max_songs", 30)
        if isinstance(max_songs, int) and not isinstance(max_songs, bool) and max_songs > 0:
            defaults["max_songs"] = max_songs
        active_llm = data.get("active_llm", False)
        if type(active_llm) is bool:
            defaults["active_llm"] = active_llm
        passive_llm = data.get("passive_llm", False)
        if type(passive_llm) is bool:
            defaults["passive_llm"] = passive_llm
        stored_templates = data.get("reply_templates", {})
        if isinstance(stored_templates, dict):
            try:
                defaults["reply_templates"] = validate_templates(stored_templates)
            except (TypeError, ValueError):
                pass
        stored_enabled = data.get("reply_enabled", {})
        if isinstance(stored_enabled, dict):
            for key in REPLY_ENABLED_KEYS:
                value = stored_enabled.get(key)
                if type(value) is bool:
                    defaults["reply_enabled"][key] = value
        for field, keys in (
            ("passive_playback", ("max_seconds", "gap_seconds")),
            ("active_playback", ("voice_id", "max_seconds", "gap_seconds")),
        ):
            stored_values = data.get(field, {})
            if not isinstance(stored_values, dict):
                continue
            for key in keys:
                value = stored_values.get(key)
                try:
                    if key == "voice_id":
                        if isinstance(value, str) and len(value) <= 200:
                            defaults[field][key] = value.strip()
                    elif key == "max_seconds":
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            defaults[field][key] = value
                    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value >= 0:
                        defaults[field][key] = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
        stored_cover = data.get("cover_service", {})
        if isinstance(stored_cover, dict):
            url = stored_cover.get("url")
            if isinstance(url, str) and (not url or _is_supported_url(url)) and len(url) <= 2048:
                defaults["cover_service"]["url"] = url.rstrip("/")
            token = stored_cover.get("token")
            if isinstance(token, str) and len(token) <= 4096:
                defaults["cover_service"]["token"] = token
        defaults["song_log"] = validate_song_log_settings(
            data.get("song_log", {}), strict=False
        )
        return defaults

    @staticmethod
    def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
        """Return UI-safe settings without revealing the service credential."""
        public = dict(settings)
        cover = settings.get("cover_service")
        if isinstance(cover, dict):
            public["cover_service"] = {
                "url": str(cover.get("url") or ""),
                "has_token": bool(str(cover.get("token") or "")),
            }
        return public

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("设置必须是JSON对象")
        incoming = values.get("tool_urls", {})
        if "tool_urls" in values and not isinstance(incoming, dict):
            raise ValueError("tool_urls必须是地址映射")
        current = self.settings()
        if "max_songs" in values:
            incoming_limit = values["max_songs"]
            if not isinstance(incoming_limit, int) or isinstance(incoming_limit, bool) or incoming_limit <= 0:
                raise ValueError("歌曲保留数量必须是正整数")
            current["max_songs"] = incoming_limit
        if "active_llm" in values:
            active_llm = values["active_llm"]
            if type(active_llm) is not bool:
                raise ValueError("主动LLM设置必须是布尔值")
            current["active_llm"] = active_llm
        if "passive_llm" in values:
            passive_llm = values["passive_llm"]
            if type(passive_llm) is not bool:
                raise ValueError("被动LLM设置必须是布尔值")
            current["passive_llm"] = passive_llm
        if "reply_templates" in values:
            templates = values["reply_templates"]
            if not isinstance(templates, dict) or any(
                not isinstance(name, str) or not isinstance(template, str)
                for name, template in templates.items()
            ):
                raise ValueError("回复模板必须是字符串映射")
            merged_templates = {**current["reply_templates"], **templates}
            current["reply_templates"] = validate_templates(merged_templates)
        if "reply_enabled" in values:
            enabled = values["reply_enabled"]
            if not isinstance(enabled, dict):
                raise ValueError("回复提示开关必须是布尔值映射")
            unknown = set(enabled) - set(REPLY_ENABLED_KEYS)
            if unknown:
                raise ValueError(f"存在未知回复提示开关键：{', '.join(sorted(map(str, unknown)))}")
            if any(type(value) is not bool for value in enabled.values()):
                raise ValueError("回复提示开关必须是布尔值")
            current["reply_enabled"].update(enabled)
        if "song_log" in values:
            current["song_log"] = validate_song_log_settings(
                values["song_log"],
                base=current["song_log"],
                strict=True,
            )
        for field, keys in (
            ("passive_playback", {"max_seconds", "gap_seconds"}),
            ("active_playback", {"voice_id", "max_seconds", "gap_seconds"}),
            ("cover_service", {"url", "token"}),
        ):
            if field not in values:
                continue
            incoming_section = values[field]
            if not isinstance(incoming_section, dict):
                raise ValueError(f"{field}必须是对象")
            unknown = set(incoming_section) - keys
            if unknown:
                raise ValueError(f"{field}含未知设置字段：{', '.join(sorted(map(str, unknown)))}")
            current_section = dict(current[field])
            for key, value in incoming_section.items():
                if key == "max_seconds":
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        raise ValueError(f"{field}.max_seconds必须是非负整数")
                    current_section[key] = value
                elif key == "gap_seconds":
                    try:
                        numeric_value = float(value)
                    except (TypeError, ValueError, OverflowError):
                        raise ValueError(f"{field}.gap_seconds必须是有限非负数") from None
                    if (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(numeric_value)
                        or numeric_value < 0
                    ):
                        raise ValueError(f"{field}.gap_seconds必须是有限非负数")
                    current_section[key] = numeric_value
                elif key == "voice_id":
                    if not isinstance(value, str) or len(value.strip()) > 200:
                        raise ValueError("active_playback.voice_id必须是长度不超过200的字符串")
                    current_section[key] = value.strip()
                elif key == "url":
                    if not isinstance(value, str) or len(value.strip()) > 2048:
                        raise ValueError("cover_service.url必须是长度不超过2048的字符串")
                    normalized_url = value.strip().rstrip("/")
                    if normalized_url and not _is_supported_url(normalized_url):
                        raise ValueError("cover_service.url必须使用HTTP或HTTPS")
                    current_section[key] = normalized_url
                elif key == "token":
                    if not isinstance(value, str) or len(value) > 4096:
                        raise ValueError("cover_service.token必须是不超过4096字符的字符串")
                    current_section[key] = value
            current[field] = current_section
        if "tool_urls" in values:
            for tool_id, value in incoming.items():
                if tool_id not in _TOOL_IDS:
                    raise ValueError(f"未知的工具下载地址：{tool_id}")
                if not isinstance(value, str):
                    raise ValueError(f"{tool_id}下载地址必须是字符串")
                if not value.strip():
                    current["tool_urls"][tool_id] = ""
                elif not _is_supported_url(value.strip()):
                    raise ValueError(f"{tool_id}下载地址必须使用http或https")
                else:
                    current["tool_urls"][tool_id] = value.strip()
        temporary = self.settings_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.settings_path)
        return current

    def _default_ffmpeg_url(self) -> str:
        system = platform.system().lower()
        arch = platform.machine().lower()
        if arch not in {"x86_64", "amd64", "x64"}:
            return ""
        if system == "windows":
            return (
                "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                "ffmpeg-n9.0-latest-win64-lgpl-9.0.zip"
            )
        if system == "linux":
            return "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        return ""

    def _default_node_url(self) -> str:
        system = platform.system().lower()
        arch = platform.machine().lower()
        if arch not in {"x86_64", "amd64", "x64"}:
            return ""
        if system == "windows":
            return "https://nodejs.org/dist/v22.23.4/node-v22.23.4-win-x64.zip"
        if system == "linux":
            return "https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-x64.tar.xz"
        return ""

    async def _provider_probe(self) -> dict[str, Any]:
        method = getattr(self.providers, "probe", None)
        if not callable(method):
            return {"checks": []}
        try:
            result = await self._call_provider(method)
        except Exception as exc:
            return {
                "checks": [
                    {
                        "id": "providers",
                        "label": "音源协议",
                        "status": "error",
                        "message": f"协议检查失败：{exc}",
                    }
                ]
            }
        if isinstance(result, dict) and isinstance(result.get("checks"), list):
            return result
        if isinstance(result, list):
            return {"checks": result}
        return {
            "checks": [
                {
                    "id": "providers",
                    "label": "音源协议",
                    "status": "warning",
                    "message": "协议检查没有返回可识别的状态",
                }
            ]
        }

    async def _call_provider(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)
        result = await self._to_thread_safely(method, *args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _to_thread_safely(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    async def _tool_version(
        self, path: Path, argument: str, env: dict[str, str] | None = None
    ) -> tuple[bool, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                str(path),
                argument,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=env,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            text = output.decode("utf-8", "replace").strip()
            return process.returncode == 0, (text.splitlines() or [""])[0]
        except (OSError, asyncio.TimeoutError) as exc:
            return False, str(exc)

    def _ffmpeg_path(self, name: str) -> Path | None:
        local = self.tools_dir / "ffmpeg" / "bin" / _ffmpeg_binary_name(name)
        if local.is_file():
            return local
        system = shutil.which(name)
        return Path(system) if system else None

    async def check_environment(self) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
        system = platform.system()
        architecture = platform.machine()
        arch_supported = (
            architecture.lower() in {"x86_64", "amd64", "x64"}
            and system.lower() in {"windows", "linux"}
        )
        checks.append(
            {
                "id": "architecture",
                "label": "系统架构",
                "status": "ok" if arch_supported else "warning",
                "message": f"{system} {architecture}" + ("，可安装预置工具" if arch_supported and system.lower() in {"windows", "linux"} else "，当前没有匹配的预置工具"),
            }
        )
        checks.append(
            {
                "id": "python",
                "label": "Python",
                "status": "ok",
                "message": platform.python_version(),
            }
        )

        ffmpeg_path = self._ffmpeg_path("ffmpeg")
        ffprobe_path = self._ffmpeg_path("ffprobe")
        ffmpeg_ok, ffmpeg_version = await self._tool_version(ffmpeg_path, "-version") if ffmpeg_path else (False, "未找到")
        ffprobe_ok, ffprobe_version = await self._tool_version(ffprobe_path, "-version") if ffprobe_path else (False, "未找到")
        checks.append(
            {
                "id": "ffmpeg",
                "label": "FFmpeg",
                "status": "ok" if ffmpeg_ok else "warning",
                "message": ffmpeg_version if ffmpeg_ok else "未安装或无法运行，可从环境设置中补充",
            }
        )
        checks.append(
            {
                "id": "ffprobe",
                "label": "FFprobe",
                "status": "ok" if ffprobe_ok else "warning",
                "message": ffprobe_version if ffprobe_ok else "未安装或无法运行，可从环境设置中补充",
            }
        )
        if ffmpeg_ok and ffprobe_ok and ffmpeg_path and ffprobe_path:
            audio_result = await self._check_audio_pipeline(ffmpeg_path, ffprobe_path)
        else:
            audio_result = {
                "status": "warning",
                "message": "需要可运行的FFmpeg和FFprobe才能验证65秒音频分段",
            }
        checks.append(
            {
                "id": "audio_pipeline",
                "label": "音频解码与分段",
                "status": audio_result["status"],
                "message": audio_result["message"],
            }
        )

        try:
            node_path, npm_path = await asyncio.to_thread(get_node_tools, self.data_dir)
            node_env = {**os.environ, **node_process_environment(self.data_dir)}
            node_ok, node_version = await self._tool_version(node_path, "--version", node_env)
            npm_ok, npm_version = await self._tool_version_with_script(node_path, npm_path, node_env)
            node_ok = node_ok and npm_ok
            node_message = f"{node_version}; npm {npm_version}" if node_ok else "Node.js或npm无法运行"
        except RuntimeError as exc:
            node_ok = False
            node_message = str(exc)
        checks.append(
            {
                "id": "node",
                "label": "Node.js与npm",
                "status": "ok" if node_ok else "warning",
                "message": node_message,
            }
        )

        usage = shutil.disk_usage(self.data_dir)
        free_gb = usage.free / (1024**3)
        checks.append(
            {
                "id": "storage",
                "label": "数据目录空间",
                "status": "ok" if usage.free >= 1024**3 else "warning",
                "message": f"剩余{free_gb:.1f} GB",
            }
        )

        provider_result = await self._provider_probe()
        provider_checks: list[dict[str, str]] = []
        for raw in provider_result.get("checks", []):
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status", "warning")).lower()
            if status in {"pass", "passed", "success", "available", "ready"}:
                status = "ok"
            elif status in {"fail", "failed", "missing", "unavailable"}:
                status = "error"
            elif status not in {"ok", "warning", "error"}:
                status = "warning"
            provider_checks.append(
                {
                    "id": str(raw.get("id", "provider")),
                    "label": str(raw.get("label", raw.get("id", "音源协议"))),
                    "status": status,
                    "message": str(raw.get("message", "")),
                    "dependency_installed": bool(raw.get("dependency_installed", status == "ok")),
                }
            )
        checks.extend(provider_checks)

        provider_by_id = {item["id"]: item for item in provider_checks}
        dependency_urls = await self._dependency_sources()
        saved_urls = self.settings()["tool_urls"]
        tools = [
            {
                "id": "ffmpeg",
                "label": "FFmpeg / FFprobe",
                "available": ffmpeg_ok and ffprobe_ok,
                "url": self._default_ffmpeg_url(),
                "custom_url": saved_urls["ffmpeg"],
            },
            {
                "id": "node",
                "label": "Node.js与npm",
                "available": node_ok,
                "url": self._default_node_url(),
                "custom_url": saved_urls["node"],
            },
            {
                "id": "netease",
                "label": "网易云音乐依赖",
                "available": bool(provider_by_id.get("netease", {}).get("dependency_installed", provider_by_id.get("netease", {}).get("status") == "ok")),
                "url": dependency_urls.get("netease", ""),
                "custom_url": saved_urls["netease"],
            },
            {
                "id": "qq",
                "label": "QQ音乐Python依赖",
                "available": bool(provider_by_id.get("qq", {}).get("dependency_installed", provider_by_id.get("qq", {}).get("status") == "ok")),
                "url": "https://pypi.org/project/qqmusic-api-python/0.8.2/",
                "custom_url": saved_urls["qq"],
            },
            {
                "id": "kugou",
                "label": "酷狗音乐依赖",
                "available": bool(provider_by_id.get("kugou", {}).get("dependency_installed", provider_by_id.get("kugou", {}).get("status") == "ok")),
                "url": dependency_urls.get("kugou", ""),
                "custom_url": saved_urls["kugou"],
            },
        ]
        return {"checks": checks, "tools": tools}

    async def _check_audio_pipeline(self, ffmpeg: Path, ffprobe: Path) -> dict[str, str]:
        try:
            key = tuple(
                (str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns)
                for path in (ffmpeg, ffprobe)
            )
        except OSError as exc:
            return {"status": "warning", "message": f"无法读取工具文件：{exc}"}
        async with self._audio_check_lock:
            cached = self._audio_check_cache.get(key)
            if cached:
                return cached
            work = Path(tempfile.mkdtemp(prefix=".audio-env-check-", dir=self.data_dir))
            try:
                sample = work / "sample.wav"
                await generate_test_audio(ffmpeg, sample, duration=65)
                probed = await probe_audio(sample, ffprobe)
                result = await transcode_and_segment(sample, work, ffmpeg, ffprobe)
                max_segment = max(item["duration"] for item in result["segment_info"])
                if abs(result["duration"] - 65.0) > 0.1 or max_segment > 60 or len(result["segments"]) < 2:
                    outcome = {
                        "status": "error",
                        "message": "65秒音频样本未能按不超过60秒的分段完整转换",
                    }
                else:
                    outcome = {
                        "status": "ok",
                        "message": f"已生成并核验{probed['duration']:.1f}秒样本，切为{len(result['segments'])}段；采样帧连续无缺失",
                    }
                self._audio_check_cache[key] = outcome
                return outcome
            except Exception as exc:
                outcome = {"status": "error", "message": f"65秒音频样本验证失败：{exc}"}
                self._audio_check_cache[key] = outcome
                return outcome
            finally:
                shutil.rmtree(work, ignore_errors=True)

    async def _tool_version_with_script(
        self, node: Path, npm_cli: Path, env: dict[str, str] | None = None
    ) -> tuple[bool, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                str(node),
                str(npm_cli),
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=env,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            text = output.decode("utf-8", "replace").strip()
            return process.returncode == 0, (text.splitlines() or [""])[0]
        except (OSError, asyncio.TimeoutError) as exc:
            return False, str(exc)

    async def _dependency_sources(self) -> dict[str, str]:
        method = getattr(self.providers, "dependency_tools", None)
        if not callable(method):
            return {}
        try:
            value = await self._call_provider(method)
        except Exception:
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            tool_id: str(value[tool_id].get("url", ""))
            for tool_id in ("netease", "kugou")
            if isinstance(value.get(tool_id), dict)
        }

    async def install_tool(self, tool_id: str, url: str = "") -> dict[str, Any]:
        if tool_id not in _TOOL_IDS:
            raise ValueError("不支持的工具编号")
        chosen_url = url.strip() if isinstance(url, str) else ""
        if not chosen_url:
            chosen_url = self.settings()["tool_urls"].get(tool_id, "")
        if not chosen_url:
            if tool_id == "ffmpeg":
                chosen_url = self._default_ffmpeg_url()
            elif tool_id == "node":
                chosen_url = self._default_node_url()
        if tool_id == "qq":
            return await self._install_qq(chosen_url)
        if tool_id in {"netease", "kugou"}:
            method = getattr(self.providers, "prepare_dependencies", None)
            if not callable(method):
                return {"tool_id": tool_id, "installed": False, "path": "", "version": "", "message": "音源依赖安装入口不可用"}
            try:
                result = await self._call_provider(method, tool_id, url=chosen_url)
                if isinstance(result, dict):
                    return {
                        "tool_id": tool_id,
                        "installed": bool(result.get("installed", result.get("ok", False))),
                        "path": str(result.get("path", "")),
                        "version": str(result.get("version", "")),
                        "message": str(result.get("message", "")),
                    }
                return {"tool_id": tool_id, "installed": bool(result), "path": "", "version": "", "message": str(result)}
            except Exception as exc:
                return {"tool_id": tool_id, "installed": False, "path": "", "version": "", "message": str(exc)}
        if not chosen_url:
            return {
                "tool_id": tool_id,
                "installed": False,
                "path": "",
                "version": "",
                "message": "当前系统架构没有预置工具地址，请配置可用的自定义压缩包地址",
            }
        if not _is_supported_url(chosen_url):
            raise ValueError("下载地址必须使用http或https")

        tool_root = self.tools_dir / tool_id
        temp_root = Path(tempfile.mkdtemp(prefix=f".{tool_id}-", dir=self.tools_dir))
        stage = temp_root / "package"
        archive = temp_root / "download.archive"
        try:
            await self._to_thread_safely(self._download_archive, chosen_url, archive)
            await self._to_thread_safely(self._extract_tool_archive, tool_id, archive, stage)
            if tool_id == "ffmpeg":
                ffmpeg = stage / "bin" / _ffmpeg_binary_name("ffmpeg")
                ffprobe = stage / "bin" / _ffmpeg_binary_name("ffprobe")
                ffmpeg_ok, ffmpeg_version = await self._tool_version(ffmpeg, "-version")
                ffprobe_ok, ffprobe_version = await self._tool_version(ffprobe, "-version")
                if not ffmpeg_ok or not ffprobe_ok:
                    raise RuntimeError("压缩包中的ffmpeg/ffprobe无法运行")
                version = f"{ffmpeg_version}; {ffprobe_version}"
                installed_path = ffmpeg
            else:
                node, npm_cli = stage / "bin" / _node_binary_name(), stage / "npm" / "bin" / "npm-cli.js"
                node_env = {**os.environ, **node_process_environment(self.data_dir)}
                node_ok, node_version = await self._tool_version(node, "--version", node_env)
                node_major = re.search(r"v?(\d+)\.", node_version)
                if not node_ok or not node_major or int(node_major.group(1)) < 22:
                    raise RuntimeError("工具压缩包未提供可运行的Node.js 22+")
                npm_ok, npm_version = await self._tool_version_with_script(node, npm_cli, node_env)
                if not npm_ok:
                    raise RuntimeError("工具压缩包中的npm无法运行")
                version = f"{node_version}; npm {npm_version}"
                installed_path = node
            self._replace_tool_directory(tool_root, stage, temp_root)
            if tool_id == "node":
                installed_path = self.tools_dir / "node" / "bin" / _node_binary_name()
            else:
                installed_path = self.tools_dir / "ffmpeg" / "bin" / _ffmpeg_binary_name("ffmpeg")
            return {
                "tool_id": tool_id,
                "installed": True,
                "path": str(installed_path),
                "version": version,
                "message": "已安装到插件数据目录并通过运行校验",
            }
        except Exception as exc:
            return {"tool_id": tool_id, "installed": False, "path": "", "version": "", "message": str(exc)}
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    @staticmethod
    def _qq_pip_command(custom_url: str = "") -> list[str]:
        sdk_spec = custom_url.strip() if custom_url else "qqmusic-api-python==0.8.2"
        return [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            sdk_spec,
            "qrcode[pil]==8.2",
        ]

    def _qq_imports_sync(self) -> None:
        provider_check = getattr(self.providers, "_qq_imports", None)
        if callable(provider_check):
            provider_check()
            return
        importlib.import_module("qrcode")
        importlib.import_module("qqmusic_api")
        importlib.import_module("qqmusic_api.models.login")
        importlib.import_module("qqmusic_api.modules.login_utils")
        importlib.import_module("qqmusic_api.modules.song")

    async def _qq_imports_available(self) -> bool:
        try:
            await asyncio.to_thread(self._qq_imports_sync)
            return True
        except Exception:
            return False

    async def _run_pip_install(self, command: list[str]) -> tuple[bool, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return False, str(exc)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=600)
        except asyncio.CancelledError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            return False, "pip安装超过10分钟，已停止"
        return process.returncode == 0, output.decode("utf-8", "replace")[-1600:].strip()

    async def _install_qq(self, custom_url: str) -> dict[str, Any]:
        if custom_url and not _is_supported_url(custom_url):
            raise ValueError("QQ音乐SDK安装地址必须使用HTTP或HTTPS")
        if await self._qq_imports_available():
            return {
                "tool_id": "qq",
                "installed": True,
                "path": sys.prefix,
                "version": "qqmusic-api-python 0.8.2; qrcode[pil] 8.2",
                "message": "QQ音乐依赖已就绪，无需重复安装。",
            }

        command = self._qq_pip_command(custom_url)
        installed, output = await self._run_pip_install(command)
        if not installed:
            lower_output = output.lower()
            if any(
                marker in lower_output
                for marker in ("permission denied", "permissionerror", "access is denied", "winerror 5", "[errno 13]", "not permitted", "权限不足")
            ):
                message = "当前AstrBot Python环境没有安装权限，未尝试提权；请由该环境所有者安装插件requirements.txt中的依赖。"
            else:
                safe_output = output.replace(custom_url, "[自定义安装地址]") if custom_url else output
                message = f"QQ音乐依赖安装失败：{safe_output or 'pip返回失败状态'}"
            return {"tool_id": "qq", "installed": False, "path": "", "version": "", "message": message}
        if not await self._qq_imports_available():
            return {
                "tool_id": "qq",
                "installed": False,
                "path": "",
                "version": "",
                "message": "pip已结束，但QQ音乐依赖当前仍无法导入；请重启AstrBot后重新检测。",
            }
        return {
            "tool_id": "qq",
            "installed": True,
            "path": sys.prefix,
            "version": "qqmusic-api-python 0.8.2; qrcode[pil] 8.2",
            "message": "QQ音乐依赖安装完成；如登录功能仍未识别，请重启AstrBot。",
        }

    def _replace_tool_directory(self, target: Path, stage: Path, temp_root: Path) -> None:
        backup = temp_root / "previous"
        moved_old = False
        if target.exists():
            if target.is_symlink() or not target.resolve().is_relative_to(self.tools_dir.resolve()):
                raise RuntimeError("插件工具目录路径异常，拒绝覆盖")
            os.replace(target, backup)
            moved_old = True
        try:
            os.replace(stage, target)
        except Exception:
            if moved_old and backup.exists():
                os.replace(backup, target)
            raise
        if moved_old:
            shutil.rmtree(backup, ignore_errors=True)

    def _download_archive(self, url: str, destination: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "BetterMusic/0.2.0"})
        with urllib.request.urlopen(request, timeout=45) as response, destination.open("wb") as output:
            final_url = response.geturl()
            if not _is_supported_url(final_url):
                raise RuntimeError("下载跳转到了不支持的地址")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > _MAX_ARCHIVE_BYTES:
                raise RuntimeError("工具压缩包超过大小限制")
            total = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > _MAX_ARCHIVE_BYTES:
                    raise RuntimeError("工具压缩包超过大小限制")
                output.write(block)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise RuntimeError("工具压缩包下载为空")

    @staticmethod
    def _safe_archive_name(name: str) -> PurePosixPath:
        normalized = name.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or re.match(r"^[A-Za-z]:", normalized) or ".." in path.parts:
            raise RuntimeError("压缩包含有越界路径，已拒绝解压")
        return path

    def _extract_tool_archive(self, tool_id: str, archive_path: Path, stage: Path) -> None:
        stage.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(archive_path):
            self._extract_zip(tool_id, archive_path, stage)
        else:
            try:
                with tarfile.open(archive_path, mode="r:*") as archive:
                    self._extract_tar(tool_id, archive, stage)
            except (tarfile.TarError, OSError) as exc:
                raise RuntimeError("只支持zip、tar.gz或tar.xz压缩包") from exc
        if tool_id == "ffmpeg":
            expected = [stage / "bin" / _ffmpeg_binary_name(name) for name in ("ffmpeg", "ffprobe")]
        else:
            expected = [stage / "bin" / _node_binary_name(), stage / "npm" / "bin" / "npm-cli.js"]
        if not all(path.is_file() for path in expected):
            raise RuntimeError("压缩包没有包含当前系统所需的运行文件")
        if os.name != "nt":
            for path in expected:
                if path.suffix != ".js":
                    path.chmod(path.stat().st_mode | 0o755)

    def _extract_zip(self, tool_id: str, archive_path: Path, stage: Path) -> None:
        total = 0
        selected: set[str] = set()
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                member = self._safe_archive_name(info.filename)
                if info.is_dir():
                    continue
                mode = info.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise RuntimeError("压缩包含符号链接，已拒绝解压")
                if tool_id == "ffmpeg":
                    base = member.name.lower()
                    if base in {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}:
                        target_name = base
                        target = stage / "bin" / target_name
                    else:
                        continue
                else:
                    parts = tuple(part.lower() for part in member.parts)
                    if member.name.lower() in {"node", "node.exe"}:
                        target = stage / "bin" / member.name.lower()
                    elif "node_modules" in parts and "npm" in parts:
                        try:
                            index = parts.index("node_modules")
                            npm_index = parts.index("npm", index + 1)
                        except ValueError:
                            continue
                        if npm_index != index + 1:
                            continue
                        suffix = member.parts[npm_index + 1 :]
                        if not suffix or not self._is_npm_runtime_file(suffix[-1]):
                            continue
                        target = stage / "npm" / Path(*suffix)
                    else:
                        continue
                    target_name = str(target.relative_to(stage)).lower()
                if target_name in selected:
                    raise RuntimeError("压缩包内发现重复的关键工具文件")
                selected.add(target_name)
                total += info.file_size
                if total > _MAX_EXTRACTED_BYTES:
                    raise RuntimeError("压缩包解压后超过大小限制")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)

    def _extract_tar(self, tool_id: str, archive: tarfile.TarFile, stage: Path) -> None:
        total = 0
        selected: set[str] = set()
        for member in archive.getmembers():
            path = self._safe_archive_name(member.name)
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                # Official Node archives include bin/npm and bin/npx links;
                # neither is needed because callers run npm-cli.js via Node.
                continue
            if not member.isfile():
                raise RuntimeError("压缩包含链接或特殊文件，已拒绝解压")
            if tool_id == "ffmpeg":
                base = path.name.lower()
                if base in {"ffmpeg", "ffprobe"}:
                    target = stage / "bin" / base
                else:
                    continue
            else:
                parts = tuple(part.lower() for part in path.parts)
                if path.name.lower() == "node":
                    target = stage / "bin" / "node"
                elif "node_modules" in parts and "npm" in parts:
                    try:
                        index = parts.index("node_modules")
                        npm_index = parts.index("npm", index + 1)
                    except ValueError:
                        continue
                    if npm_index != index + 1:
                        continue
                    suffix = path.parts[npm_index + 1 :]
                    if not suffix or not self._is_npm_runtime_file(suffix[-1]):
                        continue
                    target = stage / "npm" / Path(*suffix)
                else:
                    continue
            target_name = str(target.relative_to(stage)).lower()
            if target_name in selected:
                raise RuntimeError("压缩包内发现重复的关键工具文件")
            selected.add(target_name)
            total += member.size
            if total > _MAX_EXTRACTED_BYTES:
                raise RuntimeError("压缩包解压后超过大小限制")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("无法读取压缩包成员")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)

    @staticmethod
    def _is_npm_runtime_file(filename: str) -> bool:
        return Path(filename).suffix.lower() in {".js", ".cjs", ".mjs", ".json", ".node"}
