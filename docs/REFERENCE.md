# 详细功能与 CLI 参考

当前安装、版本与路径以 README 和 UPGRADING 为准。

## 本地 Agent 工作台与翻译功能

启动只监听本机的 Web 控制台：

```powershell
.\.venv\Scripts\nagi.exe web
```

工作台包含 Nagi Agent 对话、工作区与本地会话切换、运行轨迹、危险工具网页
审批和安全停止。QLIE、YU-RIS 479、KiriKiri/KAG、Ren'Py 与 TyranoScript 适配通过左侧“视觉小说翻译”入口使用，可以选择原始
游戏目录。翻译页分为三个独立步骤，每步有按钮、状态、进度和错误提示：

1. **提取游戏资源**：选择游戏目录及独立的文本保存目录，识别五类引擎的受支持资源，提取脚本并建立带来源哈希和回填位置的语料；此步不调用模型、不修改原游戏。
2. **开始翻译**：选择“部分翻译（开场剧情前 50 条）”或“全文翻译”，确认 API 费用后开始。先自动翻译全部已识别人物名并保存译名表，再翻译所选正文；已有匹配的译名表会复用，无需人工审核。新部分翻译沿主剧情入口选取最多 50 条对白/旁白，不计系统提示、日期、控制命令或独立姓名字段；剧情结束时不足 50 条则翻译已有条目。无法确认入口、动态跳转或分支时明确报错，不按资源顺序凑数。全文翻译处理完整提取语料中可翻译的条目。API 必须完整返回所选 ID/文本对应关系，不能以缺条结果部署。切换范围会新建翻译输出，不覆盖旧译文。
3. **嵌入游戏内**：第二步完成后，将**本次任务**的译文部署到 `playable/引擎/游戏/translation-test`（部分/试译）或 `translation-formal`（全文），游戏文件直接放入该目录，不再套一层 `localized-game-*`。游戏根目录只生成对应的 `启动测试版.cmd` 或 `启动正式版.cmd`，两者固定指向各自版本。同类旧可玩版含存档会先备份到 `playable/.history`，不直接删除。KiriKiri 覆盖散装 `.ks` 与标准未加密 XP3，Ren'Py 覆盖保留 `.rpy` 的发行包，TyranoScript 覆盖标准 `data/scenario/*.ks`；加密、编译-only 或自定义方言会明确停止。

部分翻译的选择规则见 [开场剧情选取规范](docs/architecture/opening-selection.md)。Case 六花当前
通过 `SCENARIO_MAIN → MC2_01_010` 进入开场；花梨的自我介绍在新范围内是第 3 条，
而不是其在完整资源提取列表中的第 8055 条。新规则不修改第一步原文及 ID。
旧部分翻译任务仍按原有范围恢复、续跑及部署，不会自动重译；要生成新的开场测试版，
在原文未变时无需重新提取，只需再次执行第二步部分翻译和第三步部署。

YU-RIS 已完成批次保存在 `api-batches/`，
失败后继续时只提交未完成批次；网络错误、缺条或损坏缓存会明确报错，不会默默漏掉文本。
API 返回格式错误或缺条时，先有限重试，再将该批次拆小重试；仍失败就停止，不能以不完整结果部署。
每次实际返回都保存独立回执，拆分后的汇总回执记录子批次来源；不会对译文做语义评判。
这是协议与文件完整性检查，不是译文质量检查。该已验证游戏的提取结果包含拼接表达式中的 90 段显示文字，
回包只替换文字操作数，原有变量和运算指令保持不变。技术资源名、路径及脚本指令不作为显示正文翻译。

新的部分/全文流程不依赖旧 `pilot-32` 的译文或资源包。在翻译页第三步选择本地
Locale Emulator 目录并点击“检查并保存目录”；Nagi 检查组件并保存本机路径，
为游戏生成独立日区配置，不依赖原游戏中的 `locale-fix/LE` 或固定全局配置编号。
提取、翻译不需要 Locale Emulator；旧汉化副本继续使用原组件，不会被覆盖。
旧试译恢复接口仅保留兼容，网页不再展示旧试译快捷入口。
字体适配由项目内维护的运行时提供，需当前 Windows 的 Nagi Python 环境与 `frida==17.17.0`；
依赖可通过 `deployment` 可选依赖组安装，网页不自动联网安装。
启动文件仍依赖本机 Nagi 环境，不是可任意拷贝到其他电脑的独立 EXE。
生成后双击游戏根目录中的 `启动正式版.cmd`（部分翻译则用 `启动测试版.cmd`），选择从头开始；部署不会复制原存档。
开发期间已对该 Case 六花版本完成 22,960 条文本的全文传输、回包及开场显示检查。
这些历史资料属于本机资料，不随源码分发，也不代表其他游戏版本、其他电脑或全路线已经验证。
当前输出使用上述 `translation-test` / `translation-formal` 布局；不再使用历史的 `localized-game-*` 嵌套目录。
复制按实际字节更新进度并逐文件校验，完整目录最终发布；失败的 `.partial` 目录保留供检查，不会作为成功结果返回。

