import json

from mini_nagi import FakeModelClient, Nagi, RunStore, Workspace


def test_agent_loop_runs_tool_then_final_and_writes_evidence(tmp_path):
    (tmp_path / "README.md").write_text("alpha\nbeta\n", encoding="utf-8")
    workspace = Workspace.build(tmp_path)
    run_store = RunStore(tmp_path / ".mini-nagi" / "runs")
    agent = Nagi(
        model_client=FakeModelClient(
            [
                '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>',
                "<final>Done.</final>",
            ]
        ),
        workspace=workspace,
        run_store=run_store,
    )

    final = agent.ask("read README")

    assert final == "Done."
    assert len(agent.model_client.prompts) == 2
    assert "Tool result: read_file" in agent.model_client.prompts[1]
    assert "# README.md" in agent.model_client.prompts[1]
    task_state = agent.current_task_state
    run_dir = run_store.run_dir(task_state)
    assert (run_dir / "task_state.json").exists()
    assert (run_dir / "trace.jsonl").exists()
    assert (run_dir / "report.json").exists()
    events = [json.loads(line)["event"] for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events == [
        "run_started",
        "prompt_built",
        "model_requested",
        "model_parsed",
        "tool_executed",
        "prompt_built",
        "model_requested",
        "model_parsed",
        "run_finished",
    ]
