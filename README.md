# AstrBot 小米运动健康

原生 AstrBot 插件，从小米运动健康云读取**已同步的历史数据**并缓存到本地 SQLite。它不连接蓝牙设备、不是实时监护，也不构成医疗诊断。

当前稳定读取步数、距离、活动消耗、心率、体重及部分身体成分；同时提供睡眠、血氧和压力的兼容性读取。这三类数据的云端 key/字段会因账号、设备、区域和小米服务版本不同而变化，只有通过账号实际返回并通过校验的记录才会展示；未知或缺失字段不会被伪造。

## 安装和配置

将插件 ZIP 导入 AstrBot 4.18.1 或更高的 4.x 版本后，在“插件管理 → 小米运动健康 → 配置”中填写：

- `user_id`：小米 Cookies 中的 `userId`
- `pass_token`：小米 Cookies 中的 `passToken`
- `owner_platform_id`：必填。所有者先私聊机器人发送 `/sid`，复制返回的 `UID`
- `owner_platform_instance_id`：必填。复制同一条 `/sid` 结果中的 `Bot ID`，与 `UID` 一起严格校验，避免跨平台同号越权
- 可选设置：区域、时区、默认同步天数、自动同步和个人心率提醒阈值
- `natural_query_sync_minutes`：自然语言健康问题的最短云端刷新间隔，默认 15 分钟
- `enable_proactive_health_monitor` / `health_check_interval_minutes`：主动检查开关与间隔，默认每 30 分钟
- `enable_late_night_activity_check` / `late_night_start` / `late_night_end` / `late_night_activity_window_minutes`：深夜活动检查开关、时段与近期活动窗口
- `heart_rate_high`、`heart_rate_low`、`spo2_low`、`stress_high`、`sleep_min_minutes`：个人提示阈值；`0` 表示关闭对应指标
- `alert_data_max_age_minutes`、`alert_consecutive_count`、`alert_cooldown_minutes`、`proactive_daily_limit`：样本新鲜度、连续样本、防刷屏冷却和每日主动消息上限

主动健康检查和深夜私聊活动检查默认开启。如果不需要周期性云端请求，请同时关闭 `enable_proactive_health_monitor` 与 `enable_auto_sync`；自然语言健康查询仍可能按需刷新。

`owner_platform_id` 和 `owner_platform_instance_id` 均必填；任一项为空时，所有健康数据命令、普通对话工具和主动监控都会保持禁用。v0.5.1 修复了直接复制 `/sid` 输出时因标签或括号导致真实私聊被误拒绝的问题：配置兼容 `[2914544254]`、`「银河系」`、`UID: [...]`、`Bot ID: [...]` 等常见格式；运行时事件 ID 仍执行精确双重校验。

`passToken` 等同账号登录凭证。仅应由受信任管理员在 AstrBot 配置页面填写，绝不可发送到聊天、日志、截图、Issue 或模型。Schema 已请求密码输入样式，但遮罩不等于加密存储，不同 AstrBot WebUI 版本也可能仍然回显；需要更严格保护时，请将配置页的 `pass_token` 留空并通过进程环境变量 `MI_FITNESS_PASS_TOKEN` 提供。插件不支持明文账号密码登录。

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

所有查询仅允许配置的所有者在**私聊**中使用，群聊即使由所有者发起也不会显示健康数据。插件使用 AstrBot 的公开消息类型接口识别 `FriendMessage`；UID、Bot ID 和消息类型不匹配会分别说明，不再把私聊授权失败笼统描述成群聊。展示中的时间均按 `user_timezone` 转换，并明确标注为云端数据采集时间或插件同步完成时间，而非实时数据。

## 数据和提醒

数据库位于 AstrBot 插件数据目录，可由 `database_path` 覆盖。SQLite schema 带版本迁移，更新插件不会主动删除旧数据；活动、心率和体重使用稳定唯一键去重更新。

主动检查默认每 30 分钟拉取一个短时间窗口，再评估用户配置的阈值。心率只评估连续的**非运动、被动**样本；血氧、压力同样要求达到连续样本数；睡眠时长只对最近完成的一段睡眠提示一次。阈值为 `0` 表示关闭该指标，不使用适用于所有人的硬编码医学标准。

“太晚还没睡”不会根据“云端没有睡眠记录”猜测，因为小米睡眠数据通常醒来后才上传。插件只在设定深夜时段内、所有者最近确实发过私聊消息时，最多每晚提示一次。所有异常合并为一条私聊消息，并同时受到同事件去重、同类型冷却和全局主动消息冷却保护。

主动发送需要插件先从所有者的一次私聊中记录统一会话标识。AstrBot 的 QQ 官方 API 适配器不支持该主动发送方式；AIOCQHTTP/NapCat 等通常支持。对话查询不受此主动发送限制。

睡眠、血氧和压力属于账号差异较大的兼容性数据源。可用 `/健康诊断` 查看最近 30 天候选 key 的脱敏记录数；`/健康同步` 会显示每一类数据的读取数或脱敏失败原因。自然对话中“暂无已同步记录”只代表本地缓存当前没有通过校验的数据，不代表设备不支持或手机端无法同步。诊断有记录但同步仍为空时，请在 Issue 中只提供 key 名、记录数和脱敏错误，不要提交原始健康数据或 Cookie。

## 开发验证

测试不连接真实小米服务：

```powershell
# 在仓库目录的上一级执行；仓库目录名保持 astrbot_plugin_mi_fitness_health
python -m unittest discover -s .\astrbot_plugin_mi_fitness_health\tests -v
```

实际小米云登录和字段兼容性仍需由用户使用自己的账号在受控环境中验证。请不要在测试或 Issue 中提交任何真实 Cookie。

## 版权

云端协议层基于 [Mi Fitness MCP](https://github.com/kubulashvili/mi-fitness-mcp) 的实现进行移植，作者 Aleksej Kubulashvili。相关 MIT 许可证与版权说明保留在 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