前后端都会拒绝跳过前置步骤。修改游戏目录或保存位置会清除页面的已完成状态，
但不会删除磁盘上的旧结果。网页刷新会恢复当前服务中的最新任务和进度；QLIE 和 YU-RIS 新任务会保存不含密钥的恢复记录，
后端重启后恢复最新任务关联，但不会自动发起付费调用。点击“继续翻译”前仍需确认费用，且模型服务须与原任务一致。

Agent 会话继续保存在各工作区的 `.nagi/sessions/`，运行轨迹保存在
`.nagi/runs/`。QLIE 翻译任务按脚本提取、语料构建、模型分批翻译、结构校验和
旁路脚本发布顺序执行，支持在批次之间安全停止。前端不会收到 API key，原始
游戏目录始终保持只读；翻译输出默认写到项目外层的 `translations/`，按
`QLIE` / `YU-RIS` → 游戏 → 任务分组，不为同种引擎的不同版本另建顶层目录。
可玩副本独立保存在同级 `playable/`。YU-RIS 只长期保存原文、API 返回、译文与
必要的来源/恢复记录；不重复保存解包文件和原始资源包，回包在第三步进行。
详见 [目录与 Locale Emulator 配置](docs/translation-storage.md)。

QLIE 对于无法自动判断的 unknown 脚本行，会生成审核文件并停止，不会擅自把它们
送入翻译。CP932 脚本若包含无法编码的中文，发布旁路脚本时会转换为 QLIE 已使用
的 UTF-16LE BOM 格式。独立译文脚本不等于可直接游玩的汉化包，第三步必须有引擎专属的
加载、回包和中文字体适配；直接覆盖原始资源包不受支持。

### QLIE 可玩部署范围

- 已适配原版 `美少女万華鏡.exe`（游戏窗口版本 1.02），程序 SHA-256 为
  `ad02b1115887b8e5b453d4b5760077e8b63e153bba1161e1704209cf3db5cda5`。
  不按文件名猜版本，不使用同目录的其他包装启动器；其他 QLIE 版本仍需单独适配。
- 已验证配置按编号资源包的覆盖顺序提取有效脚本，避免翻译后被更高优先级补丁覆盖。
  旧任务若只翻译了低优先级副本，会在第三步明确拒绝，须重新提取和翻译。
- 部署会核对本任务预览、译文目录、提取记录、原包条目；保留脚本控制命令与原有换行，
  将修改后的脚本转成 UTF-16LE BOM，重建 FilePack 3.1 加密条目并逐条解密核对。
  不改资源名、不增删目录项，未修改资源保持原始字节；不复制原存档。
- 中文显示使用只连接该独立游戏副本的 Unicode 字体适配，覆盖字形绘制及文字测量。
  需要 Windows、Frida 和已配置的 Locale Emulator，不会安装系统字体或修改全局区域设置。
- 2026-08-31 使用本地离线测试文本验证了实际开场加载和简体中文显示；测试没有调用 API，
  不构成全文翻译质量或全路线可玩性验证。回归测试另覆盖译文篡改、原包变化、版本不符、
  不完整结果、补丁覆盖和任务恢复。图片文字仍不在自动翻译范围内。

## QLIE 只读检查

Phase 0 Inspector 可以递归扫描游戏目录中的 `.pack` 文件，识别
`FilePackVer1.0/3.0/3.1` trailer，并输出条目数、TOC 位置、文件大小和
SHA-256。该命令不初始化模型、不解包资源，也不修改游戏目录：

```bash
nagi qlie inspect "D:\path\to\game"
```

输出稳定 JSON：

```bash
nagi qlie inspect "D:\path\to\game" --json
```

只检查格式和元数据、跳过大文件哈希：

```bash
nagi qlie inspect "D:\path\to\game" --skip-hash
```

Phase 1A 可以解码单个受支持 QLIE FilePack 的目录表，当前覆盖
`FilePackVer1.0/3.0/3.1`，列出内部路径、偏移、
压缩前后大小、压缩/混淆标志和条目哈希。它只读取 trailer 与 TOC，
不会读取条目正文或写出文件：

```bash
nagi qlie list "D:\path\to\game\GameData\data6.pack" --limit 30
```

输出包含全部条目的稳定 JSON（不受 `--limit` 影响）：

```bash
nagi qlie list "D:\path\to\game\GameData\data6.pack" --json
```

完整包 SHA-256 默认关闭；需要时显式启用：

```bash
nagi qlie list "D:\path\to\game\GameData\data6.pack" --hash --json
```

Phase 1B 的单条目探针只把一个明确指定的条目读入内存。3.1 普通条目使用
原游戏 EXE 中的 `RCDATA/RESKEY`；3.0 条目使用邻近的外部 `key.fkey`、
包内 `pack_keyfile` 和 EXE 中的 `IconKeyImage`；1.0 当前只支持目标样本所用的
无加密 payload。Nagi 只把 EXE 当作 PE 数据解析，不加载或执行它：

