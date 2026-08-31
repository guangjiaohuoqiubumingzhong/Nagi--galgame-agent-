# Nagi 0.1.0

首个 Windows x64 便携版，包含 Nagi 工作台、原头像、QLIE / YU-RIS 文本处理与已适配版本的部署功能。

## 下载和启动

- 普通 Windows 用户下载 **Nagi-0.1.0-windows-x64.zip**，完整解压到可写目录，双击 **Start Nagi.vbs**。
- 不需要预装 Python；若系统禁止 VBS，可改用 **Start Nagi.cmd**。
- 开发者可选择 **Nagi-0.1.0-source.zip**；它不包含便携运行时。
- **SHA256SUMS.txt** 用于校验附件。不要把 GitHub 自动生成的 Source code 压缩包当作便携版。

## 本版内容

- Agent 名称统一为 Nagi，恢复首页、侧栏和对话中的原头像。
- 保留工作区、会话、工具审批、模型配置与可选 MCP 支持。
- QLIE / YU-RIS 文本提取、人物译名、部分/全文翻译及指定版本的独立副本部署。
- 程序、数据目录和游戏副本支持分离；附带启动、退出和选择数据目录入口。
- README 改用引擎游戏示例，并说明后续引擎支持计划。

## 使用前须知

程序不包含 API 账户或密钥、Locale Emulator、语义检索模型、游戏资源、译文或存档。请配置自己的 API 服务，并自行确认费用及游戏处理权限。

引擎游戏示例不等于逐作适配承诺。具体支持版本、Locale Emulator 要求和升级方法请阅读包内 docs/QUICKSTART.md 与 docs/UPGRADING.md。原头像及第三方组件的授权范围见 THIRD_PARTY_NOTICES.md。

发布流程会运行离线测试、验证中文/空格路径下的便携启动与退出，并在公开前下载校验全部上传附件。
