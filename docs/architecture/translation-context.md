# 自动翻译上下文与混合检索（第二版）

网页翻译自动构建原文上下文，不需要用户准备参考资料。已移除网页文件选择框、任务 API 的 context 导入字段、CLI 的 --context-file，以及原来的参考 JSON 示例。已有运行目录中的历史快照不删除，但不能作为新版本的参考资料导入。

## 实际流程

1. 完整验证原文语料及哈希，读取对白、旁白、场景标签和控制行。
2. 从脚本结构划分能够确认的连续前文范围；未知路线不猜测。
3. 在范围内按完整对白切块，建立 BM25 和本地 E5 向量索引。
4. 用当前文本及最多四条前文形成查询。先过滤来源、位置、分支和当前批次/相邻窗口，再做召回。
5. BM25 偏重字面实体和有区分度的事件词；向量检索负责相关表达和事件。
6. 用相同 chunk ID 做 RRF 排名融合，少量候选交给本地 cross-encoder 重排；低相关结果不采用。
7. 按整批预算选择证据。相同证据在 user 消息的 rag_evidence_pool 只放一份，各单元通过 rag_evidence_ids 引用。
8. 所有请求准备完成后，再逐批调用翻译模型。响应、结构校验和独立脚本发布沿用原来的流程。

system 仍只放翻译规则，user 放原文与参考数据，assistant 返回译文 JSON。翻译批次不回传其他批次的 assistant，新生成译文也不回灌检索库。

## 选定的本地模型

- Embedding：[intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small/blob/main/README.md)，384 维、多语言，512-token 输入。使用原作者 ONNX 权重，query/passage 前缀、带 attention mask 的平均池化和 L2 归一化遵循模型说明。
- Rerank：[cross-encoder/mmarco-mMiniLMv2-L12-H384-v1](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1)，使用原仓库的 AVX2 量化 ONNX 权重，返回原始 logit，不将它解释成正确概率。

选择 E5-small 是针对本地 CPU、小段日文对白和资源占用的工程取舍，不是宣称它在所有检索榜单上最好。Qwen3-Embedding-0.6B、BGE-M3 等更大模型没有在此项目里做等条件质量/延迟比较。

模型版本及文件 SHA-256 固定在 nagi/rag/semantic.py 中。模型只在显式部署时下载到 .nagi/models/translation-rag，翻译过程没有模型下载、远程 embedding/rerank 或隐式复用聊天密钥。ONNX Runtime 使用 CPU，不要求 CUDA，也不执行模型仓库的 Python。

首次部署：

    .\.venv\Scripts\python.exe -m pip install -e ".[rag]"
    .\.venv\Scripts\python.exe -m nagi.rag.semantic download
    .\.venv\Scripts\python.exe -m nagi.rag.semantic status

四个文件约 623 MB；下载校验通过后可离线检索。模型目录未进入 Git。缺少模型或依赖时，网页明确显示 BM25 模式，不把关键词哈希向量冒充语义向量；已经安装但校验失败的模型会报错，不静默切换策略。

## 路线与“前文”的保守边界

目前没有完整的游戏控制流图，也不自动推断路线名称。

- 不跨资源包/脚本检索，不把文件名排序当成剧情顺序。
- 场景标签变化、选择项、未知行、不能确认性质的控制行切断前文范围。
- 少量明确的显示/音频指令允许穿过；变量赋值、跳转及未知指令按边界处理。
- 即使同一脚本内，候选也必须完整处于当前单元之前，不能跨越以上边界。
- 不召回当前批次已有原文、相邻窗口或重叠证据。
- 公共线到个人线、跨场景的历史事件暂不放开，直到能够证明其前置关系。第一版可能漏掉一些真实相关事件，但不会假定未确认的分支是已发生历史。
- 相邻前/后一句是独立上下文，不标成历史事件。

