import json
import re

from .agent_loop import AgentLoop
from .context_manager import ContextManager
from .tool_executor import ToolExecutor


class Nagi:
    def __init__(
        self,
        model_client,
        workspace,
        run_store,
        approval_policy="auto",
        max_steps=4,
        max_new_tokens=512,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.run_store = run_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.history = []
        self.tool_executor = ToolExecutor(workspace, approval_policy=approval_policy)
        self.context_manager = ContextManager(self)
        self.current_task_state = None

    def ask(self, user_message):
        return AgentLoop(self).run(user_message)

    def record(self, item):
        self.history.append(dict(item))

    def execute_tool(self, name, args):
        return self.tool_executor.execute(name, args or {})

    def run_tool(self, name, args):
        return self.execute_tool(name, args).content

    def emit_trace(self, task_state, event_type, payload):
        event = {"event": event_type, "run_id": task_state.run_id, **dict(payload or {})}
        self.run_store.append_trace(task_state, event)

    @staticmethod
    def parse(raw):
        text = str(raw or "")
        tool_match = re.search(r"<tool>(.*?)</tool>", text, re.DOTALL)
        if tool_match:
            try:
                payload = json.loads(tool_match.group(1).strip())
            except json.JSONDecodeError as exc:
                return "retry", f"model returned malformed tool JSON: {exc}"
            return "tool", payload
        final_match = re.search(r"<final>(.*?)</final>", text, re.DOTALL)
        if final_match:
            final = final_match.group(1).strip()
            if final:
                return "final", final
            return "retry", "model returned an empty final answer"
        return "retry", "model returned neither <tool> nor <final>"

    def build_report(self, task_state):
        return {
            "task_state": task_state.to_dict(),
            "history_items": len(self.history),
            "workspace_root": str(self.workspace.root),
        }
