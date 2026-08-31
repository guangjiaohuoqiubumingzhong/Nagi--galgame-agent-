import time

from .state import TaskState, now
from .workspace import clip


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def run(self, user_message):
        agent = self.agent
        started_at = time.monotonic()
        agent.record({"role": "user", "content": user_message, "created_at": now()})
        task_state = TaskState.create(user_message)
        agent.current_task_state = task_state
        agent.run_store.start_run(task_state)
        agent.emit_trace(task_state, "run_started", {"user_request": clip(user_message, 300)})

        max_attempts = agent.max_steps + 3
        while task_state.tool_steps < agent.max_steps and task_state.attempts < max_attempts:
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt, prompt_metadata = agent.context_manager.build(user_message)
            agent.emit_trace(task_state, "prompt_built", {"prompt_metadata": prompt_metadata})
            agent.emit_trace(task_state, "model_requested", {"attempts": task_state.attempts, "tool_steps": task_state.tool_steps})
            raw = agent.model_client.complete(prompt, agent.max_new_tokens)
            kind, payload = agent.parse(raw)
            agent.emit_trace(task_state, "model_parsed", {"kind": kind})

            if kind == "tool":
                name = payload.get("name", "")
                args = payload.get("args", {})
                task_state.record_tool(name)
                result = agent.execute_tool(name, args)
                agent.record({"role": "tool", "name": name, "args": args, "content": result.content, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "tool_executed",
                    {"name": name, "args": args, "result": clip(result.content, 500), **result.metadata},
                )
                continue

            if kind == "retry":
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                continue

            final = str(payload or raw).strip()
            task_state.finish_success(final)
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.build_report(task_state))
            return final

        if task_state.attempts >= max_attempts:
            final = "Stopped after too many malformed model responses."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit."
            task_state.stop_step_limit(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(task_state, "run_finished", {"status": task_state.status, "stop_reason": task_state.stop_reason, "final_answer": final})
        agent.run_store.write_report(task_state, agent.build_report(task_state))
        return final
