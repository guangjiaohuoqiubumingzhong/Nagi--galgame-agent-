import json

import pytest

from nagi import FakeModelClient, Nagi, SessionStore, WorkspaceContext
from nagi.evaluation.evaluator import BenchmarkEvaluator, validate_benchmark


def build_agent(tmp_path, allowed_tools=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".nagi" / "sessions")
    return Nagi(
        model_client=FakeModelClient(["<final>Done.</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        allowed_tools=allowed_tools,
    )


def test_allowed_tools_filter_prompt_and_reject_direct_execution(tmp_path):
    agent = build_agent(tmp_path, allowed_tools=["read_file"])

    prompt = agent.prompt("Read the README")

    assert "- read_file(" in prompt
    assert "- run_shell(" not in prompt
    assert agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20}) == "error: tool 'run_shell' is not allowed in this run"


def test_allowed_tools_reject_unknown_tool_at_construction(tmp_path):
    with pytest.raises(ValueError, match="unknown allowed tool"):
        build_agent(tmp_path, allowed_tools=["read_file", "missing_tool"])


def test_validate_benchmark_rejects_unknown_allowed_tool(tmp_path):
    fixture = tmp_path / "bench_repo_readme"
    fixture.mkdir()
    (fixture / "README.md").write_text("demo\n", encoding="utf-8")

    benchmark = {
        "schema_version": 1,
        "tasks": [
            {
                "id": "bad_allowed_tool",
                "prompt": "Inspect README.",
                "fixture_repo": "bench_repo_readme",
                "allowed_tools": ["read_file", "missing_tool"],
                "step_budget": 1,
                "expected_artifact": "README.md",
                "verifier": "python -c 'print(1)'",
                "category": "contract",
            }
        ],
    }

    with pytest.raises(ValueError, match="unknown allowed_tools entry"):
        validate_benchmark(benchmark, repo_root=tmp_path)


def test_benchmark_evaluator_applies_allowed_tools_to_runtime_prompt(tmp_path):
    fixture = tmp_path / "bench_repo_readme"
    fixture.mkdir()
    (fixture / "README.md").write_text("demo\n", encoding="utf-8")
    benchmark_dir = tmp_path / "benchmarks"
    benchmark_dir.mkdir()
    benchmark_path = benchmark_dir / "benchmark.json"
    benchmark_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [
                    {
                        "id": "prompt_allowlist",
                        "prompt": "Inspect README.",
                        "fixture_repo": "bench_repo_readme",
                        "allowed_tools": ["read_file"],
                        "step_budget": 1,
                        "expected_artifact": "README.md",
                        "verifier": "python -c 'import pathlib; assert pathlib.Path(\"README.md\").exists()'",
                        "category": "contract",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    captured_clients = []

    class CaptureModelClient(FakeModelClient):
        def __init__(self):
            super().__init__(["<final>Done.</final>"])
            captured_clients.append(self)

    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=lambda task, workspace: CaptureModelClient(),
    )

    row = evaluator.run_task(evaluator.load()["tasks"][0])

    assert row["status"] == "pass"
    assert "- read_file(" in captured_clients[0].prompts[0]
    assert "- run_shell(" not in captured_clients[0].prompts[0]
