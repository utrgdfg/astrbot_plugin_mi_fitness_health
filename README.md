# AstrBot 小米运动健康插件

这是一个原生 AstrBot 插件，计划读取已同步到小米运动健康云的数据；它不连接手环蓝牙，也不提供实时监护或医疗诊断。

## 当前阶段

阶段 2 已提供可加载的插件入口、配置页、所有者权限守卫和真实的小米云 token 登录、`ssecurity`、nonce、请求签名、RC4 加解密及最近 30 天数据类型探测。SQLite 同步、健康查询与提醒将在后续阶段实现。

## 安全配置

在 AstrBot 的“插件管理 → 小米运动健康 → 配置”中填写 `user_id`、`pass_token` 和 `owner_platform_id`。保存后重新加载插件，并由数据所有者发送 `/健康连接` 验证连接。

### 获取 `userId` 与 `passToken`

1. 在浏览器打开 [account.xiaomi.com](https://account.xiaomi.com)，自行完成小米账号登录和任何验证步骤。
2. 打开浏览器开发工具，进入 Storage/Application（或“应用”）中的 Cookies，选择 `account.xiaomi.com`。
3. 找到并复制 Cookies 表中名为 `userId` 和 `passToken` 的**值**，分别粘贴到插件配置页相应字段。
4. 保存配置后重新加载插件，使用 `/健康连接`。连接响应不会显示任何凭证。

`pass_token` 是登录凭证，绝不能发到聊天、日志、截图、Issue 或交给模型。插件仅支持用户自行获取的 `userId` 与 `passToken`，不支持明文账号密码登录。若触发验证码、二次验证或账号风控，插件不会尝试绕过，须由用户在浏览器处理后重新获取 Cookie。

也可在受控部署环境中使用 `MI_FITNESS_USER_ID` 与 `MI_FITNESS_PASS_TOKEN` 环境变量作为后备；AstrBot 配置优先。

## 版权

本项目会移植 [Mi Fitness MCP](https://github.com/kubulashvili/mi-fitness-mcp) 的小米云协议数据层，作者为 Aleksej Kubulashvili。其 MIT 许可证和版权说明已保留在 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
