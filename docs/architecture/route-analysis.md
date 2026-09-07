# 路线分析与可能／必经前文

QLIE、YU-RIS 479、Ren’Py、KiriKiri 和 TyranoScript 的新翻译任务使用静态控制流分析，
不把资源包、脚本文件名或场景名当成路线。引擎适配器产出 `nagi/gameio/control_flow.py` 中的
统一指令，`nagi/translation/routes.py` 负责路径分析。
QLIE 的网页和 `--with-context` 请求准备阶段共用必经前文过滤；其他四个引擎通过
`nagi/translation/route_context.py` 接入现有批次翻译、重试拆分和查缺补漏流程。

## 结果的精确定义

结果相对于入口集合以及当前支持的控制语法：

- `must`：所有从入口到达目标对白的静态执行路径，均已访问该前文。循环中的首次到达也计入。
- `may_only`：存在这样的路径访问过该前文，但并非所有路径都访问过。
- `unreachable`：在完整静态图中，没有路径先访问该前文再到达目标。
- `unknown`：入口、可达指令或分析资源限制使图不完整，不能得出否定或必经结论。
- `self`：当前对白自身不作为历史证据，即使循环可能重复访问它。

例如 `公共 → {左分支, 右分支} → 合流`：合流点的公共部分为 must，左右分支为 may_only。
左分支的文字不是右分支的前文。公共子程序在两个分支都调用时，其正文可以是合流点的 must。

条件表达式不执行、不求解，所有条件结果都保留。因此 may 是**静态图上可能**，可能包含受变量关联
约束实际不可行的路径。must 是对这个保守路径集合的必经结论。这里没有读取玩家存档、实际选项记录，
也没有推断“某角色线”的人类名称；不能把结果称为玩家当前实际路线。

## QLIE AVG 适配范围

- `@@label`：本文件标签，可解析标签后同一行的反斜线指令。
- `\go,@@label[,"script.s"]`：静态转移，没有继续执行原位置的边。
- `\sub` / `\jmp`：沿用现有开场选取器的 AVG 调用约定，压入返回位置。
- `\ret` 或脚本自然结束：返回到对应调用位置；无调用者则结束。
- `\if,expression`、可选 `\else`、`\end`：两个分支，支持嵌套。
- `\case,expression`、`\ans,value`、可选 `\else`、`\end`：多个互斥分支；没有 else 时保留不匹配路径。
- 同一行多条反斜线指令在引号外分解；引号内 Windows 路径不拆分。
- `\cal`、`\gvar`、`\svar`、`\del` 作为数据操作保留顺序，但不追踪变量值。
- 选择项文字只表示菜单文本，后续 if/case 才定义分流；不会根据选项文字猜目标。
- 显示／音频指令只允许适配器中明确列出的集合；其他指令、动态目标、缺失或重复标签均报告缺口。

