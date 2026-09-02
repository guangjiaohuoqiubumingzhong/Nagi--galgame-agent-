const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function storedJson(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(key));
    return value ?? fallback;
  } catch {
    return fallback;
  }
}

const state = {
  csrf: "",
  model: "DeepSeek",
  workspaces: [],
  ungroupedSessions: [],
  workspaceQuery: "",
  workspacePath: null,
  sessionId: null,
  agentJobId: null,
  agentJob: null,
  agentPoll: null,
  browserPresenceTimer: null,
  terminalHandled: false,
  composerBusy: false,
  sessionControls: {},
  activeView: "agent",
  collapsedWorkspaces: new Set(storedJson("nagi.workspace.collapsed", [])),
  expandedSessions: new Set(),
  groupMode: localStorage.getItem("nagi.workspace.groupMode") || "workspace",
  sortMode: localStorage.getItem("nagi.workspace.sortMode") || "manual",
};

function markBrowserPresent() {
  api("/api/browser-presence").catch(() => {});
}

function startBrowserPresence() {
  markBrowserPresent();
  if (!state.browserPresenceTimer) {
    state.browserPresenceTimer = setInterval(markBrowserPresent, 1500);
  }
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const init = { method, headers: { "Content-Type": "application/json" } };
  if (method !== "GET") {
    init.body = JSON.stringify({ ...(options.body || {}), csrf_token: state.csrf });
  }
  const response = await fetch(path, init);
  let payload;
  try { payload = await response.json(); }
  catch { payload = { error: `请求失败 (${response.status})` }; }
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function renderText(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\n/g, "<br>");
}

function shortTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(11, 19);
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function relativeDate(value) {
  if (!value) return "";
  const delta = Date.now() - new Date(value).getTime();
  if (delta < 60_000) return "刚刚";
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}分`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}时`;
  return `${Math.floor(delta / 86_400_000)}天`;
}

function setConnection(connected, text) {
  $("#connection-dot").classList.toggle("connected", connected);
  $("#connection-text").textContent = text;
}

function switchView(view) {
  closeCommandMenu();
  state.activeView = view;
  $("#agent-view").hidden = view !== "agent";
  $("#translation-view").hidden = view !== "translation";
  $$(".side-nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
}

async function loadWorkspaces({ preserve = true } = {}) {
  const previous = preserve ? state.workspacePath : null;
  const payload = await api("/api/agent/workspaces");
  state.workspaces = payload.workspaces || [];
  state.ungroupedSessions = payload.ungrouped || [];
  if (previous && state.workspaces.some((item) => item.path === previous)) state.workspacePath = previous;
  else if (!state.workspacePath || !state.workspaces.some((item) => item.path === state.workspacePath)) state.workspacePath = state.workspaces[0]?.path || null;
  renderWorkspaces();
  updateWorkspaceHeader();
}

function renderWorkspaces() {
  const root = $("#workspace-list");
  root.replaceChildren();
  if (!state.workspaces.length && !state.ungroupedSessions.length) {
    root.innerHTML = '<div class="sidebar-loading">点击＋添加本地工作区</div>';
    return;
  }
  const query = state.workspaceQuery.trim().toLocaleLowerCase();
  const orderedWorkspaces = [...state.workspaces];
  if (state.sortMode === "updated") {
    orderedWorkspaces.sort((left, right) => {
      const leftTime = left.sessions?.[0]?.updated_at || "";
      const rightTime = right.sessions?.[0]?.updated_at || "";
      return rightTime.localeCompare(leftTime);
    });
  }
  const visibleWorkspaces = query
    ? orderedWorkspaces.filter((workspace) =>
        workspace.name.toLocaleLowerCase().includes(query)
        || (workspace.sessions || []).some((session) => !session.is_blank && session.title.toLocaleLowerCase().includes(query)))
    : orderedWorkspaces;
  const ungroupedVisible = state.ungroupedSessions.length && (!query
    || state.ungroupedSessions.some((session) => !session.is_blank && session.title.toLocaleLowerCase().includes(query)));
  if (!visibleWorkspaces.length && !ungroupedVisible) {
    root.innerHTML = '<div class="sidebar-loading">没有匹配的工作区或对话</div>';
    return;
  }
  const entries = [...visibleWorkspaces];
  if (ungroupedVisible) entries.push({ path: "__ungrouped__", name: "未分组", sessions: state.ungroupedSessions, ungrouped: true });
  if (state.groupMode === "flat") {
    const flatSessions = entries.flatMap((workspace) => (workspace.sessions || [])
      .filter((session) => !session.is_blank || session.session_id === state.sessionId)
      .filter((session) => !query || session.title.toLocaleLowerCase().includes(query))
      .map((session) => ({ ...session, workspace_path: session.workspace_path || workspace.path })));
    if (state.sortMode === "updated") flatSessions.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    const list = document.createElement("div");
    list.className = "flat-session-list";
    flatSessions.forEach((session) => list.append(createSessionRow(session.workspace_path, session, true)));
    root.append(list);
    return;
  }
  entries.forEach((workspace) => {
    const group = document.createElement("div");
    group.className = "workspace-group";
    const isUngrouped = Boolean(workspace.ungrouped);
    const collapsed = state.collapsedWorkspaces.has(workspace.path);
    const header = document.createElement("div");
    header.className = `workspace-row${workspace.path === state.workspacePath ? " active" : ""}${collapsed ? "" : " is-open"}`;
    header.tabIndex = 0;
    header.setAttribute("role", "button");
    header.setAttribute("aria-expanded", String(!collapsed));
    header.setAttribute("aria-label", `${workspace.name}，${collapsed ? "展开" : "收起"}对话`);
    const toggleWorkspace = () => {
      if (collapsed) state.collapsedWorkspaces.delete(workspace.path);
      else {
        state.collapsedWorkspaces.add(workspace.path);
        state.expandedSessions.delete(workspace.path);
      }
      persistWorkspaceUi();
      renderWorkspaces();
    };
    header.addEventListener("click", toggleWorkspace);
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggleWorkspace();
      }
    });
    const folder = document.createElement("span");
    folder.className = "workspace-leading";
    folder.innerHTML = '<svg class="workspace-folder-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 6.5h6l2 2H20a1 1 0 0 1 1 1v8.8a1.2 1.2 0 0 1-1.2 1.2H4.2A1.2 1.2 0 0 1 3 18.3V7.2a.7.7 0 0 1 .5-.7Z"/></svg><svg class="workspace-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 3.8 5.2 4.2L6 12.2Z"/></svg>';
    const label = document.createElement("span");
    label.className = "workspace-label";
    label.title = workspace.path;
    label.textContent = workspace.name;
    const actions = document.createElement("div");
    actions.className = "workspace-actions";
    const moreButton = document.createElement("button");
    moreButton.type = "button";
    moreButton.className = "workspace-action workspace-more";
    moreButton.title = "工作区选项";
    moreButton.setAttribute("aria-label", `${workspace.name} 工作区选项`);
    moreButton.innerHTML = '<svg class="ellipsis-icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="3.5" cy="8" r="1.25"/><circle cx="8" cy="8" r="1.25"/><circle cx="12.5" cy="8" r="1.25"/></svg>';
    const menu = document.createElement("div");
    menu.className = "workspace-menu";
    menu.hidden = true;
    menu.innerHTML = `
      <button type="button" data-workspace-action="rename">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15.2 5.3 3.5 3.5M4 20l4.2-.9L19 8.3a1.5 1.5 0 0 0 0-2.1l-1.2-1.2a1.5 1.5 0 0 0-2.1 0L4.9 15.8 4 20Z"/></svg>
        <span>重命名</span>
      </button>
      <button type="button" class="danger" data-workspace-action="remove">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"/></svg>
        <span>删除工作区</span>
      </button>`;
    menu.addEventListener("click", (event) => event.stopPropagation());
    menu.querySelector('[data-workspace-action="rename"]').addEventListener("click", () => renameWorkspace(workspace));
    menu.querySelector('[data-workspace-action="remove"]').addEventListener("click", () => removeWorkspace(workspace));
    moreButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const willOpen = menu.hidden;
      closeWorkspaceMenus();
      menu.hidden = !willOpen;
      header.classList.toggle("menu-open", willOpen);
    });
    const createButton = document.createElement("button");
    createButton.type = "button";
    createButton.className = "workspace-action workspace-create";
    createButton.title = `在 ${workspace.name} 中新建对话`;
    createButton.setAttribute("aria-label", createButton.title);
    createButton.textContent = "+";
    createButton.addEventListener("click", (event) => {
      event.stopPropagation();
      state.collapsedWorkspaces.delete(workspace.path);
      persistWorkspaceUi();
      createSessionForWorkspace(workspace.path);
    });
    if (!isUngrouped) actions.append(moreButton, createButton);
    header.append(folder, label, actions, ...(isUngrouped ? [] : [menu]));
    group.append(header);
    const sessions = document.createElement("div");
    sessions.className = "session-list";
    sessions.hidden = collapsed;
    const workspaceMatches = workspace.name.toLocaleLowerCase().includes(query);
    const allSessions = (workspace.sessions || []).filter(
      (session) => (!session.is_blank || session.session_id === state.sessionId)
        && (!query || workspaceMatches || session.title.toLocaleLowerCase().includes(query)),
    );
    const expanded = state.expandedSessions.has(workspace.path);
    const visibleSessions = expanded ? allSessions : allSessions.slice(0, 5);
    visibleSessions.forEach((session) => sessions.append(createSessionRow(session.workspace_path || workspace.path, session)));
    if (!collapsed && allSessions.length > 5) {
      const moreButton = document.createElement("button");
      moreButton.type = "button";
      moreButton.className = "session-more";
      moreButton.textContent = expanded ? "收起部分会话" : `展开其余 ${allSessions.length - 5} 个会话`;
      moreButton.addEventListener("click", (event) => {
        event.stopPropagation();
        if (expanded) state.expandedSessions.delete(workspace.path);
        else state.expandedSessions.add(workspace.path);
        renderWorkspaces();
      });
      sessions.append(moreButton);
    }
    group.append(sessions);
    root.append(group);
  });
}

