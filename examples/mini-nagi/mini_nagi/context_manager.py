from . import tools


class ContextManager:
    def __init__(self, agent):
        self.agent = agent

    def build(self, user_message):
        history = _render_history(self.agent.history)
        prompt = f"""You are mini-nagi, a teaching-sized local coding agent.

Return exactly one of these forms:
<tool>{{"name":"read_file","args":{{"path":"README.md","start":1,"end":40}}}}</tool>
<final>Your final answer</final>

Tools:
{tools.tool_signature()}

{self.agent.workspace.snapshot_text()}

Transcript:
{history}

Current request:
{user_message}
"""
        metadata = {
            "history_items": len(self.agent.history),
            "tool_count": len(tools.TOOL_SPECS),
            "prompt_chars": len(prompt),
        }
        return prompt, metadata


def _render_history(history):
    if not history:
        return "(empty)"
    lines = []
    for item in history[-12:]:
        role = item.get("role", "")
        if role == "tool":
            lines.append(f"Tool result: {item.get('name')} -> {item.get('content')}")
        else:
            lines.append(f"{role}: {item.get('content')}")
    return "\n".join(lines)
