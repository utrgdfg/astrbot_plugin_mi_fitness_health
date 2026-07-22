"""Interfaces shared by Xiaomi cloud data adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod


class DataAdapter(ABC):
    """Minimal cloud adapter interface used by plugin services."""

    @abstractmethod
    async def connect(self) -> bool:
        """Authenticate and discover usable cloud data types."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources owned by this adapter."""

    @abstractmethod
    def get_available_data_types(self) -> list[str]:
        """Return confirmed data types without probing again."""
