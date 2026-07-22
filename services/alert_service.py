"""Non-diagnostic, cooldown-protected health finding evaluation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..storage import Database


@dataclass(frozen=True, slots=True)
class AlertFinding:
    """One exact cloud-data sequence eligible for proactive delivery."""

    alert_type: str
    event_key: str
    message: str


class AlertService:
    """Evaluate only user-configured thresholds with freshness and sequence guards."""

    def __init__(
        self,
        database: Database,
        user_id: str,
        high: int,
        low: int,
        consecutive: int,
        cooldown_minutes: int,
        spo2_low: int = 0,
        stress_high: int = 0,
        sleep_min_minutes: int = 0,
        data_max_age_minutes: int = 180,
    ):
        """Create an evaluator; a zero threshold disables that metric."""
        self.database, self.user_id = database, user_id
        self.high = max(0, min(high, 300))
        self.low = max(0, min(low, 300))
        self.spo2_low = max(0, min(spo2_low, 100))
        self.stress_high = max(0, min(stress_high, 100))
        self.sleep_min_minutes = max(0, min(sleep_min_minutes, 24 * 60))
        self.consecutive = max(2, consecutive)
        self.cooldown_minutes = max(1, cooldown_minutes)
        self.data_max_age_minutes = max(15, min(data_max_age_minutes, 24 * 60))

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None

    def _is_fresh(self, value: object) -> bool:
        timestamp = self._timestamp(value)
        return bool(
            timestamp
            and timestamp
            >= datetime.now(UTC) - timedelta(minutes=self.data_max_age_minutes)
        )

    async def _eligible(self, alert_type: str, event_key: str) -> bool:
        if await asyncio.to_thread(
            self.database.alert_event_sent, alert_type, event_key
        ):
            return False
        last = await asyncio.to_thread(self.database.last_alert_at, alert_type)
        if not last:
            return True
        timestamp = self._timestamp(last)
        return not timestamp or timestamp <= datetime.now(UTC) - timedelta(
            minutes=self.cooldown_minutes
        )

    async def _evaluate_heart_rate(
        self, cutoff: str, findings: list[AlertFinding]
    ) -> None:
        rows = await asyncio.to_thread(
            self.database.heart_rates_since, self.user_id, cutoff, 200
        )
        if not rows or not self._is_fresh(rows[0]["timestamp"]):
            return
        for label, threshold, compare in (
            ("high", self.high, lambda value, target: value > target),
            ("low", self.low, lambda value, target: value < target),
        ):
            if threshold <= 0:
                continue
            matching = 0
            for (
                row
            ) in rows:  # newest first: only the current uninterrupted sequence counts
                if (
                    not self._is_fresh(row["timestamp"])
                    or row["is_workout"]
                    or row["sample_type"] != "passive"
                    or not compare(row["bpm"], threshold)
                ):
                    break
                matching += 1
                if matching >= self.consecutive:
                    alert_type = f"heart_rate_{label}"
                    event_key = str(rows[0]["record_id"])
                    if await self._eligible(alert_type, event_key):
                        direction = "高于" if label == "high" else "低于"
                        findings.append(
                            AlertFinding(
                                alert_type,
                                event_key,
                                f"最近连续 {matching} 条非运动被动心率记录{direction}你配置的 {threshold} bpm 阈值"
                                f"（最新采集：{rows[0]['timestamp']}）。建议先休息并按需复测；这不是医疗诊断。",
                            )
                        )
                    break

    async def _evaluate_metric_sequence(
        self,
        table: str,
        column: str,
        threshold: int,
        alert_type: str,
        direction: str,
        cutoff: str,
        findings: list[AlertFinding],
    ) -> None:
        if threshold <= 0:
            return
        rows = await asyncio.to_thread(
            self.database.metric_samples_since, table, self.user_id, cutoff, 200
        )
        if not rows or not self._is_fresh(rows[0]["timestamp"]):
            return
        matching = 0
        for row in rows:
            if not self._is_fresh(row["timestamp"]):
                break
            value = int(row[column])
            abnormal = value < threshold if direction == "低于" else value > threshold
            if not abnormal:
                break
            matching += 1
            if matching >= self.consecutive:
                event_key = str(rows[0]["record_id"])
                if await self._eligible(alert_type, event_key):
                    label = "血氧" if table == "spo2_samples" else "压力分数"
                    suffix = "%" if table == "spo2_samples" else ""
                    findings.append(
                        AlertFinding(
                            alert_type,
                            event_key,
                            f"最近连续 {matching} 条{label}记录{direction}你配置的 {threshold}{suffix} 阈值"
                            f"（最新采集：{rows[0]['timestamp']}）。建议结合当时状态复测；这不是医疗诊断。",
                        )
                    )
                break

    async def _evaluate_sleep(self, findings: list[AlertFinding]) -> None:
        if self.sleep_min_minutes <= 0:
            return
        sleep = await asyncio.to_thread(self.database.latest_sleep, self.user_id)
        if not sleep or sleep["asleep_minutes"] >= self.sleep_min_minutes:
            return
        ended = self._timestamp(sleep["end_at"])
        if not ended or ended < datetime.now(UTC) - timedelta(hours=36):
            return
        alert_type = "short_sleep"
        event_key = str(sleep["record_id"])
        if not await self._eligible(alert_type, event_key):
            return
        findings.append(
            AlertFinding(
                alert_type,
                event_key,
                f"最近一段睡眠记录为 {sleep['asleep_minutes']} 分钟，低于你配置的 {self.sleep_min_minutes} 分钟阈值"
                f"（结束：{sleep['end_at']}）。今天如果方便，可以给自己留些恢复时间；这不是医疗诊断。",
            )
        )

    async def evaluate(self) -> list[AlertFinding]:
        """Return unsent, fresh findings without persisting before delivery."""
        cutoff = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        findings: list[AlertFinding] = []
        await self._evaluate_heart_rate(cutoff, findings)
        await self._evaluate_metric_sequence(
            "spo2_samples",
            "percent",
            self.spo2_low,
            "spo2_low",
            "低于",
            cutoff,
            findings,
        )
        await self._evaluate_metric_sequence(
            "stress_samples",
            "score",
            self.stress_high,
            "stress_high",
            "高于",
            cutoff,
            findings,
        )
        await self._evaluate_sleep(findings)
        return findings

    async def mark_sent(self, finding: AlertFinding) -> None:
        """Persist dedupe/cooldown state only after a successful platform send."""
        await asyncio.to_thread(
            self.database.add_alert,
            finding.alert_type,
            finding.message,
            finding.event_key,
        )
