"""Validated records supported by the Xiaomi Mi Fitness cloud adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DailyActivity:
    """One local-calendar-day activity aggregate from the cloud."""

    date: str
    steps: int
    distance_m: float
    active_kcal: float
    collected_at: datetime


@dataclass(frozen=True, slots=True)
class HeartRateSample:
    """One non-real-time heart-rate record from Xiaomi cloud."""

    record_id: str
    timestamp: datetime
    bpm: int
    sample_type: str
    is_workout: bool


@dataclass(frozen=True, slots=True)
class BodyMeasurement:
    """One smart-scale body measurement from Xiaomi cloud."""

    record_id: str
    timestamp: datetime
    weight_kg: float
    bmi: float | None = None
    body_fat_pct: float | None = None
    muscle_mass_kg: float | None = None
    water_pct: float | None = None
    bone_mass_kg: float | None = None
    visceral_fat_score: int | None = None
    basal_metabolism_kcal: int | None = None
    metabolic_age: int | None = None


@dataclass(frozen=True, slots=True)
class SleepSession:
    """One sleep session reported by Xiaomi cloud."""
    record_id: str
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    asleep_minutes: int
    awake_minutes: int
    score: int | None = None


@dataclass(frozen=True, slots=True)
class SpO2Sample:
    """One blood-oxygen record reported by Xiaomi cloud."""
    record_id: str
    timestamp: datetime
    percent: int


@dataclass(frozen=True, slots=True)
class StressSample:
    """One stress score reported by Xiaomi cloud."""
    record_id: str
    timestamp: datetime
    score: int