每块保留原文片段 ID、脚本、场景和位置，支持追溯。实体匹配来自说话人字段、明确的名称形式和文本规范化；不会猜测姓氏、昵称指向同一个人，也不会自动编造中文固定译名。

## 默认参数与预算

| 参数 | 当前值 |
| --- | --- |
| Chunk | 最多 320 个 E5 tokens，按完整对白切分 |
| Overlap | 最多两条短对白，且不超过前块约 20% |
| 查询 | 当前原文 + 最多四条前文，共最多 96 个 E5 tokens |
| BM25 召回 | 每查询最多 6 块 |
| 向量召回 | 每查询最多 10 块 |
| RRF | k=60，按 chunk ID 合并 |
| Rerank | 每查询最多 8 块 |
| 最终选择 | 每查询 0–2 块，整批最多 4 个不同块 |
| RAG 预算 | 整批 3,000 字符，含池内容、元数据、引用 ID 的保守开销 |
| 完整请求保护 | 序列化消息最多 60,000 UTF-8 字节，超出报错，不截断原文 |

窗口不足时允许少于目标长度。超长单条原文仍参与翻译，但不强行截断成检索证据；过长查询跳过检索。重排前检查 query/passage 对的长度，不让模型静默截断。没有 E5 时，用 UTF-8 字节数作为保守切块上限，不声称这是真实模型 token 数。完整请求字节保护也不是翻译模型 tokenizer 的精确 token 预算。

向量候选初始 cosine 阈值为 0.75；重排接受阈值为原始 logit -4.0。这是有限日文合成样例验证后的起始工作点，不是通用阈值或置信概率。最终质量需要在真实游戏审校集上继续评估。只有 embedding、缺少 reranker 时，语义候选还必须具备关键词支持。

批次最多 32 条，自动在上述安全边界处拆分，不为凑满数量跨分支。新部分翻译沿已确认开场选择最多 50 条对白/旁白，分为多个批次；旧 32 条任务仅按原范围恢复。即使部分翻译，也从完整语料建立可用索引。

## 缓存、继续和取消

- 模型权重缓存与文本向量缓存分开。
- .nagi/rag-cache/retrieval.sqlite3 保存内容寻址的向量和重排分数，不保存原文正文或密钥；向量本身仍应视为私有数据。
- 缓存键含模型固定版本、处理策略、输入内容和 query/passage 角色。原文变化只重新计算受影响的输入；换模型/切块策略不会误用旧向量。
- 查询向量和重排分数同样缓存；两次任务可以复用。
- 语料哈希、切块、策略和模型身份进入 tctx_v2 索引 ID，证据内容继续进入翻译请求与候选缓存身份。新旧版本不会混用翻译结果。
- context-config.json 现在只是内部自动策略快照，不包含人工参考资料，也不是导入接口。
- 同一运行的模型对象固定；准备阶段可以安全停止，模型单次推理结束后检查取消，不在推理中途破坏缓存。

## CLI 与 Python

为保留预览命令的无模型调用语义，CLI 的 --with-context 使用离线 BM25：

    nagi translate preview-request "D:\qlie-corpus" --with-context --limit 32 --json
    nagi translate init-run "D:\qlie-corpus" --with-context --output "D:\translation-run" --apply --json

Python / 网页使用 prepare_translation_requests(..., embedder=..., reranker=..., cache=...) 接入本地神经检索。网页会自动加载已安装模型，不需要上传文件。底层 build_translation_request 仍只负责消息装配，不自己启动检索。

## 验证范围

单元测试覆盖前文过滤、分支/场景隔离、块大小和重叠、语义候选重排、空结果、共享证据预算、缓存复用及身份失效、取消和已删除的导入入口。

真实本地模型冒烟测试（仅合成日文，不调用翻译 API）：

    $env:NAGI_TEST_LOCAL_RAG = "1"
    .\.venv\Scripts\python.exe -m pytest tests/test_translation_semantic.py -q

这验证了咖啡店约定样例、无关片段排序及实际消息注入，不是全游戏译文质量提升的证明。
