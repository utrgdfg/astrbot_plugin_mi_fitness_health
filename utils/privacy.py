"""Small utilities that prevent credentials from reaching logs or messages."""

from __future__ import annotations

import re


def redact_error(error: Exception | str) -> str:
    """Return a short error reason without URLs, cookies, or credential values.

    Args:
        error: Exception or text received from a network operation.

    Returns:
        A message safe to show to the owner.
    """
    message = str(error).replace("\n", " ")
    message = re.sub(r"(?i)(passToken|userId|ssecurity|cookie)=?[^\s;,&]+", r"\1=***", message)
    message = re.sub(r"https?://\S+", "[remote URL]", message)
    return message[:180] or type(error).__name__


def mask_identifier(value: str) -> str:
    """Mask an account identifier for user-facing connection status.

    Args:
        value: Identifier to mask.

    Returns:
        A short masked identifier.
    """
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"