function persistWorkspaceUi() {
  localStorage.setItem("nagi.workspace.collapsed", JSON.stringify([...state.collapsedWorkspaces]));
  localStorage.setItem("nagi.workspace.groupMode", state.groupMode);
  localStorage.setItem("nagi.workspace.sortMode", state.sortMode);
}

function createSessionRow(workspacePath, session, flat = false) {
  const row = document.createElement("div");
  row.className = `session-row${session.session_id === state.sessionId ? " active" : ""}${flat ? " flat" : ""}`;
  row.setAttribute("role", "treeitem");
  row.title = session.title;
  const sessionButton = document.createElement("button");
  sessionButton.type = "button";
  sessionButton.className = "session-button";
  sessionButton.innerHTML = `<span>${escapeHtml(session.title)}</span>${session.is_blank ? "" : `<time>${relativeDate(session.updated_at)}</time>`}`;
  sessionButton.addEventListener("click", () => selectSession(workspacePath, session.session_id));
  row.append(sessionButton);
  if (!session.is_blank) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "session-delete";
    more.title = "对话选项";
    more.setAttribute("aria-label", `对话选项：${session.title}`);
    more.innerHTML = '<svg class="ellipsis-icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="3.5" cy="8" r="1.25"/><circle cx="8" cy="8" r="1.25"/><circle cx="12.5" cy="8" r="1.25"/></svg>';
    const menu = document.createElement("div");
    menu.className = "session-menu";
    menu.hidden = true;
    menu.innerHTML = `
      <button type="button" data-session-action="rename">重命名</button>
      <button type="button" data-session-action="fork">复制对话</button>
      <button type="button" data-session-action="archive">归档</button>
      <div class="session-menu-separator"></div>
      <button type="button" class="danger" data-session-action="delete">删除对话</button>`;
    menu.addEventListener("click", (event) => event.stopPropagation());
    menu.querySelector("[data-session-action=rename]").addEventListener("click", () => renameSession(workspacePath, session));
    menu.querySelector("[data-session-action=fork]").addEventListener("click", () => forkSession(workspacePath, session));
    menu.querySelector("[data-session-action=archive]").addEventListener("click", () => archiveSession(workspacePath, session));
    menu.querySelector("[data-session-action=delete]").addEventListener("click", () => {
      closeWorkspaceMenus();
      deleteSession(workspacePath, session);
    });
    more.addEventListener("click", (event) => {
      event.stopPropagation();
      const willOpen = menu.hidden;
      closeWorkspaceMenus();
      menu.hidden = !willOpen;
      row.classList.toggle("menu-open", willOpen);
    });
    row.append(more, menu);
  }
  return row;
}

function closeWorkspaceMenus() {
  $$(".workspace-menu, .session-menu").forEach((menu) => { menu.hidden = true; });
  $$(".workspace-row.menu-open, .session-row.menu-open").forEach((row) => row.classList.remove("menu-open"));
}

let dialogResolver = null;

function openWorkspaceDialog({ title, description, value = null, confirmLabel = "保存", danger = false }) {
  const backdrop = $("#workspace-dialog");
  const input = $("#workspace-dialog-input");
  $("#workspace-dialog-title").textContent = title;
  $("#workspace-dialog-description").textContent = description;
  input.hidden = value === null;
  input.value = value ?? "";
  const confirm = $("#workspace-dialog-confirm");
  confirm.textContent = confirmLabel;
  confirm.className = danger ? "danger-button" : "primary-button";
  backdrop.hidden = false;
  if (value !== null) requestAnimationFrame(() => { input.focus(); input.select(); });
  else requestAnimationFrame(() => confirm.focus());
  return new Promise((resolve) => { dialogResolver = resolve; });
}

