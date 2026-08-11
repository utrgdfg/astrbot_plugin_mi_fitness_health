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
            r"(?i)\b([a-z0-9_-]*token|[a-z0-9_-]*(?:password|passwd|pwd)|"
            r"[a-z0-9_-]*cookie[a-z0-9_-]*|[a-z0-9_-]*secret[a-z0-9_-]*|"
            r"(?:c?user|session|owner|provider|persona)[a-z0-9_-]*id|uid|"
            r"platform_id|bot_id|session|ssecurity|set-cookie|authorization|"
            r"_nonce|signature|rc4_hash__|api[_-]?key|x-api-key|private[_-]?key)"
            r"\b[\"']?\s*"
            r"(?:=|:)\s*"
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
    message = re.sub(
        r"(?i)([\"'])(?:[A-Z]:[\\/]|\\\\)[^\"'\r\n]+\1",
        "[local path]",
        message,
    )
    message = re.sub(
        (
            r"(?i)(?<![\w])(?:[A-Z]:[\\/]|\\\\[^\\/\s]+[\\/])"
            r"(?:[^\\/\r\n:*?\"<>|]+[\\/])+[^\\/\s\r\n:*?\"<>|,;\)\]]+"
        ),
        "[local path]",
        message,
    )
    message = re.sub(
        r"(?i)([\"'])/(?!/)[^\"'\r\n]+/[^\"'\r\n]+\1",
        "[local path]",
        message,
    )
    message = re.sub(
        r"(?i)(?<![\w:/])/(?:[^/\r\n:;,\)\]]+/)+[^/\s\r\n:;,\)\]]+",
        "[local path]",
        message,
    )
    message = re.sub(
        r"(?i)\b[\w.+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[email]",
        message,
    )
    message = re.sub(
        r"(?i)\b[\w.-]+:(?:FriendMessage|GroupMessage|PrivateMessage):[\w.-]+\b",
        "[session]",
        message,
    )
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    message = " ".join(message.split())
    return message[:180] or type(error).__name__
