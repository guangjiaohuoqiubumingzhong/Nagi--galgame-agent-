# Nagi-galgame-agent-全自动视觉小说汉化(开发中)

轻量本地工作区 Agent 与视觉小说翻译工作台，提供终端和 Web 界面。当前版本 **0.3.5**，项目原创代码与矢量图标采用 [MIT](LICENSE)。

Nagi 可分析和修改工作区文件、运行受审批的工具、保存会话，并进行五类视觉小说引擎的文本提取、翻译与独立副本部署。密钥与任务数据仅在用户本机保存；API 请求会按用户选择的提供方发送内容。

## 选择发行方式

| 版本 | 下载附件 | 使用方式 |
| --- | --- | --- |
| Windows 10/11 x64 便携版 | Nagi-0.3.5-windows-x64.zip | 完整解压后双击唯一入口 Nagi.exe，自带独立 Python、MCP、Frida |
| 源码版 | Nagi-0.3.5-source.zip / 源码仓库 | 自行安装 Python 3.12+ 后初始化环境 |
| Python 安装包 | nagi-0.3.5-py3-none-any.whl | 安装到自己的 Python 环境，网页和运行模板随包提供 |

源码 ZIP 不包含运行时。便携指可直接启动，不代表免费 API、免配置、离线翻译或附带游戏。
下载附件由维护者通过 Releases 发布；仓库中的构建流程和本地构建产物不表示已经公开发布。
基础包不带 Locale Emulator、语义检索模型、游戏资源、账户、译文或存档。

## 源码安装

在源码目录中，使用自己安装的 Python 3.12+：

```powershell
py -3.13 scripts/bootstrap.py --extras mcp deployment
.\.venv\Scripts\python.exe -m nagi web
```

macOS/Linux 使用 `python3 scripts/bootstrap.py`，然后 `.venv/bin/python -m nagi web`。
基础安装不加 `--extras`；依赖缺失请重新运行初始化脚本并选择所需组。
若已安装 uv，可用 `uv sync --frozen --extra mcp --extra deployment`，然后 `uv run --frozen nagi web`。
开发环境加 `--dev`。依赖固定在 uv.lock 与 requirements/*.lock；不依赖开发者的 Codex 路径。

| 可选组 | 用途与外部要求 |
| --- | --- |
| mcp | 外部 MCP 客户端；stdio 服务可能另需 Node/Python，远程服务可能需账户 |
| deployment | Windows Frida；还需用户本地的 Locale Emulator 和已适配游戏 |
| rag | ONNX / Tokenizers / NumPy 语义检索；模型需主动下载，不随基础包分发 |

推荐 Windows 组合为 mcp + deployment。基础检索使用 BM25，不会在翻译过程中下载模型。
源码版可执行 `python -m nagi.rag.semantic download` 主动下载已固定哈希的模型。
wheel 资源由同一套路径接口加载，无需保留源码外层 web-ui 目录。

## 功能边界

| 功能 | 支持范围 |
| --- | --- |
| Agent / Web | 本地工作区、会话、审批、模型配置、MCP 可选工具 |
| QLIE | 引擎游戏示例：《美少女万华镜 -罪与罚的少女-》《美少女万华镜 -神明所创造的少女们-》《Princess Lover!》 |
| YU-RIS | 引擎游戏示例：《Aikagi》《euphoria》《Magus Tale ～Sekaiju to Koisuru Mahoutsukai～》 |
| KiriKiri / KAG | 引擎游戏示例：《11eyes》《Amairo＊Islenauts》《Clover Day's》 |
| Ren'Py | 引擎游戏示例：《Slay the Princess》《BAD END THEATER》《Highway Blossoms》 |
| TyranoScript | 引擎游戏示例：《ドトコイ》《じごくのインターネッツ》《お前のスパチャで世界を救え》 |
| 部分翻译 | 新任务沿可验证开场剧情前 50 条对白/旁白，不按资源顺序截取 |
| 人物译名 | 正文翻译前自动统一已识别人物名，旧任务不自动重译 |

当前支持上述五类引擎；作品自定义脚本方言、加密和字体插件仍可能需要按游戏单独适配。

游戏示例参考 [GARbro 已验证游戏列表](https://morkt.github.io/GARbro/supported.html)、[Ren'Py 官方作品介绍](https://www.renpy.org/)和 [TyranoScript 官方制作事例](https://tyrano.jp/example)，用于说明引擎归属，不代表 Nagi 已适配其中每款游戏的所有版本。具体适配版本和部署条件见[快速入门](docs/QUICKSTART.md)。

## 使用与开发

- [普通用户快速入门](docs/QUICKSTART.md)：启动、API、游戏、LE、翻译与输出位置。
- [更新和数据兼容](docs/UPGRADING.md)：程序/数据分离、旧名称兼容、跨电脑密钥与运行时重新定位。
- [MCP](docs/mcp.md) 与 [无密钥示例](examples/mcp.example.json)。
- [详细功能和 CLI](docs/REFERENCE.md)、[上下文检索](docs/architecture/translation-context.md)。
- [贡献指南](CONTRIBUTING.md)、[发行构建](docs/RELEASING.md)、[更新日志](CHANGELOG.md)。
- [第三方许可证与素材范围](THIRD_PARTY_NOTICES.md)。

启动 CLI：`python -m nagi`；查看版本：`python -m nagi --version`；公开类：`from nagi import Nagi`。
旧 PICO_* 环境变量与 .pico 用户数据只保留兼容读取，不再作为新安装名称。
反馈前请脱敏，不公开密钥、私人日志、会话、游戏资源或存档。