```bash
nagi qlie probe "D:\path\to\game\GameData\data6.pack" --path "scenario\root.s" --exe "D:\path\to\game\game.exe"
```

3.0 的 `key.fkey` 默认会从资源包附近或游戏 `DLL/` 目录发现，也可以显式指定：

```bash
nagi qlie probe "D:\path\to\game\GameData\data0.pack" --path "scenario\root.s" --exe "D:\path\to\game\game.exe" --key-file "D:\path\to\game\DLL\key.fkey"
```

也可以按 `qlie list` 显示的零基索引选择条目：

```bash
nagi qlie probe "D:\path\to\game\GameData\data0.pack" --index 1 --exe "D:\path\to\game\game.exe" --json
```

探针只输出大小、阶段、SHA-256 和最多 32 字节的十六进制前缀，
不会把完整正文放进 JSON，也不会在磁盘上生成文件。省略 `--exe` 时，
需要密钥的条目返回结构化 `key_required`。无法唯一判定目录结构或使用未知加密
方式的 1.0 变体会 fail closed，不会猜测解码。

Phase 1C 先生成脚本导出计划。默认冲突策略是 `preserve`：无冲突脚本
进入 `resolved/`，同路径的不同资源包版本分别进入
`layers/<archive>/`，不会猜测哪个版本优先。若游戏目录下存在含 `.pack`
的 `GameData`，默认只扫描该目录，避免把安装/卸载资源混入翻译语料；
重复的 `--archive` 可用于显式限制其他资源包范围：

```bash
nagi qlie export-plan "D:\path\to\game" --output "D:\qlie-workspace" --archive "GameData\data6.pack" --archive "GameData\data8.pack"
```

如果之后有充分证据确认加载顺序，可以显式切换到 `precedence`；此时
重复的 `--archive` 参数才表示低到高优先级，后出现的包覆盖前面的同名脚本：

```bash
nagi qlie export-plan "D:\path\to\game" --output "D:\qlie-workspace" --archive "GameData\data6.pack" --archive "GameData\data8.pack" --conflict-policy precedence
```

`export-scripts` 默认同样只预览，不创建目录：

```bash
nagi qlie export-scripts "D:\path\to\game" --output "D:\qlie-workspace" --archive "GameData\data6.pack" --archive "GameData\data8.pack" --exe "D:\path\to\game\game.exe"
```

确认计划后，显式传入 `--apply` 才会导出：

```bash
nagi qlie export-scripts "D:\path\to\game" --output "D:\qlie-workspace" --archive "GameData\data6.pack" --archive "GameData\data8.pack" --exe "D:\path\to\game\game.exe" --apply
```

导出只允许 `.s/.txt`，输出目录必须与游戏目录完全分离且尚不存在。
Nagi 会先在内存中解码全部选中条目，再通过同磁盘临时目录一次性发布，
并生成 `manifest.json`。manifest 使用 `conflict_group` 关联同一逻辑路径
的多个版本。任一条目失败时不会留下半成品输出目录；默认禁止覆盖。
Windows 提交阶段会对短暂的目录占用进行有限重试。

Phase 2A 使用 Phase 1 工作区中的 `manifest.json` 做只读脚本调查：

```bash
nagi qlie survey-scripts "D:\qlie-workspace"
```

Survey 会先验证每个导出文件的路径、大小和 SHA-256，再统计编码、BOM、
换行风格、行族和脱敏语法形状。报告不会包含对白、旁白或原始脚本行，
不会初始化 Agent，也不会创建或修改文件。`--json` 输出稳定的逐文件来源
元数据，`--top-shapes N` 控制聚合语法形状数量。哈希不一致的文件不会
进入语法统计，命令返回非零状态。

Phase 2B 定义了引擎无关的 Segment v1 协议，正式 JSON Schema 位于
`docs/product/qlie-translation/segments-v1.schema.json`。每个 segment 保存
原文、仅做 Unicode NFC/换行统一的标准化文本、文本哈希、类型、说话人、
场景、占位符/标签、前后关系，以及可回到 Phase 1 manifest 的来源信息。
byte span 使用零基半开区间，line span 使用一基闭区间。

`segment_id` 不包含绝对路径或导出目录；它由引擎、archive、internal path、
entry、源脚本 SHA-256、byte span 和原文 SHA-256 经过规范 JSON 与 SHA-256
生成。移动工作区不会改变 ID，而输入内容或来源位置变化会令旧 ID 失效。
`segments_to_jsonl` / `segments_from_jsonl` 提供严格、确定性的 JSONL 往返，
拒绝未知字段、重复 ID、悬空链接和不匹配的 token span。

Phase 2C 提供单文件、manifest 驱动的 QLIE 解析器。默认输出不含正文的
摘要，只有显式 `--emit-jsonl` 才把该文件的完整 Segment v1 写到 stdout：

