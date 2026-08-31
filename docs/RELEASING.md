# 发行构建

版本唯一来源：`nagi/__init__.py` 的 `__version__`。项目元数据、界面、ZIP 名称一致。
首版 0.1.0，仅 Windows x64 便携包；源代码需要 Python 3.12+。

1. 用自己安装的 Python 执行 `python scripts/bootstrap.py --dev`。
2. 审阅 `release/source-files.txt`。每个输入文件都必须明确列出，新增文件不会自动进入源码包。
3. 执行 `python scripts/build_release.py` 生成源码 ZIP、sdist 和 wheel。
4. 在 Windows x64 执行 `python scripts/build_release.py --portable`，同时生成程序 ZIP。
   构建会下载并校验官方 Python 3.13.15 独立嵌入运行时，再按 requirements/portable.lock
   安装带哈希锁定的 MCP/Frida 依赖，不复制开发 `.venv`，不预装模型或 LE。
5. `dist/SHA256SUMS.txt` 校验附件；便携包内 FILES.sha256 校验其程序文件。
6. 可执行 `python scripts/verify_portable.py`：在全新中文/空格路径验证实际启动、
   重复启动、无密钥/无模型首次状态、文件校验及退出，不调用模型或修改游戏。

Python 来源与 SHA-256 记录于 `release/components.json`，来源是官方 windows release manifest。
运行时内保留 Python LICENSE；每个 wheel 的 LICENSE/COPYING/NOTICE/AUTHORS 被汇总到 licenses。
构建器移除 pip 生成的可选 CLI 包装器，避免其中保留构建机 Python 路径；Nagi 直接使用包内 Python 模块入口。
缺少许可证文本的依赖会阻止打包。许可证元数据不替代对应许可证正文。
第三方依赖的原始 wheel 和上游源码许可仍适用，参见 THIRD_PARTY_NOTICES.md。

`uv.lock` 是全量解析锁；requirements/*.lock 是按用途导出的 pip 哈希锁。
更新依赖时同时更新 pyproject、uv.lock 与全部导出文件，然后审阅版本与第三方声明。
命令示例：`uv export --frozen --no-dev --no-emit-project --extra mcp --extra deployment --output-file requirements/portable.lock`。
构建工具 bootstrap 的 build.lock 也必须同步 setuptools/wheel/packaging。

## GitHub

`.github/workflows/release.yml` 在推送 v* 标签或手动触发时构建两个发行形式，上传工作流附件。
标签构建会创建 **Draft Release**，不会自动公开；人工审阅附件、说明与校验值后发布。
源码压缩包与便携 ZIP 同版本，下载说明必须区分两者。
本地仓库仍可以托管于其他 Git 服务；没有 GitHub 仓库/权限不会阻碍本地构建。
本流程不会自动迁移远端、提交开发者文件、推送标签或上传已有私人数据。

发布输入不包含 `.env`、`.pico/.nagi`、`.codex`、缓存、游戏资源、译文、存档、日志或截图。
原 PNG 头像按用户要求恢复并纳入明确的打包白名单；它不属于项目 MIT 授权范围。
公开发布含该头像的源码或程序包前，需确认作者与再分发许可，参见 `THIRD_PARTY_NOTICES.md`。
测试数据限原创合成 fixtures；历史 benchmark 输出与私人研究 checkout 不作为发行输入。
