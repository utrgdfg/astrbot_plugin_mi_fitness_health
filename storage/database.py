"""Versioned SQLite storage; callers execute its synchronous methods in a thread."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from ..models import (
    BodyMeasurement,
    DailyActivity,
    HeartRateSample,
    SleepSession,
)

SCHEMA_VERSION = 7


class Database:
    """Persist one account's cloud records with bounded, owner-controlled retention."""

    def __init__(self, path: Path):
        """Open or migrate a SQLite database.

        Args:
            path: Database file location.
        """
        self.path = path

    def initialize(self) -> None:
        """Create the schema and apply forward-only migrations."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.close(descriptor)
            except FileExistsError:
                pass
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # Some Windows filesystems expose ACLs rather than POSIX modes.
            pass
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = connection.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
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
                connection.execute("UPDATE schema_version SET version = 2")
                current = 2
            if current < 3:
                connection.executescript("""
                CREATE TABLE IF NOT EXISTS private_owner_sessions (
                    owner_platform_id TEXT PRIMARY KEY,
                    session TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS care_deliveries (
                    reminder_type TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(reminder_type, local_date)
                );
                """)
                connection.execute("UPDATE schema_version SET version = 3")
                current = 3
            if current < 4:
                alert_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(alerts)"
                    ).fetchall()
                }
                if "event_key" not in alert_columns:
                    connection.execute("ALTER TABLE alerts ADD COLUMN event_key TEXT")
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_event ON alerts(alert_type,event_key) WHERE event_key IS NOT NULL"
                )
                connection.execute("UPDATE schema_version SET version = 4")
                current = 4
            if current < 5:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sync_failures (
                        data_type TEXT PRIMARY KEY,
                        last_attempt_at TEXT NOT NULL,
                        last_error TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS sleep_sessions (
                        user_id TEXT NOT NULL, record_id TEXT NOT NULL,
                        start_at TEXT NOT NULL, end_at TEXT NOT NULL,
                        duration_minutes INTEGER NOT NULL,
                        asleep_minutes INTEGER NOT NULL,
                        awake_minutes INTEGER NOT NULL, score INTEGER,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(user_id,record_id)
                    );
                    CREATE TABLE IF NOT EXISTS spo2_samples (
                        user_id TEXT NOT NULL, record_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL, percent INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(user_id,record_id)
                    );
                    CREATE TABLE IF NOT EXISTS stress_samples (
                        user_id TEXT NOT NULL, record_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL, score INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(user_id,record_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_sleep_end
                        ON sleep_sessions(user_id, end_at);
                    CREATE INDEX IF NOT EXISTS idx_spo2_timestamp
                        ON spo2_samples(user_id, timestamp);
                    CREATE INDEX IF NOT EXISTS idx_stress_timestamp
                        ON stress_samples(user_id, timestamp);
                    CREATE INDEX IF NOT EXISTS idx_alert_created
                        ON alerts(alert_type, created_at);
                    """
                )
                connection.execute("UPDATE schema_version SET version = 5")
                current = 5
            if current < 6:
                alert_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(alerts)"
                    ).fetchall()
                }
                if "owner_platform_id" not in alert_columns:
                    connection.execute(
                        "ALTER TABLE alerts ADD COLUMN owner_platform_id TEXT NOT NULL DEFAULT ''"
                    )
                # Legacy sync state did not identify the Xiaomi account. It
                # cannot be assigned safely, so discard only freshness/error
                # metadata and force each configured account to refresh.
                connection.executescript(
                    """
                    DROP TABLE IF EXISTS sync_state;
                    CREATE TABLE sync_state (
                        user_id TEXT NOT NULL,
                        data_type TEXT NOT NULL,
                        last_sync_at TEXT NOT NULL,
                        last_record_at TEXT,
                        PRIMARY KEY(user_id, data_type)
                    );
                    DROP TABLE IF EXISTS sync_failures;
                    CREATE TABLE sync_failures (
                        user_id TEXT NOT NULL,
                        data_type TEXT NOT NULL,
                        last_attempt_at TEXT NOT NULL,
                        last_error TEXT NOT NULL,
                        PRIMARY KEY(user_id, data_type)
                    );
                    DROP INDEX IF EXISTS idx_alert_event;
                    CREATE UNIQUE INDEX idx_alert_event
                        ON alerts(owner_platform_id,alert_type,event_key)
                        WHERE event_key IS NOT NULL;
                    """
                )
                connection.execute("UPDATE schema_version SET version = 6")
                current = 6
            if current < 7:
                connection.executescript(
                    """
                    DROP TABLE IF EXISTS care_deliveries;
                    DROP INDEX IF EXISTS idx_alert_created;
                    CREATE INDEX IF NOT EXISTS idx_alert_owner_type_created
                        ON alerts(owner_platform_id,alert_type,created_at);
                    """
                )
                connection.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )

    @contextmanager
    def _connect(self):
        """Yield a transaction connection and always close its Windows file handle."""
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA secure_delete=ON")
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
                "SELECT 1 FROM daily_activity WHERE user_id=? AND date=?",
                (user_id, record.date),
            ).fetchone()
            connection.execute(
                """INSERT INTO daily_activity(user_id,date,steps,distance_m,active_kcal,collected_at,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,date) DO UPDATE SET
                   steps=excluded.steps,distance_m=excluded.distance_m,active_kcal=excluded.active_kcal,
                   collected_at=excluded.collected_at,updated_at=excluded.updated_at""",
                (
                    user_id,
                    record.date,
                    record.steps,
                    record.distance_m,
                    record.active_kcal,
                    record.collected_at.isoformat(),
                    self._now(),
                ),
            )
        return "updated" if exists else "added"

    def upsert_many(
        self, user_id: str, data_type: str, records: list[object]
    ) -> dict[str, int]:
        """Persist a cloud data type in one transaction with exact counters.

        A Xiaomi heart-rate upload can contain thousands of samples.  This
        avoids opening one SQLite connection per sample without changing the
        unique keys that make delayed cloud uploads safe to re-read.
        """
        counters = {"added": 0, "updated": 0}
        if not records:
            return counters
        with self._connect() as c:
            now = self._now()
            for record in records:
                if data_type == "daily_activity":
                    exists = c.execute(
                        "SELECT 1 FROM daily_activity WHERE user_id=? AND date=?",
                        (user_id, record.date),
                    ).fetchone()
                    c.execute(
                        """INSERT INTO daily_activity(user_id,date,steps,distance_m,active_kcal,collected_at,updated_at)
                        VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,date) DO UPDATE SET steps=excluded.steps,distance_m=excluded.distance_m,
                        active_kcal=excluded.active_kcal,collected_at=excluded.collected_at,updated_at=excluded.updated_at""",
                        (
                            user_id,
                            record.date,
                            record.steps,
                            record.distance_m,
                            record.active_kcal,
                            record.collected_at.isoformat(),
                            now,
                        ),
                    )
                elif data_type == "heart_rate":
                    exists = c.execute(
                        "SELECT 1 FROM heart_rate_samples WHERE user_id=? AND record_id=?",
                        (user_id, record.record_id),
                    ).fetchone()
                    c.execute(
                        """INSERT INTO heart_rate_samples(user_id,record_id,timestamp,bpm,sample_type,is_workout,updated_at)
                        VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET timestamp=excluded.timestamp,bpm=excluded.bpm,
                        sample_type=excluded.sample_type,is_workout=excluded.is_workout,updated_at=excluded.updated_at""",
                        (
                            user_id,
                            record.record_id,
                            record.timestamp.isoformat(),
                            record.bpm,
                            record.sample_type,
                            int(record.is_workout),
                            now,
                        ),
                    )
                elif data_type == "body_measurements":
                    exists = c.execute(
                        "SELECT 1 FROM body_measurements WHERE user_id=? AND record_id=?",
                        (user_id, record.record_id),
                    ).fetchone()
                    c.execute(
                        """INSERT INTO body_measurements(user_id,record_id,timestamp,weight_kg,bmi,body_fat_pct,muscle_mass_kg,water_pct,
                        bone_mass_kg,visceral_fat_score,basal_metabolism_kcal,metabolic_age,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(user_id,record_id) DO UPDATE SET timestamp=excluded.timestamp,weight_kg=excluded.weight_kg,bmi=excluded.bmi,
                        body_fat_pct=excluded.body_fat_pct,muscle_mass_kg=excluded.muscle_mass_kg,water_pct=excluded.water_pct,
                        bone_mass_kg=excluded.bone_mass_kg,visceral_fat_score=excluded.visceral_fat_score,
                        basal_metabolism_kcal=excluded.basal_metabolism_kcal,metabolic_age=excluded.metabolic_age,updated_at=excluded.updated_at""",
                        (
                            user_id,
                            record.record_id,
                            record.timestamp.isoformat(),
                            record.weight_kg,
                            record.bmi,
                            record.body_fat_pct,
                            record.muscle_mass_kg,
                            record.water_pct,
                            record.bone_mass_kg,
                            record.visceral_fat_score,
                            record.basal_metabolism_kcal,
                            record.metabolic_age,
                            now,
                        ),
                    )
                elif data_type == "sleep":
                    exists = c.execute(
                        "SELECT 1 FROM sleep_sessions WHERE user_id=? AND record_id=?",
                        (user_id, record.record_id),
                    ).fetchone()
                    c.execute(
                        """INSERT INTO sleep_sessions VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET
                        start_at=excluded.start_at,end_at=excluded.end_at,duration_minutes=excluded.duration_minutes,
                        asleep_minutes=excluded.asleep_minutes,awake_minutes=excluded.awake_minutes,score=excluded.score,updated_at=excluded.updated_at""",
                        (
                            user_id,
                            record.record_id,
                            record.start_at.isoformat(),
                            record.end_at.isoformat(),
                            record.duration_minutes,
                            record.asleep_minutes,
                            record.awake_minutes,
                            record.score,
                            now,
                        ),
                    )
                elif data_type in ("spo2", "stress"):
                    table, column, value = (
                        ("spo2_samples", "percent", record.percent)
                        if data_type == "spo2"
                        else ("stress_samples", "score", record.score)
                    )
                    exists = c.execute(
                        f"SELECT 1 FROM {table} WHERE user_id=? AND record_id=?",
                        (user_id, record.record_id),
                    ).fetchone()
                    c.execute(
                        f"INSERT INTO {table}(user_id,record_id,timestamp,{column},updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET timestamp=excluded.timestamp,{column}=excluded.{column},updated_at=excluded.updated_at",
                        (
                            user_id,
                            record.record_id,
                            record.timestamp.isoformat(),
                            value,
                            now,
                        ),
                    )
                else:
                    raise ValueError(f"Unsupported data type: {data_type}")
                counters["updated" if exists else "added"] += 1
        return counters

    def replace_activity_records(
        self, user_id: str, records: list[DailyActivity], user_timezone: tzinfo
    ) -> dict[str, int]:
        """Replace complete returned activity days and remove legacy misdated rows."""
        counters = {"added": 0, "updated": 0}
        if not records:
            return counters
        incoming_dates = {record.date for record in records}
        with self._connect() as connection:
            existing_rows = connection.execute(
                "SELECT date,collected_at FROM daily_activity WHERE user_id=?",
                (user_id,),
            ).fetchall()
            existing_dates = {str(row["date"]) for row in existing_rows}
            dates_to_replace = set(incoming_dates)
            for row in existing_rows:
                try:
                    collected = datetime.fromisoformat(str(row["collected_at"]))
                    collected = (
                        collected if collected.tzinfo else collected.replace(tzinfo=UTC)
                    )
                    corrected_date = (
                        collected.astimezone(user_timezone).date().isoformat()
                    )
                except (TypeError, ValueError):
                    continue
                if corrected_date in incoming_dates and row["date"] != corrected_date:
                    dates_to_replace.add(str(row["date"]))
            placeholders = ",".join("?" for _ in dates_to_replace)
            connection.execute(
                f"DELETE FROM daily_activity WHERE user_id=? AND date IN ({placeholders})",
                (user_id, *sorted(dates_to_replace)),
            )
            now = self._now()
            for record in records:
                connection.execute(
                    """INSERT INTO daily_activity(
                           user_id,date,steps,distance_m,active_kcal,collected_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        user_id,
                        record.date,
                        record.steps,
                        record.distance_m,
                        record.active_kcal,
                        record.collected_at.isoformat(),
                        now,
                    ),
                )
                counters["updated" if record.date in existing_dates else "added"] += 1
        return counters

    def upsert_heart_rate(self, user_id: str, record: HeartRateSample) -> str:
        """Insert or update one heart-rate row and return its exact outcome."""
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM heart_rate_samples WHERE user_id=? AND record_id=?",
                (user_id, record.record_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO heart_rate_samples(user_id,record_id,timestamp,bpm,sample_type,is_workout,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET
                   timestamp=excluded.timestamp,bpm=excluded.bpm,sample_type=excluded.sample_type,
                   is_workout=excluded.is_workout,updated_at=excluded.updated_at""",
                (
                    user_id,
                    record.record_id,
                    record.timestamp.isoformat(),
                    record.bpm,
                    record.sample_type,
                    int(record.is_workout),
                    self._now(),
                ),
            )
        return "updated" if exists else "added"

    def upsert_measurement(self, user_id: str, record: BodyMeasurement) -> str:
        """Insert or update one body measurement and return its exact outcome."""
        values = (
            user_id,
            record.record_id,
            record.timestamp.isoformat(),
            record.weight_kg,
            record.bmi,
            record.body_fat_pct,
            record.muscle_mass_kg,
            record.water_pct,
            record.bone_mass_kg,
            record.visceral_fat_score,
            record.basal_metabolism_kcal,
            record.metabolic_age,
            self._now(),
        )
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM body_measurements WHERE user_id=? AND record_id=?",
                (user_id, record.record_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO body_measurements(user_id,record_id,timestamp,weight_kg,bmi,body_fat_pct,
                   muscle_mass_kg,water_pct,bone_mass_kg,visceral_fat_score,basal_metabolism_kcal,metabolic_age,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET
                   timestamp=excluded.timestamp,weight_kg=excluded.weight_kg,bmi=excluded.bmi,
                   body_fat_pct=excluded.body_fat_pct,muscle_mass_kg=excluded.muscle_mass_kg,
                   water_pct=excluded.water_pct,bone_mass_kg=excluded.bone_mass_kg,
                   visceral_fat_score=excluded.visceral_fat_score,basal_metabolism_kcal=excluded.basal_metabolism_kcal,
                   metabolic_age=excluded.metabolic_age,updated_at=excluded.updated_at""",
                values,
            )
        return "updated" if exists else "added"

    def update_sync_state(
        self, user_id: str, data_type: str, last_record_at: datetime | None
    ) -> None:
        """Record a successful completion and clear any older failure."""
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO sync_state(user_id,data_type,last_sync_at,last_record_at) VALUES(?,?,?,?)
                   ON CONFLICT(user_id,data_type) DO UPDATE SET last_sync_at=excluded.last_sync_at,
                   last_record_at=excluded.last_record_at""",
                (
                    user_id,
                    data_type,
                    self._now(),
                    last_record_at.isoformat() if last_record_at else None,
                ),
            )
            connection.execute(
                "DELETE FROM sync_failures WHERE user_id=? AND data_type=?",
                (user_id, data_type),
            )

    def update_sync_failure(self, user_id: str, data_type: str, reason: str) -> None:
        """Record a sanitized dataset failure without replacing its last success."""
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO sync_failures(user_id,data_type,last_attempt_at,last_error)
                   VALUES(?,?,?,?) ON CONFLICT(user_id,data_type) DO UPDATE SET
                   last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error""",
                (user_id, data_type, self._now(), reason[:180]),
            )

    def upsert_sleep(self, user_id: str, record: SleepSession) -> str:
        """Insert or update a sleep session."""
        with self._connect() as c:
            old = c.execute(
                "SELECT 1 FROM sleep_sessions WHERE user_id=? AND record_id=?",
                (user_id, record.record_id),
            ).fetchone()
            c.execute(
                "INSERT INTO sleep_sessions VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,record_id) DO UPDATE SET start_at=excluded.start_at,end_at=excluded.end_at,duration_minutes=excluded.duration_minutes,asleep_minutes=excluded.asleep_minutes,awake_minutes=excluded.awake_minutes,score=excluded.score,updated_at=excluded.updated_at",
                (
                    user_id,
                    record.record_id,
                    record.start_at.isoformat(),
                    record.end_at.isoformat(),
                    record.duration_minutes,
                    record.asleep_minutes,
                    record.awake_minutes,
                    record.score,
                    self._now(),
                ),
            )
        return "updated" if old else "added"

    def latest_sync_at(
        self, user_id: str, data_types: tuple[str, ...] | None = None
    ) -> str | None:
        """Return global latest success or the oldest valid success for required types."""
        with self._connect() as connection:
            if not data_types:
                rows = connection.execute(
                    "SELECT last_sync_at FROM sync_state WHERE user_id=?",
                    (user_id,),
                ).fetchall()
                timestamps = self._valid_utc_timestamps(
                    row["last_sync_at"] for row in rows
                )
                return max(timestamps).isoformat() if timestamps else None
            unique_types = tuple(dict.fromkeys(data_types))
            placeholders = ",".join("?" for _ in unique_types)
            rows = connection.execute(
                f"""SELECT s.data_type,s.last_sync_at,f.last_attempt_at
                    FROM sync_state s LEFT JOIN sync_failures f
                    ON f.user_id=s.user_id AND f.data_type=s.data_type
                    WHERE s.user_id=? AND s.data_type IN ({placeholders})""",
                (user_id, *unique_types),
            ).fetchall()
        if len(rows) != len(unique_types):
            return None
        successes = self._valid_utc_timestamps(row["last_sync_at"] for row in rows)
        if len(successes) != len(rows):
            return None
        for row, success in zip(rows, successes, strict=True):
            if not row["last_attempt_at"]:
                continue
            failures = self._valid_utc_timestamps((row["last_attempt_at"],))
            if not failures or failures[0] >= success:
                return None
        return min(successes).isoformat()

    @staticmethod
    def _valid_utc_timestamps(values) -> list[datetime]:
        """Parse stored ISO values consistently; malformed state forces a refresh."""
        parsed_values: list[datetime] = []
        for value in values:
            try:
                parsed = datetime.fromisoformat(str(value))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                parsed_values.append(parsed.astimezone(UTC))
            except (TypeError, ValueError):
                continue
        return parsed_values

    def latest_sync_failure_at(
        self, user_id: str, data_types: tuple[str, ...]
    ) -> str | None:
        """Return the newest unresolved attempt time for selected datasets."""
        unique_types = tuple(dict.fromkeys(data_types))
        if not unique_types:
            return None
        placeholders = ",".join("?" for _ in unique_types)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT last_attempt_at FROM sync_failures
                    WHERE user_id=? AND data_type IN ({placeholders})""",
                (user_id, *unique_types),
            ).fetchall()
        timestamps = self._valid_utc_timestamps(row["last_attempt_at"] for row in rows)
        return max(timestamps).isoformat() if timestamps else None

    def today_activity(self, user_id: str, date: str) -> dict[str, Any] | None:
        """Return one local-day activity summary."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM daily_activity WHERE user_id=? AND date=?",
                (user_id, date),
            ).fetchone()
        return dict(row) if row else None

    def recent_activity_between(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        limit: int = 7,
    ) -> list[dict[str, Any]]:
        """Return bounded activity rows inside an explicit local-date range."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM daily_activity
                   WHERE user_id=? AND date BETWEEN ? AND ?
                   ORDER BY date DESC LIMIT ?""",
                (user_id, start_date, end_date, max(1, min(limit, 31))),
            ).fetchall()
        return [dict(row) for row in rows]

    def heart_rates_since(
        self, user_id: str, timestamp: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return recent heart-rate records in newest-first order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM heart_rate_samples WHERE user_id=? AND timestamp>=? ORDER BY timestamp DESC LIMIT ?",
                (user_id, timestamp, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def heart_rates_between(
        self, user_id: str, start_timestamp: str, end_timestamp: str
    ) -> list[dict[str, Any]]:
        """Return every heart-rate sample in a half-open UTC time range.

        Xiaomi stores samples with UTC timestamps, while a health "day" is
        defined by the user's local calendar.  Callers calculate local
        midnight boundaries first and pass their UTC values here.  There is
        intentionally no record cap: a full day must not be silently
        truncated before calculating its average or range.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM heart_rate_samples
                   WHERE user_id=? AND timestamp>=? AND timestamp<?
                   ORDER BY timestamp DESC""",
                (user_id, start_timestamp, end_timestamp),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_measurement(self, user_id: str) -> dict[str, Any] | None:
        """Return the newest body measurement."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM body_measurements WHERE user_id=? ORDER BY timestamp DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_measurement_between(
        self, user_id: str, start_timestamp: str, end_timestamp: str
    ) -> dict[str, Any] | None:
        """Return the newest body measurement inside a half-open UTC range."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM body_measurements
                   WHERE user_id=? AND timestamp>=? AND timestamp<?
                   ORDER BY timestamp DESC LIMIT 1""",
                (user_id, start_timestamp, end_timestamp),
            ).fetchone()
        return dict(row) if row else None

    def recent_sleep(self, user_id: str, limit: int = 3) -> list[dict[str, Any]]:
        """Return a small sleep history for owner-only natural-language replies."""
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM sleep_sessions WHERE user_id=? ORDER BY end_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 7))),
            ).fetchall()
        return [dict(row) for row in rows]

    def sleep_ending_between(
        self,
        user_id: str,
        start_timestamp: str,
        end_timestamp: str,
        limit: int = 7,
    ) -> list[dict[str, Any]]:
        """Return sleep sessions whose wake time belongs to a UTC range."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM sleep_sessions
                   WHERE user_id=? AND end_at>=? AND end_at<?
                   ORDER BY end_at DESC LIMIT ?""",
                (
                    user_id,
                    start_timestamp,
                    end_timestamp,
                    max(1, min(limit, 31)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_metric(self, table: str, user_id: str) -> dict[str, Any] | None:
        if table not in {"spo2_samples", "stress_samples"}:
            raise ValueError("Unsupported metric table")
        with self._connect() as c:
            row = c.execute(
                f"SELECT * FROM {table} WHERE user_id=? ORDER BY timestamp DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_metric_between(
        self,
        table: str,
        user_id: str,
        start_timestamp: str,
        end_timestamp: str,
    ) -> dict[str, Any] | None:
        """Return the newest validated metric inside a half-open UTC range."""
        if table not in {"spo2_samples", "stress_samples"}:
            raise ValueError("Unsupported metric table")
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT * FROM {table}
                    WHERE user_id=? AND timestamp>=? AND timestamp<?
                    ORDER BY timestamp DESC LIMIT 1""",
                (user_id, start_timestamp, end_timestamp),
            ).fetchone()
        return dict(row) if row else None

    def trend(
        self, user_id: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
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

    def add_alert(
        self,
        owner_platform_id: str,
        alert_type: str,
        message: str,
        event_key: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        """Persist a non-diagnostic alert audit record."""
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO alerts(
                       alert_type,created_at,message,event_key,owner_platform_id
                   ) VALUES(?,?,?,?,?)""",
                (
                    alert_type,
                    (created_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
                    message,
                    event_key,
                    owner_platform_id,
                ),
            )

    def last_alert_at(self, owner_platform_id: str, alert_type: str) -> str | None:
        """Return the latest alert timestamp for cooldown enforcement."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT created_at FROM alerts
                   WHERE owner_platform_id=? AND alert_type=?
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (owner_platform_id, alert_type),
            ).fetchone()
        return row["created_at"] if row else None

    def alert_event_sent(
        self, owner_platform_id: str, alert_type: str, event_key: str
    ) -> bool:
        """Return whether one exact cloud record sequence was already delivered."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM alerts
                   WHERE owner_platform_id=? AND alert_type=? AND event_key=? LIMIT 1""",
                (owner_platform_id, alert_type, event_key),
            ).fetchone()
        return row is not None

    def alert_count_since(
        self, owner_platform_id: str, alert_type: str, timestamp: str
    ) -> int:
        """Count successfully delivered proactive messages in a bounded period."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS value FROM alerts
                   WHERE owner_platform_id=? AND alert_type=? AND created_at>=?""",
                (owner_platform_id, alert_type, timestamp),
            ).fetchone()
        return int(row["value"]) if row else 0

    def touch_private_owner_session(
        self,
        owner_platform_id: str,
        session: str,
        seen_at: datetime | None = None,
        allow_rebind: bool = False,
    ) -> bool:
        """Bind the first owner private session and reject cross-session replacement."""
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT session FROM private_owner_sessions WHERE owner_platform_id=?",
                (owner_platform_id,),
            ).fetchone()
            if existing and existing["session"] != session and not allow_rebind:
                return False
            connection.execute(
                """INSERT INTO private_owner_sessions(owner_platform_id,session,updated_at) VALUES(?,?,?)
                   ON CONFLICT(owner_platform_id) DO UPDATE SET session=excluded.session,updated_at=excluded.updated_at""",
                (
                    owner_platform_id,
                    session,
                    (seen_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
                ),
            )
        return True

    def private_owner_session(self, owner_platform_id: str) -> dict[str, Any] | None:
        """Return the last private delivery target observed from the configured owner."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session,updated_at FROM private_owner_sessions WHERE owner_platform_id=?",
                (owner_platform_id,),
            ).fetchone()
        return dict(row) if row else None

    def prune_user_data(
        self,
        user_id: str,
        retention_days: int,
        user_timezone: tzinfo = UTC,
        owner_platform_id: str = "",
    ) -> int:
        """Prune one Xiaomi user's health rows and the selected owner's audit rows."""
        if retention_days <= 0:
            return 0
        cutoff_date_value = datetime.now(user_timezone).date() - timedelta(
            days=max(1, retention_days)
        )
        cutoff_date = cutoff_date_value.isoformat()
        cutoff_timestamp = (
            datetime.combine(cutoff_date_value, time.min, tzinfo=user_timezone)
            .astimezone(UTC)
            .isoformat()
        )
        deleted = 0
        statements = [
            (
                "DELETE FROM daily_activity WHERE user_id=? AND date<?",
                (user_id, cutoff_date),
            ),
            (
                "DELETE FROM heart_rate_samples WHERE user_id=? AND timestamp<?",
                (user_id, cutoff_timestamp),
            ),
            (
                "DELETE FROM body_measurements WHERE user_id=? AND timestamp<?",
                (user_id, cutoff_timestamp),
            ),
            (
                "DELETE FROM sleep_sessions WHERE user_id=? AND end_at<?",
                (user_id, cutoff_timestamp),
            ),
            (
                "DELETE FROM spo2_samples WHERE user_id=? AND timestamp<?",
                (user_id, cutoff_timestamp),
            ),
            (
                "DELETE FROM stress_samples WHERE user_id=? AND timestamp<?",
                (user_id, cutoff_timestamp),
            ),
        ]
        if owner_platform_id:
            statements.append(
                (
                    "DELETE FROM alerts WHERE owner_platform_id=? AND created_at<?",
                    (owner_platform_id, cutoff_timestamp),
                )
            )
        with self._connect() as connection:
            for statement, values in statements:
                deleted += max(0, connection.execute(statement, values).rowcount)
        return deleted

    def purge_user_data(self, user_id: str, owner_platform_id: str) -> int:
        """Delete all locally cached health and delivery state for this plugin owner."""
        deleted = 0
        tables = (
            "daily_activity",
            "heart_rate_samples",
            "body_measurements",
            "sleep_sessions",
            "spo2_samples",
            "stress_samples",
        )
        with self._connect() as connection:
            for table in tables:
                deleted += max(
                    0,
                    connection.execute(
                        f"DELETE FROM {table} WHERE user_id=?", (user_id,)
                    ).rowcount,
                )
            deleted += max(
                0,
                connection.execute(
                    "DELETE FROM private_owner_sessions WHERE owner_platform_id=?",
                    (owner_platform_id,),
                ).rowcount,
            )
            deleted += max(
                0,
                connection.execute(
                    "DELETE FROM alerts WHERE owner_platform_id=?",
                    (owner_platform_id,),
                ).rowcount,
            )
            for table in ("sync_state", "sync_failures"):
                deleted += max(
                    0,
                    connection.execute(
                        f"DELETE FROM {table} WHERE user_id=?", (user_id,)
                    ).rowcount,
                )
        self.compact()
        return deleted

    def compact(self) -> None:
        """Truncate the WAL and reclaim pages after an explicit full purge."""
        connection = sqlite3.connect(self.path, timeout=30.0)
        try:
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
        finally:
            connection.close()