```bash
nagi qlie parse-script "D:\qlie-workspace" --path "resolved/scenario/root.s" --json
nagi qlie parse-script "D:\qlie-workspace" --path "resolved/scenario/root.s" --emit-jsonl
```

解析器支持 UTF-16LE/BE（含 BOM）、UTF-8（含 BOM）和 CP932，按原始字节
扫描 CRLF/LF/CR，因此每个 segment 的 byte span 可以直接回读并得到完全
相同的原文。`@@`、`〖…〗`、独立文本、`^select`、文本型保存命令和控制行
分别映射为 label、speaker、dialogue/narration、choice、metadata 和 control；
`[pc,正文]`、`[rb,注音...]正文` 与 `voicedata` 角色名也会保留控制边界并
提取为 narration/metadata；
无法证明语义的行保留为不可翻译 `unknown` 并产生 warning。

只读聚合评估使用：

```bash
python -m scripts.evaluate_qlie_parser "D:\qlie-workspace"
```

该报告只包含状态、编码、kind、未知率和脱敏未知形状，不包含脚本文本。

Phase 2D 把完整导出集变成可发布的 Segment v1 corpus。命令默认只做
dry-run：它会重新验证 manifest、每个源文件的大小/SHA-256、解析状态、
byte span 回读、Segment ID 唯一性和双向链接，但不会创建输出目录：

```bash
nagi qlie build-corpus "D:\qlie-workspace" --output "D:\qlie-corpus" --json
```

如果计划包含 unknown，`--apply` 会保持阻塞。先显式生成完整的本地审核
模板；模板含原始 unknown 文本，因此必须放在 Git 仓库和 Phase 1 导出目录
之外，并且不会覆盖已有文件：

```bash
nagi qlie build-corpus "D:\qlie-workspace" --output "D:\qlie-corpus" --write-review-template "D:\qlie-review\unknown-review.jsonl"
```

模板按脱敏 shape 分层排序。逐行把 `label` 填为 `translatable` 或
`not_translatable` 后，用同一输入重新预览。审核文件必须覆盖当前 corpus
中的全部 unknown，segment ID、文本哈希、shape 和原文都必须匹配；审核后
召回率低于默认 99% 时仍会拒绝发布：

```bash
nagi qlie build-corpus "D:\qlie-workspace" --output "D:\qlie-corpus" --unknown-review "D:\qlie-review\unknown-review.jsonl" --json
nagi qlie build-corpus "D:\qlie-workspace" --output "D:\qlie-corpus" --unknown-review "D:\qlie-review\unknown-review.jsonl" --apply
```

只有最后一条显式 `--apply` 会在全量复检后，通过同文件系统临时目录一次性
发布 `segments.jsonl`、不含正文的 `parse-report.json` 和 Phase 1 manifest
原样副本 `source-manifest.json`。输出目录已存在、源文件在计划后变化或写入
失败时不会覆盖，也不会留下部分 corpus。

Phase 3A 提供引擎无关的本地关键词检索基线。默认只索引
`translatable=true` 的 Segment；构建命令仍然默认 dry-run，计划不包含正文，
也不会创建输出目录：

```bash
nagi rag build-index "D:\qlie-corpus" --output "D:\qlie-keyword-index" --json
```

确认计划后，显式 `--apply` 才会在 Git 仓库和源 corpus 之外事务发布
`manifest.json`、`documents.jsonl` 和 `postings.jsonl`：

```bash
nagi rag build-index "D:\qlie-corpus" --output "D:\qlie-keyword-index" --apply
```

索引使用 Unicode NFKC/casefold、普通单词、CJK 单字和双字词项，并以 BM25
排序。查询支持 kind、speaker、scene、archive、output path prefix 和
translatable 过滤：

```bash
nagi rag query "D:\qlie-keyword-index" "月 光" --kind dialogue --speaker "角色名" --limit 5
nagi rag query "D:\qlie-keyword-index" "月 光" --path-prefix "resolved/scenario" --metadata-only --json
```

每个结果包含 score、matched tokens、segment ID 和完整来源。查询前默认重新
计算源 `segments.jsonl` 的 SHA-256；源 corpus 变化时旧索引会被判定为 stale，
不能继续检索。需要调试控制段时可以在构建时使用 `--scope all`，默认
`translatable` scope 避免控制命令污染翻译上下文。

Phase 3B 提供不含商业原文的固定检索评测。数据集同时固定合成文档、查询、
metadata filters、期望 segment ID 和每项 K，可以重复计算 Recall@K、MRR 与
搜索延迟：

```bash
nagi rag evaluate benchmarks/qlie_retrieval_tasks.json --repeats 20 --json
```

