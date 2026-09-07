"""命令行入口。

这个模块负责把“用户怎么启动 nagi”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import json
import os
import shutil
import sys
from .paths import workspace_state
import textwrap

from .config import load_project_env, provider_env
from .gameio.qlie import (
    apply_qlie_corpus_plan,
    apply_script_export_plan,
    build_qlie_corpus_plan,
    build_script_export_plan,
    inspect_filepack_toc,
    inspect_game_directory,
    parse_exported_script,
    read_filepack_entry,
    render_corpus_plan_text,
    render_corpus_result_text,
    render_export_plan_text,
    render_export_result_text,
    render_probe_report_text,
    render_report_text,
    render_script_parse_text,
    render_script_survey_text,
    render_toc_report_text,
    survey_script_export,
    write_unknown_review_template,
)
from .providers.clients import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .rag import (
    apply_keyword_index_plan,
    build_keyword_index_plan,
    evaluate_retrieval_benchmark,
    load_keyword_index,
    load_retrieval_benchmark,
    render_index_plan_text,
    render_index_result_text,
    render_retrieval_evaluation_text,
    render_search_response_text,
    search_keyword_index,
)
from .rag.models import KeywordQuery
from .runtime import Nagi, SessionStore
from .translation import (
    TranslationCacheError,
    build_translation_batch_plan,
    build_translation_request,
    build_translation_run_spec,
    initialize_translation_run,
    render_translation_batch_plan_text,
    render_translation_request_text,
)
from .translation.context import TranslationContextConfig, prepare_translation_requests
from .workspace import WorkspaceContext, middle

DEFAULT_SECRET_ENV_NAMES = (
    "NAGI_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "NAGI_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "NAGI_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "NAGI_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "Nagi"
WELCOME_SUBTITLE = "local agent for coding, translation & localization"
WELCOME_STATUS = "calm shell, ready for work"
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /memory  Show the agent's distilled working memory.
    /session Show the path to the saved session file.
    /reset   Clear the current session history and memory.
    /exit    Exit the agent.
    """
).strip()


DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_PROVIDER = "deepseek"
PROVIDER_CHOICES = ("ollama", "openai", "anthropic", "deepseek")
SECRET_ENV_NAMES_VAR = "NAGI_SECRET_ENV_NAMES"


def _effective_provider(args):
    # Provider 选择优先级：
    # 1. 用户显式传入 --provider
    # 2. 项目 .env / shell 里的 NAGI_PROVIDER
    # 3. 代码里的默认 provider
    provider = getattr(args, "provider", None) or provider_env(
        "NAGI_PROVIDER", default=DEFAULT_PROVIDER
    )
    if provider not in PROVIDER_CHOICES:
        choices = ", ".join(PROVIDER_CHOICES)
        raise ValueError(f"unknown provider: {provider}. expected one of: {choices}")
    return provider


