# 更新日志

本项目遵循语义化版本。小米运动健康云数据来自手机端已经上传的历史记录；所有版本均不提供蓝牙实时监护，也不构成医疗诊断。

## [v0.5.9] - 2026-07-23

### 调整

- 按 AstrBot 开发原则将小米云端适配器的日志接入 AstrBot 官方 logger；离线测试使用最小 API 桩，不影响运行时日志。

## [v0.5.8] - 2026-07-23

### 修复

- 清理文档、配置提示、测试和发布包中的个人化示例；所有公开示例均使用不可关联到用户的占位值。

## [v0.5.7] - 2026-07-23

### 调整

- 插件定位调整为日常对话增强：移除心率、血氧、压力、睡眠时长等阈值式“异常”评估和主动告警链路，不再把小米数据当作专业健康检测。
- Bot 会在睡眠、疲劳、早晚问候、加班、运动和深夜聊天等日常对话线索出现时，在后台准备最小必要的已同步生活数据，并只在有助于自然交流时融入回复。
- 后台主动消息仅保留深夜仍有私聊活动等自然关心时机，继续受事件去重、冷却和每日上限保护；措辞仍由所选模型与人格生成。
- 配置页、帮助文本和 README 统一改为“生活数据 / 日常关心”表述，并清理不再使用的体征阈值配置。
- 补充小米手机活动数据说明：请关闭手机侧“运动健康”参与的计步/活动数据来源、保留手环同步应用，避免手机本地合并结果未完整上传云端而使步数、距离和活动消耗与插件不一致。
- 按 AstrBot 官方 WebUI 更新安装说明，分别提供“从链接安装”和“从文件安装”步骤，并补充插件市场安装入口。
- 按官方开发指南完善插件市场短描述、稳定作者命名空间，并改用 AstrBot 官方日志接口。

## [v0.5.6] - 2026-07-23

### 修复

- 依据 AstrBot 插件配置手册，为四项模型/人格配置增加 `_special: select_provider` 与 `_special: select_persona`。插件配置页现在直接下拉展示已配置的聊天模型和人格，不再要求手动复制 Provider ID 或 `persona_id`。

## [v0.5.5] - 2026-07-23

### 新增

- 新增 `health_dialogue_provider_id` 与 `health_dialogue_persona_id`：可为睡眠、心率等自然语言健康对话单独选择 AstrBot 聊天模型与人格。配置后先生成受事实约束的健康解读草稿，再交给正常对话流程回复。
- 新增 `proactive_reminder_provider_id` 与 `proactive_reminder_persona_id`：可为后台主动关心独立选择模型和人格；留空则继续使用所有者私聊的当前模型和人格。
- 配置页和 README 明确说明自定义模型会接收相应的已同步健康数据，便于用户按隐私边界选择服务提供商。

## [v0.5.4] - 2026-07-23

### 改进

- 主动健康流程改为“后台同步与规则判定 → 命中条件后调用当前私聊的聊天模型与人格 → 发送一两句自然关心”。不再把原始规则说明、技术免责声明和固定模板直接发给用户；模型或人格不可用时宁可跳过，不会退化为模板消息。
- 主动判断仍只基于配置阈值、足够新的云端样本、深夜时段和近期私聊活动，并保留事件去重、冷却和每日上限；LLM 仅负责措辞，不能自行触发提醒或编造健康数据。
- 根据 AstrBot 插件约定加入根目录 `logo.png`，使用用户提供图像制作的方形插件图标。

## [v0.5.3] - 2026-07-22

### 修复

- 修复“今日健康”和自然语言中的当日/昨日心率统计：按 `user_timezone` 的本地自然日（`00:00–24:00`）读取完整心率样本，不再误用滚动 24 小时窗口或默认 100 条记录上限。
- 修复“健康趋势”的日均心率按 UTC 日期归类的问题，改为按用户本地日期计算，并继续排除运动心率样本以保持趋势口径。
- 明确输出“今日心率（本地自然日）”，便于与小米运动健康 App 当日页面对照。

