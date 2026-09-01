# Nagi 0.2.0

Windows x64 便携版。本版将视觉小说翻译工作流从 QLIE / YU-RIS 扩展到五类引擎，并同步更新网页界面、任务恢复、独立副本部署和使用文档。

## 下载和启动

- 普通 Windows 用户下载 **Nagi-0.2.0-windows-x64.zip**，完整解压到可写目录，双击 **Start Nagi.vbs**。
- 不需要预装 Python；若系统禁止 VBS，可改用 **Start Nagi.cmd**。
- 开发者可选择 **Nagi-0.2.0-source.zip**；它不包含便携运行时。
- **SHA256SUMS.txt** 用于校验附件。不要把 GitHub 自动生成的 Source code 压缩包当作便携版。

## 本版内容

- 保留 QLIE 与 YU-RIS 479 的既有提取、翻译和独立副本部署流程。
- 新增 KiriKiri/KAG：支持散装 `.ks` 和标准未加密 XP3 内的 `.ks`。
- 新增 Ren'Py：支持发行包中保留的 `.rpy` 源脚本；仅含 `.rpyc` 时停止，不自动反编译。
- 新增 TyranoScript：支持标准 `data/scenario/*.ks` 项目和发行目录。
- 五类引擎共用费用确认、部分/全文翻译、任务恢复、来源哈希、控制标签校验及测试版/正式版独立目录。
- README 为每个引擎列出游戏示例；示例只说明引擎归属，不构成逐作、逐版本兼容承诺。

## 使用前须知

程序不包含 API 账户或密钥、Locale Emulator、语义检索模型、游戏资源、译文或存档。请配置自己的 API 服务，并自行确认费用及游戏处理权限。

作品自定义脚本方言、加密 XP3、字体插件及不同发行版本仍可能需要单独适配。具体支持边界和部署条件请阅读包内 `docs/QUICKSTART.md` 与 `docs/REFERENCE.md`。原头像及第三方组件的授权范围见 `THIRD_PARTY_NOTICES.md`。

发布流程会运行离线测试、验证中文及空格路径下的便携启动与退出，并在公开前下载校验全部上传附件。
