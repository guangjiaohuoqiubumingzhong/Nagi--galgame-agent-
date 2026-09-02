# Nagi 0.3.2

Windows x64 便携版。本补丁修复第三步生成的独立游戏在便携版 Python 下无法加载启动依赖的问题。

## 下载和启动

- 普通 Windows 用户下载 **Nagi-0.3.2-windows-x64.zip**，完整解压到可写目录。
- 双击根目录中的 **Nagi.exe**。不需要预装 Python。
- **SHA256SUMS.txt** 用于校验附件；不要把 GitHub 自动生成的 Source code 压缩包当作便携版。

## 本版修复

- 部署后的 `runtime/launch.py` 现在按文件路径精确加载同目录的 `locale_support.py`，不再依赖 `sys.path`。
- 修复便携版嵌入式 Python 使用 `python313._pth` 时，第三步启动游戏出现 `ModuleNotFoundError: No module named 'locale_support'` 的问题。
- 0.3.2 新生成的部分翻译和全文翻译副本均使用新启动组件；旧副本可使用运行时修复命令更新，无需重新调用翻译 API。
- 保留 0.3.1 的 API 429/5xx 退避重试、异常 JSON 防护、单一 `Nagi.exe` 入口和应用图标。

## 验证

发布验收会把启动模板复制到便携 Python 搜索路径之外的中文目录，并以隔离模式直接执行，确认同目录依赖能够加载；随后继续验证程序移动、重复启动、网页资源、文件哈希和安全退出。