## [v0.5.2] - 2026-07-22

### 修复

- 将活动、心率、体重、血氧、压力、心率记录、主动提示与“最近同步完成时间”统一按 `user_timezone` 显示，不再直接向用户展示数据库中的 UTC `+00:00` 原始值。
- 明确区分“数据采集时间”和“最近同步完成时间”，避免把小米云记录的采样时刻误认为插件执行同步的时刻。

## [v0.5.1] - 2026-07-22

### 修复

- 修复直接复制 `/sid` 输出时，UID 或 Bot ID 因携带标签、空白、括号/引号而误拒绝真实所有者私聊的问题；`owner_platform_instance_id` 仍为必填，并继续与 UID 执行精确双重校验。
- 使用 AstrBot 公共 `event.get_message_type()` 识别 `FriendMessage`，并分别报告 UID、Bot ID 与消息类型不匹配。LLM 工具不再用同一句话混淆“身份不匹配”和“群聊”。
- 配置中的 UID 与 Bot ID 会去除首尾空白、`/sid` 标签和常见成对括号/引号，避免直接复制 `[示例 UID]` 或 `「示例 Bot 名称」` 后授权失败。
- 小米云分页返回重复游标或达到安全上限时，睡眠等独立样本会保留已经取得并去重的记录；步数/热量每日聚合仍拒绝不完整结果，避免用部分页覆盖正确缓存。
- `/健康同步` 显示单项数据的脱敏失败原因；自然语言睡眠查询在缓存为空时只说明“暂无已同步记录”，不再诱导模型声称设备不支持。
- 修复初次登录返回 401/403 时未进入凭证失效暂停逻辑，以及睡眠/压力评分为合法 `0` 时被误当成缺失的问题。

### 验证

- 增加私聊授权回归矩阵：缺失/正确/错误 Bot ID、正确/错误 UID、`FriendMessage` 与 `GroupMessage`，并确认运行时事件 ID 不会被配置清洗逻辑折叠。
- 增加 `sleep` 云端解析 → 同步服务 → SQLite → 自然语言快照的完整离线回归测试。

## [v0.5.0] - 2026-07-22

### 新增

- 增加主动健康检查，默认每 30 分钟刷新一次短时间窗口，仅在存在有依据的情况且不在冷却期时私聊所有者。
- 增加个人心率、血氧、压力与睡眠时长阈值。`0` 表示关闭对应指标，不使用适用于所有人的硬编码医学阈值。
- 心率只评估新鲜、连续、非运动的被动样本；血氧和压力要求连续新鲜样本；睡眠只评估最近 36 小时内完成的一段记录。
- 增加深夜私聊活动检查。它只依据所有者近期确实发送过私聊消息，不会把“云端没有睡眠记录”推断成“没有睡觉”，每晚最多触发一次。
- 增加同事件去重、同类型冷却、全局主动消息冷却与每日上限。多项情况会合并为一条固定安全文本，并且仅在平台确认发送成功后记录提醒状态。
- 增加必填的 `owner_platform_instance_id` 校验，避免不同平台出现相同用户 ID 时越权读取健康数据。

### 对话、数据与可靠性

- 可在所有者私聊的普通对话中调用 `query_mi_fitness_health`，直接询问昨天睡眠、今日步数或最近心率；缓存过期时按需刷新小米云，不再要求先输入同步命令。
- 稳定读取步数、距离、活动消耗、心率、体重与部分身体成分，并增加睡眠、血氧和压力的兼容性读取；后三类取决于账号实际暴露的云端 schema。连接状态会显示最近 30 天实际探测到的类型。
- 保留 `resting_heart_rate` 心率回退，兼容部分账号没有标准心率采样 key 的情况。
- 对话快照只向模型提供本次问题相关的类别，减少无关健康信息暴露。
- SQLite 升级至 schema v4，无损增加提醒事件去重字段与索引，并启用 WAL 和 30 秒 busy timeout。
- 临时云端错误采用有上限的退避重试；凭证失效、需要验证或触发风控时暂停后台检查，等待重新授权。

