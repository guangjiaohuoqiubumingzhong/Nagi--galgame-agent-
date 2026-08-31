class FakeModelClient:
    """Deterministic model for learning and tests.

    Main Nagi uses real provider clients behind the same `complete()` shape.
    mini-nagi defaults to this fake client so the control loop is visible
    without API keys or network calls.
    """

    supports_prompt_cache = False

    def __init__(self, outputs=None):
        self.outputs = list(outputs) if outputs is not None else None
        self.prompts = []
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens=512, **_kwargs):
        self.prompts.append(prompt)
        self.last_completion_metadata = {"model": "FakeModelClient", "max_new_tokens": max_new_tokens}
        if self.outputs is not None:
            if not self.outputs:
                raise RuntimeError("fake model ran out of outputs")
            return self.outputs.pop(0)
        if "Tool result:" in prompt:
            return "<final>mini-nagi read the workspace through a tool and returned a final answer.</final>"
        return '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":40}}</tool>'