`KeywordContextRetriever` 可以把已加载索引接入 `Nagi(context_retriever=...)`。
普通 Agent 的 RAG prompt 注入默认关闭；只有显式设置 feature flag
`retrieved_context=true` 才会在相关记忆与历史之间加入 `Retrieved context`。
该 section 默认有独立的 1600 字符预算和稳定截断规则。prompt metadata 只记录
查询、index ID、索引来源、segment ID、score、数量和耗时，不复制检索正文；关闭开关时既不调用
retriever，也不改变原有 section 顺序或 prompt 文本。

Phase 4A 增加翻译前的只读 batch planner。它要求输入为带有已发布
`parse-report.json` 的 Segment v1 corpus，并重新校验 corpus SHA-256、记录数、
previous/next 互惠链接和 unknown 审核门：

```bash
nagi translate plan-batch "D:\qlie-corpus" \
  --model "dry-run-model" \
  --prompt-version "qlie-translation-v1" \
  --terminology-version "none" \
  --rag-index-id "kw_v1_example" \
  --batch-size 32 \
  --json
```

计划为每条可翻译 Segment 生成稳定 TranslationUnit、cache key、来源引用和 batch
ID。cache key 覆盖源文本哈希、模型、prompt、术语版本、RAG index 和证据引用；
unknown 与 `translatable=false` 片段始终排除。JSON 计划仅包含 ID、哈希、来源和
计数，不包含游戏正文。该命令没有 `--apply` 或 `--output`，不会调用模型、创建
译文文件或修改游戏。

Phase 4B 增加版本化的单批翻译请求协议和严格响应解析器。可以先预览某个
batch 将生成的请求身份与 prompt 哈希；预览结果不含源正文或完整 prompt：

```bash
nagi translate preview-request "D:\qlie-corpus" \
  --model "dry-run-model" \
  --batch-size 32 \
  --batch-ordinal 1 \
  --target-language "zh-CN" \
  --json
```

程序内的 provider-neutral 执行器只要求模型对象提供 `complete()`，并将响应按
request ID、有序 unit ID 和 segment ID 做整批校验。空译文、乱序、缺失、重复、
额外记录或额外字段都会拒绝整批，不保留部分成功。CLI 当前只暴露元数据预览，
不会连接真实 provider；候选缓存、断点恢复和受审批的真实执行将在后续阶段接入。

Phase 4C 增加独立运行目录、候选缓存和逐批原子 checkpoint。初始化默认仍是
dry-run；只有显式 `--apply` 才创建运行目录，而且目标必须位于 corpus 与 Git
工作树之外：

```bash
nagi translate init-run "D:\qlie-corpus" \
  --output "D:\qlie-translation-run" \
  --model "dry-run-model" \
  --batch-size 32 \
  --target-language "zh-CN" \
  --apply \
  --json
```

manifest 与 checkpoint 只保存 run/plan/request/batch ID、哈希、状态、尝试次数和
候选文件引用；译文只存在独立的 `candidates/` 文件中。恢复时会重新校验所有身份、
文件哈希和 TranslationCandidate 契约，已完成 cache key 不会再次请求。进程在模型
调用中断时也会留下 retryable attempt。当前 CLI 只负责安全初始化；真实 provider
执行只通过下文的预注册 Agent 工具和审批门开放。

Phase 4D 把恢复执行器接入 Agent 工具总闸。翻译 run 必须由宿主预注册，模型只能
看到 run ID 和当前 request ID，不能自行指定目录、provider 或 key：

```python
from nagi.translation import TranslationRunBinding

agent = Nagi(
    ...,
    approval_policy="ask",
    allowed_tools=("translation_run_status", "translate_game_batch"),
    translation_runs=(TranslationRunBinding(run_spec, run_directory),),
    translation_model_client=translation_client,
)
```

`translation_run_status` 是只读工具；`translate_game_batch` 是需要审批的高风险工具，
每次只执行状态工具返回的当前 request。拒绝审批不会调用模型或更新 checkpoint；
过期/越序 request、模型身份不匹配和损坏 cache 都会在执行前拒绝。结构化 trace
只记录 ID、哈希、token 统计、状态和候选引用摘要，不保存 prompt、正文、译文、
运行目录或 key。Phase 4 至此完成，下一步进入译文结构安全校验。

Phase 5A 已增加纯只读的候选结构校验：

```python
from nagi.translation import (
    candidate_acceptance_status,
    validate_translation_candidate,
)

report = validate_translation_candidate(unit, candidate, max_line_chars=42)
print(report.status, report.patch_eligible)
print(candidate_acceptance_status(report))
```

它按 Segment v1 校验 placeholder、方括号标签、QLIE 转义和换行数量，能区分
缺失、增加、重复、修改与乱序。报告只保存 ID、哈希、计数、行号和 issue code，
不复制源文或译文；任何结构 error 都会阻止 candidate 进入 accepted。Phase 5A
仍不生成或应用补丁。

Phase 5B 增加版本绑定的术语与角色称呼校验：

