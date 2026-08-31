import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskState:
    run_id: str
    user_request: str
    status: str = "running"
    attempts: int = 0
    tool_steps: int = 0
    last_tool: str = ""
    stop_reason: str = ""
    final_answer: str = ""

    @classmethod
    def create(cls, user_request):
        run_id = "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        return cls(run_id=run_id, user_request=str(user_request))

    def record_attempt(self):
        self.attempts += 1

    def record_tool(self, name):
        self.tool_steps += 1
        self.last_tool = str(name)

    def finish_success(self, final_answer):
        self.status = "completed"
        self.stop_reason = "final_answer_returned"
        self.final_answer = str(final_answer)

    def stop_step_limit(self, final_answer):
        self.status = "stopped"
        self.stop_reason = "step_limit_reached"
        self.final_answer = str(final_answer)

    def stop_retry_limit(self, final_answer):
        self.status = "stopped"
        self.stop_reason = "retry_limit_reached"
        self.final_answer = str(final_answer)

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "user_request": self.user_request,
            "status": self.status,
            "attempts": self.attempts,
            "tool_steps": self.tool_steps,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
        }


class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, task_state):
        return self.root / task_state.run_id

    def start_run(self, task_state):
        run_dir = self.run_dir(task_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        return run_dir

    def write_task_state(self, task_state):
        path = self.run_dir(task_state) / "task_state.json"
        path.write_text(json.dumps(task_state.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def append_trace(self, task_state, event):
        path = self.run_dir(task_state) / "trace.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True))
            handle.write("\n")
        return path

    def write_report(self, task_state, report):
        path = self.run_dir(task_state) / "report.json"
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path
