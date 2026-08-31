import subprocess
import sys
from pathlib import Path

import mini_nagi


def test_mini_nagi_module_and_public_exports():
    assert mini_nagi.Nagi is not None
    assert mini_nagi.FakeModelClient is not None
    assert not hasattr(mini_nagi, "MiniAgent")
    result = subprocess.run([sys.executable, "-m", "mini_nagi", "--help"], capture_output=True, text=True, check=True)
    assert "Teaching-sized Nagi agent harness" in result.stdout


def test_readme_main_mapping_points_to_existing_files():
    repo_root = Path(__file__).resolve().parents[3]
    main_files = [
        "nagi/cli.py",
        "nagi/runtime.py",
        "nagi/agent_loop.py",
        "nagi/context_manager.py",
        "nagi/providers/clients.py",
        "nagi/tool_executor.py",
        "nagi/tools.py",
        "nagi/task_state.py",
        "nagi/run_store.py",
        "nagi/workspace.py",
    ]
    for path in main_files:
        assert (repo_root / path).exists()
