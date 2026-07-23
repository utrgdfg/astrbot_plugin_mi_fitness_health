"""Owner-only access checks shared by commands and conversational tools."""

from __future__ import annotations


_WRAPPERS = (
    ("[", "]"),
    ("【", "】"),
    ("「", "」"),
    ("“", "”"),
    ('"', '"'),
    ("'", "'"),
    ("`", "`"),
)


def normalize_identifier(value: object) -> str:
    """Normalize identifiers commonly copied from AstrBot's ``/sid`` output."""
    text = str(value or "").strip()
    lowered = text.casefold()
    for label in ("uid", "bot id", "botid"):
        for separator in (":", "："):
            prefix = f"{label}{separator}"
            if lowered.startswith(prefix):
                text = text[len(prefix) :].strip()
                lowered = text.casefold()
                break

    changed = True
    while changed and len(text) >= 2:
        changed = False
        for left, right in _WRAPPERS:
            if text.startswith(left) and text.endswith(right):
                text = text[len(left) : len(text) - len(right)].strip()
                changed = True
                break
    return text


def owner_identifiers_match(
    owner_platform_id: object,
    owner_platform_instance_id: object,
    sender_id: object,
    platform_id: object,
) -> bool:
    """Match the required owner UID and Bot instance ID against exact event IDs."""
    owner_id = normalize_identifier(owner_platform_id)
    instance_id = normalize_identifier(owner_platform_instance_id)
    return (
        bool(owner_id)
        and bool(instance_id)
        and str(sender_id or "") == owner_id
        and str(platform_id or "") == instance_id
    )


def owner_access_denial_reason(
    *,
    owner_platform_id: object,
    owner_platform_instance_id: object,
    sender_id: object,
    platform_id: object,
    message_type: str,
    is_private: bool,
) -> str | None:
    """Return one precise, credential-safe reason when owner access is denied."""
    owner_id = normalize_identifier(owner_platform_id)
    instance_id = normalize_identifier(owner_platform_instance_id)
    current_type = message_type or "未知"
    if not owner_id:
        return "健康数据所有者尚未配置，请由管理员设置 owner_platform_id。"
    if not instance_id:
        return (
            "所有者平台实例尚未配置，请由管理员设置 "
            "owner_platform_instance_id 后再查询。"
        )
    if str(sender_id or "") != owner_id:
        return (
            "授权失败：当前发送者 UID 与 owner_platform_id 不匹配"
            f"（当前消息类型：{current_type}）；这不是群聊识别问题。"
        )
    if str(platform_id or "") != instance_id:
        return (
            "授权失败：当前 Bot ID 与 owner_platform_instance_id 不匹配"
            f"（当前消息类型：{current_type}）；这不是群聊识别问题。"
        )
    if not is_private:
        return (
            f"当前消息类型为 {current_type}；为避免健康数据公开，"
            "健康查询只能在与机器人的私聊中使用。"
        )
    return None
