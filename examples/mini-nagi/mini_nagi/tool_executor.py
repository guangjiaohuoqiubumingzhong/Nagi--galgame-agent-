from dataclasses import dataclass

from . import tools
from .workspace import clip


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


class ToolExecutor:
    def __init__(self, workspace, approval_policy="auto"):
        self.workspace = workspace
        self.approval_policy = approval_policy

    def execute(self, name, args):
        spec = tools.TOOL_SPECS.get(name)
        if spec is None:
            return ToolExecutionResult(f"error: unknown tool '{name}'", _metadata("rejected", name, read_only=False))
        try:
            tools.validate_tool(self.workspace, name, args)
        except Exception as exc:
            return ToolExecutionResult(
                f"error: invalid arguments for {name}: {exc}",
                _metadata("rejected", name, error_code="invalid_arguments", read_only=not spec.risky),
            )
        if spec.risky and self.approval_policy == "never":
            return ToolExecutionResult(
                f"error: approval denied for {name}",
                _metadata("rejected", name, error_code="approval_denied", read_only=False),
            )
        try:
            content = clip(tools.run_tool(self.workspace, name, args))
            affected_paths = []
            if spec.risky and isinstance(args, dict) and args.get("path"):
                affected_paths.append(str(args["path"]))
            return ToolExecutionResult(
                content,
                _metadata(
                    "ok",
                    name,
                    read_only=not spec.risky,
                    workspace_changed=bool(affected_paths),
                    affected_paths=affected_paths,
                ),
            )
        except Exception as exc:
            return ToolExecutionResult(
                f"error: tool {name} failed: {exc}",
                _metadata("error", name, error_code="tool_failed", read_only=not spec.risky),
            )


def _metadata(status, name, error_code="", read_only=True, workspace_changed=False, affected_paths=None):
    return {
        "tool_status": status,
        "tool_name": name,
        "tool_error_code": error_code,
        "read_only": bool(read_only),
        "workspace_changed": bool(workspace_changed),
        "affected_paths": list(affected_paths or []),
    }