function closeWorkspaceDialog(confirmed) {
  const backdrop = $("#workspace-dialog");
  if (backdrop.hidden) return;
  const result = confirmed ? { confirmed: true, value: $("#workspace-dialog-input").value.trim() } : { confirmed: false, value: "" };
  backdrop.hidden = true;
  const resolve = dialogResolver;
  dialogResolver = null;
  if (resolve) resolve(result);
}

async function renameWorkspace(workspace) {
  closeWorkspaceMenus();
  const result = await openWorkspaceDialog({
    title: "重命名工作区",
    description: "只修改 Nagi 中显示的名称，不会重命名磁盘上的文件夹。",
    value: workspace.name,
  });
  const name = result.value;
  if (!result.confirmed || !name || name === workspace.name) return;
  try {
    await api("/api/agent/workspaces/rename", {
      method: "POST",
      body: { path: workspace.path, name },
    });
    await loadWorkspaces();
  } catch (error) {
    $("#run-metrics").textContent = error.message;
  }
}

async function removeWorkspace(workspace) {
  closeWorkspaceMenus();
  const result = await openWorkspaceDialog({
    title: "删除工作区",
    description: `从 Nagi 中移除“${workspace.name}”？项目文件和已保存对话仍会保留在磁盘中。`,
    confirmLabel: "删除工作区",
    danger: true,
  });
  if (!result.confirmed) return;
  try {
    await api("/api/agent/workspaces/remove", {
      method: "POST",
      body: { path: workspace.path },
    });
    if (state.workspacePath === workspace.path) {
      state.workspacePath = null;
      state.sessionId = null;
      state.agentJob = null;
      renderSessionControls({});
      $("#session-title").textContent = "新会话";
      renderHistory([]);
    }
    state.collapsedWorkspaces.delete(workspace.path);
    await loadWorkspaces({ preserve: false });
  } catch (error) {
    $("#run-metrics").textContent = error.message;
  }
}

function updateWorkspaceHeader() {
  const workspace = state.workspaces.find((item) => item.path === state.workspacePath);
  const locked = !workspace || Boolean(state.agentJobId) || state.composerBusy;
  $("#prompt-input").disabled = locked;
  $("#send-button").disabled = locked;
  $("#composer-add").disabled = locked;
  renderSessionControls();
  if (locked) closeCommandMenu();
}

// Harness's composer + launches the same command source as a leading slash.
// Reference: deepseek-ai/deepseek-harness, ui-conversation/InputBar and ui-input-trigger/MenuView.
// Only expose commands backed by actual Nagi UI actions; never send them to the model.
const composerCommands = [
  { name: "compact", description: "压缩旧对话上下文", run: () => compactSession() },
  { name: "goal", description: "设置或查看长任务目标", acceptsArgs: true, run: (args = "") => args ? runSessionCommand("goal", args) : openGoalDialog() },
  { name: "plan", description: "进入规划模式；/plan off 退出", acceptsArgs: true, run: (args = "") => runSessionCommand("plan", args) },
  { name: "new", description: "在当前工作区新建对话", run: () => newSession({ fromCommand: true }) },
  { name: "permissions", description: "选择工具执行权限", run: () => openPermissionMenu() },
  { name: "workspace", description: "搜索并切换工作区或对话", run: () => openWorkspaceSearch() },
  { name: "qlie", description: "打开游戏翻译工作流", run: () => switchView("translation") },
];

function renderSessionControls(controls = state.sessionControls) {
  state.sessionControls = controls || {};
  const goal = state.sessionControls.goal;
  const planning = state.sessionControls.plan_mode === true;
  $("#session-controls").hidden = !goal && !planning;
  $("#plan-mode-badge").hidden = !planning;
  $("#plan-mode-badge").disabled = Boolean(state.agentJobId) || state.composerBusy;
  $("#goal-badge").hidden = !goal;
  if (goal) {
    const label = { active: "进行中", paused: "已暂停", completed: "已完成", blocked: "需要帮助" }[goal.status] || goal.status;
    $("#goal-badge").textContent = `目标 · ${label} · ${goal.rounds}/${goal.max_rounds} 轮 · ${goal.objective}`;
    $("#goal-badge").title = goal.objective;
  }
}

async function ensureCommandSession() {
  if (!state.sessionId) await createSessionForWorkspace(state.workspacePath, { fromCommand: true });
  if (!state.sessionId) throw new Error("请先选择工作区并创建会话");
}

async function runSessionCommand(command, args = "", maxRounds) {
  await ensureCommandSession();
  const workspace = state.workspacePath;
  const sessionId = state.sessionId;
  $("#run-metrics").textContent = command === "compact" ? "正在压缩旧上下文，请稍候；原始对话会保留…" : "正在更新会话…";
  const result = await api(`/api/agent/sessions/${encodeURIComponent(sessionId)}/command`, {
    method: "POST", body: { workspace, command, arguments: args, max_rounds: maxRounds, approval_policy: $("#approval-policy").value },
  });
  if (state.workspacePath !== workspace || state.sessionId !== sessionId) return result;
  if (result.session) renderSessionControls(result.session.controls);
  if (result.job) {
    state.terminalHandled = false;
    renderAgentJob(result.job);
    clearInterval(state.agentPoll);
    if (state.agentJobId) state.agentPoll = setInterval(pollAgentJob, 850);
  }
  $("#run-metrics").textContent = result.message;
  return result;
}

async function compactSession() {
  if (!window.confirm("压缩会调用当前模型生成摘要，可能产生 API 费用。完整聊天记录不会删除。继续吗？")) return;
  return runSessionCommand("compact");
}

async function openGoalDialog() {
  await runSessionCommand("goal");
  const goal = state.sessionControls.goal;
  $("#goal-objective").value = goal?.objective || "";
  $("#goal-rounds").value = goal?.max_rounds || 5;
  $("#goal-error").hidden = true;
  updateGoalDialog();
  if (!$("#goal-dialog").open) $("#goal-dialog").showModal();
}

function updateGoalDialog() {
  const goal = state.sessionControls.goal;
  const running = Boolean(state.agentJobId);
  const status = { active: "进行中", paused: "已暂停", completed: "已完成", blocked: "需要帮助" }[goal?.status];
  $("#goal-status").textContent = goal ? `${status} · 已使用 ${goal.rounds}/${goal.max_rounds} 轮${goal.reason ? ` · ${goal.reason}` : ""}` : "尚未设置目标";
  $("#goal-save").textContent = goal && goal.status !== "completed" ? "保存修改" : "创建并开始";
  $("#goal-save").disabled = running;
  $("#goal-objective").disabled = running;
  $("#goal-rounds").disabled = running;
  $("#goal-clear").hidden = !goal;
  $("#goal-clear").disabled = running;
  $("#goal-pause").hidden = !goal || goal.status !== "active";
  $("#goal-resume").hidden = !goal || goal.status === "completed" || running;
}

