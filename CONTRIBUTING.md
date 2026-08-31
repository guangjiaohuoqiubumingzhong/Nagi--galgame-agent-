# 参与 Nagi 开发

需要自行安装 Python 3.12+。运行 `python scripts/bootstrap.py --dev --extras mcp deployment rag`。
基础 Agent 不依赖可选组件。验证用 `python -m pytest tests -q`；前端命令菜单使用
`node --test tests/test_composer_commands.cjs`。测试必须使用合成数据，不调用真实付费 API。

核心路径：nagi/runtime.py 为 Agent；providers 为模型协议；web_ui 为打包网页资源；
gameio 为引擎；translation 为受校验的翻译流水线；rag 为检索；paths.py 为资源/数据接口。
不要从源码外层推导安装资源位置，不要加入开发电脑的盘符、Python 路径或 LE 配置。
新增发行文件需明确加入 release/source-files.txt，并同步依赖锁和第三方许可证。

接入引擎必须先阅读[新引擎接入与目录生成开发规则](docs/architecture/engine-integration-rules.md)，
复用公共的任务、测试/正式输出、启动入口和旧版归档流程；验证资料不得另建引擎专用顶层目录。
同时遵循 docs/architecture/character-glossary.md、opening-selection.md：
提取时收集人物，翻译前自动统一人物译名；开场前 50 条需有可验证入口和跳转顺序。
未知分支、动态跳转或编码不能静默猜测。引擎支持必须区分提取、翻译与可玩部署。
不可绕过结构校验、审批、费用确认或原游戏只读约束。旧任务不自动重译。

提交问题请给出版本、系统、安装方式、引擎、复现步骤及脱敏错误摘要。
不要公开 API 密钥、完整会话/日志、游戏资源、译文、存档或私人目录。
代码与项目原创矢量图标使用 MIT；第三方代码、模型和素材保留各自许可，无法确认的素材不纳入分发。
