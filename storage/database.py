"""Versioned SQLite storage; callers execute its synchronous methods in a thread."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import BodyMeasurement, DailyActivity, HeartRateSample, SleepSession, SpO2Sample, StressSample

SCHEMA_VERSION = 2


class Database:
    """Persist one account's cloud records without deleting old plugin data."""

    def __init__(self, path: Path):
        """Open or migrate a SQLite database.

        Args:
            path: Database file location.
        """
        self.path = path

    def initialize(self) -> None:
        """Create the schema and apply forward-only migrations."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_version(version) VALUES (0)")
                current = 0
            else:
                current = int(row[0])
            if current < 1:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS daily_activity (
                        user_id TEXT NOT NULL, date TEXT NOT NULL, steps INTEGER NOT NULL,
                        distance_m REAL NOT NULL, active_kcal REAL NOT NULL, collected_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL, PRIMARY KEY(user_id, date)
                    );
                    CREATE TABLE IF NOT EXISTS heart_rate_samples (
                        user_id TEXT NOT NULL, record_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                        bpm INTEGER NOT NULL, sample_type TEXT NOT NULL, is_workout INTEGER NOT NULL,
                        updated_at TEXT NOT NULL, PRIMARY KEY(user_id, record_id)
                    );
                    CREATE TABLE IF NOT EXISTS body_measurements (
                        user_id TEXT NOT NULL, record_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                        weight_kg REAL NOT NULL, bmi REAL, body_fat_pct REAL, muscle_mass_kg REAL,
                        water_pct REAL, bone_mass_kg REAL, visceral_fat_score INTEGER,
                        basal_metabolism_kcal INTEGER, metabolic_age INTEGER, updated_at TEXT NOT NULL,
                        PRIMARY KEY(user_id, record_id)
                    );
                    CREATE TABLE IF NOT EXISTS sync_state (
                        data_type TEXT PRIMARY KEY, last_sync_at TEXT NOT NULL, last_record_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, alert_type TEXT NOT NULL,
                        created_at TEXT NOT NULL, message TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_heart_rate_timestamp
                        ON heart_rate_samples(user_id, timestamp);
                    CREATE INDEX IF NOT EXISTS idx_body_timestamp
                        ON body_measurements(user_id, timestamp);
                    """
                )
                connection.execute("UPDATE schema_version SET version = 1")
                current = 1
            if current < 2:
                connection.executescript("""
                CREATE TABLE IF NOT EXISTS sleep_sessions (user_id TEXT NOT NULL, record_id TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, duration_minutes INTEGER NOT NULL, asleep_minutes INTEGER NOT NULL, awake_minutes INTEGER NOT NULL, score INTEGER, updated_at TEXT NOT NULL, PRIMARY KEY(user_id,record_id));
                CREATE TABLE IF NOT EXISTS spo2_samples (user_id TEXT NOT NULL, record_id TEXT NOT NULL, timestamp TEXT NOT NULL, percent INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(user_id,record_id));
                CREATE TABLE IF NOT EXISTS stress_samples (user_id TEXT NOT NULL, record_id TEXT NOT NULL, timestamp TEXT NOT NULL, score INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(user_id,record_id));
                """)
                connection.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    @contextmanager
    def _connect(self):
        """Yield a transaction connection and always close its Windows file handle."""
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        """Return an ISO UTC timestamp."""
        return datetime.now(UTC).isoformat()

    def upsert_activity(self, user_id: str, record: DailyActivity) -> str:
        """Insert or update an activity row and return its exact outcome."""
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM daily_activity WHERE user_id=? AND date=?", (user_id, record.date)
            ).fetchone()
            connection.execute(
                """INSERT INTO daily_activity(user_id,date,steps,distance_m,active_kcal,collected_at,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,date) DO UPDATE SET
                   steps=excluded.steps,distance_m=excluded.distance_m,active_kcal=excluded.active_kcal,
                   collected_at=excluded.collected_at,updated_at=excluded.updated_at""",
                (user_id, record.date, record.steps, record.distance_m, record.active_kcal,
                 record.collected_at.isoformat(), self._now()),
            )
        return "updated" if exists else "added"

    def upsert_heart_rate(self, user_id: str, record: HeartRateSample) -> str:
        """Insert or update one heart-rate row and return its exact outcome."""
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM heart_rate_samples WHERE user_id=? AND record_id=?", (user_id, record.record_id)
            ).fetchone()
            connection.execute(
                """INSERT INTO heart_rate_samples(user_id,record_id,timestamp,bpm,sample_type,is_workout,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET
                   timestamp=excluded.timestamp,bpm=excluded.bpm,sample_type=excluded.sample_type,
                   is_workout=excluded.is_workout,updated_at=excluded.updated_at""",
                (user_id, record.record_id, record.timestamp.isoformat(), record.bpm, record.sample_type,
                 int(record.is_workout), self._now()),
            )
        return "updated" if exists else "added"

    def upsert_measurement(self, user_id: str, record: BodyMeasurement) -> str:
        """Insert or update one body measurement and return its exact outcome."""
        values = (user_id, record.record_id, record.timestamp.isoformat(), record.weight_kg, record.bmi,
                  record.body_fat_pct, record.muscle_mass_kg, record.water_pct, record.bone_mass_kg,
                  record.visceral_fat_score, record.basal_metabolism_kcal, record.metabolic_age, self._now())
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM body_measurements WHERE user_id=? AND record_id=?", (user_id, record.record_id)
            ).fetchone()
            connection.execute(
                """INSERT INTO body_measurements(user_id,record_id,timestamp,weight_kg,bmi,body_fat_pct,
                   muscle_mass_kg,water_pct,bone_mass_kg,visceral_fat_score,basal_metabolism_kcal,metabolic_age,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET
                   timestamp=excluded.timestamp,weight_kg=excluded.weight_kg,bmi=excluded.bmi,
                   body_fat_pct=excluded.body_fat_pct,muscle_mass_kg=excluded.muscle_mass_kg,
                   water_pct=excluded.water_pct,bone_mass_kg=excluded.bone_mass_kg,
                   visceral_fat_score=excluded.visceral_fat_score,basal_metabolism_kcal=excluded.basal_metabolism_kcal,
                   metabolic_age=excluded.metabolic_age,updated_at=excluded.updated_at""", values)
        return "updated" if exists else "added"

    def update_sync_state(self, data_type: str, last_record_at: datetime | None) -> None:
        """Record completion time and maximum accepted data timestamp."""
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO sync_state(data_type,last_sync_at,last_record_at) VALUES(?,?,?)
                   ON CONFLICT(data_type) DO UPDATE SET last_sync_at=excluded.last_sync_at,
                   last_record_at=excluded.last_record_at""",
                (data_type, self._now(), last_record_at.isoformat() if last_record_at else None),
            )

    def upsert_sleep(self, user_id: str, record: SleepSession) -> str:
        """Insert or update a sleep session."""
        with self._connect() as c:
            old = c.execute("SELECT 1 FROM sleep_sessions WHERE user_id=? AND record_id=?", (user_id, record.record_id)).fetchone()
            c.execute("INSERT INTO sleep_sessions VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET start_at=excluded.start_at,end_at=excluded.end_at,duration_minutes=excluded.duration_minutes,asleep_minutes=excluded.asleep_minutes,awake_minutes=excluded.awake_minutes,score=excluded.score,updated_at=excluded.updated_at", (user_id,record.record_id,record.start_at.isoformat(),record.end_at.isoformat(),record.duration_minutes,record.asleep_minutes,record.awake_minutes,record.score,self._now()))
        return "updated" if old else "added"

    def upsert_spo2(self, user_id: str, record: SpO2Sample) -> str:
        """Insert or update a blood-oxygen sample."""
        return self._upsert_metric("spo2_samples", user_id, record.record_id, record.timestamp.isoformat(), record.percent, "percent")

    def upsert_stress(self, user_id: str, record: StressSample) -> str:
        """Insert or update a stress sample."""
        return self._upsert_metric("stress_samples", user_id, record.record_id, record.timestamp.isoformat(), record.score, "score")

    def _upsert_metric(self, table: str, user_id: str, record_id: str, timestamp: str, value: int, column: str) -> str:
        with self._connect() as c:
            old = c.execute(f"SELECT 1 FROM {table} WHERE user_id=? AND record_id=?", (user_id,record_id)).fetchone()
            c.execute(f"INSERT INTO {table}(user_id,record_id,timestamp,{column},updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET timestamp=excluded.timestamp,{column}=excluded.{column},updated_at=excluded.updated_at", (user_id,record_id,timestamp,value,self._now()))
        return "updated" if old else "added"

    def latest_sync_at(self) -> str | None:
        """Return the most recent completed synchronization timestamp."""
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(last_sync_at) AS value FROM sync_state").fetchone()
        return row["value"] if row and row["value"] else None

    def today_activity(self, user_id: str, date: str) -> dict[str, Any] | None:
        """Return one local-day activity summary."""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM daily_activity WHERE user_id=? AND date=?", (user_id, date)).fetchone()
        return dict(row) if row else None

    def heart_rates_since(self, user_id: str, timestamp: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent heart-rate records in newest-first order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM heart_rate_samples WHERE user_id=? AND timestamp>=? ORDER BY timestamp DESC LIMIT ?",
                (user_id, timestamp, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_measurement(self, user_id: str) -> dict[str, Any] | None:
        """Return the newest body measurement."""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM body_measurements WHERE user_id=? ORDER BY timestamp DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else None

    def latest_sleep(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM sleep_sessions WHERE user_id=? ORDER BY end_at DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else None

    def latest_metric(self, table: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute(f"SELECT * FROM {table} WHERE user_id=? ORDER BY timestamp DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else None

    def trend(self, user_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Return per-day activity and average passive heart rate for a date span."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.date,a.steps,a.active_kcal,
                   (SELECT AVG(h.bpm) FROM heart_rate_samples h WHERE h.user_id=a.user_id
                    AND date(h.timestamp)=a.date AND h.is_workout=0) AS avg_heart_rate
                   FROM daily_activity a WHERE a.user_id=? AND a.date BETWEEN ? AND ? ORDER BY a.date""",
                (user_id, start_date, end_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_alert(self, alert_type: str, message: str) -> None:
        """Persist a non-diagnostic alert audit record."""
        with self._connect() as connection:
            connection.execute("INSERT INTO alerts(alert_type,created_at,message) VALUES(?,?,?)", (alert_type, self._now(), message))

    def last_alert_at(self, alert_type: str) -> str | None:
        """Return the latest alert timestamp for cooldown enforcement."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM alerts WHERE alert_type=? ORDER BY id DESC LIMIT 1", (alert_type,)
            ).fetchone()
        return row["created_at"] if row else None
