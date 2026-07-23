"""Xiaomi Mi Fitness cloud adapter derived from Mi Fitness MCP's data layer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from astrbot.api import logger

from .base import DataAdapter
from ..models import (
    BodyMeasurement,
    DailyActivity,
    HeartRateSample,
    SleepSession,
    SpO2Sample,
    StressSample,
)
from ..utils.privacy import redact_error

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
        self.authentication_failed = False

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
        self.authentication_failed = False
        if not self.user_id or not self.pass_token:
            self.last_error = "缺少 userId 或 passToken。"
            return False
        await self.close()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), follow_redirects=False
        )
        try:
            await self._login_with_token()
            self.region = await self._discover_region()
            self._available_types = await self._discover_data_types()
            self._connected = True
            return True
        except MiFitnessAuthenticationError as error:
            self.authentication_failed = True
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
        if response.status_code in (401, 403):
            raise MiFitnessAuthenticationError(
                "小米登录授权已失效；请重新获取 Cookie。"
            )
        response.raise_for_status()
        raw = response.content
        if not raw.startswith(LOGIN_PREFIX):
            raise MiFitnessAuthenticationError("小米登录响应无效；请重新获取 Cookie。")
        payload = json.loads(raw[len(LOGIN_PREFIX) :].decode())
        if not payload.get("ssecurity") or not payload.get("location"):
            raise MiFitnessAuthenticationError(
                "凭证已失效、需要验证或账号受到风控；请在浏览器重新登录后更新 Cookie。"
            )
        self.user_id = str(payload.get("userId") or self.user_id)
        self.pass_token = str(payload.get("passToken") or self.pass_token)
        self._ssecurity = base64.b64decode(payload["ssecurity"])
        redirected = await self._client.get(str(payload["location"]))
        if redirected.status_code in (401, 403):
            raise MiFitnessAuthenticationError(
                "小米健康云会话授权失败；请重新获取 Cookie。"
            )
        redirected.raise_for_status()
        self._cookies = "; ".join(
            value.split(";", 1)[0]
            for value in redirected.headers.get_list("set-cookie")
        )
        if not self._cookies:
            raise MiFitnessAuthenticationError(
                "未取得小米健康云会话；请重新登录后更新 Cookie。"
            )

    async def _request(self, host: str, path: str, payload: dict[str, object]) -> dict:
        """Send one encrypted request with capped exponential retries."""
        if not self._client or not self._ssecurity:
            raise RuntimeError("Xiaomi session unavailable")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
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
                response = await self._client.post(
                    host + path,
                    headers={
                        "Cookie": self._cookies,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    content=urlencode(encrypted),
                )
                if response.status_code in (401, 403):
                    self._connected = False
                    self.authentication_failed = True
                    raise MiFitnessAuthenticationError(
                        "小米健康云授权已失效；请重新获取 Cookie。"
                    )
                response.raise_for_status()
                body = json.loads(
                    _rc4_crypt(signed_nonce, base64.b64decode(response.text))
                )
                if body.get("code") != 0:
                    message = str(body.get("message") or "Mi Fitness request failed")
                    if any(
                        marker in message.lower()
                        for marker in (
                            "auth",
                            "login",
                            "token",
                            "session",
                            "401",
                            "403",
                            "登录",
                            "授权",
                            "凭证",
                        )
                    ):
                        self._connected = False
                        self.authentication_failed = True
                        raise MiFitnessAuthenticationError(
                            "小米健康云授权已失效；请重新获取 Cookie。"
                        )
                    raise RuntimeError(message)
                return (
                    body.get("result") if isinstance(body.get("result"), dict) else {}
                )
            except MiFitnessAuthenticationError:
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

    async def _fetch_key(
        self, key: str, start: datetime, end: datetime, region: str
    ) -> list[dict]:
        """Fetch every paginated record for an upstream key in a bounded date range."""
        host = (
            "https://hlth.io.mi.com"
            if region in ("", "cn")
            else f"https://{region}.hlth.io.mi.com"
        )
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_records: set[str] = set()
        records: list[dict] = []
        for _ in range(100):
            payload: dict[str, object] = {
                "start_time": int(start.replace(tzinfo=UTC).timestamp()),
                "end_time": int(end.replace(tzinfo=UTC).timestamp()),
                "key": key,
            }
            if cursor:
                payload["next_key"] = cursor
            result = await self._request(
                host, "/app/v1/data/get_fitness_data_by_time", payload
            )
            data = result.get("data_list")
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    normalized_item = dict(item)
                    raw_value = normalized_item.get("value")
                    if isinstance(raw_value, str):
                        try:
                            normalized_item["value"] = json.loads(raw_value)
                        except json.JSONDecodeError:
                            pass
                    fingerprint = json.dumps(
                        normalized_item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    if fingerprint not in seen_records:
                        seen_records.add(fingerprint)
                        records.append(item)
            next_cursor = (
                result.get("next_key")
                if isinstance(result.get("next_key"), str)
                else None
            )
            if not result.get("has_more") or not next_cursor:
                return records
            if next_cursor in seen_cursors:
                if key in {"steps", "calories"}:
                    raise RuntimeError(
                        f"小米健康云 {key} 数据分页游标重复；已拒绝不完整的每日汇总。"
                    )
                logger.warning(
                    "Mi Fitness pagination stopped at a repeated cursor for key %s; keeping %d unique records",
                    key,
                    len(records),
                )
                return records
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        if key in {"steps", "calories"}:
            raise RuntimeError(
                f"小米健康云 {key} 数据分页超过安全上限；已拒绝不完整的每日汇总。"
            )
        logger.warning(
            "Mi Fitness pagination reached the safety limit for key %s; keeping %d unique records",
            key,
            len(records),
        )
        return records

    async def _discover_region(self) -> str:
        """Probe up to the recent 30-day window instead of hard-coded historic dates."""
        now = datetime.now(UTC)
        candidates = ([self.region] if self.region in KNOWN_REGIONS else []) + [
            item for item in KNOWN_REGIONS if item != self.region
        ]
        for region in candidates:
            try:
                if await self._fetch_key(
                    "steps", now - timedelta(days=30), now, region
                ):
                    return region
            except MiFitnessAuthenticationError:
                raise
            except RuntimeError:
                continue
        return self.region or "cn"

    async def _discover_data_types(self) -> list[str]:
        """Discover only supported datasets, using a recent bounded probe."""
        now = datetime.now(UTC)
        types: list[str] = []
        for data_type, keys in (
            ("daily_activity", ("steps",)),
            ("heart_rate", ("heart_rate", "resting_heart_rate")),
            ("body_measurements", ("weight",)),
            ("sleep", ("sleep",)),
            ("spo2", ("spo2",)),
            ("stress", ("stress",)),
        ):
            found = False
            for key in keys:
                try:
                    if await self._fetch_key(
                        key, now - timedelta(days=30), now, self.region
                    ):
                        found = True
                        break
                except MiFitnessAuthenticationError:
                    raise
                except RuntimeError:
                    continue
            if found:
                types.append(data_type)
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
    def _record_time(item: dict) -> tuple[datetime, str] | None:
        """Return UTC collection time and cloud-zone local calendar date."""
        try:
            timestamp = int(item.get("time"))
            offset = int(item.get("zone_offset", 0))
        except (TypeError, ValueError):
            return None
        if timestamp > 100_000_000_000:  # Some cloud records use milliseconds.
            timestamp //= 1000
        if timestamp <= 0 or abs(offset) > 15 * 3600:
            return None
        utc_time = datetime.fromtimestamp(timestamp, UTC)
        return utc_time, (utc_time + timedelta(seconds=offset)).date().isoformat()

    async def iter_daily_activity(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[DailyActivity]:
        """Aggregate validated step and calorie records by their cloud local day."""
        totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {"steps": 0.0, "distance_m": 0.0, "active_kcal": 0.0}
        )
        latest: dict[str, datetime] = {}
        calorie_totals: dict[str, float] = defaultdict(float)
        step_records = await self._fetch_key("steps", start, end, self.region)
        try:
            calorie_records = await self._fetch_key("calories", start, end, self.region)
        except MiFitnessAuthenticationError:
            raise
        except RuntimeError:
            calorie_records = []
        for key, records in (("steps", step_records), ("calories", calorie_records)):
            for item in records:
                record_time = self._record_time(item)
                if not record_time:
                    continue
                timestamp, date = record_time
                value = self._value(item)
                if key == "steps":
                    steps = self._number(value.get("steps"), 0, 200_000)
                    distance = self._number(value.get("distance"), 0, 500_000)
                    calories = self._number(value.get("calories"), 0, 50_000)
                    totals[date]["steps"] += steps or 0
                    totals[date]["distance_m"] += distance or 0
                    totals[date]["active_kcal"] += calories or 0
                else:
                    calories = self._number(value.get("calories"), 0, 50_000)
                    if calories is not None:
                        calorie_totals[date] += calories
                latest[date] = max(latest.get(date, timestamp), timestamp)
        for date, calories in calorie_totals.items():
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
        records: list[tuple[dict, bool]] = []
        successful_keys = 0
        errors: list[RuntimeError] = []
        for key, is_resting in (("heart_rate", False), ("resting_heart_rate", True)):
            try:
                records.extend(
                    (item, is_resting)
                    for item in await self._fetch_key(key, start, end, self.region)
                )
                successful_keys += 1
            except MiFitnessAuthenticationError:
                raise
            except RuntimeError as error:
                errors.append(error)
        if not successful_keys and errors:
            raise errors[-1]
        seen: set[tuple[int, int]] = set()
        for item, is_resting in records:
            record_time = self._record_time(item)
            if not record_time:
                continue
            timestamp, _ = record_time
            value = self._value(item)
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
            identity = (int(timestamp.timestamp()), int(bpm))
            if identity in seen:
                continue
            seen.add(identity)
            kind = (
                "passive"
                if is_resting or str(value.get("type", "0")) == "0"
                else "active"
            )
            is_workout = bool(value.get("workout_id") or value.get("is_workout"))
            source = "resting_hr" if is_resting else "hr"
            yield HeartRateSample(
                f"mi_fitness_{source}_{int(timestamp.timestamp())}_{int(bpm)}",
                timestamp,
                int(bpm),
                kind,
                is_workout,
            )

    async def iter_body_measurements(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[BodyMeasurement]:
        """Yield validated smart-scale records while tolerating missing composition fields."""
        for item in await self._fetch_key("weight", start, end, self.region):
            record_time = self._record_time(item)
            if not record_time:
                continue
            timestamp, _ = record_time
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
        """Yield validated cloud sleep sessions when the account exposes the sleep key."""
        for item in await self._fetch_key("sleep", start, end, self.region):
            value = self._value(item)
            try:
                begin = int(
                    value.get("bedtime")
                    or value.get("device_bedtime")
                    or value.get("bed_timestamp")
                )
                finish = int(
                    value.get("wake_up_time")
                    or value.get("device_wake_up_time")
                    or value.get("out_bed_timestamp")
                    or item.get("time")
                )
            except (TypeError, ValueError):
                continue
            if begin > 100_000_000_000:
                begin //= 1000
            if finish > 100_000_000_000:
                finish //= 1000
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
                datetime.fromtimestamp(begin, UTC),
                datetime.fromtimestamp(finish, UTC),
                duration,
                duration - int(awake or 0),
                int(awake or 0),
                int(score) if score is not None else None,
            )

    async def iter_spo2(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[SpO2Sample]:
        """Yield validated blood-oxygen records; unsupported keys simply return no rows."""
        for item in await self._fetch_key("spo2", start, end, self.region):
            time = self._record_time(item)
            value = self._value(item)
            percent = self._number(value.get("spo2") or value.get("value"), 70, 100)
            if time and percent is not None:
                timestamp, _ = time
                yield SpO2Sample(
                    f"mi_fitness_spo2_{int(timestamp.timestamp())}",
                    timestamp,
                    int(percent),
                )

    async def iter_stress(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[StressSample]:
        """Yield validated stress scores; no medical inference is made here."""
        for item in await self._fetch_key("stress", start, end, self.region):
            time = self._record_time(item)
            value = self._value(item)
            stress_value = value.get("stress")
            if stress_value is None:
                stress_value = value.get("score")
            if stress_value is None:
                stress_value = value.get("value")
            score = self._number(stress_value, 0, 100)
            if time and score is not None:
                timestamp, _ = time
                yield StressSample(
                    f"mi_fitness_stress_{int(timestamp.timestamp())}",
                    timestamp,
                    int(score),
                )

    async def close(self) -> None:
        """Close the plugin-owned async HTTP client."""
        self._connected = False
        if self._client:
            await self._client.aclose()
            self._client = None

    async def probe_data_keys(self, start: datetime, end: datetime) -> dict[str, str]:
        """Return safe key-level availability diagnostics without returning raw records.

        Args:
            start: Probe window start.
            end: Probe window end.

        Returns:
            Mapping of candidate key to count or a sanitized error category.
        """
        result: dict[str, str] = {}
        for key in (
            "steps",
            "heart_rate",
            "resting_heart_rate",
            "heartrate",
            "hr",
            "sleep",
            "spo2",
            "blood_oxygen",
            "stress",
            "weight",
        ):
            try:
                result[key] = str(
                    len(await self._fetch_key(key, start, end, self.region))
                )
            except Exception as error:
                result[key] = f"错误：{redact_error(error)}"
        return result