async function goalAction(action) {
  if (state.composerBusy) return;
  const goal = state.sessionControls.goal;
  let args = action;
  const maxRounds = Number($("#goal-rounds").value);
  if (action === "save") {
    if (!$("#goal-form").reportValidity()) return;
    const objective = $("#goal-objective").value.trim();
    // Prefix edit explicitly: control words are legitimate text in a goal objective.
    if (!objective) return;
    args = goal && goal.status !== "completed" ? `edit ${objective}` : objective;
    if ((!goal || goal.status === "completed") && /^(pause|resume|clear)$/i.test(objective)) {
      $("#goal-error").textContent = "请填写完整目标，不要只填写 pause、resume 或 clear 控制词";
      $("#goal-error").hidden = false;
      return;
    }
  }
  if (action === "clear" && !window.confirm("清除当前目标？以往目标状态仍会保留在会话文件中。")) return;
  if ((action === "resume" || (action === "save" && (!goal || goal.status === "completed")))
      && !window.confirm(`将开始自动推进目标，累计最多 ${maxRounds} 轮，每轮可能产生多次 API 费用。继续吗？`)) return;
  state.composerBusy = true;
  updateWorkspaceHeader();
  $("#goal-error").hidden = true;
  try {
    await runSessionCommand("goal", args, maxRounds);
    updateGoalDialog();
  } catch (error) {
    $("#goal-error").textContent = error.message;
    $("#goal-error").hidden = false;
  } finally {
    state.composerBusy = false;
    updateWorkspaceHeader();
  }
}
const commandMenuState = { open: false, source: null, items: [], active: 0, span: null, composing: false };

function commandMatches(query) {
  const needle = query.toLowerCase();
  return composerCommands.map((command, index) => {
    const name = command.name.toLowerCase();
    let cursor = 0;
    let score = 0;
    let previous = -2;
    for (const character of needle) {
      const position = name.indexOf(character, cursor);
      if (position < 0) return null;
      score += (position === 0 || "-_".includes(name[position - 1]) ? 8 : 0)
        + (position === previous + 1 ? 4 : 0) - (position - cursor);
      previous = position;
      cursor = position + 1;
    }
    return { command, index, score, prefix: name.startsWith(needle) };
  }).filter(Boolean).sort((a, b) => Number(b.prefix) - Number(a.prefix) || b.score - a.score || a.index - b.index)
    .map((match) => match.command);
}

function closeCommandMenu() {
  commandMenuState.open = false;
  commandMenuState.source = null;
  commandMenuState.span = null;
  $("#command-menu").hidden = true;
  $("#composer-add").setAttribute("aria-expanded", "false");
  $("#prompt-input").removeAttribute("aria-activedescendant");
  $("#prompt-input").removeAttribute("aria-controls");
}

function renderCommandMenu() {
  const menu = $("#command-menu");
  menu.replaceChildren();
  if (!commandMenuState.items.length) {
    const empty = document.createElement("div");
    empty.className = "command-empty";
    empty.textContent = "没有匹配的命令";
    menu.append(empty);
  }
  commandMenuState.items.forEach((command, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.id = `command-option-${command.name}`;
    button.className = "command-item";
    button.tabIndex = -1;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(index === commandMenuState.active));
    button.innerHTML = `<span class="command-name">/${command.name}</span><span class="command-description">${escapeHtml(command.description)}</span>`;
    button.addEventListener("mousedown", (event) => event.preventDefault());
    button.addEventListener("click", (event) => { event.stopPropagation(); pickCommand(command); });
    menu.append(button);
  });
  menu.style.maxHeight = `${Math.max(0, Math.min(320, $(".composer").getBoundingClientRect().top - 12))}px`;
  menu.hidden = false;
  $("#composer-add").setAttribute("aria-expanded", "true");
  $("#prompt-input").setAttribute("aria-controls", "command-menu");
  const active = commandMenuState.items[commandMenuState.active];
  if (active) {
    $("#prompt-input").setAttribute("aria-activedescendant", `command-option-${active.name}`);
    $(`#command-option-${active.name}`).scrollIntoView({ block: "nearest" });
  } else $("#prompt-input").removeAttribute("aria-activedescendant");
}

function openCommandMenu(source, query = "", span = null) {
  if ($("#prompt-input").disabled || commandMenuState.composing) return;
  closePermissionMenu();
  Object.assign(commandMenuState, { open: true, source, items: commandMatches(query), active: 0, span });
  renderCommandMenu();
}

function toggleCommandMenu() {
  $("#prompt-input").focus({ preventScroll: true });
  if (commandMenuState.open) closeCommandMenu();
  else openCommandMenu("launcher");
}

function trackCommandInput() {
  const input = $("#prompt-input");
  if (commandMenuState.composing) return;
  const beforeCaret = input.value.slice(0, input.selectionStart);
  const match = /^\/([^\s/]*)$/.exec(beforeCaret);
  if (match && input.selectionStart === input.selectionEnd) {
    const end = input.value.search(/\s/);
    openCommandMenu("slash", match[1], { start: 0, end: end < 0 ? input.value.length : end });
  } else closeCommandMenu();
}

async function pickCommand(command, span = commandMenuState.span, args = "") {
  const input = $("#prompt-input");
  if (input.disabled || state.composerBusy || state.agentJobId) return;
  const draft = input.value;
  if (span) {
    input.value = draft.slice(0, span.start) + draft.slice(span.end);
    input.setSelectionRange(span.start, span.start);
  }
  const remainingDraft = input.value;
  closeCommandMenu();
  state.composerBusy = true;
  updateWorkspaceHeader();
  try {
    await command.run(args);
  } catch (error) {
    if (input.value === remainingDraft) input.value = draft;
    $("#run-metrics").textContent = `命令执行失败：${error.message}`;
  } finally {
    state.composerBusy = false;
    updateWorkspaceHeader();
    renderSessionControls();
    // Search and translation deliberately move focus to their own interface.
    if (["new", "permissions"].includes(command.name)) input.focus({ preventScroll: true });
  }
}

function handleComposerKeydown(event) {
  if (event.isComposing || event.keyCode === 229 || commandMenuState.composing) return;
  if (commandMenuState.open && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey) {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      const length = commandMenuState.items.length;
      if (length) commandMenuState.active = (commandMenuState.active + (event.key === "ArrowDown" ? 1 : -1) + length) % length;
      renderCommandMenu();
      return;
    }
    if (event.key === "Escape") { event.preventDefault(); closeCommandMenu(); return; }
    if (event.key === "Enter") {
      event.preventDefault();
      const command = commandMenuState.items[commandMenuState.active];
      if (command) pickCommand(command);
      return;
    }
  }
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); }
}

async function selectWorkspace(path) {
  if (state.agentJobId || state.composerBusy) return;
  renderSessionControls({});
  state.workspacePath = path;
  state.collapsedWorkspaces.delete(path);
  persistWorkspaceUi();
  state.sessionId = null;
  state.agentJob = null;
  $("#session-title").textContent = "新会话";
  renderHistory([]);
  renderWorkspaces();
  updateWorkspaceHeader();
  switchView("agent");
}