```python
from nagi.translation import (
    CharacterNameRule,
    TerminologyRule,
    TerminologySnapshot,
    validate_translation_candidate,
)

terms = TerminologySnapshot(
    version="terms-v1",
    terms=(TerminologyRule("term.alpha", "原词", "规范译法"),),
    characters=(
        CharacterNameRule(
            "character.alpha", "Alice", "爱丽丝",
            source_names=("アリス",), variants=("艾丽丝",),
        ),
    ),
)
report = validate_translation_candidate(
    unit, candidate, terminology_snapshot=terms,
)
```

候选的 `terminology_version` 必须与快照版本一致；规范译法缺失或版本不匹配会
阻止 accepted，未知术语和称呼变体会进入 review。术语报告仍只包含规则 ID、哈希、
计数和 speaker 摘要，不复制规则正文、源文或译文。

Phase 5C 可以从已通过两道校验门的候选生成旁路预览：

```python
from nagi.translation import (
    TranslationPreviewSpec,
    apply_translation_preview,
    build_translation_patch_preview,
    publish_translation_preview,
)

preview = build_translation_patch_preview(
    units, candidates, spec,
    current_segments_sha256=spec.segments_sha256,
    current_parse_report_sha256=spec.parse_report_sha256,
    current_source_hashes=current_source_hashes,
    terminology_snapshot=terms,
)
publish_translation_preview(preview, external_preview_dir, include_text=True)

# 默认只在临时目录验证，返回 dry_run，不创建 external_translated_scripts_dir。
apply_translation_preview(
    external_preview_dir,
    exported_scripts_dir,
    external_translated_scripts_dir,
)

# 人工检查并明确批准后，原子发布一个新的旁路脚本目录。
apply_translation_preview(
    external_preview_dir,
    exported_scripts_dir,
    external_translated_scripts_dir,
    approved=True,
    dry_run=False,
)
```

预览默认只输出 ID、来源路径、字节范围和哈希；只有显式要求时才在仓库外 sidecar
目录中写入实际 diff。corpus、来源文件或 candidate 哈希失配会拒绝预览，原始游戏
文件不会被修改。Phase 5D 应用器还会复核 manifest、当前 corpus、编码、目标路径和
每条源文本的唯一匹配；默认 dry-run，正式执行必须显式批准，并且只能创建全新的旁路
脚本树。它不会重建或写入 `data*.pack`，也不会覆盖导出的源脚本或游戏目录。

## QLIE 合成端到端评测

Phase 6A 提供一条不需要 API key 或真实模型的可复现命令：

```bash
python -m scripts.evaluate_qlie_translation \
  --output benchmarks/results/qlie-translation-e2e-v1.json
```

评测使用原创合成 QLIE fixture 和确定性 scripted provider，依次覆盖脚本提取、
Segment/corpus、关键词检索、翻译 checkpoint 失败恢复、结构/术语校验、预览发布和
Phase 5D dry-run。稳定 `artifact_id` 不包含临时目录和耗时，因此相同数据与配置可以
跨运行比较；各阶段耗时仍单独保留。

当前基线的文本召回、Retrieval Recall@K、MRR、结构保留率、术语一致率和恢复成功率
均为 `1.0`，估算模型成本为 `0`。基线 artifact 位于
`benchmarks/results/qlie-translation-e2e-v1.json`。报告不包含源文或译文，不联网、
不调用真实模型、不生成正式翻译 sidecar，也不修改游戏目录或 `data*.pack`。

Phase 6C 在相同任务、候选译文和质量约束上完成四策略检索消融：

```bash
python -m scripts.evaluate_qlie_rag_ablation \
  --output benchmarks/results/qlie-rag-ablation-v1.json
```

当前 `no_rag` 是已执行但不计算 Recall/MRR 的控制组；`keyword` 使用 BM25，`vector`
使用完全离线、固定 1024 维并做 L2 归一化的稳定特征哈希，`hybrid` 使用 BM25 与
vector 两路结果的 RRF 融合（`k=60`）。三个检索后端在固定 7 项合成任务上的 Recall@K
和 MRR 均为 `1.0`，artifact 的 `comparison_status` 为 `complete`。哈希向量后端用于证明
可复现的向量检索和融合工程，不等同于神经语义 embedding；后续可在保持相同协议与
任务集的前提下替换模型。

## 模型后端

Nagi 启动时会读取项目根目录的 `.env`。本地真实 key 放在 `.env`，仓库只保留 `.env.example`。配置优先级是：

```text
显式 CLI 参数 > .env 里的 NAGI_* 变量 > 旧环境变量 > 代码默认值
```

Provider 选择的具体顺序是：

```text
--provider > NAGI_PROVIDER > 代码默认 deepseek
```

不传 `--provider` 且没有 `NAGI_PROVIDER` 时默认使用 `deepseek`。这是推荐配置路径：DeepSeek 的 Anthropic-compatible endpoint 比本地 Ollama 更少依赖本机模型环境，也比 OpenAI-compatible/Anthropic-compatible 代理少一层默认 gateway 假设。其他 provider 仍然保留，可以在 `.env` 里写 `NAGI_PROVIDER=openai`、`NAGI_PROVIDER=anthropic`、`NAGI_PROVIDER=ollama`，也可以显式传 `--provider openai`、`--provider anthropic` 或 `--provider ollama`。

