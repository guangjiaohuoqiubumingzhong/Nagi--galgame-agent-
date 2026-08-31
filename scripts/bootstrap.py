"""Initialize source installs using the user's Python and hash-locked packages."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Initialize Nagi source environment (Python 3.12+)")
    parser.add_argument("--extras", nargs="*", choices=("mcp", "deployment", "rag"), default=[])
    parser.add_argument("--dev", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("Please install Python 3.12+ yourself, then run this script using that Python.")
    root = Path(__file__).resolve().parents[1]
    environment = root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    for group in ["dev" if args.dev else "build", *args.extras]:
        subprocess.run([str(python), "-m", "pip", "install", "--require-hashes", "-r", str(root / "requirements" / f"{group}.lock")], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", str(root)], check=True)
    print(f'Nagi is ready. Start with: "{python}" -m nagi web')


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Setup failed: {exc}. Check your Python version and network, then rerun; no user data was deleted.")