async function selectSession(workspacePath, sessionId) {
  if (state.agentJobId || state.composerBusy) return;
  state.workspacePath = workspacePath;
  state.sessionId = sessionId;
  const session = await api(`/api/agent/sessions/${encodeURIComponent(sessionId)}?workspace=${encodeURIComponent(workspacePath)}`);
  $("#session-title").textContent = session.title;
  renderSessionControls(session.controls);
  renderHistory(session.history || []);
  renderWorkspaces();
  updateWorkspaceHeader();
  switchView("agent");
}

async function newSession({ fromCommand = false } = {}) {
  if (!state.workspacePath || state.agentJobId || (state.composerBusy && !fromCommand)) return;
  await createSessionForWorkspace(state.workspacePath, { fromCommand });
}

async function createSessionForWorkspace(workspacePath, { fromCommand = false } = {}) {
  if (!workspacePath || state.agentJobId || (state.composerBusy && !fromCommand)) return;
  state.workspacePath = workspacePath;
  state.collapsedWorkspaces.delete(workspacePath);
  persistWorkspaceUi();
  const session = await api("/api/agent/sessions", { method: "POST", body: { workspace: workspacePath } });
  state.sessionId = session.session_id;
  renderSessionControls(session.controls || {});
  $("#session-title").textContent = "新会话";
  renderHistory([]);
  await loadWorkspaces();
  switchView("agent");
  $("#prompt-input").focus();
}

async function deleteSession(workspacePath, session) {
  if (state.agentJobId && session.session_id === state.sessionId) return;
  const result = await openWorkspaceDialog({
    title: "删除对话",
    description: `确定删除“${session.title}”吗？此操作无法撤销。`,
    confirmLabel: "删除对话",
    danger: true,
  });
  if (!result.confirmed) return;
  try {
    await api(`/api/agent/sessions/${encodeURIComponent(session.session_id)}`, {
      method: "DELETE",
      body: { workspace: workspacePath },
    });
    if (state.sessionId === session.session_id) {
      state.sessionId = null;
      state.agentJob = null;
      renderSessionControls({});
      $("#session-title").textContent = "新会话";
      renderHistory([]);
    }
    await loadWorkspaces();
  } catch (error) {
    $("#run-metrics").textContent = error.message;
  }
}

async function renameSession(workspacePath, session) {
  closeWorkspaceMenus();
  const result = await openWorkspaceDialog({
    title: "重命名对话",
    description: "设置一个便于识别的对话名称。",
    value: session.title,
  });
  if (!result.confirmed || !result.value || result.value === session.title) return;
  try {
    const renamed = await api(`/api/agent/sessions/${encodeURIComponent(session.session_id)}/rename`, {
      method: "POST",
      body: { workspace: workspacePath, title: result.value },
    });
    if (state.sessionId === session.session_id) $("#session-title").textContent = renamed.title;
    await loadWorkspaces();
  } catch (error) {
    $("#run-metrics").textContent = error.message;
  }
}

async function forkSession(workspacePath, session) {
  closeWorkspaceMenus();
  try {
    const forked = await api(`/api/agent/sessions/${encodeURIComponent(session.session_id)}/fork`, {
      method: "POST",
      body: { workspace: workspacePath },
    });
    await loadWorkspaces();
    await selectSession(workspacePath, forked.session_id);
  } catch (error) {
    $("#run-metrics").textContent = error.message;
  }
}

async function archiveSession(workspacePath, session) {
  closeWorkspaceMenus();
  try {
    await api(`/api/agent/sessions/${encodeURIComponent(session.session_id)}/archive`, {
      method: "POST",
      body: { workspace: workspacePath },
    });
    if (state.sessionId === session.session_id) {
      state.sessionId = null;
      state.agentJob = null;
      renderSessionControls({});
      $("#session-title").textContent = "新会话";
      renderHistory([]);
    }
    await loadWorkspaces();
  } catch (error) {
    $("#run-metrics").textContent = error.message;
  }
}

function renderHistory(history, running = false) {
  const root = $("#message-list");
  root.replaceChildren();
  // Tool calls and their output are internal execution records, not chat messages.
  const visible = (history || []).filter((item) => ["user", "assistant"].includes(item.role));
  $("#empty-state").hidden = visible.length > 0 || running;
  visible.forEach((item) => {
    const article = document.createElement("article");
    article.className = `message ${item.role}`;
    const label = item.role === "user" ? "你" : "Nagi";
    const avatar = item.role === "user" ? "YOU" : '<img class="nagi-avatar" src="/nagi-avatar.png" alt="" />';
    article.innerHTML = `<div class="message-avatar">${avatar}</div><div><div class="message-head"><strong>${escapeHtml(label)}</strong><time>${shortTime(item.created_at)}</time></div><div class="message-body">${renderText(item.content || "")}</div></div>`;
    root.append(article);
  });
  if (running) {
    const thinking = document.createElement("div");
    thinking.className = "thinking-card";
    thinking.innerHTML = '<span class="spinner"></span><span>Nagi 正在读取工作区并决定下一步…</span>';
    root.append(thinking);
  }
  $("#conversation-pane").scrollTop = $("#conversation-pane").scrollHeight;
}

function statusLabel(status) {
  return { queued: "排队中", running: "运行中", awaiting_approval: "等待批准", completed: "已完成", failed: "失败", cancelled: "已停止" }[status] || "空闲";
}

function renderAgentJob(job) {
  state.agentJob = job;
  const active = ["queued", "running", "awaiting_approval"].includes(job.status);
  state.agentJobId = active ? job.job_id : null;
  if (job.controls) renderSessionControls(job.controls);
  if ($("#goal-dialog").open) updateGoalDialog();
  if (job.session_id) state.sessionId = job.session_id;
  renderHistory(job.history || [], active && job.status !== "awaiting_approval");
  $("#stop-agent").hidden = !active;
  $("#approval-card").hidden = !job.pending_approval;
  if (job.pending_approval) {
    $("#approval-tool").textContent = job.pending_approval.tool;
    $("#approval-args").textContent = JSON.stringify(job.pending_approval.args, null, 2);
  }
  const task = job.task || {};
  $("#run-metrics").textContent = active
    ? `${statusLabel(job.status)} · ${task.attempts || 0} 轮模型 · ${task.tool_steps || 0} 次工具调用`
    : job.error
      ? `任务失败 · ${job.error}`
      : `${statusLabel(job.status)} · ${task.attempts || 0} 轮模型 · ${task.tool_steps || 0} 次工具调用`;
  updateWorkspaceHeader();
  if (!active && !state.terminalHandled) {
    state.terminalHandled = true;
    clearInterval(state.agentPoll);
    state.agentPoll = null;
    loadWorkspaces().then(() => {
      const session = state.workspaces.flatMap((item) => item.sessions || []).find((item) => item.session_id === state.sessionId);
      if (session) $("#session-title").textContent = session.title;
    }).catch(() => {});
  }
}

