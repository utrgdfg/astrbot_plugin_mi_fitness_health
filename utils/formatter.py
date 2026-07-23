"""User-facing formatting that always identifies cloud collection timestamps."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo


def local_timestamp(value: object, user_timezone: tzinfo = UTC) -> str:
    """Format a stored UTC timestamp in the configured user timezone."""
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value))
        )
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        local = parsed.astimezone(user_timezone)
    except (TypeError, ValueError):
        return str(value) if value else "未知"
    offset = local.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else "本地时区"
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')}（{zone}）"


def measurement_text(row: dict | None, user_timezone: tzinfo = UTC) -> str:
    """Format an optional body measurement without inventing missing fields."""
    if not row:
        return "最近身体测量：暂无数据"
    labels = (
        ("weight_kg", "体重", "kg"),
        ("bmi", "BMI", ""),
        ("body_fat_pct", "体脂", "%"),
        ("muscle_mass_kg", "肌肉量", "kg"),
        ("water_pct", "水分", "%"),
        ("basal_metabolism_kcal", "基础代谢", "kcal"),
        ("metabolic_age", "身体年龄", ""),
    )
    values = [
        f"{label}：{row[key]}{unit}"
        for key, label, unit in labels
        if row.get(key) is not None
    ]
    return (
        f"最近身体测量（数据采集时间：{local_timestamp(row['timestamp'], user_timezone)}）\n"
        + "，".join(values)
    )


def today_text(
    activity: dict | None,
    heart_rates: list[dict],
    measurement: dict | None,
    user_timezone: tzinfo = UTC,
) -> str:
    """Format today's cached cloud summary with latest data timestamps."""
    lines = ["今日健康（小米云端已同步数据，非实时监护）"]
    if activity:
        lines.append(
            f"步数：{activity['steps']}｜距离：{activity['distance_m']:.0f} m｜活动消耗：{activity['active_kcal']:.0f} kcal"
        )
        lines.append(
            "活动数据采集时间："
            + local_timestamp(activity["collected_at"], user_timezone)
        )
    else:
        lines.append("步数/距离/活动消耗：暂无今日数据")
    if heart_rates:
        values = [item["bpm"] for item in heart_rates]
        lines.append(
            f"今日心率（本地自然日）：最新 {heart_rates[0]['bpm']} bpm（数据采集时间：{local_timestamp(heart_rates[0]['timestamp'], user_timezone)}），平均 {sum(values) / len(values):.0f}，最高 {max(values)}，最低 {min(values)}"
        )
    else:
        lines.append("今日心率（本地自然日）：暂无数据")
    lines.append(measurement_text(measurement, user_timezone))
    return "\n".join(lines)
