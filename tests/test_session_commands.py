import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from nagi.agent_web import AgentWebService
from nagi.context_manager import ContextManager
from nagi.providers import FakeModelClient
from nagi.runtime import Nagi
from nagi.session_commands import compact_history, context_history, mutate_goal
from nagi.session_store import SessionStore
from nagi.workspace import WorkspaceContext


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = FakeModelClient([])
    service = AgentWebService(tmp_path / "web", workspace, lambda: model)
    sid = service.create_session(workspace)["session_id"]
    store = SessionStore(workspace / ".nagi" / "sessions")
    return service, workspace, sid, store, model


def long_history():
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"record-{i}-" + "context " * 90} for i in range(14)]


def join_job(service, result):
    job = service.get_job(result["job"]["job_id"])
    job.thread.join(timeout=10)
    assert not job.thread.is_alive()
    return job


def test_compact_keeps_audit_history_and_uses_summary_in_future_prompts(setup):
    service, workspace, sid, store, model = setup
    session = store.load(sid)
    session["history"] = long_history()
    session["plan_mode"] = True
    store.save(session)
    model.outputs = ["SUMMARY: retain verified decisions and unresolved work."]
    result = service.run_command(workspace, sid, "compact")
    saved = store.load(sid)
    assert saved["history"] == session["history"]
    assert saved["plan_mode"] is True
    assert saved["context_compaction"]["through"] == 8
    assert len(result["session"]["history"]) == 14
    agent = Nagi.from_session(model_client=model, workspace=WorkspaceContext.build(workspace), session_store=store, session_id=sid)
    prompt, _ = ContextManager(agent).build("next request")
    assert "SUMMARY: retain" in prompt
    assert "record-0-" not in prompt
    assert "record-13-" in prompt
    assert "PLAN MODE" in prompt
    assert service.run_command(workspace, sid, "compact")["message"].startswith("暂无")
    assert len(model.prompts) == 1


@pytest.mark.parametrize("output", ["", "<tool>unsafe</tool>", "x" * 4001])
def test_compact_bad_summary_leaves_session_unchanged(setup, output):
    service, workspace, sid, store, model = setup
    session = store.load(sid)
    session["history"] = long_history()
    store.save(session)
    before = store.path(sid).read_bytes()
    model.outputs = [output]
    with pytest.raises(ValueError):
        service.run_command(workspace, sid, "compact")
    assert store.path(sid).read_bytes() == before
    assert not service.busy_sessions


def test_compaction_rejects_concurrent_mutations_and_detects_external_changes(setup):
    service, workspace, sid, store, model = setup
    session = store.load(sid)
    session["history"] = long_history()
    store.save(session)

    def summarize(prompt, max_new_tokens):
        for operation in [lambda: service.start_job(workspace_path=workspace, session_id=sid, message="run"),
                          lambda: service.delete_session(workspace, sid),
                          lambda: service.rename_session(workspace, sid, "changed"),
                          lambda: service.run_command(workspace, sid, "plan")]:
            with pytest.raises(ValueError, match="running task"):
                operation()
        changed = store.load(sid)
        changed["display_title"] = "external edit"
        store.save(changed)
        return "summary"

    model.complete = summarize
    with pytest.raises(ValueError, match="会话在操作期间发生变化"):
        service.run_command(workspace, sid, "compact")
    assert store.load(sid)["display_title"] == "external edit"
    assert "context_compaction" not in store.load(sid)


def test_compaction_processes_multiple_chunks_and_fails_closed_on_modified_history():
    session = {"history": long_history()}
    for item in session["history"]:
        item["content"] *= 4
    model = FakeModelClient(["first summary", "merged summary"])
    session["context_compaction"] = compact_history(session, model)
    assert len(model.prompts) == 2
    assert "first summary" in model.prompts[1]
    assert context_history(session)[0]["role"] == "summary"
    session["history"][0]["content"] = "changed"
    assert context_history(session) == session["history"]


def test_goal_domain_prevents_implicit_replacement_and_keeps_history():
    session = {}
    assert mutate_goal(session, "complete project", 5)
    identity = session["goal"]["id"]
    with pytest.raises(ValueError):
        mutate_goal(session, "replace without consent")
    assert not mutate_goal(session, "edit refined objective", 6)
    assert session["goal"]["id"] == identity
    mutate_goal(session, "PAUSE")
    assert session["goal"]["status"] == "paused"
    assert mutate_goal(session, "resume")
    mutate_goal(session, "clear")
    assert session["goal"] is None
    assert session["goal_history"][0]["id"] == identity
    assert mutate_goal(session, "pause after verification")
    assert session["goal"]["objective"] == "pause after verification"


def test_goal_continues_across_final_answers_until_tool_marks_complete(setup):
    service, workspace, sid, store, model = setup
    model.outputs = ["<final>Phase one checked.</final>",
                     '<tool>{"name":"update_goal","args":{"status":"completed","reason":"All acceptance checks passed."}}</tool>',
                     "<final>Finished with evidence.</final>"]
    result = service.run_command(workspace, sid, "goal", "Check both phases", max_rounds=5)
    job = join_job(service, result)
    assert job.status == "completed", job.error
    goal = store.load(sid)["goal"]
    assert goal["status"] == "completed"
    assert goal["rounds"] == 2
    assert "USER GOAL: Check both phases" in model.prompts[0]
    assert len(model.prompts) == 3
    reloaded = AgentWebService(service.workspaces.storage_path.parent, workspace, lambda: model)
    assert reloaded.get_session(workspace, sid)["controls"]["goal"] == goal