async function sendMessage() {
  const input = $("#prompt-input");
  const message = input.value.trim();
  if (!message || !state.workspacePath || state.agentJobId || state.composerBusy || commandMenuState.composing) return;
  if (message.startsWith("/")) {
    const match = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(message);
    const command = composerCommands.find((item) => item.name === match?.[1].toLowerCase());
    const args = match?.[2]?.trim() || "";
    if (command && (!args || command.acceptsArgs)) await pickCommand(command, { start: 0, end: input.value.length }, args);
    else $("#run-metrics").textContent = "未执行：未知命令或多余参数。输入 / 查看可用命令；本地命令不会发送给模型。";
    return;
  }
  closeCommandMenu();
  input.value = "";
  state.terminalHandled = false;
  state.composerBusy = true;
  updateWorkspaceHeader();
  try {
    const job = await api("/api/agent/jobs", {
      method: "POST",
      body: {
        workspace: state.workspacePath,
        session_id: state.sessionId,
        message,
        approval_policy: $("#approval-policy").value,
      },
    });
    renderAgentJob(job);
    state.agentPoll = setInterval(pollAgentJob, 850);
  } catch (error) {
    $("#run-metrics").textContent = error.message;
    input.value = message;
  } finally {
    state.composerBusy = false;
    updateWorkspaceHeader();
  }
}

async function pollAgentJob() {
  if (!state.agentJobId && state.agentJob?.status !== "awaiting_approval") return;
  const jobId = state.agentJobId || state.agentJob.job_id;
  try { renderAgentJob(await api(`/api/agent/jobs/${jobId}`)); }
  catch (error) { $("#run-metrics").textContent = error.message; }
}

async function resolveApproval(approved) {
  const pending = state.agentJob?.pending_approval;
  if (!pending) return;
  try {
    const job = await api(`/api/agent/jobs/${state.agentJob.job_id}/approval`, { method: "POST", body: { approval_id: pending.approval_id, approved } });
    renderAgentJob(job);
  } catch (error) { $("#run-metrics").textContent = error.message; }
}

async function stopAgent() {
  const jobId = state.agentJobId;
  if (!jobId || !window.confirm("将在当前模型或工具步骤结束后安全停止，是否继续？")) return;
  renderAgentJob(await api(`/api/agent/jobs/${jobId}/cancel`, { method: "POST", body: {} }));
}


const permissionLabels = {
  never: "只读模式",
  ask: "每次询问",
  auto: "自动批准",
};

function setPermissionPolicy(policy) {
  if (!permissionLabels[policy]) return;
  $("#approval-policy").value = policy;
  $("#permission-label").textContent = permissionLabels[policy];
  $("#permission-trigger").dataset.policy = policy;
  $$("#permission-menu [data-policy]").forEach((button) => {
    button.setAttribute("aria-checked", String(button.dataset.policy === policy));
  });
  closePermissionMenu();
}

function openPermissionMenu() {
  closeCommandMenu();
  $("#permission-menu").hidden = false;
  $("#permission-trigger").setAttribute("aria-expanded", "true");
}

function closePermissionMenu() {
  $("#permission-menu").hidden = true;
  $("#permission-trigger").setAttribute("aria-expanded", "false");
}

function togglePermissionMenu() {
  if ($("#permission-menu").hidden) openPermissionMenu();
  else closePermissionMenu();
}

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  const toggle = $("#sidebar-toggle");
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
  toggle.title = collapsed ? "展开侧栏" : "收起侧栏";
  localStorage.setItem("nagi.sidebar.collapsed", JSON.stringify(collapsed));
}

function openWorkspaceSearch() {
  setSidebarCollapsed(false);
  $("#workspace-heading").classList.add("searching");
  $(".workspace-heading-default").hidden = true;
  $(".workspace-search-box").hidden = false;
  requestAnimationFrame(() => $("#workspace-search-input").focus());
}

function closeWorkspaceSearch() {
  state.workspaceQuery = "";
  $("#workspace-search-input").value = "";
  $("#workspace-heading").classList.remove("searching");
  $(".workspace-heading-default").hidden = false;
  $(".workspace-search-box").hidden = true;
  renderWorkspaces();
}

function closeViewOptions() {
  $("#workspace-options-menu").hidden = true;
  $("#workspace-options").setAttribute("aria-expanded", "false");
}

function updateViewOptions() {
  $$('[data-group-mode]').forEach((button) => button.setAttribute("aria-checked", String(button.dataset.groupMode === state.groupMode)));
  $$('[data-sort-mode]').forEach((button) => button.setAttribute("aria-checked", String(button.dataset.sortMode === state.sortMode)));
}

async function addWorkspace() {
  if (state.composerBusy) return;
  try {
    const result = await api("/api/agent/select-workspace", { method: "POST", body: {} });
    if (result.workspace) {
      state.workspacePath = result.workspace.path;
      await loadWorkspaces({ preserve: false });
    }
  } catch (error) {
    $("#run-metrics").textContent = error.message;
  }
}

