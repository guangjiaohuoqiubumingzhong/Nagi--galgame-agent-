"""Hidden-window desktop entry with identity checks and explicit shutdown."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager

from .paths import APP_ID, application_root, data_root, ensure_data_directory, state_root


def installation_id():
    value = os.path.normcase(str(application_root())) + "\n" + os.path.normcase(str(data_root()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request(port, route, payload=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    # Local requests must never pass through an environment-configured proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
        return json.loads(response.read(1024 * 1024))


def probe(port):
    try:
        result = request(port, "/api/health")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"端口 {port} 已被其他服务占用，请关闭该服务或通过 -Port 指定其他端口。") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError) or getattr(reason, "winerror", None) == 10061:
            return None
        # Windows may time out before returning WSAECONNREFUSED. Binding an
        # exclusive socket distinguishes an unused port from an unknown service.
        with socket.socket() as check:
            if os.name == "nt":
                check.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                check.bind(("127.0.0.1", port))
                return None
            except OSError:
                pass
        raise ValueError(f"端口 {port} 无法确认服务身份或连接超时，请换一个端口。") from exc
    except (ValueError, TimeoutError, OSError) as exc:
        raise ValueError(f"端口 {port} 响应异常，不能当作 Nagi 使用。") from exc
    if not isinstance(result, dict) or result.get("app_id") != APP_ID or result.get("installation_id") != installation_id():
        raise ValueError(f"端口 {port} 被其他程序或另一份 Nagi 占用，请使用其他端口。")
    return result


@contextmanager
def startup_lock():
    path = state_root() / "web" / "launcher.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 40
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise ValueError("另一个 Nagi 启动器仍在运行，请稍后重试。")
                time.sleep(0.2)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def launch(port=8765, *, open_browser=True):
    ensure_data_directory()
    with startup_lock():
        existing = probe(port)
        if existing is None:
            log_path = state_root() / "web" / "server.log"
            environment = dict(os.environ, NAGI_APP_ROOT=str(application_root()), NAGI_DATA_DIR=str(data_root()), PYTHONUTF8="1")
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "nagi", "web", "--port", str(port), "--no-open"],
                    cwd=application_root(), env=environment,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ValueError(f"Nagi 启动失败。日志：{log_path}")
                if probe(port):
                    break
                time.sleep(0.25)
            else:
                process.terminate()
                raise ValueError(f"Nagi 启动超时。日志：{log_path}")
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")


def stop(port=8765):
    if probe(port) is None:
        return
    config = request(port, "/api/config")
    try:
        request(port, "/api/shutdown", {"csrf_token": config["csrf_token"]})
    except urllib.error.HTTPError as exc:
        raise ValueError("仍有任务运行，请先在网页停止任务，等待保存后再退出 Nagi。") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nagi desktop launcher")
    parser.add_argument("action", choices=("start", "stop"), nargs="?", default="start")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError("端口必须在 1–65535 之间")
        if args.action == "stop":
            stop(args.port)
        else:
            launch(args.port, open_browser=not args.no_open)
        return 0
    except Exception as exc:
        message = f"{exc}\n\n可在启动器使用 -DataDirectory 指定可写目录。"
        if os.name == "nt" and sys.stderr is None:
            ctypes.windll.user32.MessageBoxW(None, message, "Nagi 启动器", 0x10)
        elif sys.stderr is not None:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
