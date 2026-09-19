# Galgame 能力接入 DeepSeek Harness 与 Codex 的可行性

调研日期：2026-09-19。Nagi 基线：`190bfd5` 加当前工作区改动（含五引擎 BM25 + E5 接入）。DeepSeek Harness 指官方 `deepseek-ai/deepseek-harness`。这是源码及官方文档调研，没有安装 DSH、运行双宿主集成或调用付费翻译 API。

## 结论

可行。建议形态为 **共享 skill + Nagi 无界面运行库 + CLI，随后增加可选 MCP 服务端**。skill 描述流程和决策，程序承担游戏解析、控制流、检索、人物译名、批次、缓存、回写和部署。

先支持本机 Windows 的完整流程。不能将“能安装 skill”解释为任意宿主、云端环境都能直接访问和启动本地游戏，也不能将五类引擎支持扩展为所有游戏版本支持。

## 宿主能力与共用边界

| 项目 | DeepSeek Harness | Codex | 对方案的影响 |
| --- | --- | --- | --- |
| 技能 | 原生 skill 注册、发现及按需加载 | 原生 SKILL.md 与按需加载 | 共用核心指令和参考文档 |
| 项目内发现 | 支持 `.dsh/skills`、`.agents/skills` | 支持 `.agents/skills` | 首版可用 `.agents/skills/nagi-galgame/SKILL.md` |
| 调用本地程序 | 官方 PowerShell 执行器可用于 Windows | 本地命令工具，受宿主权限约束 | 共用 Python CLI；用明确的运行时路径 |
| MCP | 官方客户端支持 stdio、Streamable HTTP | 支持 stdio、Streamable HTTP | 可共用同一 Python 服务端，配置各自适配 |
| 原生扩展 | Cordis 插件，可扩展工具及界面 | skill / 插件 / MCP 集成 | 不共用原生插件代码；仅在必要时单独做界面 |