function bindEvents() {
  $("#goal-badge").addEventListener("click", () => openGoalDialog().catch(error => { $("#run-metrics").textContent = error.message; }));
  $("#plan-mode-badge").addEventListener("click", () => pickCommand(composerCommands.find(item => item.name === "plan"), null, "off"));
  $("#goal-close").addEventListener("click", () => $("#goal-dialog").close());
  $("#goal-form").addEventListener("submit", event => { event.preventDefault(); goalAction("save"); });
  $("#goal-pause").addEventListener("click", () => goalAction("pause"));
  $("#goal-resume").addEventListener("click", () => goalAction("resume"));
  $("#goal-clear").addEventListener("click", () => goalAction("clear"));
  $$(".side-nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $("#sidebar-toggle").addEventListener("click", () => setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed")));
  $("#brand-new-session").addEventListener("click", newSession);
  $("#new-session").addEventListener("click", newSession);
  $("#workspace-search").addEventListener("click", openWorkspaceSearch);
  $("#workspace-search-rail").addEventListener("click", openWorkspaceSearch);
  $("#workspace-search-input").addEventListener("input", (event) => {
    state.workspaceQuery = event.target.value;
    renderWorkspaces();
  });
  $("#workspace-search-close").addEventListener("click", closeWorkspaceSearch);
  $("#workspace-options").addEventListener("click", () => {
    const menu = $("#workspace-options-menu");
    const opening = menu.hidden;
    closeWorkspaceMenus();
    menu.hidden = !opening;
    $("#workspace-options").setAttribute("aria-expanded", String(opening));
  });
  $$('[data-group-mode]').forEach((button) => button.addEventListener("click", () => {
    state.groupMode = button.dataset.groupMode;
    persistWorkspaceUi();
    updateViewOptions();
    closeViewOptions();
    renderWorkspaces();
  }));
  $$('[data-sort-mode]').forEach((button) => button.addEventListener("click", () => {
    state.sortMode = button.dataset.sortMode;
    persistWorkspaceUi();
    updateViewOptions();
    closeViewOptions();
    renderWorkspaces();
  }));
  $("#add-workspace").addEventListener("click", addWorkspace);
  $("#add-workspace-rail").addEventListener("click", addWorkspace);
  bindModelSettings();
  $("#workspace-dialog-cancel").addEventListener("click", () => closeWorkspaceDialog(false));
  $("#workspace-dialog-confirm").addEventListener("click", () => closeWorkspaceDialog(true));
  $("#workspace-dialog-input").addEventListener("keydown", (event) => { if (event.key === "Enter") closeWorkspaceDialog(true); });
  $("#workspace-dialog").addEventListener("mousedown", (event) => { if (event.target === $("#workspace-dialog")) closeWorkspaceDialog(false); });
  $("#send-button").addEventListener("click", sendMessage);
  $("#composer-add").addEventListener("mousedown", (event) => {
    event.preventDefault();
    $("#prompt-input").focus({ preventScroll: true });
  });
  $("#composer-add").addEventListener("click", toggleCommandMenu);
  $("#prompt-input").addEventListener("keydown", handleComposerKeydown);
  $("#prompt-input").addEventListener("input", trackCommandInput);
  $("#prompt-input").addEventListener("compositionstart", () => { commandMenuState.composing = true; closeCommandMenu(); });
  $("#prompt-input").addEventListener("compositionend", () => { commandMenuState.composing = false; trackCommandInput(); });
  $("#prompt-input").addEventListener("click", () => { if (commandMenuState.source !== "launcher") trackCommandInput(); });
  $("#prompt-input").addEventListener("keyup", (event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) trackCommandInput(); });
  document.addEventListener("pointerdown", (event) => { if (!event.target.closest(".composer")) closeCommandMenu(); });
  window.addEventListener("resize", () => { if (commandMenuState.open) renderCommandMenu(); });
  $("#permission-trigger").addEventListener("click", togglePermissionMenu);
  $$("#permission-menu [data-policy]").forEach((button) => button.addEventListener("click", () => setPermissionPolicy(button.dataset.policy)));
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".permission-picker")) closePermissionMenu();
    if (!event.target.closest(".workspace-menu") && !event.target.closest(".workspace-more") && !event.target.closest(".session-menu") && !event.target.closest(".session-delete")) closeWorkspaceMenus();
    if (!event.target.closest(".view-options-wrap")) closeViewOptions();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (event.isComposing || commandMenuState.composing) return;
      closeCommandMenu();
      closePermissionMenu();
      closeWorkspaceMenus();
      closeViewOptions();
      if (!$("#workspace-dialog").hidden) closeWorkspaceDialog(false);
    }
  });
  $("#allow-approval").addEventListener("click", () => resolveApproval(true));
  $("#deny-approval").addEventListener("click", () => resolveApproval(false));
  $("#stop-agent").addEventListener("click", stopAgent);
  translationUI.bind();
}

async function boot() {
  bindEvents();
  startBrowserPresence();
  setSidebarCollapsed(storedJson("nagi.sidebar.collapsed", false) === true);
  updateViewOptions();
  try {
    const config = await api("/api/config");
    state.csrf = config.csrf_token;
    state.model = config.model;
    $("#model-name").textContent = config.model;
    $("#translation-model").textContent = config.model;
    if (config.translation_rag) {
      const rag = config.translation_rag;
      $("#translation-rag-status").textContent = rag.mode === 'hybrid'
        ? `自动原文上下文：BM25 + ${rag.embedding}${rag.reranker ? ' + 本地重排' : ''}。无需导入参考文件；仅相关片段发送给翻译模型。`
        : '自动原文上下文：BM25（本地语义模型未安装）。无需导入参考文件；仅相关片段发送给翻译模型。';
    }
    setConnection(true, "本机服务已连接");
    translationUI.init(config);
    await loadWorkspaces({ preserve: false });
    const agentJobs = await api("/api/agent/jobs");
    if (agentJobs.latest_job) {
      state.workspacePath = agentJobs.latest_job.workspace_path;
      state.sessionId = agentJobs.latest_job.session_id;
      state.terminalHandled = false;
      renderAgentJob(agentJobs.latest_job);
      if (["queued", "running", "awaiting_approval"].includes(agentJobs.latest_job.status)) {
        state.agentPoll = setInterval(pollAgentJob, 850);
      }
      renderWorkspaces();
    }
  } catch (error) {
    setConnection(false, "本机服务连接失败");
    $("#run-metrics").textContent = error.message;
  }
}

const modelSettings = { document: null, editing: null, busy: false, generation: 0 };

function settingsFeedback(message = "", error = false) {
  const node = $("#settings-feedback");
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle("error", error);
}

