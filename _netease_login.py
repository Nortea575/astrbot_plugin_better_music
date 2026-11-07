"""Native NetEase QR login using the mobile type=1 weapi endpoints."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import Cookie, CookieJar
from typing import Any, Awaitable, Callable


_BASE_URL = "https://music.163.com"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class NeteaseLoginError(RuntimeError):
    """A NetEase QR login request or response was invalid."""


class NeteaseQrLogin:
    """One QR login's encrypted request flow and isolated cookie jar."""

    def __init__(
        self,
        encrypt: Callable[[dict[str, Any]], Awaitable[dict[str, str]]],
        *,
        timeout: float = 15.0,
        base_url: str = _BASE_URL,
    ) -> None:
        self._encrypt = encrypt
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self._cookies = CookieJar()
        self._closed = False
        parsed = urllib.parse.urlparse(self._base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("NetEase login base URL must be HTTP or HTTPS.")
        for name, value in (("os", "pc"), ("appver", "8.9.70")):
            self._cookies.set_cookie(_cookie(
                name,
                value,
                parsed.hostname,
                secure=parsed.scheme == "https",
            ))
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._cookies))

    async def create_key(self) -> str:
        result = await self._post("/weapi/login/qrcode/unikey", {"type": 1, "csrf_token": ""})
        body = result.get("body", result)
        data = body.get("data", body) if isinstance(body, dict) else {}
        key = (data.get("unikey") or data.get("key")) if isinstance(data, dict) else None
        if not key:
            raise NeteaseLoginError("网易云音乐没有返回二维码登录Key。")
        return str(key)

    async def poll(self, key: str) -> dict[str, Any]:
        return await self._post(
            "/weapi/login/qrcode/client/login",
            {"key": key, "type": 1, "csrf_token": ""},
        )

    async def verify_account(self) -> dict[str, Any]:
        csrf_token = self._cookie_value("__csrf") or ""
        path = "/weapi/w/nuser/account/get"
        if csrf_token:
            path += "?csrf_token=" + urllib.parse.quote(csrf_token, safe="")
        return await self._post(path, {"csrf_token": csrf_token})

    def music_u(self) -> str | None:
        return self._cookie_value("MUSIC_U")

    def cookie_header(self) -> str:
        request = urllib.request.Request(self._base_url + "/")
        self._cookies.add_cookie_header(request)
        return request.get_header("Cookie") or ""

    def _cookie_value(self, name: str) -> str | None:
        for item in self.cookie_header().split(";"):
            cookie_name, separator, value = item.strip().partition("=")
            if separator and cookie_name == name and value:
                return value
        return None

    def close(self) -> None:
        # A canceled asyncio.to_thread request may still be finishing. Do not
        # mutate its CookieJar here; the worker owns it until that request ends.
        self._closed = True

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise NeteaseLoginError("网易云扫码登录会话已关闭。")
        encrypted = await self._encrypt(data)
        if self._closed:
            raise NeteaseLoginError("网易云扫码登录会话已关闭。")
        params = encrypted.get("params")
        enc_sec_key = encrypted.get("encSecKey")
        if not params or not enc_sec_key:
            raise NeteaseLoginError("网易云扫码登录请求加密失败。")
        form = urllib.parse.urlencode({"params": params, "encSecKey": enc_sec_key}).encode("ascii")
        result = await asyncio.to_thread(self._blocking_post, path, form)
        if self._closed:
            raise NeteaseLoginError("网易云扫码登录会话已关闭。")
        return result

    def _blocking_post(self, path: str, form: bytes) -> dict[str, Any]:
        request = urllib.request.Request(
            self._base_url + path,
            data=form,
            method="POST",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": _BASE_URL,
                "Referer": _BASE_URL + "/",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if not raw:
                raise NeteaseLoginError(f"网易云登录接口返回 HTTP {exc.code}。") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NeteaseLoginError(f"网易云登录接口请求失败：{exc}") from exc
        try:
            result = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NeteaseLoginError("网易云登录接口返回了无法解析的数据。") from exc
        if not isinstance(result, dict):
            raise NeteaseLoginError("网易云登录接口返回了无效的数据结构。")
        return result


def _cookie(name: str, value: str, domain: str, *, secure: bool) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=False,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=secure,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )
