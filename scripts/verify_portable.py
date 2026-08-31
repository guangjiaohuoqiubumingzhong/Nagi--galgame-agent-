from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile

root = Path(__file__).resolve().parents[1]
version = re.search(r'^__version__ = "([^"]+)"', (root / 'nagi/__init__.py').read_text(encoding='utf-8'), re.M)[1]
destination = root / '.nagi-build' / ('portable-smoke-' + str(time.time_ns()))
destination.mkdir()
with zipfile.ZipFile(root / f'dist/Nagi-{version}-windows-x64.zip') as archive:
    assert not any('/site-packages/bin/' in name for name in archive.namelist())
    archive.extractall(destination)
bundle = destination / f'Nagi-{version}-windows-x64'
moved = destination / '移动后的 Nagi 程序'
# Both resolved paths are inside our freshly created, empty test directory.
assert bundle.resolve().is_relative_to(destination.resolve()) and moved.resolve().is_relative_to(destination.resolve())
bundle.rename(moved)
python = moved / 'runtime/python.exe'
env = {k:v for k,v in os.environ.items() if not k.startswith(('PICO_', 'NAGI_')) and k not in {'PYTHONHOME','PYTHONPATH'}}
env['PYTHONUTF8'] = '1'
env['NAGI_APP_ROOT'] = str(moved)
env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
subprocess.run([str(python), '-I', '-c', 'import nagi,frida,mcp,win32api; from nagi.paths import resource_root; assert (resource_root()/"web_ui/index.html").is_file(); print(nagi.__version__)'], cwd=destination, env=env, check=True)
port = 18766
powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
command = [str(powershell), '-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(moved / 'launch-nagi.ps1'),'-Port',str(port),'-NoOpen']
def read(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}' + path, timeout=4) as response:
        return response.read()
try:
    subprocess.run(command, cwd=destination, env=env, check=True, timeout=50)
    first = json.loads(read('/api/health'))
    subprocess.run(command, cwd=destination, env=env, check=True, timeout=50)
    assert json.loads(read('/api/health')) == first
    config = json.loads(read('/api/config'))
    assert not config['api_key_configured']
    assert config['release']['first_run']
    assert config['translation_rag']['mode'] == 'bm25' or config['translation_rag']['mode'] == 'keyword'
    assert config['release']['features']['mcp'] and config['release']['features']['deployment']
    assert not config['release']['features']['semantic_runtime']
    assert Path(config['release']['paths']['data']) == moved / 'data'
    for asset in ('/', '/app.js', '/release.js', '/styles.css', '/nagi-avatar.png', '/nagi-avatar.svg'):
        assert read(asset)
    # Check published file hashes after launch (new user data is intentionally not in this manifest).
    lines = (moved / 'FILES.sha256').read_text(encoding='utf-8').splitlines()
    for line in lines:
        expected, name = line.split('  ',1)
        assert hashlib.sha256((moved / name).read_bytes()).hexdigest() == expected, name
    result = {'status':'passed','version':first['version'],'relocated_bundle':str(moved),'features':config['release']['features'], 'verified_program_files':len(lines), 'checks':['independent Python and native extensions','Unicode/space relocation','actual PowerShell launcher','duplicate launch identity','offline first run without key','packaged page resources','per-file SHA-256']}
finally:
    subprocess.run([*command, '-Stop'], cwd=destination, env=env, check=True, timeout=20)
result['checks'].append('PowerShell stop')
(root / '.nagi-build/portable-smoke.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2))
