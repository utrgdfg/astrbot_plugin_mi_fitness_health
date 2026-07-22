# AstrBot 小米运动健康

原生 AstrBot 插件，从小米运动健康云读取**已同步的历史数据**并缓存到本地 SQLite。它不连接蓝牙设备、不是实时监护，也不构成医疗诊断。

当前支持步数、距离、活动消耗、心率、体重、部分身体成分、睡眠、血氧和压力。仅使用账号实际返回的数据；未知或缺失字段不会被伪造。

## 安装和配置

将插件 ZIP 导入 AstrBot 后，在“插件管理 → 小米运动健康 → 配置”中填写：

- `user_id`：小米 Cookies 中的 `userId`
- `pass_token`：小米 Cookies 中的 `passToken`
- `owner_platform_id`：唯一允许查询健康数据的消息平台用户 ID
- `owner_platform_instance_id`：建议同时填写 AstrBot 平台实例 ID，避免跨平台同号
- 可选设置：区域、时区、默认同步天数、自动同步和个人心率提醒阈值
- `natural_query_sync_minutes`：自然语言健康问题的最短云端刷新间隔，默认 15 分钟
- `enable_proactive_health_monitor` / `health_check_interval_minutes`：主动检查开关与间隔，默认每 30 分钟
- `late_night_start` / `late_night_end`：深夜活动检查窗口，默认 00:30–06:00
- `heart_rate_high`、`heart_rate_low`、`spo2_low`、`stress_high`、`sleep_min_minutes`：个人提示阈值；`0` 表示关闭对应指标
- `alert_consecutive_count`、`alert_cooldown_minutes`、`proactive_daily_limit`：连续样本、防刷屏冷却和每日主动消息上限

`passToken` 等同账号登录凭证。仅应由受信任管理员在 AstrBot 配置页面填写，绝不可发送到聊天、日志、截图、Issue 或模型。插件不支持明文密码登录。

### 获取凭证

1. 在浏览器打开 [account.xiaomi.com](https://account.xiaomi.com)，自行完成登录和必要验证。
2. 打开浏览器开发工具，进入 Storage/Application（或“应用”）→ Cookies → `account.xiaomi.com`。
3. 复制名为 `userId` 与 `passToken` 的 Cookie **值**，填写进插件配置。
4. 保存并重新加载插件，再由所有者执行 `/健康连接`。

验证码、二次验证或账号风控必须在浏览器中由用户自行处理。插件不会尝试绕过这些机制；凭证失效后自动同步会暂停。

## 指令

- `/健康连接`：验证云端登录，展示区域和已探测的数据类型（不显示凭证）。
- `/健康同步`：同步默认天数，额外回看 48 小时以处理延迟上传和历史修正。
- `/健康状态`：显示连接、缓存和自动同步状态。
- `/今日健康`：显示当天步数、距离、活动消耗、心率、身体测量及最近睡眠/血氧/压力。
- `/健康详情`：显示最近睡眠、血氧和压力云端记录。
- `/健康诊断`：显示最近 30 天各候选云端 key 的记录数或脱敏错误，便于定位某账号缺少的指标。
- `/心率记录 24`：显示最近指定小时数的云端心率记录（最大 168 小时）。
- `/身体数据`：显示最新体重与身体成分。
- `/健康趋势 7`：显示最近天数的文字趋势（最大 90 天）。
- `/健康帮助`：显示隐私和数据范围说明。

也不必先输入指令：插件注册了 `query_mi_fitness_health` 对话工具。例如直接私聊机器人“我昨天睡得怎么样？”、“我今天走了多少步？”或“刚同步了，看看最新心率”，模型会调用插件；缓存超过 `natural_query_sync_minutes`（或你明确要求最新数据）时会先刷新小米云端缓存，再根据可用记录回答。手机端仍要先将手环/手机数据同步到小米健康云；插件无法读取蓝牙实时数据。

所有查询仅允许配置的所有者在**私聊**中使用，群聊即使由所有者发起也不会显示健康数据。展示中的时间均为云端采集时间或同步时间，而非实时数据。

## 数据和提醒

数据库位于 AstrBot 插件数据目录，可由 `database_path` 覆盖。SQLite schema 带版本迁移，更新插件不会主动删除旧数据；活动、心率和体重使用稳定唯一键去重更新。

主动检查默认每 30 分钟拉取一个短时间窗口，再评估用户配置的阈值。心率只评估连续的**非运动、被动**样本；血氧、压力同样要求达到连续样本数；睡眠时长只对最近完成的一段睡眠提示一次。阈值为 `0` 表示关闭该指标，不使用适用于所有人的硬编码医学标准。

“太晚还没睡”不会根据“云端没有睡眠记录”猜测，因为小米睡眠数据通常醒来后才上传。插件只在设定深夜时段内、所有者最近确实发过私聊消息时，最多每晚提示一次。所有异常合并为一条私聊消息，并同时受到同事件去重、同类型冷却和全局主动消息冷却保护。

主动发送需要插件先从所有者的一次私聊中记录统一会话标识。AstrBot 的 QQ 官方 API 适配器不支持该主动发送方式；AIOCQHTTP/NapCat 等通常支持。对话查询不受此主动发送限制。

## 开发验证

测试不连接真实小米服务：

```powershell
$env:PYTHONPATH = 'G:\'
python -m unittest astrbot_plugin_mi_fitness_health.tests.test_database
```

实际小米云登录和字段兼容性仍需由用户使用自己的账号在受控环境中验证。请不要在测试或 Issue 中提交任何真实 Cookie。

## 版权

云端协议层基于 [Mi Fitness MCP](https://github.com/kubulashvili/mi-fitness-mcp) 的实现进行移植，作者 Aleksej Kubulashvili。相关 MIT 许可证与版权说明保留在 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
