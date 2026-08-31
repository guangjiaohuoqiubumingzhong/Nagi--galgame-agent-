"""Local Web orchestration for Nagi workspaces, sessions, runs, and approvals."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from .paths import workspace_state
from uuid import uuid4

from . import security as securitylib
from .runtime import Nagi
from .session_commands import compact_history, controls_for_ui, mutate_goal
from .session_store import SessionStore
from .workspace import WorkspaceContext, clip, now

ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "awaiting_approval"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_MESSAGE_CHARS = 64 * 1024
MAX_TRACE_EVENTS = 160
APPROVAL_TIMEOUT_SECONDS = 10 * 60


def _safe_workspace(path):
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError("workspace directory does not exist")
    return candidate


def _safe_session_id(session_id):
    value = str(session_id or "").strip()
    if not SESSION_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid session id")
    return value


def _session_path(workspace_root, session_id):
    session_id = _safe_session_id(session_id)
    return workspace_state(workspace_root) / "sessions" / f"{session_id}.json"


def _load_session_file(workspace_root, session_id):
    path = _session_path(workspace_root, session_id)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("session not found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("session file is invalid") from exc
    if not isinstance(payload, dict) or payload.get("id") != session_id:
        raise ValueError("session file identity mismatch")
    return payload, path


def _session_title(session):
    display_title = session.get("display_title")
    if isinstance(display_title, str) and display_title.strip():
        return clip(display_title.strip(), 80)
    for item in session.get("history", []):
        if item.get("role") == "user" and str(item.get("content", "")).strip():
            return clip(str(item["content"]).strip().replace("\n", " "), 42)
    return "新会话"


def _session_summary(session, path, workspace_root=None):
    history = session.get("history", [])
    is_blank = not any(
        item.get("role") == "user" and str(item.get("content", "")).strip()
        for item in history
        if isinstance(item, dict)
    )
    return {
        "session_id": session["id"],
        "title": _session_title(session),
        "is_blank": is_blank,
        "workspace_path": str(workspace_root) if workspace_root is not None else None,
        "created_at": session.get("created_at"),
        "updated_at": datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        "turn_count": sum(item.get("role") == "user" for item in history),
    }


def _conversation_history(history):
    """Return only user-facing turns, keeping tool execution details internal."""
    if not isinstance(history, list):
        return []
    return [
        item
        for item in history
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
    ]


def list_workspace_sessions(workspace_root):
    workspace_root = _safe_workspace(workspace_root)
    session_root = workspace_state(workspace_root) / "sessions"
    if not session_root.is_dir():
        return []
    summaries = []
    for path in session_root.glob("*.json"):
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            continue
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(session, dict) or session.get("id") != path.stem:
                continue
            summaries.append(_session_summary(session, path, workspace_root))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError):
            continue
    return sorted(summaries, key=lambda item: item["updated_at"], reverse=True)


def session_for_ui(workspace_root, session_id):
    workspace = WorkspaceContext.build(_safe_workspace(workspace_root))
    session, path = _load_session_file(workspace.repo_root, session_id)
    return {
        **_session_summary(session, path, workspace.repo_root),
        "workspace_root": workspace.repo_root,
        "history": securitylib.redact_artifact(
            _conversation_history(session.get("history", []))
        ),
        "controls": securitylib.redact_artifact(controls_for_ui(session)),
    }


class WorkspaceRegistry:
    def __init__(self, storage_path, defaults=()):
        self.storage_path = Path(storage_path)
        self.lock = threading.RLock()
        self.paths = []
        self.aliases = {}
        self.removed = []
        self._load()
        for path in defaults:
            try:
                canonical = str(_safe_workspace(path))
                if os.path.normcase(canonical) in {
                    os.path.normcase(item) for item in self.removed
                }:
                    continue
                self.add(path)
            except ValueError:
                continue

    def _load(self):
        if not self.storage_path.is_file():
            return
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("paths"), list):
            return
        raw_aliases = payload.get("aliases", {})
        if not isinstance(raw_aliases, dict):
            raw_aliases = {}
        raw_removed = payload.get("removed", [])
        if isinstance(raw_removed, list):
            self.removed = [str(item) for item in raw_removed if isinstance(item, str)]
        for value in payload["paths"]:
            try:
                path = str(_safe_workspace(value))
            except (TypeError, ValueError, OSError):
                continue
            if os.path.normcase(path) not in {
                os.path.normcase(item) for item in self.paths
            }:
                self.paths.append(path)
                alias = raw_aliases.get(path)
                if isinstance(alias, str) and alias.strip():
                    self.aliases[path] = alias.strip()

    def _save(self):
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=str(self.storage_path.parent),
            prefix=self.storage_path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(
                {
                    "paths": self.paths,
                    "aliases": self.aliases,
                    "removed": self.removed,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(self.storage_path)

    def add(self, path):
        workspace = WorkspaceContext.build(_safe_workspace(path))
        canonical = str(Path(workspace.repo_root).resolve())
        with self.lock:
            existing = {os.path.normcase(item) for item in self.paths}
            if os.path.normcase(canonical) not in existing:
                self.paths.append(canonical)
            self.removed = [
                item
                for item in self.removed
                if os.path.normcase(item) != os.path.normcase(canonical)
            ]
            self._save()
        return canonical

    def rename(self, path, name):
        canonical = str(_safe_workspace(path))
        name = str(name or "").strip()
        if not name or len(name) > 80 or any(char in name for char in "\r\n\t"):
            raise ValueError("workspace name must be 1 to 80 characters")
        with self.lock:
            registered = next(
                (
                    item
                    for item in self.paths
                    if os.path.normcase(item) == os.path.normcase(canonical)
                ),
                None,
            )
            if registered is None:
                raise ValueError("workspace is not registered")
            if any(
                item != registered
                and self.aliases.get(item, Path(item).name).casefold() == name.casefold()
                for item in self.paths
            ):
                raise ValueError("workspace name is already in use")
            self.aliases[registered] = name
            self._save()
        return registered

    def remove(self, path):
        canonical = str(_safe_workspace(path))
        with self.lock:
            registered = next(
                (
                    item
                    for item in self.paths
                    if os.path.normcase(item) == os.path.normcase(canonical)
                ),
                None,
            )
            if registered is None:
                raise ValueError("workspace is not registered")
            self.paths.remove(registered)
            self.aliases.pop(registered, None)
            if os.path.normcase(registered) not in {
                os.path.normcase(item) for item in self.removed
            }:
                self.removed.append(registered)
            self._save()
        return registered

    def list(self):
        with self.lock:
            paths = list(self.paths)
        workspaces = []
        for path in paths:
            try:
                context = WorkspaceContext.build(path)
            except (OSError, ValueError):
                continue
            sessions = list_workspace_sessions(context.repo_root)
            workspaces.append(
                {
                    "path": context.repo_root,
                    "name": self.aliases.get(
                        path, Path(context.repo_root).name or context.repo_root
                    ),
                    "branch": context.branch,
                    "status": context.status,
                    "session_count": len(sessions),
                    "sessions": sessions,
                }
            )
        return workspaces


class AgentJob:
    def __init__(
        self,
        *,
        workspace_path,
        session_id,
        message,
        approval_policy,
        model_factory,
        model_client=None,
    ):
        if approval_policy not in {"ask", "auto", "never"}:
            raise ValueError("unsupported approval policy")
        message = str(message or "").strip()
        if not message or len(message) > MAX_MESSAGE_CHARS:
            raise ValueError("message must be non-empty and reasonably sized")
        self.job_id = uuid4().hex[:12]
        self.workspace_path = str(_safe_workspace(workspace_path))
        self.session_id = _safe_session_id(session_id) if session_id else None
        self.message = message
        self.approval_policy = approval_policy
        self.model_factory = model_factory
        self.model_client = model_client
        self.status = "queued"
        self.error = None
        self.final_answer = None
        self.created_at = now()
        self.updated_at = self.created_at
        self.events = []
        self.agent = None
        self.pending_approval = None
        self.approval_decision = None
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"nagi-agent-{self.job_id}",
        )

    def _event(self, message):
        with self.lock:
            self.events.append({"time": now(), "message": str(message)})
            self.events = self.events[-80:]
            self.updated_at = now()

    def start(self):
        self.thread.start()

    def _request_approval(self, name, args):
        with self.condition:
            approval_id = uuid4().hex[:12]
            safe_args = (
                self.agent.redact_artifact(args)
                if self.agent is not None
                else securitylib.redact_artifact(args)
            )
            self.pending_approval = {
                "approval_id": approval_id,
                "tool": str(name),
                "args": safe_args,
                "requested_at": now(),
            }
            self.approval_decision = None
            self.status = "awaiting_approval"
            self._event(f"等待批准工具：{name}")
            deadline = time.monotonic() + APPROVAL_TIMEOUT_SECONDS
            while (
                self.approval_decision is None
                and not self.cancel_event.is_set()
                and time.monotonic() < deadline
            ):
                self.condition.wait(timeout=0.5)
            approved = self.approval_decision is True and not self.cancel_event.is_set()
            self.pending_approval = None
            self.approval_decision = None
            if not self.cancel_event.is_set():
                self.status = "running"
            self._event(f"工具 {name} 已{'批准' if approved else '拒绝'}")
            return approved

    def resolve_approval(self, approval_id, approved):
        with self.condition:
            if not self.pending_approval:
                raise ValueError("no approval is pending")
            if self.pending_approval["approval_id"] != str(approval_id):
                raise ValueError("approval request is stale")
            if not isinstance(approved, bool):
                raise TypeError("approved must be a boolean")
            self.approval_decision = approved
            self.condition.notify_all()

    def cancel(self):
        with self.condition:
            self.cancel_event.set()
            self.condition.notify_all()
            self._event("已请求在当前模型或工具步骤结束后停止")

    def _run(self):
        finished_status = "failed"
        try:
            self.status = "running"
            self._event("Agent 任务已开始")
            workspace = WorkspaceContext.build(self.workspace_path)
            self.workspace_path = workspace.repo_root
            store = SessionStore(workspace_state(workspace.repo_root) / "sessions")
            model_client = self.model_client if self.model_client is not None else self.model_factory()
            common = {
                "model_client": model_client,
                "workspace": workspace,
                "session_store": store,
                "approval_policy": self.approval_policy,
                "approval_handler": self._request_approval,
                "cancel_event": self.cancel_event,
                "max_steps": 8,
                "max_new_tokens": 2048,
            }
            if self.session_id:
                _load_session_file(workspace.repo_root, self.session_id)
                agent = Nagi.from_session(session_id=self.session_id, **common)
            else:
                agent = Nagi(**common)
                self.session_id = agent.session["id"]
            self.agent = agent
            self._event(f"会话已就绪：{self.session_id}")
            message = self.message
            repeated_finals = []
            while True:
                goal = agent.session.get("goal")
                goal_running = bool(goal and goal["status"] == "active" and not agent.session.get("plan_mode"))
                if goal_running:
                    if self.cancel_event.is_set() or goal["rounds"] >= goal["max_rounds"]:
                        goal.update(status="paused", reason="用户暂停" if self.cancel_event.is_set() else "已达到目标轮次上限", updated_at=now())
                        agent.session_store.save(agent.session)
                        final = goal["reason"]
                        break
                    goal["rounds"] += 1
                    goal["updated_at"] = now()
                    agent.session_store.save(agent.session)
                    self._event(f"目标第 {goal['rounds']}/{goal['max_rounds']} 轮")
                history_start = len(agent.session["history"])
                final = agent.ask(message)
                self.final_answer = final
                if not goal_running or self.cancel_event.is_set() or goal["status"] != "active":
                    break
                denied = any(item.get("role") == "tool" and "approval denied" in item.get("content", "")
                             for item in agent.session["history"][history_start:])
                if denied:
                    goal.update(status="blocked", reason="工具操作未获批准，请确认权限或调整目标", updated_at=now())
                    agent.session_store.save(agent.session)
                    break
                repeated_finals.append(final if not agent.current_task_state.tool_steps else None)
                if len(repeated_finals) >= 3 and repeated_finals[-1] is not None and len(set(repeated_finals[-3:])) == 1:
                    goal.update(status="blocked", reason="连续三轮未产生新进展，请补充信息或调整目标", updated_at=now())
                    agent.session_store.save(agent.session)
                    break
                message = "继续推进已设定目标。依据当前工作和验证结果决定下一步；完成或需要用户帮助时调用 update_goal，再说明结果。"
            self.final_answer = final
            if self.cancel_event.is_set() or (
                agent.current_task_state is not None
                and agent.current_task_state.stop_reason == "user_cancelled"
            ):
                finished_status = "cancelled"
                self._event("Agent 任务已停止")
            else:
                finished_status = "completed"
                self._event("Agent 任务已完成")
        except Exception as exc:  # noqa: BLE001 - background worker boundary
            self.error = securitylib.redact_text(str(exc))
            finished_status = "failed"
            self._event(f"Agent 任务失败：{self.error}")
        finally:
            try:
                if self.agent is not None:
                    goal = self.agent.session.get("goal")
                    if goal and goal["status"] == "active" and (self.cancel_event.is_set() or finished_status == "failed"):
                        goal.update(status="paused" if self.cancel_event.is_set() else "blocked",
                                    reason="用户暂停" if self.cancel_event.is_set() else (self.error or "任务失败"), updated_at=now())
                        self.agent.session_store.save(self.agent.session)
            except OSError as exc:
                self.error = securitylib.redact_text(str(exc))
                finished_status = "failed"
            self.status = finished_status
            self.updated_at = now()

    def _trace(self):
        agent = self.agent
        if agent is None or agent.current_run_dir is None:
            return []
        path = Path(agent.current_run_dir) / "trace.jsonl"
        if not path.is_file() or path.is_symlink():
            return []
        events = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-MAX_TRACE_EVENTS:]
            for line in lines:
                item = json.loads(line)
                if isinstance(item, dict):
                    events.append(item)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        return events

    def public_dict(self):
        with self.lock:
            agent = self.agent
            history = agent.session.get("history", []) if agent is not None else []
            task = agent.current_task_state.to_dict() if agent and agent.current_task_state else None
            return {
                "job_id": self.job_id,
                "workspace_path": self.workspace_path,
                "session_id": self.session_id,
                "status": self.status,
                "approval_policy": self.approval_policy,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "error": self.error,
                "final_answer": self.final_answer,
                "pending_approval": self.pending_approval,
                "events": list(self.events),
                "history": (
                    agent.redact_artifact(_conversation_history(history))
                    if agent is not None
                    else securitylib.redact_artifact(_conversation_history(history))
                ),
                "task": task,
                "controls": securitylib.redact_artifact(controls_for_ui(agent.session)) if agent else None,
                "trace": self._trace(),
            }


class AgentWebService:
    def __init__(self, storage_root, default_workspace, model_factory):
        storage_root = Path(storage_root)
        self.model_factory = model_factory
        self.workspaces = WorkspaceRegistry(
            storage_root / "workspaces.json",
            defaults=(default_workspace,),
        )
        self.jobs = {}
        self.lock = threading.RLock()
        self.busy_sessions = set()

    def _session_key(self, workspace, session_id):
        return (os.path.normcase(str(workspace)), _safe_session_id(session_id))

    def _ensure_idle(self, workspace, session_id):
        key = self._session_key(workspace, session_id)
        if key in self.busy_sessions or any(
            job.status in ACTIVE_JOB_STATUSES and job.session_id == session_id
            and os.path.normcase(job.workspace_path) == key[0] for job in self.jobs.values()
        ):
            raise ValueError("会话正在运行或压缩，请先停止任务或等待操作完成 (running task or compaction)")

    def run_command(self, workspace_path, session_id, command, arguments="", max_rounds=None, approval_policy="ask"):
        if command not in {"compact", "goal", "plan"}:
            raise ValueError("未知会话命令")
        if not isinstance(arguments, str) or len(arguments) > MAX_MESSAGE_CHARS:
            raise ValueError("命令参数无效")
        if approval_policy not in {"ask", "auto", "never"}:
            raise ValueError("unsupported approval policy")
        workspace = WorkspaceContext.build(_safe_workspace(workspace_path))
        key = self._session_key(workspace.repo_root, session_id)
        arguments = arguments.strip()
        with self.lock:
            if command == "goal" and arguments.lower() == "pause":
                running = next((job for job in self.jobs.values() if job.status in ACTIVE_JOB_STATUSES
                                and job.session_id == session_id and os.path.normcase(job.workspace_path) == key[0]), None)
                if running:
                    running.cancel()
                    return {"message": "已请求在当前步骤结束后暂停目标", "job": running.public_dict()}
            if command == "goal" and not arguments:
                return {"session": self.get_session(workspace.repo_root, session_id), "message": "当前目标"}
            self._ensure_idle(workspace.repo_root, session_id)
            session, path = _load_session_file(workspace.repo_root, session_id)
            self.busy_sessions.add(key)
        try:
            original = json.dumps(session, sort_keys=True)
            followup = None
            message = ""
            if command == "compact":
                if arguments:
                    raise ValueError("用法：/compact，不接受额外参数")
                compact = compact_history(session, self.model_factory())
                if compact:
                    session["context_compaction"] = compact
                    message = f"已压缩 {compact['through']} 条旧上下文：{compact['before_chars']} → {compact['after_chars']} 字符；完整对话仍保留"
                else:
                    message = "暂无足够的旧上下文需要压缩；最近至少 6 条记录会保留"
            elif command == "plan":
                session["plan_mode"] = arguments.lower() != "off"
                message = "已进入规划模式：只读分析，不实施修改" if session["plan_mode"] else "已退出规划模式，原权限设置保持不变"
                if arguments and arguments.lower() != "off":
                    followup = arguments
            else:
                armed = mutate_goal(session, arguments, max_rounds)
                if armed and session.get("plan_mode"):
                    raise ValueError("请先退出规划模式，再启动目标")
                message = "目标已更新" if session.get("goal") else "目标已清除，历史状态已保留"
                if armed:
                    followup = f"推进目标：{session['goal']['objective']}"
            with self.lock:
                current, _ = _load_session_file(workspace.repo_root, session_id)
                if json.dumps(current, sort_keys=True) != original:
                    raise ValueError("会话在操作期间发生变化，本次结果未覆盖原记录，请重试")
                SessionStore(path.parent).save(session)
                result = {"session": self.get_session(workspace.repo_root, session_id), "message": message}
                # Commit and launch under one lock, without a race with another tab.
                self.busy_sessions.discard(key)
                if followup:
                    job = self.start_job(workspace_path=workspace.repo_root, session_id=session_id, message=followup, approval_policy=approval_policy)
                    result["job"] = job.public_dict()
        finally:
            with self.lock:
                self.busy_sessions.discard(key)
        return result

    def list_workspaces(self):
        return self.workspaces.list()

    def add_workspace(self, path):
        canonical = self.workspaces.add(path)
        return next(
            item for item in self.workspaces.list() if item["path"] == canonical
        )

    def rename_workspace(self, path, name):
        canonical = self.workspaces.rename(path, name)
        return next(
            item for item in self.workspaces.list() if item["path"] == canonical
        )

    def remove_workspace(self, path):
        workspace = WorkspaceContext.build(_safe_workspace(path))
        with self.lock:
            if any(key[0] == os.path.normcase(workspace.repo_root) for key in self.busy_sessions):
                raise ValueError("工作区中的会话正在压缩")
            if any(
                job.status in ACTIVE_JOB_STATUSES
                and os.path.normcase(job.workspace_path)
                == os.path.normcase(workspace.repo_root)
                for job in self.jobs.values()
            ):
                raise ValueError("cannot remove a workspace with a running task")
            canonical = self.workspaces.remove(workspace.repo_root)
        return {"removed": True, "path": canonical}

    def list_sessions(self, workspace_path):
        workspace = WorkspaceContext.build(_safe_workspace(workspace_path))
        return list_workspace_sessions(workspace.repo_root)

    def list_ungrouped_sessions(self):
        with self.workspaces.lock:
            removed = list(self.workspaces.removed)
        sessions = []
        for path in removed:
            try:
                sessions.extend(list_workspace_sessions(path))
            except (OSError, ValueError):
                continue
        return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)

    def get_session(self, workspace_path, session_id):
        return session_for_ui(workspace_path, session_id)

    def create_session(self, workspace_path):
        workspace = WorkspaceContext.build(_safe_workspace(workspace_path))
        store = SessionStore(workspace_state(workspace.repo_root) / "sessions")
        agent = Nagi(
            model_client=self.model_factory(),
            workspace=workspace,
            session_store=store,
            approval_policy="never",
        )
        session, path = _load_session_file(workspace.repo_root, agent.session["id"])
        return _session_summary(session, path, workspace.repo_root)

    def delete_session(self, workspace_path, session_id):
        workspace = WorkspaceContext.build(_safe_workspace(workspace_path))
        session_id = _safe_session_id(session_id)
        _session, path = _load_session_file(workspace.repo_root, session_id)
        with self.lock:
            self._ensure_idle(workspace.repo_root, session_id)
            if any(
                job.status in ACTIVE_JOB_STATUSES
                and job.session_id == session_id
                and os.path.normcase(job.workspace_path)
                == os.path.normcase(workspace.repo_root)
                for job in self.jobs.values()
            ):
                raise ValueError("cannot delete a session with a running task")
            path.unlink()
        return {"deleted": True, "session_id": session_id}

    def rename_session(self, workspace_path, session_id, title):
        workspace = WorkspaceContext.build(_safe_workspace(workspace_path))
        title = str(title or "").strip()
        if not title or len(title) > 80 or any(char in title for char in "\r\n\t"):
            raise ValueError("session title must be 1 to 80 characters")
        session, path = _load_session_file(workspace.repo_root, session_id)
        session["display_title"] = title
        with self.lock:
            self._ensure_idle(workspace.repo_root, session_id)
            session, path = _load_session_file(workspace.repo_root, session_id)
            session["display_title"] = title
            SessionStore(path.parent).save(session)
        return _session_summary(session, path, workspace.repo_root)

    def fork_session(self, workspace_path, session_id):
        workspace = WorkspaceContext.build(_safe_workspace(workspace_path))
        session, _path = _load_session_file(workspace.repo_root, session_id)
        forked = json.loads(json.dumps(session))
        forked["id"] = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        forked["created_at"] = now()
        forked["workspace_root"] = workspace.repo_root
        if isinstance(forked.get("display_title"), str):
            forked["display_title"] = clip(f"{forked['display_title']} 副本", 80)
        store = SessionStore(workspace_state(workspace.repo_root) / "sessions")
        with self.lock:
            self._ensure_idle(workspace.repo_root, session_id)
            path = store.save(forked)
        return _session_summary(forked, path, workspace.repo_root)

    def archive_session(self, workspace_path, session_id):
        workspace = WorkspaceContext.build(_safe_workspace(workspace_path))
        session_id = _safe_session_id(session_id)
        _session, path = _load_session_file(workspace.repo_root, session_id)
        with self.lock:
            self._ensure_idle(workspace.repo_root, session_id)
            if any(
                job.status in ACTIVE_JOB_STATUSES
                and job.session_id == session_id
                and os.path.normcase(job.workspace_path)
                == os.path.normcase(workspace.repo_root)
                for job in self.jobs.values()
            ):
                raise ValueError("cannot archive a session with a running task")
            archive_root = path.parent / "archive"
            archive_root.mkdir(parents=True, exist_ok=True)
            path.replace(archive_root / path.name)
        return {"archived": True, "session_id": session_id}

    def start_job(
        self,
        *,
        workspace_path,
        session_id,
        message,
        approval_policy="ask",
    ):
        workspace = WorkspaceContext.build(_safe_workspace(workspace_path))
        if session_id:
            _load_session_file(workspace.repo_root, session_id)
        with self.lock:
            if session_id:
                self._ensure_idle(workspace.repo_root, session_id)
                _load_session_file(workspace.repo_root, session_id)
            for job in self.jobs.values():
                if (
                    session_id
                    and job.status in ACTIVE_JOB_STATUSES
                    and job.session_id == session_id
                    and os.path.normcase(job.workspace_path)
                    == os.path.normcase(workspace.repo_root)
                ):
                    raise ValueError("this session already has a running task")
            job = AgentJob(
                workspace_path=workspace.repo_root,
                session_id=session_id,
                message=message,
                approval_policy=approval_policy,
                model_factory=self.model_factory,
                model_client=self.model_factory(),
            )
            self.jobs[job.job_id] = job
            job.start()
            return job

    def get_job(self, job_id):
        with self.lock:
            return self.jobs.get(str(job_id))

    def latest_active_job(self):
        with self.lock:
            jobs = [job for job in self.jobs.values() if job.status in ACTIVE_JOB_STATUSES]
        if not jobs:
            return None
        return max(jobs, key=lambda job: job.created_at)

    def latest_job(self):
        with self.lock:
            jobs = list(self.jobs.values())
        if not jobs:
            return None
        return max(jobs, key=lambda job: job.created_at)
