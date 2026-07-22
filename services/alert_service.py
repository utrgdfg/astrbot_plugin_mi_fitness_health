"""Non-diagnostic, cooldown-protected alert evaluation for passive cloud heart-rate records."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ..storage import Database


class AlertService:
    """Persist alerts only after configured consecutive passive samples."""

    def __init__(self, database: Database, user_id: str, high: int, low: int, consecutive: int, cooldown_minutes: int):
        """Create user-configured alert evaluator."""
        self.database, self.user_id = database, user_id
        self.high, self.low = high, low
        self.consecutive = max(2, consecutive)
        self.cooldown_minutes = max(1, cooldown_minutes)

    async def evaluate(self) -> list[str]:
        """Return newly persisted health reminders; never infer a diagnosis."""
        cutoff = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        rows = await asyncio.to_thread(self.database.heart_rates_since, self.user_id, cutoff, 200)
        rows.reverse()
        messages: list[str] = []
        for label, threshold, compare in (("high", self.high, lambda value, target: value > target), ("low", self.low, lambda value, target: value < target)):
            if threshold <= 0:
                continue
            matching = 0
            for row in reversed(rows):
                if row["is_workout"] or row["sample_type"] != "passive":
                    # A workout or active measurement breaks a passive-resting sequence.
                    matching = 0
                    continue
                matching = matching + 1 if compare(row["bpm"], threshold) else 0
                if matching >= self.consecutive:
                    last = await asyncio.to_thread(self.database.last_alert_at, f"heart_rate_{label}")
                    if last and datetime.fromisoformat(last) > datetime.now(UTC) - timedelta(minutes=self.cooldown_minutes):
                        break
                    message = f"连续 {matching} 条非运动被动心率记录{'高于' if label == 'high' else '低于'}个人阈值 {threshold} bpm。数据仅用于提醒，不构成医疗诊断。"
                    await asyncio.to_thread(self.database.add_alert, f"heart_rate_{label}", message)
                    messages.append(message)
                    break
        return messages
