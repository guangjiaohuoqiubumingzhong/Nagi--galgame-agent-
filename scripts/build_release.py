"""Build releases only from a reviewed source allowlist and pinned components."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DENIED = {".git", ".codex", ".agents", ".pico", ".nagi", ".venv", "__pycache__", "node_modules", "translations", "playable", ".nagi-build"}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_files(root=ROOT):
    manifest = root / "release/source-files.txt"
    names = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate source manifest entry")
    for name in names:
        relative = PurePosixPath(name)
        path = root / name
        if (relative.is_absolute() or ".." in relative.parts or DENIED.intersection(relative.parts)
                or (relative.name.startswith(".env") and relative.name != ".env.example")
                or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve())):
            raise ValueError(f"Unsafe or missing source manifest entry: {name}")
        yield path, name


def archive(root, destination):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                info = zipfile.ZipInfo(root.name + "/" + path.relative_to(root).as_posix(), (2026, 8, 31, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                output.writestr(info, path.read_bytes())


def extract(archive_path, target):
    with zipfile.ZipFile(archive_path) as source:
        for info in source.infolist():
            path = (target / info.filename).resolve()
            if not path.is_relative_to(target.resolve()) or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Unsafe path in component archive")
        source.extractall(target)


def download(spec, cache):
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / spec["url"].rsplit("/", 1)[1]
    if not path.exists():
        partial = path.with_suffix(path.suffix + ".partial")
        with urllib.request.urlopen(spec["url"], timeout=120) as response, partial.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        if digest(partial) != spec["sha256"]:
            raise ValueError("Downloaded component SHA-256 mismatch")
        partial.replace(path)
    if digest(path) != spec["sha256"]:
        raise ValueError("Cached component SHA-256 mismatch")
    return path


def collect_notices(site, output):
    records = []
    for distribution in sorted(importlib.metadata.distributions(path=[str(site)]), key=lambda d: d.metadata["Name"].lower()):
        name, version = distribution.metadata["Name"], distribution.version
        target = output / f"{name}-{version}"
        copied = []
        for item in distribution.files or []:
            # Wheel authors declare licenses in METADATA/RECORD; preserve all notices, not just metadata labels.
            if any(token in item.name.lower() for token in ("license", "copying", "notice", "authors")) or "licenses" in item.parts:
                source = Path(distribution.locate_file(item)).resolve()
                if source.is_file() and source.is_relative_to(site.resolve()):
                    destination = target / str(item).replace("../", "")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    copied.append(destination.relative_to(output).as_posix())
        if not copied:
            raise ValueError(f"No license text found for {name} {version}; cannot release this component")
        license_name = distribution.metadata.get("License-Expression") or distribution.metadata.get("License", "See license files")
        records.append({"name": name, "version": version, "license": license_name, "notice_files": copied})
    output.mkdir(parents=True, exist_ok=True)
    (output / "components.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return records


def run(*args, cwd=ROOT):
    subprocess.run([str(arg) for arg in args], cwd=cwd, check=True)


def build(portable=False):
    if portable and (sys.platform != "win32" or sys.maxsize <= 2**32):
        raise ValueError("The portable builder requires 64-bit Windows")
    version = re.search(r'^__version__ = "([^"]+)"', (ROOT / "nagi/__init__.py").read_text(encoding="utf-8"), re.M)[1]
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    scratch = ROOT / ".nagi-build"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=scratch) as temporary:
        source = Path(temporary) / f"Nagi-{version}-source"
        for path, name in source_files():
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        archive(source, output / f"Nagi-{version}-source.zip")
        wheel_dir = Path(temporary) / "wheels"
        run(sys.executable, "-m", "build", "--no-isolation", "--outdir", wheel_dir, source)
        for path in wheel_dir.iterdir():
            shutil.copyfile(path, output / path.name)
        if portable:
            spec = json.loads((source / "release/components.json").read_text(encoding="utf-8"))
            bundle = Path(temporary) / f"Nagi-{version}-windows-x64"
            runtime = bundle / "runtime"
            runtime.mkdir(parents=True)
            extract(download(spec["python"], scratch / "downloads"), runtime)
            site = runtime / "Lib/site-packages"
            run(sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile", "--only-binary=:all:",
                "--python-version", "3.13", "--platform", "win_amd64", "--implementation", "cp", "--require-hashes",
                "--target", site, "-r", source / spec["dependency_lock"])
            # pip --target creates optional CLI wrappers using the BUILD Python
            # path. They are not needed by Nagi and must not ship in a portable
            # application. Only delete this freshly assembled staging directory.
            wrappers = site / "bin"
            if wrappers.exists():
                if wrappers.is_symlink() or wrappers.resolve().parent != site.resolve() or not wrappers.resolve().is_relative_to(Path(temporary).resolve()):
                    raise ValueError("Unsafe generated wrapper directory")
                shutil.rmtree(wrappers)
            wheel = next(wheel_dir.glob("*.whl"))
            extract(wheel, site)
            (runtime / "python313._pth").write_text("python313.zip\n.\nLib/site-packages\nimport site\n", encoding="utf-8")
            launchers = ["Start Nagi.vbs", "Start Nagi.cmd", "Stop Nagi.cmd", "Choose Data Folder.cmd", "launch-nagi.ps1"]
            for name in [*launchers, "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md"]:
                shutil.copyfile(source / name, bundle / name)
            for name in ("QUICKSTART.md", "UPGRADING.md", "mcp.md"):
                destination = bundle / "docs" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / "docs" / name, destination)
            architecture = bundle / "docs/architecture"
            architecture.mkdir()
            shutil.copyfile(source / "docs/architecture/translation-context.md", architecture / "translation-context.md")
            (bundle / "README.txt").write_text("Nagi " + version + "\n\nDouble-click Start Nagi.vbs (or Start Nagi.cmd).\nRead docs/QUICKSTART.md before configuring your own API.\nStop with Stop Nagi.cmd. Choose writable data with Choose Data Folder.cmd.\n", encoding="utf-8")
            shutil.copytree(source / "release/licenses", bundle / "licenses/supplemental")
            (bundle / "VERSION.txt").write_text(version + "\n", encoding="utf-8")
            (bundle / "portable.json").write_text(json.dumps({"app_id": "nagi-workbench", "version": version, **spec}, indent=2), encoding="utf-8")
            notices = bundle / "licenses"
            collect_notices(site, notices)
            shutil.copyfile(runtime / "LICENSE.txt", notices / "Python-LICENSE.txt")
            # Import checks run in an empty directory; source-tree imports cannot hide packaging errors.
            run(runtime / "python.exe", "-I", "-c", "import nagi,frida,mcp,jsonschema; from nagi.paths import resource_root; assert (resource_root()/'web_ui/index.html').is_file(); print('Nagi', nagi.__version__)", cwd=Path(temporary))
            sums = [f"{digest(path)}  {path.relative_to(bundle).as_posix()}" for path in sorted(bundle.rglob("*")) if path.is_file() and "__pycache__" not in path.parts]
            (bundle / "FILES.sha256").write_text("\n".join(sums) + "\n", encoding="utf-8")
            archive(bundle, output / f"Nagi-{version}-windows-x64.zip")
    files = [p for p in output.iterdir() if p.is_file() and p.suffix in {".zip", ".whl", ".gz"} and version in p.name]
    (output / "SHA256SUMS.txt").write_text("\n".join(f"{digest(p)}  {p.name}" for p in sorted(files)) + "\n", encoding="utf-8")
    print(f"Release artifacts: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portable", action="store_true")
    build(parser.parse_args().portable)
