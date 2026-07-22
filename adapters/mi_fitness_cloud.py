"""Xiaomi Mi Fitness cloud adapter derived from Mi Fitness MCP's data layer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from .base import DataAdapter
from ..utils.privacy import redact_error

logger = logging.getLogger(__name__)
LOGIN_PREFIX = b"&&&START&&&"
KNOWN_REGIONS = ("cn", "ru", "de", "i2", "sg", "us")


class MiFitnessAuthenticationError(RuntimeError):
    """Authentication requires user action and must pause automatic synchronization."""


def _rc4_crypt(key: bytes, payload: bytes) -> bytes:
    """Apply Xiaomi's RC4-compatible stream cipher after its 1 KiB warm-up."""
    state = list(range(256))
    offset = 0
    for index in range(256):
        offset = (offset + state[index] + key[index % len(key)]) % 256
        state[index], state[offset] = state[offset], state[index]
    index = offset = 0

    def next_byte() -> int:
        nonlocal index, offset
        index = (index + 1) % 256
        offset = (offset + state[index]) % 256
        state[index], state[offset] = state[offset], state[index]
        return state[(state[index] + state[offset]) % 256]

    for _ in range(1024):
        next_byte()
    return bytes(value ^ next_byte() for value in payload)


def _nonce() -> bytes:
    """Build the 12-byte Xiaomi nonce used by the upstream protocol."""
    return os.urandom(8) + struct.pack(">I", int(datetime.now(UTC).timestamp() // 60))


def _signature(method: str, path: str, values: dict[str, str], signed_nonce: bytes) -> str:
    """Build the request signature used by Mi Fitness MCP.

    Args:
        method: HTTP method.
        path: API path excluding host.
        values: Form fields to sign.
        signed_nonce: SHA-256 ssecurity/nonce derivative.

    Returns:
        Base64 request signature.
    """
    text = f"{method}&{path}&data={values['data']}"
    if "rc4_hash__" in values:
        text += f"&rc4_hash__={values['rc4_hash__']}"
    text += "&" + base64.b64encode(signed_nonce).decode()
    return base64.b64encode(hashlib.sha1(text.encode()).digest()).decode()


class MiFitnessCloudAdapter(DataAdapter):
    """Authenticate with userId/passToken and safely fetch Mi Fitness cloud records."""

    def __init__(self, user_id: str, pass_token: str, region: str = ""):
        """Create an adapter without making a network request.

        Args:
            user_id: Xiaomi account userId.
            pass_token: Xiaomi account passToken.
            region: Optional known Mi Fitness region.
        """
        self.user_id = user_id
        self.pass_token = pass_token
        self.region = region.lower()
        self._client: httpx.AsyncClient | None = None
        self._cookies = ""
        self._ssecurity = b""
        self._connected = False
        self._available_types: list[str] = []
        self.last_error: str | None = None

    def get_available_data_types(self) -> list[str]:
        """Return discovered data types."""
        return self._available_types.copy()

    def is_connected(self) -> bool:
        """Return whether Xiaomi session setup completed."""
        return self._connected and self._client is not None

    async def connect(self) -> bool:
        """Log in and probe recent data without concealing the sanitized cause.

        Returns:
            True when authentication and connection setup succeed.
        """
        self.last_error = None
        if not self.user_id or not self.pass_token:
            self.last_error = "缺少 userId 或 passToken。"
            return False
        await self.close()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False)
        try:
            await self._login_with_token()
            self.region = await self._discover_region()
            self._available_types = await self._discover_data_types()
            self._connected = True
            return True
        except MiFitnessAuthenticationError as error:
            self.last_error = redact_error(error)
        except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as error:
            self.last_error = redact_error(error)
        await self.close()
        logger.warning("Mi Fitness connection failed: %s", self.last_error)
        return False

    async def _login_with_token(self) -> None:
        """Exchange the configured login cookies for ssecurity and health session cookies."""
        if not self._client:
            raise RuntimeError("HTTP client unavailable")
        response = await self._client.get(
            "https://account.xiaomi.com/pass/serviceLogin?_json=true&sid=miothealth",
            headers={"Cookie": f"userId={self.user_id}; passToken={self.pass_token}"},
        )
        response.raise_for_status()
        raw = response.content
        if not raw.startswith(LOGIN_PREFIX):
            raise MiFitnessAuthenticationError("小米登录响应无效；请重新获取 Cookie。")
        payload = json.loads(raw[len(LOGIN_PREFIX) :].decode())
        if not payload.get("ssecurity") or not payload.get("location"):
            raise MiFitnessAuthenticationError("凭证已失效、需要验证或账号受到风控；请在浏览器重新登录后更新 Cookie。")
        self.user_id = str(payload.get("userId") or self.user_id)
        self.pass_token = str(payload.get("passToken") or self.pass_token)
        self._ssecurity = base64.b64decode(payload["ssecurity"])
        redirected = await self._client.get(str(payload["location"]))
        redirected.raise_for_status()
        self._cookies = "; ".join(
            value.split(";", 1)[0] for value in redirected.headers.get_list("set-cookie")
        )
        if not self._cookies:
            raise MiFitnessAuthenticationError("未取得小米健康云会话；请重新登录后更新 Cookie。")

    async def _request(self, host: str, path: str, payload: dict[str, object]) -> dict:
        """Send one encrypted request with capped exponential retries."""
        if not self._client or not self._ssecurity:
            raise RuntimeError("Xiaomi session unavailable")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                nonce = _nonce()
                signed_nonce = hashlib.sha256(self._ssecurity + nonce).digest()
                form = {"data": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}
                form["rc4_hash__"] = _signature("POST", path, form, signed_nonce)
                encrypted = {
                    key: base64.b64encode(_rc4_crypt(signed_nonce, value.encode())).decode()
                    for key, value in form.items()
                }
                encrypted["signature"] = _signature("POST", path, encrypted, signed_nonce)
                encrypted["_nonce"] = base64.b64encode(nonce).decode()
                response = await self._client.post(
                    host + path,
                    headers={"Cookie": self._cookies, "Content-Type": "application/x-www-form-urlencoded"},
                    content=urlencode(encrypted),
                )
                response.raise_for_status()
                body = json.loads(_rc4_crypt(signed_nonce, base64.b64decode(response.text)))
                if body.get("code") != 0:
                    raise RuntimeError(str(body.get("message") or "Mi Fitness request failed"))
                return body.get("result") if isinstance(body.get("result"), dict) else {}
            except (httpx.HTTPError, ValueError, UnicodeDecodeError, RuntimeError) as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"小米健康云请求失败：{redact_error(last_error or 'unknown error')}")

    async def _fetch_key(self, key: str, start: datetime, end: datetime, region: str) -> list[dict]:
        """Fetch every paginated record for an upstream key in a bounded date range."""
        host = "https://hlth.io.mi.com" if region in ("", "cn") else f"https://{region}.hlth.io.mi.com"
        cursor: str | None = None
        records: list[dict] = []
        while True:
            payload: dict[str, object] = {
                "start_time": int(start.replace(tzinfo=UTC).timestamp()),
                "end_time": int(end.replace(tzinfo=UTC).timestamp()),
                "key": key,
            }
            if cursor:
                payload["next_key"] = cursor
            result = await self._request(host, "/app/v1/data/get_fitness_data_by_time", payload)
            data = result.get("data_list")
            if isinstance(data, list):
                records.extend(item for item in data if isinstance(item, dict))
            cursor = result.get("next_key") if isinstance(result.get("next_key"), str) else None
            if not result.get("has_more") or not cursor:
                return records

    async def _discover_region(self) -> str:
        """Probe up to the recent 30-day window instead of hard-coded historic dates."""
        now = datetime.now(UTC)
        candidates = ([self.region] if self.region in KNOWN_REGIONS else []) + [
            item for item in KNOWN_REGIONS if item != self.region
        ]
        for region in candidates:
            try:
                if await self._fetch_key("steps", now - timedelta(days=30), now, region):
                    return region
            except RuntimeError:
                continue
        return self.region or "cn"

    async def _discover_data_types(self) -> list[str]:
        """Discover only supported datasets, using a recent bounded probe."""
        now = datetime.now(UTC)
        types: list[str] = []
        for data_type, key in (("daily_activity", "steps"), ("heart_rate", "heart_rate"), ("body_measurements", "weight")):
            try:
                if await self._fetch_key(key, now - timedelta(days=30), now, self.region):
                    types.append(data_type)
            except RuntimeError:
                continue
        return types

    async def close(self) -> None:
        """Close the plugin-owned async HTTP client."""
        self._connected = False
        if self._client:
            await self._client.aclose()
            self._client = None
