from nagi.prompt_prefix import build_prompt_prefix, tool_signature
from nagi.tools import build_tool_registry
from nagi.workspace import WorkspaceContext


class _Agent:
    depth = 0
    max_depth = 1

    def __init__(self, root):
        self.root = root


class _ConfiguredModelClient:
    model = "deepseek-fixture-model"


def test_tool_signature_is_stable_across_registry_insertion_order(tmp_path):
    tools = {
        "b": {"schema": {"path": "str"}, "risky": False, "description": "B", "run": object()},
        "a": {"schema": {"command": "str"}, "risky": True, "description": "A", "run": object()},
    }
    reordered = {"a": tools["a"], "b": tools["b"]}

    assert tool_signature(tools) == tool_signature(reordered)


def test_build_prompt_prefix_renders_tools_and_workspace_metadata(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))

    prefix = build_prompt_prefix(
        workspace=workspace,
        tools=tools,
        built_at="2026-06-02T00:00:00+08:00",
        model_client=_ConfiguredModelClient(),
    )

    assert "You are Nagi" in prefix.text
    assert "software development, translation, and localization" in prefix.text
    assert "only claim and perform capabilities listed in Tools" in prefix.text
    assert "Configured model: deepseek-fixture-model" in prefix.text
    assert "Protocol adapter: _ConfiguredModelClient" in prefix.text
    assert "Do not say that no underlying model exists" in prefix.text
    assert "Tools:" in prefix.text
    assert "- read_file(" in prefix.text
    assert "Workspace:" in prefix.text
    assert prefix.hash
    assert prefix.workspace_fingerprint == workspace.fingerprint()
    assert prefix.tool_signature == tool_signature(tools)
    assert prefix.built_at == "2026-06-02T00:00:00+08:00"


def test_translation_identity_separates_product_scope_from_implemented_support(tmp_path):
    workspace = WorkspaceContext.build(tmp_path)
    prefix = build_prompt_prefix(workspace=workspace, tools={})

    identity = prefix.text.split("Product identity:", 1)[1].split("Runtime model:", 1)[0]
    assert "not limited to a particular game engine or content type" in identity
    assert "currently implemented game-engine integrations are QLIE, YU-RIS 479" in identity
    assert "KiriKiri/KAG, Ren'Py, and TyranoScript" in identity
    assert "Do not present unlisted engines, encrypted or compiled-only formats" in identity
    assert "only claim and perform capabilities listed in Tools" in identity
    assert 'direct supported game tasks to "视觉小说翻译"' in identity
    assert "distinguish text-level translation assistance from unavailable automated workflows" in identity
    assert "original game directory read-only" in identity
