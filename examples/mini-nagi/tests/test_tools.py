from mini_nagi.tool_executor import ToolExecutor
from mini_nagi.workspace import Workspace


def test_tools_read_search_list_and_reject_path_escape(tmp_path):
    (tmp_path / "README.md").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("beta note\n", encoding="utf-8")
    executor = ToolExecutor(Workspace.build(tmp_path))

    assert "# README.md" in executor.execute("read_file", {"path": "README.md"}).content
    assert "notes.txt:1:beta note" in executor.execute("search", {"pattern": "beta", "path": "."}).content
    assert "[F] README.md" in executor.execute("list_files", {"path": "."}).content
    escaped = executor.execute("read_file", {"path": "../outside.txt"})
    assert escaped.metadata["tool_status"] == "rejected"
    assert "path escapes workspace" in escaped.content


def test_risky_tools_write_patch_and_respect_approval_policy(tmp_path):
    workspace = Workspace.build(tmp_path)
    executor = ToolExecutor(workspace)

    write = executor.execute("write_file", {"path": "sample.txt", "content": "alpha beta\n"})
    assert write.metadata["workspace_changed"] is True
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "alpha beta\n"

    patch = executor.execute("patch_file", {"path": "sample.txt", "old_text": "beta", "new_text": "gamma"})
    assert patch.metadata["workspace_changed"] is True
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "alpha gamma\n"

    denied = ToolExecutor(workspace, approval_policy="never").execute("write_file", {"path": "blocked.txt", "content": "nope"})
    assert denied.metadata["tool_status"] == "rejected"
    assert "approval denied" in denied.content
    assert not (tmp_path / "blocked.txt").exists()