function settingsPage(page) {
  if (modelSettings.busy) return;
  $("#settings-general").hidden = page !== "general";
  $("#settings-models").hidden = page !== "models";
  for (const name of ["general", "models"]) {
    const button = $(`#settings-${name}-tab`);
    if (page === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}

function providerPanel(panel) {
  for (const name of ["overview", "picker", "editor"]) $(name === "overview" ? "#provider-overview" : `#provider-${name}`).hidden = name !== panel;
  $("#provider-key").value = "";
}

function syncModelSettings(document) {
  modelSettings.document = document;
  setConnection(true, "本机服务已连接");
  const active = document.providers.find((item) => item.id === document.active_provider);
  state.model = active?.model || "未选择模型";
  $("#model-name").textContent = state.model;
  $("#translation-model").textContent = state.model;
  $("#settings-default-model").textContent = active ? `${active.name} · ${active.model}` : "未选择模型";
  $("#settings-key-storage").textContent = document.credential_storage;
  renderProviderList();
}

async function openModelSettings() {
  if (modelSettings.busy) return;
  closeCommandMenu();
  settingsPage("models");
  providerPanel("overview");
  modelSettings.document = null;
  $("#provider-list").replaceChildren();
  $("#provider-add").disabled = $("#provider-add-custom").disabled = true;
  if (!$("#settings-dialog").open) $("#settings-dialog").showModal();
  settingsFeedback("正在读取本机模型配置…");
  const generation = ++modelSettings.generation;
  try {
    const document = await api("/api/settings/models");
    if (generation !== modelSettings.generation || !$("#settings-dialog").open) return;
    syncModelSettings(document);
    $("#provider-add").disabled = $("#provider-add-custom").disabled = false;
    settingsFeedback();
  } catch (error) { if (generation === modelSettings.generation) settingsFeedback(error.message, true); }
}

function closeModelSettings() {
  if (modelSettings.busy) return;
  modelSettings.generation += 1;
  $("#provider-key").value = "";
  modelSettings.editing = null;
  $("#settings-dialog").close();
}

function renderProviderList() {
  const root = $("#provider-list");
  root.replaceChildren();
  const config = modelSettings.document;
  if (!config.providers.length) {
    const empty = document.createElement("p");
    empty.className = "settings-help";
    empty.textContent = "还没有提供方，请添加一个以开始使用。";
    root.append(empty);
  }
  for (const provider of config.providers) {
    const active = provider.id === config.active_provider;
    const card = document.createElement("article");
    card.className = "provider-card";
    const heading = document.createElement("div");
    heading.className = "provider-card-heading";
    const title = document.createElement("strong");
    title.textContent = provider.name;
    const dot = document.createElement("span");
    dot.className = `provider-dot${provider.api_key_configured ? " configured" : ""}`;
    dot.title = provider.api_key_configured ? "已配置密钥" : "未配置可用密钥";
    dot.setAttribute("aria-label", dot.title);
    heading.append(title); heading.append(dot);
    if (active) { const badge = document.createElement("span"); badge.className = "provider-active"; badge.textContent = "当前使用"; heading.append(badge); }
    const edit = document.createElement("button");
    edit.type = "button"; edit.className = "provider-edit"; edit.textContent = "编辑";
    edit.setAttribute("aria-label", `编辑 ${provider.name}`);
    edit.addEventListener("click", () => editProvider(provider));
    heading.append(edit); card.append(heading);
    const controls = document.createElement("div");
    controls.className = "provider-card-controls";
    const select = document.createElement("select");
    select.setAttribute("aria-label", `${provider.name} 模型`);
    for (const name of provider.models) { const option = document.createElement("option"); option.value = name; option.textContent = name; select.append(option); }
    select.value = provider.model;
    const use = document.createElement("button");
    use.type = "button"; use.className = "provider-use";
    const update = () => { use.textContent = active && select.value === provider.model ? "使用中" : "使用此模型"; use.disabled = !provider.api_key_configured || (active && select.value === provider.model); };
    select.addEventListener("change", update); update();
    use.addEventListener("click", () => modelSettingsAction("activate", { id: provider.id, model: select.value }, "已切换模型，对之后启动的任务生效。"));
    controls.append(select); controls.append(use); card.append(controls);
    if (!provider.api_key_configured) { const help = document.createElement("p"); help.className = "settings-help"; help.textContent = provider.credential_error || "尚未配置 API 密钥，点击编辑添加。"; card.append(help); }
    root.append(card);
  }
}

function showProviderPicker() {
  if (modelSettings.busy || !modelSettings.document) return;
  settingsFeedback(); providerPanel("picker");
  const root = $("#provider-presets"); root.replaceChildren();
  for (const preset of modelSettings.document.presets) {
    const button = document.createElement("button"); button.type = "button";
    const title = document.createElement("strong"); title.textContent = preset.name;
    const detail = document.createElement("span"); detail.textContent = preset.models.join(" · ");
    button.append(title); button.append(detail);
    button.addEventListener("click", () => editProvider({ ...preset, model: preset.models[0], protocol: "openai-chat", json_output: true }));
    root.append(button);
  }
}

function editProvider(provider) {
  if (modelSettings.busy || !modelSettings.document) return;
  modelSettings.editing = { id: provider.id, kind: provider.kind };
  settingsFeedback(); providerPanel("editor");
  $("#provider-editor-title").textContent = provider.id ? "编辑提供方" : "添加提供方";
  $("#provider-name").value = provider.name || "自定义提供方";
  $("#provider-base").value = provider.base_url || "";
  $("#provider-model").value = provider.model || "";
  $("#provider-protocol").value = provider.protocol || "openai-chat";
  $("#provider-json").checked = provider.json_output === true;
  $("#provider-key").placeholder = provider.api_key_configured ? "已配置，留空保留现有密钥" : "填写 API 密钥";
  $("#provider-key-help").textContent = provider.api_key_configured ? "现有密钥已配置，留空可保留；输入新密钥后保存即可替换。" : "密钥不会回显，也不会保存在浏览器中。";
  $("#provider-delete").hidden = !provider.id;
  const preset = modelSettings.document.presets.find((item) => item.kind === provider.kind);
  const hints = $("#provider-model-hints"); hints.replaceChildren();
  for (const model of preset?.models || []) { const option = document.createElement("option"); option.value = model; hints.append(option); }
  $("#provider-docs").hidden = !preset;
  if (preset) $("#provider-docs").href = preset.docs_url;
  $("#provider-name").focus();
}

function providerFormValues() {
  return { ...modelSettings.editing, name: $("#provider-name").value.trim(), base_url: $("#provider-base").value.trim(), model: $("#provider-model").value.trim(), protocol: $("#provider-protocol").value, api_key: $("#provider-key").value, json_output: $("#provider-json").checked };
}

async function modelSettingsAction(action, body, success) {
  if (modelSettings.busy || !modelSettings.document) return;
  modelSettings.busy = true;
  const controls = $$("#settings-dialog button, #settings-dialog input, #settings-dialog select");
  const previous = controls.map((node) => node.disabled);
  controls.forEach((node) => { node.disabled = true; });
  $("#settings-dialog").setAttribute("aria-busy", "true");
  settingsFeedback(action === "test" ? "正在测试连接，请稍候…" : "正在保存设置…");
  try {
    const result = await api(`/api/settings/models/${action}`, { method: "POST", body: { ...body, expected_revision: modelSettings.document.revision } });
    if (action === "test") settingsFeedback(result.message);
    else { providerPanel("overview"); syncModelSettings(result); modelSettings.editing = null; settingsFeedback(success); }
  } catch (error) { settingsFeedback(error.message, true); }
  finally {
    controls.forEach((node, index) => { node.disabled = previous[index]; });
    $("#settings-dialog").removeAttribute("aria-busy");
    modelSettings.busy = false;
  }
}

function saveProvider(activate) {
  if (!$("#provider-editor").reportValidity()) return;
  return modelSettingsAction("save", { provider: providerFormValues(), activate }, activate ? "已保存并切换模型，对之后启动的任务生效。" : "提供方已保存。");
}

function bindModelSettings() {
  $("#settings-button").addEventListener("click", openModelSettings);
  $("#model-name").addEventListener("click", openModelSettings);
  $("#settings-close").addEventListener("click", closeModelSettings);
  $("#settings-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeModelSettings(); });
  $("#settings-dialog").addEventListener("close", () => { $("#provider-key").value = ""; });
  $("#settings-general-tab").addEventListener("click", () => settingsPage("general"));
  $("#settings-models-tab").addEventListener("click", () => settingsPage("models"));
  $("#provider-add").addEventListener("click", showProviderPicker);
  $("#provider-add-custom").addEventListener("click", () => editProvider({ kind: "custom" }));
  for (const id of ["#provider-picker-back", "#provider-editor-back"]) $(id).addEventListener("click", () => { if (!modelSettings.busy) { providerPanel("overview"); settingsFeedback(); } });
  $("#provider-editor").addEventListener("submit", (event) => { event.preventDefault(); saveProvider(false); });
  $("#provider-save-use").addEventListener("click", () => saveProvider(true));
  $("#provider-test").addEventListener("click", () => { if ($("#provider-editor").reportValidity()) modelSettingsAction("test", { provider: providerFormValues() }); });
  $("#provider-delete").addEventListener("click", () => {
    const id = modelSettings.editing?.id;
    if (!id || modelSettings.busy) return;
    const active = id === modelSettings.document.active_provider;
    if (window.confirm(`删除此提供方配置？不会删除对话、工作区或原有环境配置。${active ? "当前模型将取消选择，需要重新选择提供方。" : ""}`)) modelSettingsAction("remove", { id }, "已删除提供方配置。");
  });
}

boot();