def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = provider_env("NAGI_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = provider_env("NAGI_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    if provider == "deepseek":
        model = provider_env("NAGI_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",))
        if model:
            return model
        return DEFAULT_DEEPSEEK_MODEL
    return DEFAULT_OLLAMA_MODEL


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update("PICO_" + name[5:] for name in DEFAULT_SECRET_ENV_NAMES if name.startswith("NAGI_"))
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = provider_env(SECRET_ENV_NAMES_VAR)
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args):
    provider = _effective_provider(args)
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("NAGI_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL)
        api_key = provider_env(
            "NAGI_OPENAI_API_KEY",
            ("OPENAI_API_KEY", "NAGI_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "NAGI_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        )
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("NAGI_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), DEFAULT_ANTHROPIC_BASE_URL)
        api_key = provider_env(
            "NAGI_ANTHROPIC_API_KEY",
            ("ANTHROPIC_API_KEY", "NAGI_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "NAGI_OPENAI_API_KEY", "OPENAI_API_KEY"),
        )
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "deepseek":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("NAGI_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), DEFAULT_DEEPSEEK_BASE_URL)
        api_key = provider_env("NAGI_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
            # Nagi currently uses text-encoded tool calls and does not replay
            # provider-native thinking blocks. Keep the default transport mode
            # deterministic until that protocol is implemented end to end.
            thinking={"type": "disabled"},
        )

    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )


def build_welcome(agent, model, host):
    from . import __version__
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(f"{WELCOME_NAME} {__version__}"),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Nagi 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Nagi`，或一个从旧 session 恢复出来的 `Nagi`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = SessionStore(workspace_state(workspace.repo_root) / "sessions")
    model = _build_model_client(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return Nagi.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            mcp_config=getattr(args, "mcp_config", None),
        )
    return Nagi(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        mcp_config=getattr(args, "mcp_config", None),
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Local agent for software development, translation, and localization; automated translation currently supports QLIE game scripts.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--mcp-config", default=None, help="MCP config file; defaults to NAGI_MCP_CONFIG or .nagi/mcp.json in the workspace.")
    parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=None,
        help="Model backend to use. Defaults to NAGI_PROVIDER or deepseek.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, NAGI_OPENAI_MODEL for openai, NAGI_ANTHROPIC_MODEL for anthropic, and NAGI_DEEPSEEK_MODEL for deepseek when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for deepseek, openai, or anthropic.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    return parser


def build_qlie_arg_parser():
    parser = argparse.ArgumentParser(
        prog="nagi qlie",
        description="QLIE inspection and explicitly applied script-export utilities.",
    )
    commands = parser.add_subparsers(dest="qlie_command", required=True)
    inspect_parser = commands.add_parser(
        "inspect",
        help="Inspect QLIE .pack trailers without extracting or modifying files.",
    )
    inspect_parser.add_argument("game_dir", help="Game directory to inspect recursively.")
    inspect_parser.add_argument(
        "--json",
        action="store_true",
        help="Write the stable inspection report as JSON to stdout.",
    )
    inspect_parser.add_argument(
        "--skip-hash",
        action="store_true",
        help="Skip SHA-256 streaming for a faster metadata-only inspection.",
    )
    list_parser = commands.add_parser(
        "list",
        help="Decode one supported QLIE FilePack table of contents without reading payloads.",
    )
    list_parser.add_argument("archive", help="QLIE .pack archive to inspect.")
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="Write every decoded TOC entry as stable JSON.",
    )
    list_parser.add_argument(
        "--hash",
        action="store_true",
        help="Also stream the complete archive to calculate SHA-256.",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum entries shown in human-readable output; JSON is never truncated.",
    )
    probe_parser = commands.add_parser(
        "probe",
        help="Read and decode exactly one supported QLIE FilePack entry in memory.",
    )
    probe_parser.add_argument("archive", help="QLIE .pack archive containing the entry.")
    selector = probe_parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--index", type=int, help="Zero-based TOC entry index.")
    selector.add_argument("--path", dest="internal_path", help="Exact internal path from qlie list.")
    probe_parser.add_argument(
        "--exe",
        dest="exe_path",
        help="Game EXE containing version-specific QLIE key data; parsed as data and never loaded.",
    )
    probe_parser.add_argument(
        "--key-file",
        dest="key_file_path",
        help="Optional FilePackVer3.0 key.fkey path; otherwise discovered near the archive.",
    )
    probe_parser.add_argument(
        "--json",
        action="store_true",
        help="Write diagnostics as stable JSON; the full decoded payload is never included.",
    )
    export_plan_parser = commands.add_parser(
        "export-plan",
        help="Preview an allowlisted script export without reading entry payloads.",
    )
    export_plan_parser.add_argument("game_dir", help="QLIE game directory.")
    export_plan_parser.add_argument("--output", required=True, help="New export directory outside the game.")
    export_plan_parser.add_argument(
        "--archive",
        dest="archives",
        action="append",
        help="Archive path relative to the game; repeat to select scope, ordered low-to-high only for precedence.",
    )
    export_plan_parser.add_argument(
        "--extension",
        dest="extensions",
        action="append",
        help="Allowlisted extension; repeat as needed. Defaults to .s and .txt.",
    )
    export_plan_parser.add_argument(
        "--conflict-policy",
        choices=("preserve", "precedence"),
        default="preserve",
        help="Preserve duplicate variants in layers, or resolve using explicit archive precedence.",
    )
    export_plan_parser.add_argument("--json", action="store_true", help="Write the complete plan as JSON.")

    export_parser = commands.add_parser(
        "export-scripts",
        help="Preview or explicitly apply a transactional script export.",
    )
    export_parser.add_argument("game_dir", help="QLIE game directory.")
    export_parser.add_argument("--output", required=True, help="New export directory outside the game.")
    export_parser.add_argument(
        "--archive",
        dest="archives",
        action="append",
        help="Archive path relative to the game; repeat to select scope, ordered low-to-high only for precedence.",
    )
    export_parser.add_argument(
        "--extension",
        dest="extensions",
        action="append",
        help="Allowlisted extension; repeat as needed. Defaults to .s and .txt.",
    )
    export_parser.add_argument(
        "--conflict-policy",
        choices=("preserve", "precedence"),
        default="preserve",
        help="Preserve duplicate variants in layers, or resolve using explicit archive precedence.",
    )
    export_parser.add_argument(
        "--exe",
        dest="exe_path",
        help="Game EXE containing version-specific QLIE key data; required for encrypted 3.x entries.",
    )
    export_parser.add_argument(
        "--key-file",
        dest="key_file_path",
        help="Optional FilePackVer3.0 key.fkey path; otherwise discovered near the archive.",
    )
    export_parser.add_argument(
        "--apply",
        action="store_true",
        help="Decode and publish the planned scripts; omission is a dry run.",
    )
    export_parser.add_argument("--json", action="store_true", help="Write the plan or result as JSON.")
    survey_parser = commands.add_parser(
        "survey-scripts",
        help="Validate and structurally survey a Phase 1 export without emitting script text.",
    )
    survey_parser.add_argument("export_dir", help="Phase 1 export directory containing manifest.json.")
    survey_parser.add_argument(
        "--top-shapes",
        type=int,
        default=30,
        help="Maximum redacted syntax shapes included in the report.",
    )
    survey_parser.add_argument(
        "--json",
        action="store_true",
        help="Write stable metadata-only survey JSON.",
    )
    parse_parser = commands.add_parser(
        "parse-script",
        help="Parse exactly one manifest-addressed script into Segment v1 values.",
    )
    parse_parser.add_argument(
        "export_dir",
        help="Phase 1 export directory containing manifest.json.",
    )
    parse_parser.add_argument(
        "--path",
        dest="output_path",
        required=True,
        help="Exact POSIX output_path from the Phase 1 manifest.",
    )
    parse_output = parse_parser.add_mutually_exclusive_group()
    parse_output.add_argument(
        "--json",
        action="store_true",
        help="Write a metadata-only parse report without script text.",
    )
    parse_output.add_argument(
        "--emit-jsonl",
        action="store_true",
        help="Explicitly emit full Segment v1 JSONL, including source text.",
    )
    corpus_parser = commands.add_parser(
        "build-corpus",
        help="Plan or explicitly publish a validated Segment v1 corpus.",
    )
    corpus_parser.add_argument(
        "source_dir",
        help="Phase 1 export directory containing manifest.json.",
    )
    corpus_parser.add_argument(
        "--output",
        required=True,
        help="New corpus directory outside the source export and Git worktrees.",
    )
    corpus_parser.add_argument(
        "--unknown-review",
        help="Completed local JSONL review for every unknown segment.",
    )
    corpus_parser.add_argument(
        "--recall-target",
        type=float,
        default=0.99,
        help="Minimum accepted parser recall after unknown review.",
    )
    corpus_action = corpus_parser.add_mutually_exclusive_group()
    corpus_action.add_argument(
        "--write-review-template",
        metavar="PATH",
        help="Explicitly write a complete local unknown-review template outside Git.",
    )
    corpus_action.add_argument(
        "--apply",
        action="store_true",
        help="Transactionally publish the ready plan; omission is a dry run.",
    )
    corpus_parser.add_argument(
        "--json",
        action="store_true",
        help="Write stable metadata-only plan or result JSON.",
    )
    return parser


def run_qlie_command(argv):
    args = build_qlie_arg_parser().parse_args(argv)
    if args.qlie_command == "inspect":
        try:
            report = inspect_game_directory(args.game_dir, hash_files=not args.skip_hash)
        except ValueError as exc:
            if args.json:
                print(
                    json.dumps(
                        {
                            "engine": "qlie",
                            "error": str(exc),
                            "schema_version": 1,
                            "status": "invalid_input",
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"QLIE inspection failed: {exc}", file=sys.stderr)
            return 2
        print(report.to_json() if args.json else render_report_text(report))
        return 0

    if args.qlie_command == "list":
        if args.limit < 0:
            print("QLIE TOC listing failed: --limit must be zero or greater", file=sys.stderr)
            return 2
        report = inspect_filepack_toc(args.archive, hash_file=args.hash)
        print(
            report.to_json()
            if args.json
            else render_toc_report_text(report, limit=args.limit)
        )
        return 0 if report.status in {"supported", "partial"} else 2

    if args.qlie_command == "probe":
        result = read_filepack_entry(
            args.archive,
            entry_index=args.index,
            internal_path=args.internal_path,
            exe_path=args.exe_path,
            key_file_path=args.key_file_path,
        )
        print(
            result.report.to_json()
            if args.json
            else render_probe_report_text(result.report)
        )
        return 0 if result.report.status == "supported" else 2

    if args.qlie_command == "survey-scripts":
        report = survey_script_export(args.export_dir, top_shapes=args.top_shapes)
        print(
            report.to_json()
            if args.json
            else render_script_survey_text(report)
        )
        return 0 if report.status == "supported" else 2

    if args.qlie_command == "parse-script":
        result = parse_exported_script(args.export_dir, args.output_path)
        if args.emit_jsonl and result.status == "supported":
            print(result.to_jsonl(), end="")
        elif args.emit_jsonl:
            print(render_script_parse_text(result), file=sys.stderr)
        else:
            print(
                result.to_json()
                if args.json
                else render_script_parse_text(result)
            )
        return 0 if result.status == "supported" else 2

    if args.qlie_command == "build-corpus":
        plan = build_qlie_corpus_plan(
            args.source_dir,
            args.output,
            review_path=args.unknown_review,
            recall_target=args.recall_target,
        )
        if args.write_review_template:
            try:
                template_path = write_unknown_review_template(
                    plan, args.write_review_template
                )
            except (OSError, ValueError) as exc:
                print(f"QLIE unknown review template failed: {exc}", file=sys.stderr)
                return 2
            if args.json:
                payload = plan.to_dict()
                payload["unknown_review_template"] = str(template_path)
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(render_corpus_plan_text(plan))
                print(f"unknown_review_template: {template_path}")
            return 0
        if not args.apply:
            print(plan.to_json() if args.json else render_corpus_plan_text(plan))
            return 0 if plan.status in {"ready", "review_required"} else 2
        result = apply_qlie_corpus_plan(plan)
        print(result.to_json() if args.json else render_corpus_result_text(result))
        return 0 if result.status == "published" else 2

    if args.qlie_command in {"export-plan", "export-scripts"}:
        plan = build_script_export_plan(
            args.game_dir,
            args.output,
            archives=args.archives,
            extensions=args.extensions,
            conflict_policy=args.conflict_policy,
        )
        if args.qlie_command == "export-plan" or not args.apply:
            print(plan.to_json() if args.json else render_export_plan_text(plan))
            return 0 if plan.status == "ready" else 2
        result = apply_script_export_plan(
            plan,
            exe_path=args.exe_path,
            key_file_path=args.key_file_path,
        )
        print(result.to_json() if args.json else render_export_result_text(result))
        return 0 if result.status == "exported" else 2

    raise ValueError(f"unsupported QLIE command: {args.qlie_command}")


def build_rag_arg_parser():
    parser = argparse.ArgumentParser(
        prog="nagi rag",
        description="Build and query a local, source-validated keyword index.",
    )
    commands = parser.add_subparsers(dest="rag_command", required=True)
    build_parser = commands.add_parser(
        "build-index",
        help="Plan or explicitly publish a deterministic keyword index.",
    )
    build_parser.add_argument(
        "corpus",
        help="Segment v1 corpus directory or segments.jsonl path.",
    )
    build_parser.add_argument(
        "--output",
        required=True,
        help="New index directory outside the corpus and Git worktrees.",
    )
    build_parser.add_argument(
        "--scope",
        choices=("translatable", "all"),
        default="translatable",
        help="Index only translation candidates, or every Segment v1 record.",
    )
    build_parser.add_argument(
        "--apply",
        action="store_true",
        help="Transactionally publish the index; omission is a dry run.",
    )
    build_parser.add_argument("--json", action="store_true", help="Write stable metadata-only JSON.")

    query_parser = commands.add_parser(
        "query",
        help="Search a published keyword index with optional metadata filters.",
    )
    query_parser.add_argument("index_dir", help="Published keyword index directory.")
    query_parser.add_argument("query", help="Keyword query text.")
    query_parser.add_argument("--corpus", help="Relocated source corpus used for freshness validation.")
    query_parser.add_argument("--limit", type=int, default=10, help="Maximum results from 1 to 100.")
    query_parser.add_argument("--kind", dest="kinds", action="append", default=[], help="Exact Segment kind; repeat as needed.")
    query_parser.add_argument("--speaker", help="Exact normalized speaker filter.")
    query_parser.add_argument("--scene", help="Exact normalized scene filter.")
    query_parser.add_argument("--archive", dest="archive_name", help="Exact archive_name filter.")
    query_parser.add_argument("--path-prefix", dest="output_path_prefix", help="POSIX output_path prefix filter.")
    query_parser.add_argument(
        "--translatable",
        choices=("any", "true", "false"),
        default="true",
        help="Filter by Segment translatable status.",
    )
    query_parser.add_argument("--metadata-only", action="store_true", help="Omit normalized text from JSON results.")
    query_parser.add_argument("--json", action="store_true", help="Write structured search JSON.")
    evaluate_parser = commands.add_parser(
        "evaluate",
        help="Run a fixed synthetic Recall@K, MRR, and latency benchmark.",
    )
    evaluate_parser.add_argument(
        "benchmark",
        help="Versioned synthetic retrieval benchmark JSON.",
    )
    evaluate_parser.add_argument(
        "--repeats",
        type=int,
        default=20,
        help="Timed deterministic searches per task, from 1 to 1000.",
    )
    evaluate_parser.add_argument("--json", action="store_true", help="Write the evaluation report as JSON.")
    return parser


def run_rag_command(argv):
    args = build_rag_arg_parser().parse_args(argv)
    if args.rag_command == "build-index":
        plan = build_keyword_index_plan(args.corpus, args.output, scope=args.scope)
        if not args.apply:
            print(plan.to_json() if args.json else render_index_plan_text(plan))
            return 0 if plan.status == "ready" else 2
        result = apply_keyword_index_plan(plan)
        print(result.to_json() if args.json else render_index_result_text(result))
        return 0 if result.status == "published" else 2

    if args.rag_command == "query":
        translatable = {
            "any": None,
            "true": True,
            "false": False,
        }[args.translatable]
        try:
            index = load_keyword_index(
                args.index_dir,
                source_corpus=args.corpus,
            )
            response = search_keyword_index(
                index,
                KeywordQuery(
                    text=args.query,
                    limit=args.limit,
                    kinds=tuple(args.kinds),
                    speaker=args.speaker,
                    scene=args.scene,
                    archive_name=args.archive_name,
                    output_path_prefix=args.output_path_prefix,
                    translatable=translatable,
                ),
            )
        except (OSError, TypeError, ValueError) as exc:
            if args.json:
                print(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "invalid_input",
                            "reason": str(exc),
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"Keyword search failed: {exc}", file=sys.stderr)
            return 2
        print(
            response.to_json(include_text=not args.metadata_only)
            if args.json
            else render_search_response_text(response)
        )
        return 0

    if args.rag_command == "evaluate":
        try:
            benchmark = load_retrieval_benchmark(args.benchmark)
            report = evaluate_retrieval_benchmark(benchmark, repeats=args.repeats)
        except (OSError, TypeError, ValueError) as exc:
            if args.json:
                print(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "invalid_input",
                            "reason": str(exc),
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"Retrieval benchmark failed: {exc}", file=sys.stderr)
            return 2
        print(report.to_json() if args.json else render_retrieval_evaluation_text(report))
        return 0 if report.passed else 1

    raise ValueError(f"unsupported RAG command: {args.rag_command}")


def build_translate_arg_parser():
    parser = argparse.ArgumentParser(
        prog="nagi translate",
        description="Build content-free, read-only translation batch plans.",
    )
    commands = parser.add_subparsers(dest="translate_command", required=True)
    plan_parser = commands.add_parser(
        "plan-batch",
        help="Validate a published Segment v1 corpus and plan deterministic batches.",
    )
    plan_parser.add_argument("corpus", help="Published Segment v1 corpus directory.")
    plan_parser.add_argument("--model", default="dry-run-model", help="Model identity included in cache keys.")
    plan_parser.add_argument(
        "--prompt-version",
        default="qlie-translation-v1",
        help="Prompt contract version included in cache keys.",
    )
    plan_parser.add_argument(
        "--terminology-version",
        default="none",
        help="Terminology snapshot identity included in cache keys.",
    )
    plan_parser.add_argument(
        "--rag-index-id",
        default="none",
        help="RAG index identity included in cache keys; no retrieval is run in Phase 4A.",
    )
    plan_parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Translation units per planned batch, from 1 to 1000.",
    )
    plan_parser.add_argument(
        "--limit",
        type=int,
        help="Optional maximum selected units; the full corpus is still validated.",
    )
    plan_parser.add_argument(
        "--kind",
        dest="kinds",
        action="append",
        default=[],
        help="Exact Segment kind; repeat as needed.",
    )
    plan_parser.add_argument("--speaker", help="Exact normalized speaker filter.")
    plan_parser.add_argument("--scene", help="Exact normalized scene filter.")
    plan_parser.add_argument(
        "--path-prefix",
        dest="output_path_prefix",
        help="POSIX output_path prefix filter.",
    )
    plan_parser.add_argument(
        "--json",
        action="store_true",
        help="Write the complete metadata-only plan; source text is always omitted.",
    )
    preview_parser = commands.add_parser(
        "preview-request",
        help="Build one stable model request and show metadata without calling a provider.",
    )
    preview_parser.add_argument("corpus", help="Published Segment v1 corpus directory.")
    preview_parser.add_argument("--model", default="dry-run-model", help="Model identity included in the request.")
    preview_parser.add_argument(
        "--prompt-version",
        default="qlie-translation-v1",
        help="Versioned prompt contract identity.",
    )
    preview_parser.add_argument(
        "--terminology-version",
        default="none",
        help="Terminology snapshot identity; preview does not load a glossary.",
    )
    preview_parser.add_argument(
        "--rag-index-id",
        default="none",
        help="RAG index identity; preview does not run retrieval.",
    )
    preview_parser.add_argument(
        "--target-language",
        default="zh-CN",
        help="Target language identifier embedded in the request.",
    )
    preview_parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Translation units per planned batch, from 1 to 1000.",
    )
    preview_parser.add_argument(
        "--batch-ordinal",
        type=int,
        default=1,
        help="One-based planned batch to preview.",
    )
    preview_parser.add_argument(
        "--limit",
        type=int,
        help="Optional maximum selected units; the full corpus is still validated.",
    )
    preview_parser.add_argument("--kind", dest="kinds", action="append", default=[], help="Exact Segment kind; repeat as needed.")
    preview_parser.add_argument("--speaker", help="Exact normalized speaker filter.")
    preview_parser.add_argument("--scene", help="Exact normalized scene filter.")
    preview_parser.add_argument("--path-prefix", dest="output_path_prefix", help="POSIX output_path prefix filter.")
    preview_parser.add_argument(
        "--json",
        action="store_true",
        help="Write metadata only; the generated prompt and source text are omitted.",
    )
    init_parser = commands.add_parser(
        "init-run",
        help="Preview or explicitly initialize a resumable translation run without calling a model.",
    )
    init_parser.add_argument("corpus", help="Published Segment v1 corpus directory.")
    init_parser.add_argument("--output", required=True, help="New run directory outside the corpus and Git worktrees.")
    init_parser.add_argument("--model", default="dry-run-model", help="Model identity included in cache keys.")
    init_parser.add_argument("--prompt-version", default="qlie-translation-v1", help="Versioned prompt contract identity.")
    init_parser.add_argument("--terminology-version", default="none", help="Terminology snapshot identity.")
    init_parser.add_argument("--rag-index-id", default="none", help="RAG index identity; initialization runs no retrieval.")
    init_parser.add_argument("--target-language", default="zh-CN", help="Target language identifier for every request.")
    init_parser.add_argument("--batch-size", type=int, default=32, help="Translation units per planned batch, from 1 to 1000.")
    init_parser.add_argument("--limit", type=int, help="Optional maximum selected units; the full corpus is still validated.")
    init_parser.add_argument("--kind", dest="kinds", action="append", default=[], help="Exact Segment kind; repeat as needed.")
    init_parser.add_argument("--speaker", help="Exact normalized speaker filter.")
    init_parser.add_argument("--scene", help="Exact normalized scene filter.")
    init_parser.add_argument("--path-prefix", dest="output_path_prefix", help="POSIX output_path prefix filter.")
    init_parser.add_argument("--apply", action="store_true", help="Create the manifest and initial checkpoint; omission is a dry run.")
    init_parser.add_argument("--json", action="store_true", help="Write content-free run metadata as JSON.")
    for context_parser in (preview_parser, init_parser):
        context_parser.add_argument("--with-context", action="store_true", help="Retrieve bounded local source evidence and adjacent text; no model call.")
    routes_parser = commands.add_parser("analyze-routes", help="Inspect static route may/must history without model calls.")
    routes_parser.add_argument("corpus", help="Published QLIE or extracted text-engine corpus directory.")
    routes_parser.add_argument("--game-dir", help="Original YU-RIS game directory (defaults to extraction manifest).")
    routes_parser.add_argument("--segment-id", help="Dialogue/narration ID whose incoming history should be inspected.")
    routes_parser.add_argument("--entry-segment-id", action="append", default=[], help="Explicit entry ID; repeat to analyse all given entries.")
    return parser


def run_translate_command(argv):
    args = build_translate_arg_parser().parse_args(argv)
    if args.translate_command == "analyze-routes":
        from pathlib import Path
        from .translation.routes import analyze_corpus_routes
        if (Path(args.corpus) / "extraction.json").is_file():
            from .translation.route_context import analyze_extracted_routes
            analysis, _ = analyze_extracted_routes(args.corpus, game_dir=args.game_dir,
                                                   entry_ids=args.entry_segment_id)
        else:
            plan = build_translation_batch_plan(args.corpus)
            if plan.status != "ready":
                raise ValueError("route analysis requires a complete validated corpus")
            analysis = analyze_corpus_routes(plan, entry_segment_ids=args.entry_segment_id)
        payload = analysis.summary() if analysis else {"status": "unsupported", "reason": "no_control_flow_adapter"}
        if analysis:
            payload["branches"] = list(analysis.program.branches)
            if args.segment_id:
                payload["history"] = analysis.history(args.segment_id)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    prepared = None
    context_config = None
    if getattr(args, "with_context", False):
        try:
            if args.rag_index_id != "none" or args.terminology_version != "none":
                raise ValueError("--with-context derives rag/terminology identities; do not override them")
            prepared = prepare_translation_requests(args.corpus, model_id=args.model,
                prompt_version=args.prompt_version, target_language=args.target_language,
                config=context_config, batch_size=args.batch_size, limit=args.limit,
                kinds=tuple(args.kinds), speaker=args.speaker, scene=args.scene,
                output_path_prefix=args.output_path_prefix)
        except (OSError, TypeError, ValueError) as exc:
            payload = {"schema_version": 1, "status": "invalid_input", "reason": str(exc)}
            print(json.dumps(payload, ensure_ascii=False) if args.json else f"Translation context failed: {exc}")
            return 2
    if args.translate_command == "plan-batch":
        plan = build_translation_batch_plan(
            args.corpus,
            model_id=args.model,
            prompt_version=args.prompt_version,
            terminology_version=args.terminology_version,
            rag_index_id=args.rag_index_id,
            batch_size=args.batch_size,
            limit=args.limit,
            kinds=tuple(args.kinds),
            speaker=args.speaker,
            scene=args.scene,
            output_path_prefix=args.output_path_prefix,
        )
        print(plan.to_json() if args.json else render_translation_batch_plan_text(plan))
        return 0 if plan.status in {"ready", "empty"} else 2
    if args.translate_command == "preview-request":
        plan = prepared[0] if prepared else build_translation_batch_plan(
            args.corpus,
            model_id=args.model,
            prompt_version=args.prompt_version,
            terminology_version=args.terminology_version,
            rag_index_id=args.rag_index_id,
            batch_size=args.batch_size,
            limit=args.limit,
            kinds=tuple(args.kinds),
            speaker=args.speaker,
            scene=args.scene,
            output_path_prefix=args.output_path_prefix,
        )
        if plan.status != "ready":
            print(plan.to_json() if args.json else render_translation_batch_plan_text(plan))
            return 2
        if args.batch_ordinal < 1 or args.batch_ordinal > len(plan.batches):
            message = f"batch_ordinal must be between 1 and {len(plan.batches)}"
            if args.json:
                print(json.dumps({"schema_version": 1, "status": "invalid_input", "reason": message}, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"Translation request preview failed: {message}", file=sys.stderr)
            return 2
        batch = plan.batches[args.batch_ordinal - 1]
        request = prepared[1][args.batch_ordinal - 1] if prepared else build_translation_request(
            batch,
            model_id=plan.config.model_id,
            prompt_version=plan.config.prompt_version,
            terminology_version=plan.config.terminology_version,
            rag_index_id=plan.config.rag_index_id,
            target_language=args.target_language,
        )
        print(request.to_json() if args.json else render_translation_request_text(request))
        return 0
    if args.translate_command == "init-run":
        plan = prepared[0] if prepared else build_translation_batch_plan(
            args.corpus,
            model_id=args.model,
            prompt_version=args.prompt_version,
            terminology_version=args.terminology_version,
            rag_index_id=args.rag_index_id,
            batch_size=args.batch_size,
            limit=args.limit,
            kinds=tuple(args.kinds),
            speaker=args.speaker,
            scene=args.scene,
            output_path_prefix=args.output_path_prefix,
        )
        if plan.status != "ready":
            print(plan.to_json() if args.json else render_translation_batch_plan_text(plan))
            return 2
        requests = prepared[1] if prepared else tuple(
            build_translation_request(
                batch,
                model_id=plan.config.model_id,
                prompt_version=plan.config.prompt_version,
                terminology_version=plan.config.terminology_version,
                rag_index_id=plan.config.rag_index_id,
                target_language=args.target_language,
            )
            for batch in plan.batches
        )
        spec = build_translation_run_spec(plan, requests)
        output_dir = os.path.abspath(os.path.expanduser(args.output))
        status = "ready"
        reason = "dry_run"
        artifacts = {"manifest": None, "checkpoint": None, "candidates": None}
        if args.apply:
            try:
                snapshot = (context_config or TranslationContextConfig()).to_dict() if prepared else None
                initialized = initialize_translation_run(spec, output_dir, context_config=snapshot)
            except (OSError, TranslationCacheError, ValueError) as exc:
                payload = {
                    "schema_version": 1,
                    "status": "invalid_input",
                    "reason": str(exc),
                    "run_id": spec.run_id,
                    "plan_id": spec.plan_id,
                    "output_dir": output_dir,
                    "side_effects": {
                        "model_called": False,
                        "output_written": False,
                        "game_modified": False,
                    },
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) if args.json else f"Translation run initialization failed: {exc}")
                return 2
            status = "initialized"
            reason = "initial_checkpoint_created"
            artifacts = {
                "manifest": str(initialized / "manifest.json"),
                "checkpoint": str(initialized / "checkpoint.json"),
                "candidates": str(initialized / "candidates"),
            }
        payload = {
            "schema_version": 1,
            "status": status,
            "reason": reason,
            "run_id": spec.run_id,
            "plan_id": spec.plan_id,
            "output_dir": output_dir,
            "summary": {
                "batch_count": len(spec.requests),
                "unit_count": sum(len(request.units) for request in spec.requests),
            },
            "artifacts": artifacts,
            "side_effects": {
                "model_called": False,
                "output_written": bool(args.apply),
                "game_modified": False,
            },
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("\n".join((
                "Translation Run Initialization",
                f"status: {status}",
                f"run_id: {spec.run_id}",
                f"plan_id: {spec.plan_id}",
                f"output_dir: {output_dir}",
                f"batch_count: {len(spec.requests)}",
                f"unit_count: {sum(len(request.units) for request in spec.requests)}",
                "model_called: false",
                f"output_written: {str(bool(args.apply)).lower()}",
                "game_modified: false",
            )))
        return 0
    raise ValueError(f"unsupported translation command: {args.translate_command}")


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv == ["--version"]:
        from . import __version__
        print(f"Nagi {__version__}")
        return 0
    if raw_argv[:1] == ["web"]:
        from .webapp import main as run_web_app

        return run_web_app(raw_argv[1:])
    if raw_argv[:1] == ["qlie"]:
        return run_qlie_command(raw_argv[1:])
    if raw_argv[:1] == ["rag"]:
        return run_rag_command(raw_argv[1:])
    if raw_argv[:1] == ["translate"]:
        return run_translate_command(raw_argv[1:])

    args = build_arg_parser().parse_args(raw_argv)
    agent = build_agent(args)

    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                print(agent.ask(prompt))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        try:
            user_input = input("\nnagi> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
