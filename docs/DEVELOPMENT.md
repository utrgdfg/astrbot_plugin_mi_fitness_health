# 开发与维护说明

本文面向插件维护者。普通用户请阅读项目首页的 [README](../README.md)。

## 插件定位

插件读取小米运动健康云端已经上传的历史记录，为 AstrBot 私聊中的自然对话提供生活数据上下文。它不连接蓝牙设备、不提供实时监护，也不进行医疗诊断。

## 数据与同步

- SQLite 数据库默认位于 AstrBot 插件数据目录，可通过高级配置覆盖路径。
- 当前 schema 版本为 v7，迁移会保留健康记录，并移除已无运行时用途的旧 `care_deliveries` 表。
- 健康记录和同步状态按小米 `userId` 隔离。
- 主动关心记录和保留期清理按 AstrBot 所有者隔离。
- 默认保留最近 90 天数据；配置为 `0` 时不自动清理。
- 数据库启用 `secure_delete`，新建文件在支持 POSIX 权限的系统上设置为 `0600`。
- SQLite 主文件、WAL、宿主机备份、快照和 SSD 历史不等于加密存储；部署者仍需保护 AstrBot 数据目录或使用磁盘加密。

活动同步会按配置的用户时区分桶，并对同一分钟的候选记录取最大值，降低手机和手环重复上传造成的重复计数。独立卡路里记录只能修正已有步数日期，不能创建零步活动日。

心率兼容 `heart_rate`、`heartrate`、`hr` 和静息心率候选 key；血氧兼容 `spo2` 与 `blood_oxygen`。睡眠、血氧和压力的字段可能随账号、设备、区域和小米服务版本变化。

## 并发和失败处理

- 连接、诊断、同步和本地清除共用一个操作锁。
- 自然对话的并发刷新会合并为单个后台任务。
- 相关对话在缓存为空或过期时最多等待刷新 5 秒；超时通过 `asyncio.shield` 保留后台任务，当前回复改用已有缓存。
- 主动关心检查和普通自动同步是独立任务；前者只评估本地状态，不发起小米云同步，后者仅在用户明确开启时运行。
- 深夜规则只产生候选 finding；发送前必须再经过上下文模型的严格布尔闸门，模型失败或上下文为空时 fail closed。
- 云端读取设有五分钟安全时限和单数据集记录数量上限。
- 已经开始的 SQLite 工作线程不会被伪装成“已取消”；事务完成前不会释放同步锁。
- 单个数据集失败不会删除其他数据集的缓存。
- 明确鉴权失败会暂停后台同步；普通网络错误使用有上限的退避重试。

## 权限和隐私

- 所有健康数据入口同时校验使用者 UID、Bot ID 和私聊消息类型。
- 群聊不会返回健康数据。
- 向 LLM 提供健康摘要需要用户显式开启授权，默认关闭。
- 用户关注文本会被限长、压缩为单行并作为不可信数据隔离。
- 深夜闸门可通过 AstrBot 官方会话管理器、平台消息历史管理器或混合模式读取当前所有者私聊。配置范围为 0～50 条，总量仍限制为 4000 字符；可选择排除 assistant/Bot 文本，图片、工具结果和系统消息始终不会传入。刚收到但尚未落入 AstrBot 历史的所有者文本仅在插件内存中短暂补充，不写入插件 SQLite。
- 日志和用户可见错误会脱敏 Cookie、Token、授权头、签名参数和常见模型密钥格式。
- 主动消息只发送到已记录且与配置 Bot ID 匹配的统一会话标识。

## 配置页约定

配置文件仍使用稳定的内部键名，以兼容已有安装；README 和配置页说明使用面向用户的中文名称。模型与人格字段使用 AstrBot 官方的 `select_provider` 和 `select_persona` 选择器。

`context_decision_provider_id` 是可选的对话分类模型。它只接收经过长度限制和边界转义的当前所有者私聊消息，并返回严格 JSON；不接收小米生活数据。有效结果最多选择两个数据类别，随后仍由插件根据每类缓存的新鲜度决定是否联网刷新。模型失败或输出无效时按 1、5、15 分钟退避并回退到本地关键词规则；有效结果立即清零退避。原始消息的强制刷新意图单独保留，不依赖模型生成的焦点文本。

`context_decision_prompt` 和 `proactive_decision_prompt` 使用官方 `text` 配置类型。前者只定义生活数据调用任务，后者定义深夜候选时机的发送取舍；代码固定追加允许类别、JSON 输出协议、提示注入隔离和 fail-closed 规则，配置提示词不能移除这些边界。

`proactive_context_source` 支持 `conversation_history`、`platform_message_history` 与 `hybrid`。平台流水通过 AstrBot 官方 `message_history_manager.get()` 读取；读取前会将完整 UMO 与插件数据库中由所有者事件绑定的私聊会话进行精确匹配，而不是假设 UMO 第三段必然等于 UID。读取失败或为空时回退到当前对话历史。`proactive_context_prompt` 支持 `{{context_lines}}` 占位符，缺少占位符时插件会自动在末尾追加序列化上下文。

主动关心使用 `proactive_reminder_provider_id` 选择的模型执行两步流程：先读取受限的当前会话文字上下文并输出 `{"send_care": boolean}`，通过后才结合人格生成最终消息。留空 provider 时两步均沿用当前私聊模型。

`passToken` 可以从插件配置读取，也可以在配置留空时通过 `MI_FITNESS_PASS_TOKEN` 环境变量提供。配置页密码遮罩不等于静态加密；AstrBot 配置、SQLite、WAL、备份和环境变量都需要按宿主机权限模型保护。部署者应限制配置目录和数据目录权限，并在需要时使用磁盘加密。

## 本地验证

测试不连接真实小米服务。在仓库目录的上一级执行：

```powershell
python -m unittest discover -s .\astrbot_plugin_mi_fitness_health\tests -v
python -m ruff check .\astrbot_plugin_mi_fitness_health
python -m ruff format --check .\astrbot_plugin_mi_fitness_health
```

测试覆盖授权边界、云端字段解析、睡眠链路、心率范围、活动覆盖保护、SQLite 迁移、同步并发、超时语义、自然刷新、主动关心和隐私脱敏。

真实小米账号、设备字段和区域兼容性必须由维护者在受控环境验证。不要把真实 Cookie、原始健康数据或个人截图加入 fixture、日志或 Issue。

## 发布检查

发布前至少确认：

1. `metadata.yaml`、CHANGELOG 和发布标签版本一致。
2. `_conf_schema.json` 可以解析，并且配置页名称与 README 一致。
3. 完整单元测试、Ruff、格式和真实 AstrBot 导入检查通过。
4. 仓库中不存在 Cookie、`passToken`、用户 UID、Bot ID、个人截图或本地数据库。
5. 安装说明仍与 AstrBot 当前 WebUI 的“从链接安装”和“从文件安装”入口一致。
