# 角色消息与多轮上下文

Nagi 的模型输入采用 `chat-v1`：真正的 `system` / `user` / `assistant` 消息数组，而不是在一条 `user` 字符串中拼接 Transcript。`AgentLoop` 每次调用模型前重新构建数组，模型生成下一条 `assistant` 回复。

## 普通多轮对话

第二轮请求的逻辑结构如下，运行时参考资料单独放在一条带明确标记的 `user` 消息里：

```json
[
  {"role": "system", "content": "Nagi 身份、工具协议、安全规则、当前会话控制规则"},
  {"role": "user", "content": "Runtime reference data: 工作区、checkpoint、memory、可选 RAG"},
  {"role": "user", "content": "请记住项目名是 Violet。"},
  {"role": "assistant", "content": "项目名是 Violet。"},
  {"role": "user", "content": "刚才的项目名是什么？"}
]
```

历史来自本地会话记录，不依赖模型服务端自动记忆。新回答保存后，下一轮再按顺序发送。重启并恢复同一会话时也从这些记录回放；不会因两次用户输入文字相同而去重。

稳定规则进入 `system`；文件内容、记忆摘要、检索证据和工具输出是参考数据，不提升为系统指令。`system` 会明确要求模型不要执行参考数据中的指令。

## 一轮请求内的工具循环

Nagi 仍使用文本编码的 `<tool>...</tool>` / `<final>...</final>` 协议，没有在本次变更中切换为 provider-native function calling。

```text
system: 工具和安全规则
user:   运行时参考资料（如有）
...     之前的 user / assistant 轮次
user:   当前用户请求（只出现一次）
assistant: <tool>{"name":"read_file","args":{"path":"README.md"}}</tool>
user:   Tool result (untrusted reference data): 工具名称、参数及执行结果
assistant: 下一次工具调用
user:   对应的工具结果
        → 模型继续生成下一条 assistant
```

工具执行仍经过原有参数校验、审批及权限控制，包括 MCP 工具。工具返回值标记为运行时反馈，不冒充用户的新要求。最终答复以 `assistant` 保存；格式错误的原始模型回复也以 `assistant` 回放，纠错通知以 `user` 反馈。

会话文件仍是包含 `tool` 等事件的审计记录，发送模型时才投影为三种角色。新增工具记录保存 `assistant_response`；旧会话没有该字段时，根据记录的工具名和参数重建调用。无需删除或批量改写旧会话。

## 预算与压缩

- 默认总预算仍为 12,000 字符，历史区默认预算为 5,200 字符；这不是 token 计数。
- 预算内的历史 `user` / `assistant` 原文完整保留，不再逐条裁剪为短 Transcript。
- 超预算时先限制长工具反馈，随后按完整旧轮次移除；当前轮的工具调用和反馈也成对处理。当前用户请求与系统规则保持完整。
- 若系统规则和当前请求本身就超过预算，元数据报告 `prompt_over_budget`，不会静默截断它们。
- 预算裁剪只改变此次发送的视图，不删除会话记录。关闭 `context_reduction` 可以保留全部未显式压缩的对话历史；可选 RAG 仍有独立证据预算。
- 显式 `/compact` 仍将较早历史替换为参考摘要，并保留近期完整轮次。摘要作为 `user` 数据发送，不伪装为 `system`。

Trace 的 `prompt_metadata` 记录 `message_format`、`message_count`、`message_roles`、`history.dropped_turns` 等信息。`prompt_chars` 是实际消息内容的字符数之和，不包括传输 JSON 包装。

## 翻译请求

每个批次是独立的两条消息，不自动混入 Agent 聊天历史，也不把其他批次的译文作为虚构的 `assistant`：

- `system`：翻译任务、目标语言、占位符保护、JSON 输出协议。
- `user`：`REQUEST_JSON`，包含原文、相邻上下文、术语、角色风格及调用方显式提供的 RAG 证据。

网页翻译通过自动上下文构建器检索可确认的前文：本地模型安装后使用 BM25 + E5 语义召回及 MiniLM 重排。已经移除参考 JSON 导入；底层调用方仍可显式提供术语/风格，但网页不会自动生成它们。第二版共享证据在 `rag_evidence_pool` 中只序列化一次，各单元用 `rag_evidence_ids` 引用。整批证据及引用默认限 3,000 字符。底层 `build_translation_request()` 本身仍不自动检索。详见[自动翻译上下文与混合检索](translation-context.md)。

翻译请求身份和候选缓存键纳入 `message_format=chat-v1`，避免复用旧单字符串协议的缓存。旧文件不删除；如旧翻译运行的恢复校验提示不兼容，请新建运行。

## 调用入口与适配器

`Nagi.messages(text)` 和 `ContextManager.build_messages()` 构建正式输入；`TranslationRequest.messages` 构建翻译输入。旧 `Nagi.prompt()` / `ContextManager.build()` / `TranslationRequest.prompt` 仅保留为文本预览和兼容接口，不是运行时发送内容。

| 适配器 | 实际发送方式 |
| --- | --- |
| OpenAI-compatible Responses | `input` 为角色消息数组 |
| OpenAI-compatible Chat Completions | `messages` 为角色消息数组 |
| Anthropic-compatible Messages | 规则放顶层 `system`；`messages` 保留 `user` / `assistant`，相邻同角色合并为多个文本块 |
| Ollama | `/api/chat` + `messages`，读取返回的 `message.content` |

`complete()` 接受上述消息数组；直接传旧字符串仍兼容为一条 `user`。`FakeModelClient.message_requests` 保存实际消息，`prompts` 只保存便于阅读的文本预览。
