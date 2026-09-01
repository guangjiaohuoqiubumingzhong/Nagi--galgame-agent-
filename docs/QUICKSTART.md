# Nagi 0.1.0 快速入门

## 下载与启动

普通用户选择 Releases 附件 **Nagi-0.1.0-windows-x64.zip**。GitHub 自动生成的
Source code ZIP 和 Nagi-0.1.0-source.zip 是源码，不能当成便携程序双击运行。
首版支持 Windows 10/11 x64；不提供 ARM64、32 位或 macOS/Linux 便携包。

1. 用 `Get-FileHash 文件名 -Algorithm SHA256` 对照同一发行页的 SHA256SUMS.txt。
2. 将整个 ZIP 解压到自己的可写目录，不要在压缩包内运行。
3. 双击 **Start Nagi.vbs**，浏览器会打开本机工作台；无需 Python、Git 或 Codex。
   Windows 禁用 VBScript 时使用 **Start Nagi.cmd**。若系统阻止下载文件，先核对来源与哈希；
   不要全局关闭安全软件或修改系统执行策略。
4. 重复双击会打开同一个服务。退出用 **Stop Nagi.cmd** 或页面底部“使用说明 → 退出 Nagi 服务”。
   先停止正在执行的任务，等待其保存完成；关闭浏览器本身不会停止后端。

启动失败会显示错误；日志位置在“使用说明 → 数据与日志位置”，默认 `data/.nagi/web/server.log`。
如果程序目录不可写，双击 **Choose Data Folder.cmd** 选择一个已有的可写目录；选择会保存。
也可运行 `powershell -File .\launch-nagi.ps1 -DataDirectory "D:\MyNagiData"`。
端口冲突用 `-Port 8766`，启动和退出时指定同一端口；不会把其他服务误认为 Nagi。

## 第一次配置

程序不含开发者账户或 API 密钥。先阅读首次使用说明，再到 **设置 → 模型** 填入自己的
服务地址、模型 ID、密钥。确认服务价格和额度；API 提供方会收到你发送的内容及相关上下文。
保存配置不联网；测试连接、发送 Agent 对话、翻译会发起请求，可能产生费用。
Windows 密钥由当前登录用户加密；换用户或换电脑需要重新输入。

## 游戏翻译

1. 选择“视觉小说翻译”，选择自己有权处理的游戏与文本保存目录。
2. 点击提取：只读原游戏，不调用模型；人物信息与原文一同提取。
3. 选择部分翻译（开场前 50 条对白/旁白）或全文翻译，确认费用后开始。
   程序先自动统一已识别人物译名，再进行正文翻译；旧任务不自动重译。
4. 需要可玩版时，在翻译页选择你本机的 Locale Emulator 目录并保存。
   [官方获取入口](https://github.com/xupefei/Locale-Emulator/releases)。它不随 Nagi 分发。
5. 完成第二步后执行“嵌入游戏内”。当前支持 QLIE、YU-RIS 479、KiriKiri/KAG、Ren'Py
   和 TyranoScript，均校验原始脚本、本次译文与控制标签，并生成独立游戏副本。
   KiriKiri 当前覆盖散装 `.ks` 和标准未加密 XP3；Ren'Py 要求发行包保留 `.rpy`；
   TyranoScript 要求标准 `data/scenario/*.ks`。不满足边界时会停止，不会生成伪可玩入口。
   QLIE 会优先提取实际生效的补丁脚本；若旧译文来自被覆盖的基础脚本，会提示重新提取和翻译。
   原先仅保存独立译文、缺少完整预览或来源记录的旧任务，不能直接作为可玩部署输入。

文本按 `translations/引擎/游戏/任务` 保存；可玩副本按
`playable/引擎/游戏/translation-test` 或 `translation-formal` 保存。
可玩游戏根目录的“启动测试版 / 启动正式版”分别指向对应版本。
同类旧副本会保存在 `playable/.history`，包括存档，不会直接删除。

QLIE 与 YU-RIS 的字体桥接副本仍需要 Nagi 的 Python/Frida 运行时；三种文本脚本引擎使用
引擎原生启动入口，不依赖字体桥接运行时。
移动 Nagi 后，双击副本中的 **重新定位Nagi.cmd** 选择新的 Nagi 目录；也可设置 NAGI_HOME。
移动 Locale Emulator 后在 Nagi 重新选择并保存，跨电脑时需要重新准备组件。

## 可选功能

基础检索默认 BM25。便携首版带 MCP 与 Frida，不带语义检索运行库或模型。
源码用户可安装 `rag` 并主动运行 `python -m nagi.rag.semantic download`；
下载地址、版本与 SHA-256 固定，翻译过程中不会偷偷下载。
模型方案和许可见 [第三方声明](../THIRD_PARTY_NOTICES.md) 与
[检索说明](architecture/translation-context.md)。不提供预装模型增强包。

MCP 需要自行配置服务；stdio 可能需要 Node.js/Python，远程服务可能需要账户。
组件是否已安装会在“使用说明”显示，但已安装不代表 API、外部服务或游戏版本已配置可用。

## 限制

部分翻译沿可确认的主剧情入口读取前 50 条，不按资源排列截取。入口不能确认、分支和
动态跳转无法解析时会明确报错，不会以不相关文本凑数。图片文字、未适配引擎不在支持范围。
API 完整返回和结构校验通过后才允许部署；不完整译文不会伪装成成功。

更新程序与处理旧路径见 [升级说明](UPGRADING.md)。
