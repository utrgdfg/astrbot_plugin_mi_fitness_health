"""Offline time-boundary tests for cached cloud data."""

from __future__ import annotations

import asyncio
import unittest

from astrbot_plugin_mi_fitness_health.services.query_service import QueryService


class _RecordingDatabase:
    def __init__(self):
        self.cutoff = ""

    def heart_rates_since(self, user_id, cutoff, limit=100):
        self.cutoff = cutoff
        return []


class QueryServiceTest(unittest.TestCase):
    def test_heart_rate_cutoff_is_utc(self) -> None:
        """UTC storage must not be lexically compared against +08:00 text."""
        database = _RecordingDatabase()
        service = QueryService(database, "user", "Asia/Shanghai")
        asyncio.run(service.heart_rates(24))
        self.assertTrue(database.cutoff.endswith("+00:00"))