def test_goal_cap_pauses_instead_of_claiming_completion_and_can_resume(setup):
    service, workspace, sid, store, model = setup
    model.outputs = ["<final>Need more work.</final>"]
    job = join_job(service, service.run_command(workspace, sid, "goal", "long task", max_rounds=1))
    assert job.status == "completed", job.error
    assert store.load(sid)["goal"]["status"] == "paused"
    assert store.load(sid)["goal"]["rounds"] == 1
    with pytest.raises(ValueError, match="上限"):
        service.run_command(workspace, sid, "goal", "resume")
    model.outputs = ['<tool>{"name":"update_goal","args":{"status":"blocked","reason":"Need user decision"}}</tool>', "<final>Need input.</final>"]
    job = join_job(service, service.run_command(workspace, sid, "goal", "resume", max_rounds=2))
    assert store.load(sid)["goal"]["status"] == "blocked"
    assert store.load(sid)["goal"]["rounds"] == 2


def test_goal_pause_interrupts_pending_approval_and_does_not_write(setup):
    service, workspace, sid, store, model = setup
    model.outputs = ['<tool>{"name":"write_file","args":{"path":"note.txt","content":"x"}}</tool>']
    result = service.run_command(workspace, sid, "goal", "create note")
    job = service.get_job(result["job"]["job_id"])
    # Wait for the real worker's bounded approval state, not a live model.
    import time
    deadline = time.monotonic() + 10
    while not job.pending_approval and time.monotonic() < deadline:
        time.sleep(.02)
    assert job.pending_approval
    service.run_command(workspace, sid, "goal", "pause")
    job.thread.join(timeout=5)
    assert not job.thread.is_alive()
    assert store.load(sid)["goal"]["status"] == "paused"
    assert not (workspace / "note.txt").exists()


def test_plan_enforces_read_only_even_with_auto_approval_and_can_exit(setup):
    service, workspace, sid, _store, model = setup
    result = service.run_command(workspace, sid, "plan")
    assert result["session"]["controls"]["plan_mode"]
    assert not model.prompts
    model.outputs = ['<tool>{"name":"write_file","args":{"path":"note.txt","content":"x"}}</tool>', "<final>Here is the plan.</final>"]
    job = service.start_job(workspace_path=workspace, session_id=sid, message="Design it", approval_policy="auto")
    job.thread.join(timeout=10)
    assert job.status == "completed", job.error
    assert "PLAN MODE" in model.prompts[0]
    assert not (workspace / "note.txt").exists()
    for name, args in [("run_shell", {"command": "echo unsafe"}), ("delegate", {"task": "write code"}),
                       ("update_goal", {"status": "completed", "reason": "No"})]:
        with pytest.raises(ValueError, match="规划模式"):
            job.agent.validate_tool(name, args)
    result = service.run_command(workspace, sid, "plan", "off")
    assert not result["session"]["controls"]["plan_mode"]


def test_plan_with_message_runs_under_plan_and_rejects_goal_start(setup):
    service, workspace, sid, store, model = setup
    model.outputs = ["<final>Design with tests.</final>"]
    job = join_job(service, service.run_command(workspace, sid, "plan", "Design a feature", approval_policy="auto"))
    assert job.approval_policy == "auto"
    assert store.load(sid)["history"][0]["content"] == "Design a feature"
    with pytest.raises(ValueError, match="退出规划"):
        service.run_command(workspace, sid, "goal", "build it")
    assert not store.load(sid).get("goal")


def test_goal_model_cannot_increase_cap_or_change_objective(setup):
    _service, workspace, sid, store, model = setup
    session = store.load(sid)
    mutate_goal(session, "bounded task", 1)
    store.save(session)
    agent = Nagi.from_session(model_client=model, workspace=WorkspaceContext.build(workspace), session_store=store, session_id=sid)
    with pytest.raises(ValueError):
        agent.validate_tool("update_goal", {"status": "active", "reason": "continue", "max_rounds": 100})
    assert agent.session["goal"]["max_rounds"] == 1


def test_atomic_session_save_failure_keeps_existing_file(setup, monkeypatch):
    _, _, sid, store, _ = setup
    before = store.path(sid).read_bytes()
    session = store.load(sid)
    session["plan_mode"] = True
    def fail_replace(*args):
        raise OSError("disk failure")
    monkeypatch.setattr("nagi.session_store.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.save(session)
    assert store.path(sid).read_bytes() == before
    assert not list(store.root.glob("*.tmp"))


def test_command_http_route_persists_state_and_requires_csrf(setup, monkeypatch):
    from nagi.webapp import QlieWebServer
    service, workspace, sid, store, model = setup
    monkeypatch.setattr("nagi.webapp._project_root", lambda: workspace)
    server = QlieWebServer(("127.0.0.1", 0))
    server.agent_service = service
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/agent/sessions/{sid}/command"
    payload = {"workspace": str(workspace), "command": "plan", "arguments": ""}
    try:
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 403
        assert not store.load(sid).get("plan_mode")
        payload["csrf_token"] = server.csrf_token
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.load(response)
        assert result["session"]["controls"]["plan_mode"]
        query = urllib.parse.urlencode({"workspace": str(workspace)})
        with urllib.request.urlopen(url.removesuffix("/command") + "?" + query, timeout=5) as response:
            assert json.load(response)["controls"]["plan_mode"]
        assert not model.prompts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
