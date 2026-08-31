import json
import time

from nagi.agent_web import AgentWebService
from nagi.providers import FakeModelClient


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition was not reached before timeout")


def test_agent_web_runs_message_and_lists_saved_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")
    service = AgentWebService(
        tmp_path / "web-state",
        workspace,
        lambda: FakeModelClient(["<final>检查完成。</final>"]),
    )

    job = service.start_job(
        workspace_path=workspace,
        session_id=None,
        message="检查项目",
        approval_policy="ask",
    )
    wait_for(lambda: job.status in {"completed", "failed"})

    assert job.status == "completed"
    assert job.public_dict()["final_answer"] == "检查完成。"
    assert job.public_dict()["trace"][-1]["event"] == "run_finished"
    sessions = service.list_sessions(workspace)
    assert sessions[0]["session_id"] == job.session_id
    assert sessions[0]["title"] == "检查项目"
    detail = service.get_session(workspace, job.session_id)
    assert [item["role"] for item in detail["history"]] == ["user", "assistant"]


def test_agent_web_exposes_and_resolves_tool_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = AgentWebService(
        tmp_path / "web-state",
        workspace,
        lambda: FakeModelClient(
            [
                '<tool>{"name":"write_file","args":{"path":"note.txt","content":"hello"}}</tool>',
                "<final>没有修改文件。</final>",
            ]
        ),
    )

    job = service.start_job(
        workspace_path=workspace,
        session_id=None,
        message="创建说明",
        approval_policy="ask",
    )
    pending = wait_for(lambda: job.public_dict()["pending_approval"])
    job.resolve_approval(pending["approval_id"], False)
    wait_for(lambda: job.status in {"completed", "failed"})

    assert job.status == "completed"
    assert not (workspace / "note.txt").exists()
    assert [item["role"] for item in job.public_dict()["history"]] == [
        "user",
        "assistant",
    ]
    assert [
        item["role"]
        for item in service.get_session(workspace, job.session_id)["history"]
    ] == ["user", "assistant"]
    persisted = json.loads(
        (workspace / ".nagi" / "sessions" / f"{job.session_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert any(
        item.get("name") == "write_file" and "approval denied" in item["content"]
        for item in persisted["history"]
        if item.get("role") == "tool"
    )


def test_agent_web_can_cancel_while_waiting_for_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = AgentWebService(
        tmp_path / "web-state",
        workspace,
        lambda: FakeModelClient(
            [
                '<tool>{"name":"write_file","args":{"path":"note.txt","content":"hello"}}</tool>'
            ]
        ),
    )
    job = service.start_job(
        workspace_path=workspace,
        session_id=None,
        message="创建说明",
        approval_policy="ask",
    )
    wait_for(lambda: job.public_dict()["pending_approval"])

    job.cancel()
    wait_for(lambda: job.status in {"cancelled", "failed"})

    assert job.status == "cancelled"
    assert job.public_dict()["task"]["stop_reason"] == "user_cancelled"
    assert not (workspace / "note.txt").exists()


def test_agent_web_deletes_saved_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = AgentWebService(
        tmp_path / "web-state",
        workspace,
        lambda: FakeModelClient(["<final>完成。</final>"]),
    )
    session = service.create_session(workspace)

    result = service.delete_session(workspace, session["session_id"])

    assert result == {"deleted": True, "session_id": session["session_id"]}
    assert service.list_sessions(workspace) == []


def test_agent_web_renames_forks_and_archives_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = AgentWebService(
        tmp_path / "web-state",
        workspace,
        lambda: FakeModelClient(["<final>完成。</final>"]),
    )
    session = service.create_session(workspace)

    renamed = service.rename_session(
        workspace, session["session_id"], "设计工作区交互"
    )
    forked = service.fork_session(workspace, session["session_id"])
    archived = service.archive_session(workspace, session["session_id"])

    assert renamed["title"] == "设计工作区交互"
    assert forked["session_id"] != session["session_id"]
    assert archived == {"archived": True, "session_id": session["session_id"]}
    assert [item["session_id"] for item in service.list_sessions(workspace)] == [
        forked["session_id"]
    ]
    assert (
        workspace
        / ".nagi"
        / "sessions"
        / "archive"
        / f"{session['session_id']}.json"
    ).is_file()


def test_agent_web_rejects_deleting_running_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = AgentWebService(
        tmp_path / "web-state",
        workspace,
        lambda: FakeModelClient(
            ['<tool>{"name":"write_file","args":{"path":"note.txt","content":"hello"}}</tool>']
        ),
    )
    job = service.start_job(
        workspace_path=workspace,
        session_id=None,
        message="创建说明",
        approval_policy="ask",
    )
    wait_for(lambda: job.public_dict()["pending_approval"])

    try:
        service.delete_session(workspace, job.session_id)
    except ValueError as exc:
        assert "running task" in str(exc)
    else:
        raise AssertionError("running session should not be deleted")
    finally:
        job.cancel()
        wait_for(lambda: job.status in {"cancelled", "failed"})


def test_workspace_can_be_renamed_and_removed_without_deleting_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    storage = tmp_path / "web-state"
    service = AgentWebService(
        storage,
        workspace,
        lambda: FakeModelClient(["<final>完成。</final>"]),
    )

    renamed = service.rename_workspace(workspace, "我的项目")
    assert renamed["name"] == "我的项目"
    assert service.list_workspaces()[0]["name"] == "我的项目"

    result = service.remove_workspace(workspace)
    assert result["removed"] is True
    assert service.list_workspaces() == []
    assert marker.read_text(encoding="utf-8") == "keep\n"

    reloaded = AgentWebService(
        storage,
        workspace,
        lambda: FakeModelClient(["<final>完成。</final>"]),
    )
    assert reloaded.list_workspaces() == []
