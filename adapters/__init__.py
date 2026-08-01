"""Cloud adapter implementations."""

from .mi_fitness_cloud import (
    MiFitnessAuthenticationError,
    MiFitnessCloudAdapter,
    MiFitnessRateLimitError,
)

__all__ = [
    "MiFitnessAuthenticationError",
    "MiFitnessCloudAdapter",
    "MiFitnessRateLimitError",
]