`.env` 会在构建 provider client 前加载，并覆盖当前进程里的同名环境变量。模型名和 base URL 可以通过 `--model`、`--base-url` 临时覆盖；API key 只从环境变量读取。

本地第一次配置：

```bash
cp .env.example .env
```

然后把要使用的 provider key 填进去。`.env` 已经被 `.gitignore` 忽略，不要提交真实 key。

### 推荐配置：DeepSeek

最小配置只需要 key：

```bash
NAGI_DEEPSEEK_API_KEY="your-api-key"
```

默认模型和接口是：

```bash
NAGI_DEEPSEEK_API_BASE="https://api.deepseek.com/anthropic"
NAGI_DEEPSEEK_MODEL="deepseek-v4-pro"
```

所以常规情况下 `.env` 里只填 `NAGI_DEEPSEEK_API_KEY` 就能直接启动：

```bash
uv run nagi
```

如果你需要临时切模型或代理地址，不必改 `.env`，可以直接覆盖：

```bash
uv run nagi --model deepseek-v4-pro --base-url https://api.deepseek.com/anthropic
```

DeepSeek 当前走 Anthropic-compatible Messages API，所以 runtime 里复用的是 Anthropic-compatible client；这只影响 HTTP 协议，不影响 CLI 用法。

Nagi 当前使用文本编码的工具协议，因此会在 DeepSeek 请求中显式关闭 provider-native thinking，避免思考内容耗尽单步输出预算或产生无法回放的 thinking block。后续如果接入原生工具协议，需要同时实现 thinking block 的完整回放，不能只删除这个开关。

模型输入现已使用真正的 `system` / `user` / `assistant` 角色消息。规则放在 `system`，多轮对话按原角色回传，工具结果作为明确标记的运行时反馈；翻译批次分别发送系统规则和用户数据。详见[角色消息与多轮上下文](docs/architecture/messages.md)，包含上下文预算、旧会话恢复和各后端的传输格式。

网页翻译自动构建原文上下文，不再需要或支持参考 JSON 导入。安装本地检索模型后，使用实体优先 BM25 + multilingual-e5-small 语义召回 + 多语言 MiniLM 重排；模型未安装时明确显示 BM25 模式。仅检索可确认的连续前文，整批证据默认最多 3,000 字符，新译文不会自动回灌。模型部署、边界限制和缓存规则见[自动翻译上下文与混合检索](docs/architecture/translation-context.md)。

### 可选配置：right.codes

right.codes 在 Nagi 里有两条可选 provider 路径：

- `--provider openai`：走 OpenAI-compatible `/responses`，默认 base URL 是 `https://www.right.codes/codex/v1`，默认模型是 `gpt-5.4`
- `--provider anthropic`：走 Anthropic-compatible `/messages`，默认 base URL 是 `https://www.right.codes/claude/v1`，默认模型是 `claude-sonnet-4-6`

如果 right.codes 给你的是一把共享 key，推荐只填这一项：

```bash
NAGI_RIGHT_CODES_API_KEY="your-right-codes-key"
```

然后按需要选择 provider：

```bash
uv run nagi --provider openai
uv run nagi --provider anthropic
```

如果你想显式区分两条 provider 的 key，也可以分别配置：

```bash
NAGI_OPENAI_API_KEY="your-right-codes-key-for-codex"
NAGI_ANTHROPIC_API_KEY="your-right-codes-key-for-claude"
```

不要在 `.env` 里写 `NAGI_OPENAI_API_KEY=$NAGI_RIGHT_CODES_API_KEY` 这种 shell 展开形式；Nagi 的 `.env` 解析器只读取字面量，不展开变量引用。要么只写 `NAGI_RIGHT_CODES_API_KEY`，要么把 key 字符串分别填到 provider-specific 变量里。

如果请求 right.codes 返回 `API Key额度不足`，说明协议和 endpoint 已经打通，但当前 key 没有可用额度；换一把有额度的 key，或到 right.codes 后台处理额度。

当前 provider 环境变量：

