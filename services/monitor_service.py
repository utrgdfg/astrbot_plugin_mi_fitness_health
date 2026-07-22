"""Private, cooldown-protected proactive health-monitor decisions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo

from ..storage import Database


@dataclass(frozen=True, slots=True)
class MonitorFinding:
    """One non-diagnostic finding that may be sent to the owner."""

    alert_type: str
    event_key: str
    message: str


class HealthMonitorService:
    """Evaluate late-night private activity without pretending to detect sleep."""

    def __init__(
        self,
        database: Database,
        owner_platform_id: str,
        timezone: tzinfo,
        late_night_enabled: bool,
        late_night_start: str,
        late_night_end: str,
        activity_window_minutes: int,
        cooldown_minutes: int,
        daily_limit: int = 3,
    ):
        self.database = database
        self.owner_platform_id = owner_platform_id
        self.timezone = timezone
        self.late_night_enabled = late_night_enabled
        self.late_night_start = self._parse_clock(late_night_start, time(0, 30))
        self.late_night_end = self._parse_clock(late_night_end, time(6, 0))
        self.activity_window_minutes = max(5, min(activity_window_minutes, 180))
        self.cooldown_minutes = max(30, cooldown_minutes)
        self.daily_limit = max(1, min(daily_limit, 24))

    @staticmethod
    def _parse_clock(value: str, fallback: time) -> time:
        """Parse HH:MM and use a safe default for malformed configuration."""
        try:
            hour, minute = (int(part) for part in value.strip().split(":", 1))
            return time(hour, minute)
        except (AttributeError, TypeError, ValueError):
            return fallback

    def _is_late_night(self, current: time) -> bool:
        """Support both same-day and across-midnight monitoring windows."""
        if self.late_night_start == self.late_night_end:
            return False
        if self.late_night_start < self.late_night_end:
            return self.late_night_start <= current < self.late_night_end
        return current >= self.late_night_start or current < self.late_night_end

    def _night_key(self, now: datetime) -> str:
        """Map an across-midnight window to one night for once-per-night dedupe."""
        if (
            self.late_night_start > self.late_night_end
            and now.time() >= self.late_night_start
        ):
            return (now.date() + timedelta(days=1)).isoformat()
        return now.date().isoformat()

    async def proactive_cooling_down(self, now: datetime | None = None) -> bool:
        """Apply one global cooldown across all proactive message categories."""
        current_local = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        local_midnight = current_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        sent_today = await asyncio.to_thread(
            self.database.alert_count_since,
            "proactive_message",
            local_midnight.astimezone(UTC).isoformat(),
        )
        if sent_today >= self.daily_limit:
            return True
        last = await asyncio.to_thread(self.database.last_alert_at, "proactive_message")
        if not last:
            return False
        try:
            parsed = datetime.fromisoformat(last)
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            current = current_local.astimezone(UTC)
            return parsed > current - timedelta(minutes=self.cooldown_minutes)
        except ValueError:
            return False

    async def evaluate_late_activity(
        self, now: datetime | None = None
    ) -> MonitorFinding | None:
        """Return a finding only when recent owner chat proves late activity.

        Missing Xiaomi sleep data is deliberately not considered proof that a
        person is awake because sleep records are commonly uploaded afterward.
        """
        if not self.late_night_enabled or not self.owner_platform_id:
            return None
        now = now or datetime.now(self.timezone)
        now_utc = now.astimezone(UTC)
        if not self._is_late_night(now.time()):
            return None
        event_key = self._night_key(now)
        if await asyncio.to_thread(
            self.database.alert_event_sent, "late_night_activity", event_key
        ):
            return None
        state = await asyncio.to_thread(
            self.database.private_owner_session, self.owner_platform_id
        )
        if not state:
            return None
        try:
            last_seen = datetime.fromisoformat(state["updated_at"])
            last_seen = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=UTC)
        except (KeyError, TypeError, ValueError):
            return None
        if now_utc - last_seen > timedelta(minutes=self.activity_window_minutes):
            return None
        last_alert = await asyncio.to_thread(
            self.database.last_alert_at, "late_night_activity"
        )
        if last_alert:
            try:
                if datetime.fromisoformat(last_alert) > now_utc - timedelta(
                    minutes=self.cooldown_minutes
                ):
                    return None
            except ValueError:
                pass
        return MonitorFinding(
            "late_night_activity",
            event_key,
            f"已经 {now.strftime('%H:%M')} 了，刚才还看到你在聊天。要是还没休息，可以考虑先放下手机准备睡觉。"
            "这个判断只来自你的私聊活动，不是手环实时检测。",
        )

    async def mark_sent(
        self, finding: MonitorFinding, sent_at: datetime | None = None
    ) -> None:
        """Start cooldown only after the proactive message was delivered."""
        await asyncio.to_thread(
            self.database.add_alert,
            finding.alert_type,
            finding.message,
            finding.event_key,
            sent_at,
        )

    async def mark_proactive_sent(
        self, message: str, sent_at: datetime | None = None
    ) -> None:
        """Record the global cooldown after one combined message is delivered."""
        await asyncio.to_thread(
            self.database.add_alert, "proactive_message", message, None, sent_at
        )
