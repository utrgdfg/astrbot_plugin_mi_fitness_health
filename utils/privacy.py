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
    message = str(error)
    message = re.sub(
        r"(?im)^\s*(authorization|set-cookie|cookie|x-api-key|api-key)\s*:\s*.*$",
        r"\1: ***",
        message,
    )
    message = re.sub(
        r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+",
        "authorization=***",
        message,
    )
    message = re.sub(
        (
            r"(?i)\b(passToken|pass_token|serviceToken|accessToken|refreshToken|"
            r"userId|cUserId|ssecurity|cookie|set-cookie|authorization|_nonce|"
            r"signature|rc4_hash__|api[_-]?key|x-api-key|client[_-]?secret|"
            r"secret[_-]?key|private[_-]?key)\b[\"']?\s*(?:=|:)\s*"
            r"(?:[\"'][^\"']*[\"']|[^\s;,&]+)"
        ),
        r"\1=***",
        message,
    )
    message = re.sub(
        r"(?i)\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{12,})\b",
        "***",
        message,
    )
    message = re.sub(r"https?://\S+", "[remote URL]", message)
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    message = " ".join(message.split())
    return message[:180] or type(error).__name__
