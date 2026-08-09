"""Xiaomi Mi Fitness cloud adapter derived from Mi Fitness MCP's data layer."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import os
import struct
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, tzinfo
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlparse

import httpx
from astrbot.api import logger

from ..models import (
    BodyMeasurement,
    DailyActivity,
    HeartRateSample,
    SleepSession,
    SpO2Sample,
    StressSample,
)
from ..utils.privacy import redact_error
from .base import DataAdapter

LOGIN_PREFIX = b"&&&START&&&"
ALLOWED_REDIRECT_HOSTS = (
    "account.xiaomi.com",
    "io.mi.com",
    "hlth.io.mi.com",
    "api.io.mi.com",
)
KNOWN_REGIONS = ("cn", "ru", "de", "i2", "sg", "us")
PRIMARY_HEART_RATE_KEYS = ("heart_rate", "heartrate", "hr")
RESTING_HEART_RATE_KEYS = ("resting_heart_rate",)
SPO2_KEYS = ("spo2", "blood_oxygen")
REGION_PROBE_KEYS = (
    "steps",
    "sleep",
    "heart_rate",
    "heartrate",
    "hr",
    "spo2",
    "blood_oxygen",
    "weight",
    "stress",
)
CONNECT_TIMEOUT_SECONDS = 90
MAX_RECORDS_PER_KEY = 100_000
LOGIN_RESPONSE_MAX_BYTES = 1 * 1024 * 1024
API_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
MAX_DATASET_BYTES = 64 * 1024 * 1024
MAX_LOGIN_FIELD_LENGTH = 8192
MAX_SESSION_COOKIE_BYTES = 32 * 1024
SESSION_COOKIE_NAMES = frozenset(
    {"serviceToken", "yetAnotherServiceToken", "cUserId", "userId"}
)
MAX_RATE_LIMIT_DELAY_SECONDS = 15 * 60
DEFAULT_RATE_LIMIT_DELAY_SECONDS = 60
DIAGNOSTIC_TIMEOUT_SECONDS = 120


class MiFitnessAuthenticationError(RuntimeError):
    """Authentication requires user action and must pause automatic synchronization."""


class MiFitnessResponseError(RuntimeError):
    """A non-transient remote response that must not be retried."""


class MiFitnessRateLimitError(RuntimeError):
    """Xiaomi asked the client to stop sending requests until a shared deadline."""

    def __init__(self, retry_after_seconds: float):
        """Build a credential-safe error carrying the server-directed cooldown."""
        self.retry_after_seconds = max(1.0, float(retry_after_seconds))
        remaining = max(1, int(self.retry_after_seconds + 0.999))
        super().__init__(f"小米健康云请求过于频繁；请约 {remaining} 秒后再尝试同步。")


class MiFitnessBudgetError(MiFitnessResponseError):
    """One bounded cloud operation exhausted its byte or record allowance."""


class _OperationBudget:
    """Share response-byte and record limits across pages and alias keys."""

    __slots__ = ("bytes_used", "max_bytes", "max_records", "records_seen")

    def __init__(
        self,
        *,
        max_bytes: int = MAX_DATASET_BYTES,
        max_records: int = MAX_RECORDS_PER_KEY,
    ):
        self.max_bytes = max(1, int(max_bytes))
        self.max_records = max(1, int(max_records))
        self.bytes_used = 0
        self.records_seen = 0

    def remaining_bytes(self) -> int:
        """Return the response allowance left before another HTTP read starts."""
        if self.records_seen >= self.max_records:
            raise MiFitnessBudgetError("小米健康云数据超过单次操作记录安全上限。")
        remaining = self.max_bytes - self.bytes_used
        if remaining <= 0:
            raise MiFitnessBudgetError("小米健康云数据超过单次操作字节安全上限。")
        return remaining

    def consume_response(self, payload: bytes) -> None:
        """Count the full encrypted response, including unused result fields."""
        self.bytes_used += len(payload)
        if self.bytes_used > self.max_bytes:
            raise MiFitnessBudgetError("小米健康云数据超过单次操作字节安全上限。")

    def consume_record(self) -> None:
        """Count every dictionary record before normalization or deduplication."""
        self.records_seen += 1
        if self.records_seen > self.max_records:
            raise MiFitnessBudgetError("小米健康云数据超过单次操作记录安全上限。")


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


def _signature(
    method: str, path: str, values: dict[str, str], signed_nonce: bytes
) -> str:
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

    def __init__(
        self,
        user_id: str,
        pass_token: str,
        region: str = "",
        user_timezone: tzinfo = UTC,
    ):
        """Create an adapter without making a network request.

        Args:
            user_id: Xiaomi account userId.
            pass_token: Xiaomi account passToken.
            region: Optional known Mi Fitness region.
            user_timezone: Calendar timezone used to group daily activity.
        """
        self.user_id = user_id
        self.pass_token = pass_token
        self.region = region.lower()
        self.user_timezone = user_timezone
        self._client: httpx.AsyncClient | None = None
        self._cookies = ""
        self._ssecurity = b""
        self._connected = False
        self._available_types: list[str] = []
        self.last_error: str | None = None
        self.authentication_failed = False
        self._connect_lock = asyncio.Lock()
        self._next_allowed_request_at = 0.0

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
        async with self._connect_lock:
            if self.is_connected():
                return True
            self.last_error = None
            self.authentication_failed = False
            self._available_types = []
            if not self.user_id or not self.pass_token:
                self.last_error = "缺少 userId 或 passToken。"
                return False
            await self.close()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0), follow_redirects=False
            )
            try:
                await asyncio.wait_for(
                    self._establish_session(), timeout=CONNECT_TIMEOUT_SECONDS
                )
                self._connected = True
                return True
            except asyncio.CancelledError:
                await self.close()
                raise
            except MiFitnessRateLimitError:
                await self.close()
                raise
            except MiFitnessAuthenticationError as error:
                self.authentication_failed = True
                self.last_error = redact_error(error)
            except asyncio.TimeoutError:
                self.last_error = "连接小米健康云超时，请稍后重试或手动指定区域。"
            except Exception as error:
                self.last_error = redact_error(error)
            await self.close()
            logger.warning("Mi Fitness connection failed: %s", self.last_error)
            return False

    async def _establish_session(self) -> None:
        """Authenticate and complete bounded region/type discovery."""
        candidate_user_id, candidate_pass_token = await self._login_with_token()
        discovery_budget = _OperationBudget()
        if self.region not in KNOWN_REGIONS:
            self.region = await self._discover_region(discovery_budget)
        self._available_types = await self._discover_data_types(discovery_budget)
        # The login response is not trusted to replace configured credentials
        # until the resulting session has completed at least one health API
        # request.  This also keeps a failed/risk-control response from
        # poisoning later connection attempts in the same plugin process.
        self.user_id = candidate_user_id
        self.pass_token = candidate_pass_token

    async def _limited_request(
        self,
        method: str,
        url: str,
        *,
        max_bytes: int,
        read_body: bool = True,
        headers: dict[str, str] | None = None,
        content: str | bytes | None = None,
    ) -> tuple[httpx.Response, bytes]:
        """Stream one response and reject oversized decoded bodies before buffering."""
        if not self._client:
            raise RuntimeError("HTTP client unavailable")
        chunks: list[bytes] = []
        total = 0
        async with self._client.stream(
            method,
            url,
            headers=headers,
            content=content,
        ) as response:
            if not read_body or response.status_code >= 400:
                return response, b""
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise MiFitnessBudgetError("小米云响应超过单次读取安全上限。")
                chunks.append(chunk)
            return response, b"".join(chunks)

    def rate_limit_remaining(self) -> float:
        """Return the shared Xiaomi cooldown remaining in the current event loop."""
        if self._next_allowed_request_at <= 0:
            return 0.0
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return 0.0
        remaining = self._next_allowed_request_at - now
        if remaining <= 0:
            self._next_allowed_request_at = 0.0
            return 0.0
        return remaining

    def _raise_if_rate_limited(self) -> None:
        """Fail fast so aliases and datasets cannot add requests during a cooldown."""
        remaining = self.rate_limit_remaining()
        if remaining > 0:
            raise MiFitnessRateLimitError(remaining)

    def _set_rate_limit(self, delay: float) -> None:
        """Share the longest server-requested cooldown across all cloud operations."""
        loop = asyncio.get_running_loop()
        self._next_allowed_request_at = max(
            self._next_allowed_request_at,
            loop.time() + max(1.0, delay),
        )

    async def _login_with_token(self) -> tuple[str, str]:
        """Exchange configured login cookies and return uncommitted credentials."""
        if not self._client:
            raise RuntimeError("HTTP client unavailable")
        self._raise_if_rate_limited()
        response, raw = await self._limited_request(
            "GET",
            "https://account.xiaomi.com/pass/serviceLogin?_json=true&sid=miothealth",
            max_bytes=LOGIN_RESPONSE_MAX_BYTES,
            headers={"Cookie": f"userId={self.user_id}; passToken={self.pass_token}"},
        )
        if response.status_code == 429:
            delay = self._retry_after_delay(response.headers.get("Retry-After", ""), 0)
            self._set_rate_limit(delay)
            raise MiFitnessRateLimitError(delay)
        if response.status_code in (401, 403):
            raise MiFitnessAuthenticationError(
                "小米登录授权已失效；请重新获取 Cookie。"
            )
        response.raise_for_status()
        if not raw.startswith(LOGIN_PREFIX):
            raise MiFitnessAuthenticationError("小米登录响应无效；请重新获取 Cookie。")
        payload = json.loads(raw[len(LOGIN_PREFIX) :].decode())
        if not isinstance(payload, dict):
            raise MiFitnessAuthenticationError(
                "小米登录响应格式无效；请重新获取 Cookie。"
            )
        raw_ssecurity = payload.get("ssecurity")
        raw_location = payload.get("location")
        if (
            not isinstance(raw_ssecurity, str)
            or not 1 <= len(raw_ssecurity) <= MAX_LOGIN_FIELD_LENGTH
            or not isinstance(raw_location, str)
            or not 1 <= len(raw_location) <= MAX_LOGIN_FIELD_LENGTH
        ):
            raise MiFitnessAuthenticationError(
                "凭证已失效、需要验证或账号受到风控；请在浏览器重新登录后更新 Cookie。"
            )
        try:
            candidate_ssecurity = base64.b64decode(raw_ssecurity, validate=True)
        except (binascii.Error, ValueError) as error:
            raise MiFitnessAuthenticationError(
                "小米登录安全参数无效；请重新获取 Cookie。"
            ) from error
        if not 16 <= len(candidate_ssecurity) <= 64:
            raise MiFitnessAuthenticationError(
                "小米登录安全参数长度无效；请重新获取 Cookie。"
            )
        candidate_user_id = self.user_id
        returned_user_id = payload.get("userId")
        if returned_user_id not in (None, ""):
            if isinstance(returned_user_id, bool) or not isinstance(
                returned_user_id, (str, int)
            ):
                raise MiFitnessAuthenticationError(
                    "小米登录账号标识格式无效；请重新获取 Cookie。"
                )
            candidate_user_id = str(returned_user_id).strip()
            if not 1 <= len(candidate_user_id) <= 1024:
                raise MiFitnessAuthenticationError(
                    "小米登录账号标识长度无效；请重新获取 Cookie。"
                )
            if candidate_user_id != self.user_id:
                raise MiFitnessAuthenticationError(
                    "小米登录返回的账号与配置 userId 不一致；请重新获取同一账号的 Cookie。"
                )
        candidate_pass_token = self.pass_token
        returned_pass_token = payload.get("passToken")
        if returned_pass_token not in (None, ""):
            if not isinstance(returned_pass_token, str) or not (
                1 <= len(returned_pass_token) <= MAX_LOGIN_FIELD_LENGTH
            ):
                raise MiFitnessAuthenticationError(
                    "小米登录凭证格式无效；请重新获取 Cookie。"
                )
            candidate_pass_token = returned_pass_token
        location = raw_location
        parsed_location = urlparse(location)
        host = parsed_location.hostname or ""
        try:
            port = parsed_location.port
        except ValueError:
            port = -1
        is_allowed_host = (
            parsed_location.scheme == "https"
            and port in (None, 443)
            and parsed_location.username is None
            and parsed_location.password is None
            and host.endswith(ALLOWED_REDIRECT_HOSTS)
            and any(
                host == allowed or host.endswith(f".{allowed}")
                for allowed in ALLOWED_REDIRECT_HOSTS
            )
        )
        if not is_allowed_host:
            raise MiFitnessAuthenticationError(
                "小米登录重定向必须使用 HTTPS 且目标位于受信任域内；请重新获取 Cookie。"
            )
        redirected, _ = await self._limited_request(
            "GET",
            location,
            max_bytes=0,
            read_body=False,
        )
        if redirected.status_code == 429:
            delay = self._retry_after_delay(
                redirected.headers.get("Retry-After", ""), 0
            )
            self._set_rate_limit(delay)
            raise MiFitnessRateLimitError(delay)
        if redirected.status_code in (401, 403):
            raise MiFitnessAuthenticationError(
                "小米健康云会话授权失败；请重新获取 Cookie。"
            )
        redirected.raise_for_status()
        session_cookies: dict[str, str] = {}
        for header in redirected.headers.get_list("set-cookie"):
            pair = header.split(";", 1)[0]
            name, separator, cookie_value = pair.partition("=")
            name = name.strip()
            if separator and name in SESSION_COOKIE_NAMES and cookie_value:
                session_cookies[name] = cookie_value
        candidate_cookies = "; ".join(
            f"{name}={value}" for name, value in session_cookies.items()
        )
        if not candidate_cookies:
            raise MiFitnessAuthenticationError(
                "未取得小米健康云会话；请重新登录后更新 Cookie。"
            )
        if len(candidate_cookies.encode("utf-8")) > MAX_SESSION_COOKIE_BYTES:
            raise MiFitnessAuthenticationError(
                "小米健康云会话 Cookie 超过安全上限；请重新登录后重试。"
            )
        self._ssecurity = candidate_ssecurity
        self._cookies = candidate_cookies
        return candidate_user_id, candidate_pass_token

    async def _request(
        self,
        host: str,
        path: str,
        payload: dict[str, object],
        *,
        budget: _OperationBudget | None = None,
    ) -> dict:
        """Send one encrypted request with capped exponential retries."""
        if not self._client or not self._ssecurity:
            raise RuntimeError("Xiaomi session unavailable")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                self._raise_if_rate_limited()
                nonce = _nonce()
                signed_nonce = hashlib.sha256(self._ssecurity + nonce).digest()
                form = {
                    "data": json.dumps(
                        payload, separators=(",", ":"), ensure_ascii=False
                    )
                }
                form["rc4_hash__"] = _signature("POST", path, form, signed_nonce)
                encrypted = {
                    key: base64.b64encode(
                        _rc4_crypt(signed_nonce, value.encode())
                    ).decode()
                    for key, value in form.items()
                }
                encrypted["signature"] = _signature(
                    "POST", path, encrypted, signed_nonce
                )
                encrypted["_nonce"] = base64.b64encode(nonce).decode()
                max_response_bytes = API_RESPONSE_MAX_BYTES
                if budget is not None:
                    max_response_bytes = min(
                        max_response_bytes, budget.remaining_bytes()
                    )
                response, encrypted_body = await self._limited_request(
                    "POST",
                    host + path,
                    max_bytes=max_response_bytes,
                    headers={
                        "Cookie": self._cookies,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    content=urlencode(encrypted),
                )
                if budget is not None:
                    budget.consume_response(encrypted_body)
                if response.status_code in (401, 403):
                    self._connected = False
                    self.authentication_failed = True
                    raise MiFitnessAuthenticationError(
                        "小米健康云授权已失效；请重新获取 Cookie。"
                    )
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    delay = self._retry_after_delay(retry_after, attempt)
                    self._set_rate_limit(delay)
                    raise MiFitnessRateLimitError(delay)
                if response.status_code >= 400 and response.status_code not in {
                    408,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    raise MiFitnessResponseError(
                        f"小米健康云返回 HTTP {response.status_code}"
                    )
                response.raise_for_status()
                try:
                    decoded_body = base64.b64decode(
                        encrypted_body.strip(), validate=True
                    )
                except (binascii.Error, ValueError) as error:
                    raise MiFitnessResponseError("小米健康云响应编码无效。") from error
                body = json.loads(_rc4_crypt(signed_nonce, decoded_body))
                if not isinstance(body, dict):
                    raise MiFitnessResponseError("小米健康云响应格式无效。")
                code_value = body.get("code")
                if isinstance(code_value, bool) or not isinstance(
                    code_value, (int, str)
                ):
                    raise MiFitnessResponseError("小米健康云响应状态格式无效。")
                if str(code_value) != "0":
                    message = str(body.get("message") or "Mi Fitness request failed")
                    if any(
                        marker in message.lower()
                        for marker in (
                            "unauthorized",
                            "authentication failed",
                            "authentication expired",
                            "authorization expired",
                            "not logged in",
                            "login required",
                            "invalid token",
                            "token expired",
                            "invalid session",
                            "session expired",
                            "invalid credential",
                            "credentials expired",
                            "401",
                            "403",
                            "请登录",
                            "登录失效",
                            "登录过期",
                            "授权失效",
                            "授权过期",
                            "凭证失效",
                            "凭证过期",
                        )
                    ):
                        self._connected = False
                        self.authentication_failed = True
                        raise MiFitnessAuthenticationError(
                            "小米健康云授权已失效；请重新获取 Cookie。"
                        )
                    code = str(code_value)
                    raise MiFitnessResponseError(f"小米健康云返回错误代码 {code}")
                result = body.get("result")
                if not isinstance(result, dict):
                    raise MiFitnessResponseError("小米健康云响应结果格式无效。")
                return result
            except (
                MiFitnessAuthenticationError,
                MiFitnessRateLimitError,
                MiFitnessResponseError,
            ):
                raise
            except (
                httpx.HTTPError,
                ValueError,
                UnicodeDecodeError,
                RuntimeError,
            ) as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(
            f"小米健康云请求失败：{redact_error(last_error or 'unknown error')}"
        )

    @staticmethod
    def _retry_after_delay(value: str, attempt: int) -> float:
        """Parse Retry-After seconds or HTTP-date within a bounded shared cooldown."""
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                retry_at = (
                    retry_at.replace(tzinfo=UTC)
                    if retry_at.tzinfo is None
                    else retry_at.astimezone(UTC)
                )
                delay = (retry_at - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = DEFAULT_RATE_LIMIT_DELAY_SECONDS
        if delay != delay:  # NaN must not bypass the shared cooldown.
            delay = DEFAULT_RATE_LIMIT_DELAY_SECONDS
        return max(1.0, min(delay, MAX_RATE_LIMIT_DELAY_SECONDS))

    @staticmethod
    def _utc_timestamp(value: datetime) -> int:
        """Convert an aware datetime to UTC without discarding its offset."""
        normalized = (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )
        return int(normalized.timestamp())

    async def _fetch_page(
        self,
        key: str,
        start: datetime,
        end: datetime,
        region: str,
        cursor: str | None = None,
        *,
        budget: _OperationBudget | None = None,
    ) -> dict:
        """Fetch one bounded page for a fixed Xiaomi region and data key."""
        host = (
            "https://hlth.io.mi.com"
            if region in ("", "cn")
            else f"https://{region}.hlth.io.mi.com"
        )
        payload: dict[str, object] = {
            "start_time": self._utc_timestamp(start),
            "end_time": self._utc_timestamp(end),
            "key": key,
        }
        if cursor:
            payload["next_key"] = cursor
        return await self._request(
            host,
            "/app/v1/data/get_fitness_data_by_time",
            payload,
            budget=budget,
        )

    async def _probe_key(
        self,
        key: str,
        start: datetime,
        end: datetime,
        region: str,
        *,
        budget: _OperationBudget | None = None,
    ) -> list[dict]:
        """Return only the first page of dict records for low-cost discovery."""
        operation_budget = budget or _OperationBudget()
        result = await self._fetch_page(
            key, start, end, region, budget=operation_budget
        )
        data = result.get("data_list")
        if not isinstance(data, list):
            return []
        records = []
        for item in data:
            if isinstance(item, dict):
                operation_budget.consume_record()
                records.append(item)
        return records

    async def _fetch_key(
        self,
        key: str,
        start: datetime,
        end: datetime,
        region: str,
        *,
        budget: _OperationBudget | None = None,
    ) -> list[dict]:
        """Fetch every paginated record within record, page, and byte budgets."""
        operation_budget = budget or _OperationBudget()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_records: set[bytes] = set()
        records: list[dict] = []
        dataset_bytes = 0
        for _ in range(100):
            result = await self._fetch_page(
                key,
                start,
                end,
                region,
                cursor,
                budget=operation_budget,
            )
            data = result.get("data_list")
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    operation_budget.consume_record()
                    normalized_item = dict(item)
                    raw_value = normalized_item.get("value")
                    if isinstance(raw_value, str):
                        try:
                            normalized_item["value"] = json.loads(raw_value)
                        except json.JSONDecodeError:
                            pass
                    canonical = json.dumps(
                        normalized_item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                    dataset_bytes += len(canonical)
                    if dataset_bytes > MAX_DATASET_BYTES:
                        raise RuntimeError(
                            f"小米健康云 {key} 数据超过单次同步字节安全上限。"
                        )
                    fingerprint = hashlib.sha256(canonical).digest()
                    if fingerprint not in seen_records:
                        seen_records.add(fingerprint)
                        if len(records) >= MAX_RECORDS_PER_KEY:
                            raise RuntimeError(
                                f"小米健康云 {key} 数据超过单次同步安全上限。"
                            )
                        records.append(normalized_item)
            next_cursor = (
                result.get("next_key")
                if isinstance(result.get("next_key"), str)
                else None
            )
            if not result.get("has_more"):
                return records
            if not next_cursor:
                raise RuntimeError(
                    f"小米健康云 {key} 数据分页缺少有效游标；已拒绝不完整的同步结果。"
                )
            if next_cursor in seen_cursors:
                raise RuntimeError(
                    f"小米健康云 {key} 数据分页游标重复；已拒绝不完整的同步结果。"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError(
            f"小米健康云 {key} 数据分页超过安全上限；已拒绝不完整的同步结果。"
        )

    async def _discover_region(self, budget: _OperationBudget | None = None) -> str:
        """Probe only first pages and stop at the first region with usable data."""
        operation_budget = budget or _OperationBudget()
        now = datetime.now(UTC)
        successful_probe = False
        last_error: Exception | None = None
        for region in KNOWN_REGIONS:
            for key in REGION_PROBE_KEYS:
                try:
                    records = await asyncio.wait_for(
                        self._probe_key(
                            key,
                            now - timedelta(days=30),
                            now,
                            region,
                            budget=operation_budget,
                        ),
                        timeout=10,
                    )
                    successful_probe = True
                    if records:
                        return region
                except MiFitnessAuthenticationError:
                    raise
                except MiFitnessRateLimitError:
                    raise
                except MiFitnessBudgetError:
                    raise
                except asyncio.TimeoutError as error:
                    last_error = error
                    break
                except RuntimeError as error:
                    last_error = error
                    continue
        if not successful_probe and last_error:
            raise RuntimeError(
                f"小米健康云区域探测失败：{redact_error(last_error)}"
            ) from last_error
        raise RuntimeError(
            "最近 30 天没有可用于自动识别区域的云端记录；请在插件配置中手动选择 region。"
        )

    async def _discover_data_types(
        self, budget: _OperationBudget | None = None
    ) -> list[str]:
        """Discover supported datasets from one bounded first page per candidate key."""
        operation_budget = budget or _OperationBudget()
        now = datetime.now(UTC)
        types: list[str] = []
        successful_probe = False
        for data_type, keys in (
            ("daily_activity", ("steps",)),
            ("heart_rate", PRIMARY_HEART_RATE_KEYS + RESTING_HEART_RATE_KEYS),
            ("body_measurements", ("weight",)),
            ("sleep", ("sleep",)),
            ("spo2", SPO2_KEYS),
            ("stress", ("stress",)),
        ):
            if data_type == "heart_rate":
                for key in keys:
                    try:
                        records = await self._probe_key(
                            key,
                            now - timedelta(days=30),
                            now,
                            self.region,
                            budget=operation_budget,
                        )
                        successful_probe = True
                        if self._parse_heart_rate_records(
                            records,
                            is_resting=key in RESTING_HEART_RATE_KEYS,
                        ):
                            types.append(data_type)
                            break
                    except (
                        MiFitnessAuthenticationError,
                        MiFitnessRateLimitError,
                        MiFitnessBudgetError,
                    ):
                        raise
                    except RuntimeError:
                        continue
                continue
            if data_type == "spo2":
                for key in keys:
                    try:
                        records = await self._probe_key(
                            key,
                            now - timedelta(days=30),
                            now,
                            self.region,
                            budget=operation_budget,
                        )
                        successful_probe = True
                        if self._parse_spo2_records(records):
                            types.append(data_type)
                            break
                    except (
                        MiFitnessAuthenticationError,
                        MiFitnessRateLimitError,
                        MiFitnessBudgetError,
                    ):
                        raise
                    except RuntimeError:
                        continue
                continue
            found = False
            for key in keys:
                try:
                    records = await self._probe_key(
                        key,
                        now - timedelta(days=30),
                        now,
                        self.region,
                        budget=operation_budget,
                    )
                    successful_probe = True
                    if records:
                        found = True
                        break
                except (
                    MiFitnessAuthenticationError,
                    MiFitnessRateLimitError,
                    MiFitnessBudgetError,
                ):
                    raise
                except RuntimeError:
                    continue
            if found:
                types.append(data_type)
        if not successful_probe:
            raise RuntimeError(
                "小米健康云会话未能通过健康数据接口验证；请稍后重试或检查区域设置。"
            )
        return types

    @staticmethod
    def _value(item: dict) -> dict:
        """Decode a cloud value field without assuming its shape."""
        value = item.get("value", {})
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                return decoded if isinstance(decoded, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    @staticmethod
    def _number(value: object, minimum: float, maximum: float) -> float | None:
        """Return a bounded numeric value or None for malformed cloud data."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if minimum <= number <= maximum else None

    @staticmethod
    def _boolish(value: object) -> bool | None:
        """Parse only explicit boolean-like cloud values."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            try:
                number = float(value)
            except (OverflowError, ValueError):
                return None
            return number != 0 if math.isfinite(number) else None
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "on"}:
                return True
            if normalized in {"false", "no", "off", ""}:
                return False
            try:
                number = float(normalized)
                return number != 0 if math.isfinite(number) else None
            except (OverflowError, ValueError):
                return None
        return None

    @staticmethod
    def _timestamp_datetime(value: object) -> datetime | None:
        """Convert a cloud timestamp only when it falls in a reasonable range."""
        try:
            timestamp = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if timestamp > 100_000_000_000:  # Some cloud records use milliseconds.
            timestamp //= 1000
        earliest = int(datetime(2000, 1, 1, tzinfo=UTC).timestamp())
        latest = int((datetime.now(UTC) + timedelta(days=2)).timestamp())
        if not earliest <= timestamp <= latest:
            return None
        try:
            return datetime.fromtimestamp(timestamp, UTC)
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _record_time(
        item: dict, preferred_time: object | None = None
    ) -> tuple[datetime, str] | None:
        """Return UTC collection time and cloud-zone local calendar date."""
        try:
            offset = int(item.get("zone_offset", 0))
        except (TypeError, ValueError, OverflowError):
            return None
        utc_time = MiFitnessCloudAdapter._timestamp_datetime(preferred_time)
        if utc_time is None:
            utc_time = MiFitnessCloudAdapter._timestamp_datetime(item.get("time"))
        if utc_time is None or abs(offset) > 15 * 3600:
            return None
        return utc_time, (utc_time + timedelta(seconds=offset)).date().isoformat()

    @staticmethod
    def _in_requested_range(
        timestamp: datetime, start: datetime, end: datetime
    ) -> bool:
        """Return whether a non-sleep sample lies in the inclusive UTC request range."""

        def as_utc(value: datetime) -> datetime:
            return (
                value.replace(tzinfo=UTC)
                if value.tzinfo is None
                else value.astimezone(UTC)
            )

        return as_utc(start) <= timestamp <= as_utc(end) and timestamp <= datetime.now(
            UTC
        )

    async def iter_daily_activity(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[DailyActivity]:
        """Aggregate validated records by the configured user-local calendar day."""
        step_buckets: dict[tuple[str, int], dict[str, float]] = defaultdict(
            lambda: {"steps": 0.0, "distance_m": 0.0, "active_kcal": 0.0}
        )
        calorie_buckets: dict[tuple[str, int], float] = defaultdict(float)
        latest: dict[str, datetime] = {}

        def consume_records(key: str, records: list[dict]) -> None:
            for item in records:
                record_time = self._record_time(item)
                if not record_time:
                    continue
                timestamp, _ = record_time
                if not self._in_requested_range(timestamp, start, end):
                    continue
                date = timestamp.astimezone(self.user_timezone).date().isoformat()
                bucket_key = (date, int(timestamp.timestamp()) // 60)
                value = self._value(item)
                if key == "steps":
                    steps = self._number(value.get("steps"), 0, 200_000)
                    distance = self._number(value.get("distance"), 0, 500_000)
                    calories = self._number(value.get("calories"), 0, 50_000)
                    # A calorie-only or malformed row from the required steps
                    # endpoint is not a complete daily activity sample.  In
                    # particular, it must never replace a cached non-zero step
                    # total with an invented zero.
                    if steps is None:
                        continue
                    bucket = step_buckets[bucket_key]
                    bucket["steps"] = max(bucket["steps"], steps)
                    bucket["distance_m"] = max(bucket["distance_m"], distance or 0)
                    bucket["active_kcal"] = max(bucket["active_kcal"], calories or 0)
                else:
                    calories = self._number(value.get("calories"), 0, 50_000)
                    if calories is not None:
                        calorie_buckets[bucket_key] = max(
                            calorie_buckets[bucket_key], calories
                        )
                latest[date] = max(latest.get(date, timestamp), timestamp)

        operation_budget = _OperationBudget()
        step_records = await self._fetch_key(
            "steps", start, end, self.region, budget=operation_budget
        )
        consume_records("steps", step_records)
        del step_records
        try:
            calorie_records = await self._fetch_key(
                "calories", start, end, self.region, budget=operation_budget
            )
            consume_records("calories", calorie_records)
            del calorie_records
        except MiFitnessAuthenticationError:
            raise
        except MiFitnessRateLimitError:
            raise
        except MiFitnessBudgetError:
            raise
        except RuntimeError:
            pass
        totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {"steps": 0.0, "distance_m": 0.0, "active_kcal": 0.0}
        )
        for (date, _minute), values in step_buckets.items():
            for metric, number in values.items():
                totals[date][metric] += number
        calorie_totals: dict[str, float] = defaultdict(float)
        for (date, _minute), calories in calorie_buckets.items():
            calorie_totals[date] += calories
        for date, calories in calorie_totals.items():
            # The optional calorie key may refine a complete steps day, but it
            # cannot establish an activity day by itself.
            if date in totals:
                totals[date]["active_kcal"] = calories
        for date, values in sorted(totals.items()):
            yield DailyActivity(
                date,
                int(values["steps"]),
                values["distance_m"],
                values["active_kcal"],
                latest[date],
            )

    async def iter_heart_rate(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[HeartRateSample]:
        """Yield standard and resting heart-rate records with tolerant field parsing.

        Xiaomi accounts do not all expose sampled heart rates under the same key.
        ``resting_heart_rate`` is treated as an optional account-specific
        fallback and cannot invalidate records returned by ``heart_rate``.
        """
        operation_budget = _OperationBudget()
        errors: list[RuntimeError] = []
        response_errors: list[MiFitnessResponseError] = []
        successful_keys = 0
        seen: set[tuple[str, int]] = set()
        for key in PRIMARY_HEART_RATE_KEYS + RESTING_HEART_RATE_KEYS:
            try:
                samples = self._parse_heart_rate_records(
                    await self._fetch_key(
                        key,
                        start,
                        end,
                        self.region,
                        budget=operation_budget,
                    ),
                    is_resting=key in RESTING_HEART_RATE_KEYS,
                    start=start,
                    end=end,
                )
            except MiFitnessAuthenticationError:
                raise
            except MiFitnessRateLimitError:
                raise
            except MiFitnessBudgetError:
                raise
            except MiFitnessResponseError as error:
                response_errors.append(error)
                continue
            except RuntimeError as error:
                errors.append(error)
                continue
            successful_keys += 1
            for sample in samples:
                source = "resting_hr" if key in RESTING_HEART_RATE_KEYS else "hr"
                identity = (source, int(sample.timestamp.timestamp()))
                if identity in seen:
                    continue
                seen.add(identity)
                yield sample
        if errors:
            raise errors[-1]
        if successful_keys == 0 and response_errors:
            raise response_errors[-1]

    def _parse_heart_rate_records(
        self,
        records: list[dict],
        *,
        is_resting: bool,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[HeartRateSample]:
        """Validate one candidate key before it can suppress another alias."""
        samples: dict[int, HeartRateSample] = {}
        for item in records:
            value = self._value(item)
            preferred_time = value.get("date_time") if is_resting else None
            record_time = self._record_time(item, preferred_time)
            if not record_time:
                continue
            timestamp, _ = record_time
            if (
                start is not None
                and end is not None
                and not self._in_requested_range(timestamp, start, end)
            ):
                continue
            bpm = self._number(
                value.get("bpm")
                or value.get("heart_rate")
                or value.get("heartRate")
                or value.get("hr")
                or value.get("rate")
                or value.get("value"),
                20,
                260,
            )
            if bpm is None:
                continue
            kind = (
                "active"
                if not is_resting and self._boolish(value.get("type")) is True
                else "passive"
            )
            workout_id = value.get("workout_id")
            workout_id_flag = self._boolish(workout_id)
            if workout_id_flag is None and isinstance(workout_id, str):
                workout_id_flag = bool(workout_id.strip())
            is_workout = (
                workout_id_flag is True
                or self._boolish(value.get("is_workout")) is True
            )
            source = "resting_hr" if is_resting else "hr"
            timestamp_seconds = int(timestamp.timestamp())
            samples[timestamp_seconds] = HeartRateSample(
                f"mi_fitness_{source}_{timestamp_seconds}",
                timestamp,
                int(bpm),
                kind,
                is_workout,
            )
        return list(samples.values())

    async def iter_body_measurements(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[BodyMeasurement]:
        """Yield validated smart-scale records while tolerating missing composition fields."""
        for item in await self._fetch_key("weight", start, end, self.region):
            record_time = self._record_time(item)
            if not record_time:
                continue
            timestamp, _ = record_time
            if not self._in_requested_range(timestamp, start, end):
                continue
            value = self._value(item)
            weight = self._number(value.get("weight"), 10, 400)
            if weight is None:
                continue
            visceral = self._number(value.get("visceral_fat"), 0, 100)
            metabolism = self._number(value.get("basal_metabolism"), 0, 20_000)
            age = self._number(value.get("body_age"), 0, 150)
            yield BodyMeasurement(
                f"mi_fitness_weight_{int(timestamp.timestamp())}",
                timestamp,
                weight,
                self._number(value.get("bmi"), 5, 100),
                self._number(value.get("body_fat_rate"), 0, 100),
                self._number(value.get("muscle_rate"), 0, 300),
                self._number(value.get("moisture_rate"), 0, 100),
                self._number(value.get("bone_mass"), 0, 30),
                int(visceral) if visceral is not None else None,
                int(metabolism) if metabolism is not None else None,
                int(age) if age is not None else None,
            )

    async def iter_sleep(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[SleepSession]:
        """Yield completed sessions even when Xiaomi indexes today's summary later."""
        requested_start = (
            start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
        )
        requested_end = (
            end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
        )
        # Xiaomi can index a completed morning sleep session at the end of the
        # local summary day rather than at its actual wake-up time.  Looking one
        # day ahead retrieves that envelope without accepting a future session:
        # the nested wake-up timestamp is still bounded by ``requested_end``.
        fetch_end = requested_end + timedelta(days=1)
        for item in await self._fetch_key("sleep", start, fetch_end, self.region):
            value = self._value(item)
            begin_time = self._timestamp_datetime(
                value.get("bedtime")
                or value.get("device_bedtime")
                or value.get("bed_timestamp")
            )
            finish_time = self._timestamp_datetime(
                value.get("wake_up_time")
                or value.get("device_wake_up_time")
                or value.get("out_bed_timestamp")
                or item.get("time")
            )
            if begin_time is None or finish_time is None:
                continue
            if not requested_start <= finish_time <= requested_end:
                continue
            begin = int(begin_time.timestamp())
            finish = int(finish_time.timestamp())
            duration = max(0, (finish - begin) // 60)
            if not 30 <= duration <= 24 * 60:
                continue
            awake = self._number(
                value.get("awake_duration") or value.get("sleep_awake_duration") or 0,
                0,
                duration,
            )
            score_value = value.get("score")
            if score_value is None:
                score_value = value.get("sleep_score")
            score = self._number(score_value, 0, 100)
            yield SleepSession(
                f"mi_fitness_sleep_{begin}",
                begin_time,
                finish_time,
                duration,
                duration - int(awake or 0),
                int(awake or 0),
                int(score) if score is not None else None,
            )

    async def iter_spo2(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[SpO2Sample]:
        """Yield validated blood-oxygen records; unsupported keys simply return no rows."""
        operation_budget = _OperationBudget()
        errors: list[RuntimeError] = []
        response_errors: list[MiFitnessResponseError] = []
        successful_keys = 0
        seen_timestamps: set[int] = set()
        for key in SPO2_KEYS:
            try:
                samples = self._parse_spo2_records(
                    await self._fetch_key(
                        key,
                        start,
                        end,
                        self.region,
                        budget=operation_budget,
                    ),
                    start=start,
                    end=end,
                )
            except MiFitnessAuthenticationError:
                raise
            except MiFitnessRateLimitError:
                raise
            except MiFitnessBudgetError:
                raise
            except MiFitnessResponseError as error:
                response_errors.append(error)
                continue
            except RuntimeError as error:
                errors.append(error)
                continue
            successful_keys += 1
            for sample in samples:
                timestamp = int(sample.timestamp.timestamp())
                if timestamp in seen_timestamps:
                    continue
                seen_timestamps.add(timestamp)
                yield sample
        if errors:
            raise errors[-1]
        if successful_keys == 0 and response_errors:
            raise response_errors[-1]

    def _parse_spo2_records(
        self,
        records: list[dict],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[SpO2Sample]:
        """Return only validated samples from one SpO2 candidate key."""
        samples: list[SpO2Sample] = []
        for item in records:
            value = self._value(item)
            record_time = self._record_time(item, value.get("time"))
            raw_percent = value.get("spo2")
            if raw_percent is None:
                raw_percent = value.get("blood_oxygen")
            if raw_percent is None:
                raw_percent = value.get("oxygen")
            if raw_percent is None:
                raw_percent = value.get("value")
            percent = self._number(raw_percent, 70, 100)
            if record_time and percent is not None:
                timestamp, _ = record_time
                if (
                    start is not None
                    and end is not None
                    and not self._in_requested_range(timestamp, start, end)
                ):
                    continue
                samples.append(
                    SpO2Sample(
                        f"mi_fitness_spo2_{int(timestamp.timestamp())}",
                        timestamp,
                        int(percent),
                    )
                )
        return samples

    async def iter_stress(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[StressSample]:
        """Yield validated stress scores; no medical inference is made here."""
        for item in await self._fetch_key("stress", start, end, self.region):
            value = self._value(item)
            time = self._record_time(item, value.get("time"))
            stress_value = value.get("stress")
            if stress_value is None:
                stress_value = value.get("score")
            if stress_value is None:
                stress_value = value.get("value")
            score = self._number(stress_value, 0, 100)
            if time and score is not None:
                timestamp, _ = time
                if not self._in_requested_range(timestamp, start, end):
                    continue
                yield StressSample(
                    f"mi_fitness_stress_{int(timestamp.timestamp())}",
                    timestamp,
                    int(score),
                )

    async def close(self) -> None:
        """Close the plugin-owned async HTTP client."""
        self._connected = False
        client = self._client
        self._client = None
        self._cookies = ""
        self._ssecurity = b""
        if client:
            await client.aclose()

    async def probe_data_keys(
        self,
        start: datetime,
        end: datetime,
        *,
        timeout_seconds: float = DIAGNOSTIC_TIMEOUT_SECONDS,
    ) -> dict[str, str]:
        """Return safe key-level availability diagnostics without returning raw records.

        Args:
            start: Probe window start.
            end: Probe window end.
            timeout_seconds: Total wall-clock budget shared by every diagnostic key.

        Returns:
            Mapping of candidate key to count or a sanitized error category.
        """
        result: dict[str, str] = {}
        keys = tuple(
            dict.fromkeys(
                ("steps",)
                + PRIMARY_HEART_RATE_KEYS
                + RESTING_HEART_RATE_KEYS
                + ("sleep",)
                + SPO2_KEYS
                + ("stress", "weight")
            )
        )
        time_budget = max(1.0, min(float(timeout_seconds), 5 * 60.0))
        deadline = asyncio.get_running_loop().time() + time_budget
        operation_budget = _OperationBudget()
        for key in keys:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("小米健康云数据诊断超过安全时限。")
            try:
                result[key] = str(
                    len(
                        await asyncio.wait_for(
                            self._fetch_key(
                                key,
                                start,
                                end,
                                self.region,
                                budget=operation_budget,
                            ),
                            timeout=remaining,
                        )
                    )
                )
            except MiFitnessAuthenticationError:
                raise
            except MiFitnessRateLimitError:
                raise
            except MiFitnessBudgetError:
                raise
            except asyncio.TimeoutError as error:
                raise TimeoutError("小米健康云数据诊断超过安全时限。") from error
            except Exception as error:
                result[key] = f"错误：{redact_error(error)}"
        return result