| provider | base URL | API key | model |
| --- | --- | --- | --- |
| `deepseek` | `NAGI_DEEPSEEK_API_BASE`，回退 `DEEPSEEK_API_BASE`，默认 `https://api.deepseek.com/anthropic` | `NAGI_DEEPSEEK_API_KEY`，回退 `DEEPSEEK_API_KEY` | `NAGI_DEEPSEEK_MODEL`，回退 `DEEPSEEK_MODEL`，默认 `deepseek-v4-pro` |
| `openai` | `NAGI_OPENAI_API_BASE`，回退 `OPENAI_API_BASE`，默认 `https://www.right.codes/codex/v1` | `NAGI_OPENAI_API_KEY`，回退 `OPENAI_API_KEY`、`NAGI_RIGHT_CODES_API_KEY`、`RIGHT_CODES_API_KEY`、`NAGI_ANTHROPIC_API_KEY`、`ANTHROPIC_API_KEY` | `NAGI_OPENAI_MODEL`，回退 `OPENAI_MODEL`，默认 `gpt-5.4` |
| `anthropic` | `NAGI_ANTHROPIC_API_BASE`，回退 `ANTHROPIC_API_BASE`，默认 `https://www.right.codes/claude/v1` | `NAGI_ANTHROPIC_API_KEY`，回退 `ANTHROPIC_API_KEY`、`NAGI_RIGHT_CODES_API_KEY`、`RIGHT_CODES_API_KEY`、`NAGI_OPENAI_API_KEY`、`OPENAI_API_KEY` | `NAGI_ANTHROPIC_MODEL`，回退 `ANTHROPIC_MODEL`，默认 `claude-sonnet-4-6` |
| `ollama` | `--host`，默认 `http://127.0.0.1:11434` | 不需要 | `--model`，默认 `qwen3.5:4b` |

如果有额外的敏感环境变量需要从 trace/report 里脱敏，可以用 `NAGI_SECRET_ENV_NAMES` 配置逗号分隔的变量名，或启动时重复传 `--secret-env-name NAME`。

### OpenAI 兼容接口

如果要改用 OpenAI-compatible `/responses` 服务，显式传 `--provider openai`：

```bash
uv run nagi --provider openai
```

默认 OpenAI 兼容接口使用 right.codes 的 Codex endpoint：

```bash
NAGI_OPENAI_API_BASE="https://www.right.codes/codex/v1"
NAGI_RIGHT_CODES_API_KEY="your-right-codes-key"
NAGI_OPENAI_MODEL="gpt-5.4"
```

也可以改成其他 OpenAI-compatible 服务：

```bash
NAGI_OPENAI_API_BASE="https://your-api.example/v1"
NAGI_OPENAI_API_KEY="your-api-key"
NAGI_OPENAI_MODEL="gpt-5.4"
```

### Anthropic 兼容接口

如果要改用 Anthropic-compatible 服务，显式传 `--provider anthropic`：

```bash
uv run nagi --provider anthropic
```

默认 Anthropic 兼容接口使用 right.codes 的 Claude endpoint：

```bash
NAGI_ANTHROPIC_API_BASE="https://www.right.codes/claude/v1"
NAGI_RIGHT_CODES_API_KEY="your-right-codes-key"
NAGI_ANTHROPIC_MODEL="claude-sonnet-4-6"
```

如果你的服务端对多个兼容接口复用了同一套密钥，`nagi` 也支持从 `NAGI_ANTHROPIC_API_KEY` 回退到 `ANTHROPIC_API_KEY`、`NAGI_RIGHT_CODES_API_KEY`、`RIGHT_CODES_API_KEY`、`NAGI_OPENAI_API_KEY` 或 `OPENAI_API_KEY`。

### Ollama

如果要改用本地 Ollama，显式传 `--provider ollama`：

```bash
ollama serve
ollama pull qwen3.5:4b
uv run nagi --provider ollama --model qwen3.5:4b
```

## 常用交互命令

- `/help`：查看内置命令
- `/memory`：查看提炼后的工作记忆
- `/session`：查看当前会话文件路径
- `/reset`：清空当前会话状态
- `/exit` 或 `/quit`：退出 REPL

## 安全与持久化

`nagi` 不会默认把所有动作都放开。像 shell 执行、文件写入这类高风险操作，会受审批模式控制：

- `--approval ask`
- `--approval auto`
- `--approval never`

每次运行结束后，都会在 `.nagi/runs/<run_id>/` 下写出这些文件：

- `task_state.json`
- `trace.jsonl`
- `report.json`

这些内容默认只保存在本地，不需要跟仓库一起提交。

## 开发

新增游戏引擎必须遵循 [自动人物译名流程与引擎接入规范](docs/architecture/character-glossary.md)：
第一步提取人物，第二步确认费用后自动统一译名并用于正文，回填时分离识别键与显示名。
QLIE 和 YU-RIS 已接入工作台流程，不需要人工审核人物译名；旧任务不会自动重译。
部分翻译还必须遵循 [开场剧情选取规范](docs/architecture/opening-selection.md)：按可验证的剧情
入口及跳转选择有序文本，翻译、恢复、部署和审计使用一致的范围；不能自行改为文件排序后截取。

常用本地检查：

```bash
uv run pytest tests -q
uv run ruff check nagi tests scripts
```

内部代码现在按较轻的边界拆分：`nagi/evaluation/` 放 benchmark 和 metrics，`nagi/providers/` 放模型 provider client，`nagi/features/` 放可选运行时能力。新代码应直接使用这些包路径；旧的 `nagi.evaluator`、`nagi.metrics`、`nagi.models` 和 `nagi.memory` import 不再作为公共入口保留。