DSH 的本地技能发现路径及调用策略以其[技能文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)为据；Codex 的结构、发现目录和插件分发方式见[技能文档](https://developers.openai.com/codex/skills/)。两者可以共用 name、description、正文和脚本；宿主专有的调用策略元数据分别维护，不能假设完全等价。

MCP 接入依据：[DSH MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)、[Codex MCP](https://developers.openai.com/codex/mcp/)。Windows 命令层依据：[DSH shell 包](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/shell/README.md)。DSH 官方仍将产品标为[开发者预览](https://deepseek.com/harness/en/)，集成验收必须固定其版本，不能将 master 文档当作所有已安装版本的保证。

## Nagi 已有资产与实际缺口

| 能力 | 现有实现 | 所需改造 |
| --- | --- | --- |
| 五引擎提取与回写 | `nagi/gameio/` | 保留实现，提供能力查询和统一调用入口 |
| 剧情与 RAG | `translation/routes.py`、`context.py`、`route_context.py`、`retrieval.py`、`rag/semantic.py` | 继续在本地运行；对外返回模式、模型身份和证据摘要 |
| 人物名与分批翻译 | `translation/characters.py`、`yuris.py`、`text_engines.py`、QLIE 流程 | 统一服务层，但保留引擎特有的语料和回写契约 |
| 任务与恢复 | `translation_workflow.py` 的 create/start/cancel/restore/checkpoint | 从 Web 后端依赖中分离；增加跨进程锁、任务索引和稳定状态接口 |
| CLI | `nagi translate` 只有 plan-batch、preview-request、init-run、analyze-routes | 新增覆盖提取、执行、恢复、修补、部署的无界面命令 |
| Agent 工具 | `translation/tool.py` 可操作预先注册的 TranslationRunSpec | 不是全部五引擎的统一服务；不能直接当完整适配完成 |
| MCP | `docs/mcp.md` 明确当前只有客户端 | 若选择 MCP，另增服务端适配层 |

关键耦合点：

- `webapp.py:333` 定义 TranslationJob；模型读取、客户端构造和 QLIE 编排也在该模块。
- `translation_workflow.py:327` 的 start 依赖 backend 的 `_configured_model`、`TranslationJob`；执行阶段还调用 `_prepare_source`、`_translate_and_publish` 等私有方法。
- 当前工作流启动 daemon 线程。命令行程序调用 start 后直接退出会终止工作，不能原样包装为“后台启动成功”。
- 当前工作流用 confirmed 表达 API 费用授权。适配时需要映射用户已有授权、范围与预算，不能让模型自行填 true 代替真实授权，也不应每批重复询问。

无须重写 Nagi 的全部 Agent，也无须将核心迁移成 TypeScript。先抽出 Job、模型工厂、引擎调度与 QLIE 编排，让 Web 和 CLI 共用服务层即可。

## 推荐结构

```text
DeepSeek Harness / Codex
           │
    nagi-galgame skill
           │
  CLI 或 MCP 薄适配层
           │
   Nagi Galgame Service
     ├─ 引擎检测与语料提取
     ├─ 控制流、BM25 + E5、人物译名
     ├─ 翻译执行器、批次回执与恢复
     └─ 引擎回写与独立游戏副本
```

建议新增以下接口；**这些是设计草案，不是当前可执行命令**：

| 操作 | 输出或行为 |
| --- | --- |
| doctor / inspect | 运行时、模型、引擎能力和部署条件 |
| create / extract | 固定源路径、输出路径及原文指纹，返回 run_id |
| plan | 翻译范围、预计批次数和可计算的费用估计；未知价格不猜测 |
| start / status / cancel / resume | 稳定任务状态，安全停止，复用已完成批次 |
| repair | 仅对引擎实际支持的修补模式开放 |
| deploy | 校验后生成独立副本，返回可启动文件及部署回执 |

接口只返回必要的 JSON 摘要、错误码和产物位置。检索及批次处理在后端完成，不把整部游戏塞进聊天历史。启动游戏与生成副本分成独立动作。

skill 建议只含一个入口及少量按需参考：引擎支持边界、命令与状态、恢复与错误处理。Python 包与 ONNX 模型独立安装和版本管理；不把现有 Web UI、游戏资源、密钥或大型模型复制进技能目录。

## 谁执行翻译：必须显式区分

### A. 宿主负责调度，Nagi 调用已配置的翻译 API（首版推荐）

Codex/DSH 负责理解范围、启动任务、查状态和报告结果。Nagi 在持久化 worker 中调用用户配置的模型，保留现有并发、响应结构校验、回执和重试。

这是改动较小、适合全文任务的路径。安装 skill 不会让后台 Python 自动获得 Codex 的会话模型或账户凭据；API 配置仍由运行时单独管理，密钥不进入提示词。

### B. 由当前宿主会话模型翻译

可以设计 `next_batch` / `submit_batch`：后端提供有界原文、人物词表和检索证据，宿主模型生成译文，再交后端验证和持久化。两边可共用这一数据协议。

现有 TranslationToolService.execute_next 仍调用 model_client.complete，并非领取原文再提交译文的接口。因此此路径需要新协议及事务实现，不能只改 skill 文案。

提交必须绑定 run_id、batch_id、原文/请求指纹；检查 ID 完整性、重复提交和控制标记，支持租约超时及失败恢复。QLIE 和其他引擎的响应结构不同，需要适配而非强行合并全部语料格式。人物名预处理也要走相同的宿主执行路径。

该方案适合试译或明确希望用宿主模型的场景。全文规模的吞吐、会话限额、上下文压缩后的续跑都要实测；不能承诺“免费全文翻译”或关掉会话后仍自动完成。还要验证独立批次不会被聊天历史中的其他路线文本影响。

### C. DSH 专用原生插件

可进一步做原生任务面板和模型执行适配，但增加 DSH 专有代码与版本维护。Codex 仍需自己的适配。优先实现共享后端，再按界面需要增加插件，不建议首轮把业务逻辑分别写进两个宿主。

## 运行边界

- 首版完整体验定位于 Windows 本机。当前 QLIE 部署明确依赖 Windows、Frida、Locale Emulator 配置及已知程序版本；其他引擎也按自身条件判断。云端可做部分文本处理，但不会因安装技能获得本地游戏文件和运行环境。
- 能力查询要分别给出 can_extract、can_translate、can_repair、can_deploy 和原因。例如当前 Ren'Py 仅 `.rpyc` 的发行包不能直接提取回写；工作流目前不支持 QLIE repair 模式。
- 保留原文件哈希、输出目录隔离和独立副本。现有输出位置检查要求部分产物在 Git 工作区之外；skill 的工作目录不能直接作为默认游戏输出目录。
- E5 本地模型缺失时报告 BM25；校验失败不能伪装成正常混合检索。沿用固定模型身份、证据预算和 must-history 过滤。
- 长任务状态属于 Nagi，不能只存在 MCP 连接、聊天记忆或临时 shell session 中。MCP 启动返回 job_id，再用短调用查状态；服务被宿主重启时应能识别中断并恢复。
- 已落盘的完成回执用于防止重复执行；提供方已返回但本地尚未持久化时仍可能发生重试，不能对该窗口宣称绝对不重复计费。

## 分阶段落地与验收

1. **无界面核心与 CLI。** 抽离 Web 依赖，统一能力报告，固定 JSON 契约。先用同一批合成游戏语料证明结果与现有流程一致。
2. **共享 skill。** 两宿主分别发现技能并完成提取、部分翻译、恢复、部署。先走明确配置 API 的执行器；默认范围用显式 partial/full，避开旧 pilot 在不同引擎中的不同含义。
3. **长任务与 MCP。** 增加 worker 生命周期、跨进程锁和恢复机制，同一 MCP 服务端接两宿主。验证断开宿主、服务重启和重复启动不会创建冲突任务。
4. **宿主模型执行器与可选界面。** 按需要实现 batch 提交协议，再考虑 DSH 原生面板或 Codex 插件分发。

验收至少覆盖：五引擎能力矩阵；BM25 与 E5 两模式；分支文本不串入证据；中文/空格路径；取消恢复；模型或策略改变时拒绝混用旧回执；可部署版本输出不改原游戏；不支持的引擎方言明确失败；两宿主都不需要启动 Nagi Web 才能完成已支持流程。

工程判断：复用主体逻辑的可行性高，无界面改造工作量中等，长期任务可靠性与宿主模型执行器是主要新增工作。调研尚不支持精确工期或“已实现双端即装即用”的结论。
