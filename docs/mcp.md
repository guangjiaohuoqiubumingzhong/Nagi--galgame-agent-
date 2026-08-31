# Nagi 的 MCP 工具接入

Nagi 是 MCP **客户端**：通过官方 Python SDK 连接服务端，完成初始化、工具发现、
参数校验、调用和关闭连接。它没有把原有工具替换成 MCP，也没有把 Nagi 变成 MCP 服务端。

当前支持 stdio（本地进程）及 Streamable HTTP；不支持旧版独立 HTTP+SSE transport。
Streamable HTTP 内部的 SSE 响应由 SDK 处理。暂不提供 OAuth 登录流程、资源/提示词
浏览、sampling、elicitation、roots 或二进制多模态输出渲染。

## 安装与启动

在 Nagi 项目目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
```

也可使用 `uv sync --extra mcp`，随后用 `uv run --extra mcp nagi` 启动。
仅使用本地工具及“视觉小说翻译”时不需要安装这个可选依赖。

已有桌面快捷方式继续可用。更新代码后，需要在任务结束后重启 Web 后端；
只刷新网页或再次点击快捷方式不会替换已运行的旧后端。

## 配置

默认读取当前工作区的 `.nagi/mcp.json`，不是固定读取 Nagi 安装目录。
CLI 可用 `--mcp-config 路径` 覆盖；CLI 与 Web 均可用环境变量 `NAGI_MCP_CONFIG`。
优先级：显式参数 → 环境变量 → 工作区默认路径。相对配置路径及 stdio 的 `cwd`
均相对于当前工作区。配置只在创建 Nagi 实例时读取；Web 中下一次发消息会创建新实例，
CLI 需要重新启动。读取配置、创建/打开会话本身不会启动进程或访问服务端。

以下示例默认禁用，替换成自己信任的服务与路径后再启用：

```json
{
  "mcpServers": {
    "local_tools": {
      "disabled": true,
      "transport": "stdio",
      "command": "C:\\path\\to\\python.exe",
      "args": ["C:\\path\\to\\my_mcp_server.py"],
      "cwd": ".",
      "env": {"SERVICE_API_KEY": "${MY_SERVICE_API_KEY}"},
      "timeout": 30
    },
    "remote_tools": {
      "disabled": true,
      "transport": "http",
      "url": "https://your-mcp-service.example/mcp",
      "headers": {"Authorization": "Bearer ${MY_MCP_TOKEN}"},
      "timeout": 30,
      "tools": ["search", "lookup"]
    }
  }
}
```

- `command` 是可执行文件，参数放在 `args`；不经过 shell 拼接。Windows 上推荐完整路径。
- `transport` 可省略：有 `url` 时使用 HTTP，否则 stdio。`streamable-http` 是 `http` 的别名。
- `disabled: true` 跳过该服务；启用时删除该项或设为 `false`。
- `env` 和 `headers` 支持 `${环境变量名}`，变量必须存在。不要把令牌写进 URL、模型消息或命令行参数。
- stdio 不继承整个 Nagi 环境，只保留 SDK 必需的基础环境，并加入显式 `env`。
  本地进程拥有当前用户权限，**不是沙箱**；请先检查来源和启动命令。
- 远程地址要求 HTTPS；HTTP 只允许 localhost 或回环 IP。不自动跟随重定向，不使用系统代理环境变量。
- `tools` 是可选的服务端工具名称白名单。省略表示允许发现该服务器的全部工具；空数组表示不允许任何工具。
- `timeout` 是每次连接/请求的等待上限，范围 0.1～300 秒，默认 30 秒；关闭连接另有短暂清理时间。
- 顶层只接受 `mcpServers`。服务名限字母、数字、下划线和连字符。最多 32 个服务，配置文件最多 256 KB。

`.nagi/` 已被 Git 忽略。缺少依赖、配置错误或服务不可用会返回诊断，不会关闭本地工具。
`mcp_list_servers` 不显示命令、地址、环境变量值或请求头。已配置的 env/header 值会在
MCP 返回内容、运行记录中脱敏；仍应避免让服务输出任何凭据。

## 对话中使用

可以直接对 Nagi 说：“列出已配置的 MCP 服务，查看 local_tools 有哪些工具，再帮我执行……”。
模型会使用四个固定入口：

1. `mcp_list_servers({})`：只读本地配置概况，不连接。
2. `mcp_list_tools({"server":"local_tools"})`：审批后连接并发现工具，每页 8 项；下一页传 `offset`。
3. `mcp_describe_tool({"server":"local_tools","tool":"search"})`：读取本轮已发现工具的完整 inputSchema，不连接。
4. `mcp_call_tool({"server":"local_tools","tool":"search","arguments":{...}})`：本地验证参数，审批通过后调用。

服务端工具即使叫 `read_file` 或 `translate_game_batch`，也只能从 `mcp_call_tool` 调用，
不会覆盖 Nagi 的同名本地工具。发现上限为每服务 256 个工具、32 页；超过 2800 字符
或无效的输入/输出 schema 会标记为不可调用，不会偷偷省略约束。输入参数与声明了
outputSchema 的结构化结果都会本地校验；JSON Schema 不访问远程引用。
输出过长时返回带 `truncated` 标记的 JSON 摘要。文本及结构化结果受支持，图片、音频、
二进制资源只返回省略提示，资源链接不会自动访问。

## 审批、生命周期与翻译兼容

- `mcp_list_tools` 可能启动进程，`mcp_call_tool` 可能造成外部修改，两者都走 Nagi 原有审批。
  `ask` 在网页/终端询问，`never` 或只读运行拒绝，`auto` 沿用用户显式选择的自动审批。
- 服务自报的 `readOnlyHint` 等注解不构成授权。规划模式不能连接或调用 MCP；只读委派也不会继承 MCP 配置。
- 服务返回的描述、内容和错误属于外部数据，不是对 Nagi 的指令；不会采用服务器初始化返回的 instructions。
- 同一次 `ask()` 中复用连接；结束、失败、取消、重置时关闭连接。下一轮需重新发现，旧会话不会携带活连接。
  直接通过 Python 调用 `execute_tool()` 时，请使用 `with Nagi(...) as agent:` 或在 `finally` 中调用 `agent.close()`。
- `isError` 会记录成工具失败。超时或断连后不自动重试；外部动作可能已经执行，重试前应确认实际状态。
- 原有 `translation_run_status` / `translate_game_batch` 的注册、审批和批次校验保持原样；
  “视觉小说翻译”的提取、翻译、校验与独立输出流程不依赖 MCP。

协议与 SDK 参考：[MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、
[官方 Python SDK v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。