条件结构参考实际 QLIE 移植记录中的 if/case 语法，而非新增自定义脚本语言：
[Kind Kanji Studio 的 QLIE 移植记录](https://kindkanjistudio.wordpress.com/2019/07/13/qlie_port_renpy_part2/)。
该记录只覆盖特定游戏，适配器不宣称支持所有 QLIE 框架。更多宏需逐项增加解析与测试。

默认入口为已生效的 `scenario/root.s` 起点；单脚本语料可从唯一脚本起点进入。
多脚本且无已知入口时返回 unknown-entry，可显式指定入口片段 ID。
跨脚本解析使用内部路径及当前文件相对路径，歧义拒绝；资源覆盖复用开场选取器的已校验清单。
无可靠加载顺序的同名脚本拒绝分析，不按资源包名排序选一个。

开场前 50 条的实际路径选择仍遵循原规则；本次静态分析不会替用户选择分支或改变付费翻译范围。

## 其他引擎适配范围

| 引擎 | 输入与入口 | 已解析控制结构 |
| --- | --- | --- |
| Ren’Py | 已导出的完整 `.rpy` 源码；`label start` | 全局／局部标签、静态 jump/call/from、return、缩进 if/elif/else、简单 menu、while/break/continue |
| KiriKiri | 已导出的完整 `.ks`，含未加密 XP3 中的脚本；唯一 `first.ks` 或单脚本起点 | `*label`、静态 jump/call/return、if/elsif/else/endif、link/endlink/button 与 s |
| TyranoScript | `data/scenario` 中完整 `.ks`；唯一 `first.ks` 或单脚本起点 | 同上，并支持 glink；storage 可跨脚本 |
| YU-RIS 479 | 原脚本包经哈希校验后解码的 YSTB 和 YSLB；`SCENARIO_MAIN` 或无标签的唯一脚本 | GO/GOSUB/RETURN/END、IF/条件 ELSE/IFEND、LOOP/LOOPEND、无参数的 IF／LOOP BREAK／CONTINUE |

Ren’Py 不执行 Python，不解析动态 jump/call、screen 动作、自定义语句或复杂菜单扩展。
KAG/Tyrano 解析引号内属性及行首 `@tag`；条件跳转／调用保留执行与跳过两条路径。
链接创建后保留提前点击边，`s` 处保留可选目标；不会假设按钮后面的文字一定已经显示。
动态 storage/target、eval/iscript、未适配宏、含条件的非转移标签、跨转移的整行文本均报告缺口。
尚未闭合到 `s` 的菜单状态跨标签、脚本结束或 jump/call/return 时也报告缺口，不猜测按钮是否仍存活。
YU-RIS 使用真实命令表及标签目标，不按压缩包枚举顺序拼剧情；静态宏调用进入对应标签正文。
缺失宏（包括没有提供实现的 ES./MAC.）不会当成无操作跳过，SWITCH 等未适配指令会报告缺口。
YU-RIS 的条件 ELSE 保留真假路径；BREAK／CONTINUE 带 LV 等参数时暂报不完整，不默认跳到最近一层。
循环次数不求解，图中允许零次或多次循环，是对真实计数路径的保守扩大。

文本引擎校验完整导出脚本的 `text_sha256`，再重建文本跨度和 ID 与 texts.json 比较。
旧导出无该哈希时，只有能够按原编码往返验证 `source_sha256` 才用于分析，否则要求重新提取。
KiriKiri 的同名 loose／XP3 脚本没有可靠覆盖顺序时拒绝分析，不猜测哪个版本生效。
仅有抽取台词而没有源脚本／标签表，无法恢复分支。加密资源和已编译 `.rpyc` 不在现有提取支持范围。

语法依据：[Ren’Py 标签与调用](https://www.renpy.org/doc/html/label.html)、
[TyranoScript 标签参考](https://tyrano.jp/tag/v5)、
[KAG 标签参考](https://a-rabin.github.io/krkr2doc-en/kag3doc/contents/Tags.html)、
[YU-RIS 命令手册](https://yu-ris.net/manual/yu-ris/html/menu.html)。

## 算法与边界

执行状态为 `(指令位置, 返回栈)`；同一子程序的不同调用位置拥有不同状态，避免产生错误返回边。
从入口只展开可达状态。不可达死代码中的未知指令不影响有效图；可达未知指令会使报告 incomplete。
上限为 20,000 个状态、16 层调用栈；超限明确报告，不把截断图视为完整图。

每个状态的输入历史由前驱输出历史计算：may 取并集，must 取交集；前驱输出添加该节点的原文 ID。
入口历史为空。循环通过单调不动点迭代求解，不枚举无限路径。历史以位集存储，只给可达对白分配位。
同一对白的多个调用上下文再次按 may 并集、must 交集合并。
这种按原文 ID 聚合的支配关系能识别“两个不同调用状态都执行了同一句正文”的必经历史。

图不完整时清空所有 must；已知路径上出现的 may 仍可报告，但没有找到的关系只能为 unknown。
这会牺牲一些可用前文，但不会把未知边删除后产生的假支配关系当成事实。

## RAG 与缓存

切块及批次仍不跨局部 scope，避免一个块混入多个分支。检索范围可以跨脚本、跨场景：
要求块内每条对白都是当前单元的 must，并排除当前批次／相邻窗口和重复证据。
may_only 仅用于分析报告，默认不注入翻译提示词。
相邻上下文与查询扩展也校验必经关系；分析 incomplete 时不给 QLIE 注入推测的相邻／历史文本。

这五种引擎的路线证据均带 `history=all_paths`。
上下文索引升级为 `tctx_v3_`，包含语料哈希、策略、入口与分析版本，避免复用旧的翻译缓存。
共享证据池继续识别 v2/v3 身份；既有运行目录、付费结果不改写，不自动重译。

其他四个引擎当前使用本地 BM25 对原文单元召回，不宣称已接入 QLIE 的 E5／重排链路。
每个目标先独立过滤 must，再选最多两条；整批最多四条证据，序列化池和引用最多 3,000 字符。
只引用对应目标的 references，当前批次 ID 不作证据，不把批次顺序当剧情顺序。
新任务的 `route_context_id` 绑定完整图、入口、源单元和策略；API 回执继续校验完整消息哈希。
重试拆分重新计算子批证据；YU-RIS 审计工具用相同上下文重建请求。
无 `route_context_id` 的旧翻译计划继续原提示词与回执，只有新任务启用新上下文。

## 使用与验证

```powershell
python -m nagi translate analyze-routes "D:\corpus"
python -m nagi translate analyze-routes "D:\corpus" --segment-id seg_v1_...
python -m nagi translate analyze-routes "D:\corpus" --entry-segment-id seg_v1_... --segment-id seg_v1_...
python -m nagi translate analyze-routes "D:\extracted\corpus" --segment-id TEXT_ID
python -m nagi translate analyze-routes "D:\yuris\source" --game-dir "D:\game" --segment-id TEXT_ID
```

命令只输出结构和 ID，不输出游戏原文，不调用模型。`--entry-segment-id` 可重复，表示对所有指定入口分析。
QLIE Python 请求配置 `route_entry_segment_ids` 使用同样的入口语义，并进入缓存身份。
网页自动采用默认入口，`context_summary.route_analysis` 返回完整性、入口、状态数和诊断。

`tests/test_translation_routes.py` 使用真实 QLIE 解析器处理合成脚本，覆盖菱形汇合、嵌套条件、多臂分支、
跨脚本调用、返回栈、循环、死代码、未知边、资源覆盖、缓存和实际证据注入。
这些验证不等于真实游戏全路线覆盖；扩展到新语法必须先增加对应的引擎夹具。
`tests/test_engine_routes.py` 通过实际文本提取器和加密 YU-RIS 合成字节码验证其他四种引擎，
覆盖分支、跨文件调用、循环、提前点击、未知控制、源脚本改动、CLI、实际 API 证据及旧回执兼容。