### 隐私与安全

- 所有健康数据命令、LLM 工具和上下文注入只允许配置的所有者在私聊中使用；群聊不会返回健康数据。
- 主动消息只发送到插件最近记录的所有者私聊会话，使用固定模板，不交给 LLM 二次生成。
- `pass_token` 配置声明为密码字段。它等同登录凭证，不得发送到聊天、日志、截图或 Issue；请只在受信任的 AstrBot 管理页面填写。输入遮罩不等于加密存储；若所用 AstrBot WebUI 未正确遮罩或需要更严格保护，可留空配置并改用 `MI_FITNESS_PASS_TOKEN` 环境变量。

### 从 v0.4.x 升级

- 数据库会自动迁移到 schema v4，不会主动删除已有健康记录。
- 新增 12 项配置：`owner_platform_instance_id`、`enable_proactive_health_monitor`、`health_check_interval_minutes`、`proactive_daily_limit`、`enable_late_night_activity_check`、`late_night_start`、`late_night_end`、`late_night_activity_window_minutes`、`spo2_low`、`stress_high`、`sleep_min_minutes`、`alert_data_max_age_minutes`。
- `enable_health_alerts` 的默认值由 `false` 改为 `true`，但心率、血氧、压力和睡眠阈值默认均为 `0`，因此升级后这些阈值提示仍然关闭。
- `enable_proactive_health_monitor` 与 `enable_late_night_activity_check` 默认开启。所有者需先私聊机器人一次，插件才能记录主动发送目标。
- 如果要停止周期性小米云请求，需同时关闭 `enable_proactive_health_monitor` 和 `enable_auto_sync`；自然语言健康查询仍可能按需刷新。
- v0.5.0 起必须配置 `owner_platform_instance_id`；所有者私聊发送 `/sid` 后，将 `UID` 填入 `owner_platform_id`、将 `Bot ID` 填入该项。任一项为空时，所有健康数据命令、对话工具和主动监控都不会启用。升级后群聊健康查询也会被拒绝，这是有意收紧的隐私边界。

### 已知限制

- 手机端仍需先把手环或手机数据同步到小米运动健康云。插件会额外回看 48 小时以处理延迟上传和历史修正；睡眠数据通常在醒来后才上传。
- AstrBot 的 QQ 官方 API 适配器不支持当前主动发送方式；AIOCQHTTP/NapCat 等通常支持。普通私聊中的健康查询不受此限制。
- 不同小米账号、区域与设备可能暴露不同的云端 key；缺失或未知字段不会被伪造。

[v0.5.7]: https://github.com/utrgdfg/astrbot_plugin_mi_fitness_health/releases/tag/v0.5.7
[v0.5.6]: https://github.com/utrgdfg/astrbot_plugin_mi_fitness_health/releases/tag/v0.5.6
[v0.5.5]: https://github.com/utrgdfg/astrbot_plugin_mi_fitness_health/releases/tag/v0.5.5
[v0.5.4]: https://github.com/utrgdfg/astrbot_plugin_mi_fitness_health/releases/tag/v0.5.4
[v0.5.3]: https://github.com/utrgdfg/astrbot_plugin_mi_fitness_health/releases/tag/v0.5.3
[v0.5.2]: https://github.com/utrgdfg/astrbot_plugin_mi_fitness_health/releases/tag/v0.5.2
[v0.5.1]: https://github.com/utrgdfg/astrbot_plugin_mi_fitness_health/releases/tag/v0.5.1
[v0.5.0]: https://github.com/utrgdfg/astrbot_plugin_mi_fitness_health/releases/tag/v0.5.0
