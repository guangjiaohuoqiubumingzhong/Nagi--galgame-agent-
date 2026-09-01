// Stage controls intentionally stay clickable before prerequisites are met:
// explain the missing step instead of silently disabling the action.
const translationUI = {
  workflow: null, pending: false, timer: null, polling: false, locale: null,
  names: { extract: '提取资源', translate: '开始翻译', deploy: '嵌入游戏' },

  error(stage, message = '') {
    const node = document.querySelector(`#${stage}-error`);
    node.textContent = message;
    node.hidden = !message;
  },

  bind() {
    document.querySelector('#browse-button').addEventListener('click', () => this.browse('game'));
    document.querySelector('#browse-storage-button').addEventListener('click', () => this.browse('storage'));
    document.querySelector('#browse-locale-button').addEventListener('click', () => this.browseLocale());
    document.querySelector('#save-locale-button').addEventListener('click', () => this.saveLocale());
    for (const name of ['game', 'storage']) {
      document.querySelector(`#${name}-path`).addEventListener('input', () => this.invalidate());
    }
    document.querySelectorAll('input[name="mode"]').forEach(node => {
      node.addEventListener('change', () => { this.error('translate'); this.error('deploy'); this.render(); });
    });
    document.querySelector('#extract-button').addEventListener('click', () => this.run('extract'));
    document.querySelector('#reuse-trial-button').addEventListener('click', () => this.run('extract', true));
    document.querySelector('#copy-launcher-button').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(this.workflow.launcher_path); document.querySelector('#copy-launcher-button').textContent = '路径已复制'; }
      catch { this.error('deploy', '无法复制，请从上方结果中手动复制启动文件路径。'); }
    });
    document.querySelector('#start-button').addEventListener('click', () => this.run('translate'));
    document.querySelector('#deploy-button').addEventListener('click', () => this.run('deploy'));
    for (const id of ['extract-stop', 'stop-button']) document.querySelector(`#${id}`).addEventListener('click', () => this.cancel());
  },

  init(config) {
    this.renderLocale(config.locale_emulator);
    document.querySelector('#storage-path').value = config.output_root || '';
    if (config.latest_workflow) {
      document.querySelector('#game-path').value = config.latest_workflow.game_dir;
      document.querySelector('#storage-path').value = config.latest_workflow.storage_dir;
      const savedMode = config.latest_workflow.mode === 'pilot' ? 'partial' : config.latest_workflow.mode || 'full';
      const mode = document.querySelector(`input[name="mode"][value="${savedMode}"]`);
      if (mode) mode.checked = true;
      this.render(config.latest_workflow);
    } else this.render();
  },

  renderLocale(settings) {
    this.locale = settings || null;
    document.querySelector('#locale-path').value = settings?.directory || '';
  },

  async browseLocale() {
    if (this.pending || this.workflow?.active_stage) return;
    this.pending = true; this.render(); this.error('locale');
    try {
      const result = await api('/api/select-directory', { method: 'POST', body: { purpose: 'locale' } });
      if (result.path) {
        document.querySelector('#locale-path').value = result.path;
      }
    } catch (error) { this.error('locale', error.message); }
    finally { this.pending = false; this.render(); }
  },

  async saveLocale() {
    if (this.pending || this.workflow?.active_stage) return;
    this.pending = true; this.render(); this.error('locale');
    try {
      this.renderLocale(await api('/api/settings/locale-emulator', {
        method: 'POST', body: { directory: document.querySelector('#locale-path').value.trim() },
      }));
    } catch (error) { this.error('locale', error.message); }
    finally { this.pending = false; this.render(); }
  },

  invalidate() {
    if (this.workflow?.active_stage || this.pending) return;
    this.workflow = null;
    clearInterval(this.timer); this.timer = null;
    for (const stage of Object.keys(this.names)) this.error(stage);
    this.render();
  },

  async browse(purpose) {
    if (this.pending || this.workflow?.active_stage) return;
    this.pending = true; this.render(); this.error('extract');
    try {
      const result = await api('/api/select-directory', { method: 'POST', body: { purpose } });
      if (result.path) {
        document.querySelector(`#${purpose}-path`).value = result.path;
        this.pending = false; this.invalidate();
      }
    } catch (error) { this.error('extract', error.message); }
    finally { this.pending = false; this.render(); }
  },

  prerequisite(stage) {
    const work = this.workflow;
    if (this.pending || work?.active_stage) return '当前步骤正在执行，请等待完成或安全停止。';
    if (stage !== 'extract' && work?.stages.extract.status !== 'completed') return '请先完成第一步：提取资源，再执行后续操作。';
    if (stage === 'translate' && work?.source_kind && !['yuris-479', 'qlie', 'kirikiri', 'renpy', 'tyranoscript'].includes(work.source_kind)) return '这是旧开场试译快照；如需全文，请重新执行第一步提取资源。';
    if (stage === 'deploy' && work?.stages.translate.status !== 'completed') return '请先完成第二步：开始翻译。';
    if (stage === 'deploy' && work?.stages.deploy?.status !== 'completed' && work?.source_kind === 'yuris-479'
      && (!this.locale?.valid || document.querySelector('#locale-path').value.trim() !== this.locale.directory)) return '请先选择本地 Locale Emulator 目录，并点击“检查并保存目录”。';
    if (stage === 'deploy' && (work.mode === 'pilot' ? 'partial' : work.mode) !== document.querySelector('input[name="mode"]:checked').value) return '翻译范围已经改变，请先按新范围完成第二步，再生成启动文件。';
    if (stage === 'extract' && !document.querySelector('#game-path').value.trim()) return '请选择原始游戏目录。';
    if (stage === 'extract' && !document.querySelector('#storage-path').value.trim()) return '请选择资源保存目录。';
    if (stage === 'translate' && !document.querySelector('#cost-confirm').checked) return '请先勾选并确认翻译 API 费用。';
    return '';
  },

  async run(stage, reuseTrial = false) {
    this.error(stage);
    const problem = this.prerequisite(stage);
    if (problem) { this.error(stage, problem); return; }
    const mode = document.querySelector('input[name="mode"]:checked').value;
    if (stage === 'translate' && !window.confirm('请保持网络通畅，调用 API 会产生费用。确认开始？')) return;
    this.pending = true; this.render();
    try {
      const work = this.workflow;
      const newExtraction = stage === 'extract' && (reuseTrial || !work || work.stages.extract.status === 'completed');
      const path = newExtraction ? '/api/translation-workflows' : `/api/translation-workflows/${work.workflow_id}/${stage}`;
      const body = newExtraction
        ? { game_dir: document.querySelector('#game-path').value.trim(), storage_dir: document.querySelector('#storage-path').value.trim(), reuse_trial: reuseTrial }
        : { mode, confirmed: stage === 'translate' && document.querySelector('#cost-confirm').checked };
      this.render(await api(path, { method: 'POST', body }));
    } catch (error) { this.error(stage, error.message); }
    finally { this.pending = false; this.render(); }
  },

  async cancel() {
    if (!this.workflow?.active_stage || this.pending) return;
    const stage = this.workflow.active_stage;
    this.pending = true; this.render();
    try { this.render(await api(`/api/translation-workflows/${this.workflow.workflow_id}/cancel`, { method: 'POST', body: {} })); }
    catch (error) { this.error(stage, error.message); }
    finally { this.pending = false; this.render(); }
  },

  async poll() {
    if (!this.workflow?.active_stage || this.polling) return;
    const identifier = this.workflow.workflow_id;
    this.polling = true;
    try {
      const work = await api(`/api/translation-workflows/${identifier}`);
      if (this.workflow?.workflow_id === identifier) this.render(work);
    } catch (error) { this.error(this.workflow?.active_stage || 'extract', `无法更新进度：${error.message}`); }
    finally { this.polling = false; }
  },

  render(work) {
    const received = Boolean(work);
    if (work) this.workflow = work;
    work = this.workflow;
    const active = work?.active_stage;
    const busy = Boolean(active || this.pending);
    const labels = { pending: '待开始', running: '执行中', completed: '已完成', failed: '未完成', cancelled: '已停止', review_required: '待审核' };
    const initial = { extract: '选择两个目录后即可提取', translate: '请先完成资源提取', deploy: '请先完成翻译' };
    for (const stage of Object.keys(this.names)) {
      const info = work?.stages[stage] || { status: 'pending', progress: 0, message: initial[stage] };
      const percent = Math.round(Math.max(0, Math.min(1, info.progress)) * 100);
      const status = document.querySelector(`#${stage}-status`);
      const predecessor = stage === 'translate' ? 'extract' : 'translate';
      const waiting = stage !== 'extract' && work?.stages[predecessor].status !== 'completed';
      status.textContent = info.status === 'pending' && waiting
        ? (stage === 'translate' ? '等待提取' : '等待翻译') : labels[info.status];
      status.className = `status-pill ${info.status}`;
      document.querySelector(`#${stage}-card`).classList.toggle('stage-active', active === stage);
      document.querySelector(`#${stage}-message`).textContent = info.message;
      document.querySelector(`#${stage}-percent`).textContent = `${percent}%`;
      document.querySelector(`#${stage}-progress`).setAttribute('aria-valuenow', String(percent));
      document.querySelector(`#${stage}-progress span`).style.width = `${percent}%`;
      if (received && info.error) this.error(stage, info.error);
      else if (received) this.error(stage);
    }
    for (const id of ['extract-button', 'reuse-trial-button', 'start-button', 'deploy-button', 'browse-button', 'browse-storage-button', 'game-path', 'storage-path', 'cost-confirm']) document.querySelector(`#${id}`).disabled = busy;
    for (const id of ['locale-path', 'browse-locale-button', 'save-locale-button']) document.querySelector(`#${id}`).disabled = busy;
    document.querySelectorAll('input[name="mode"]').forEach(node => {
      node.disabled = busy || Boolean(work?.source_kind && !['yuris-479', 'qlie', 'kirikiri', 'renpy', 'tyranoscript'].includes(work.source_kind));
      if (received && work.mode) node.checked = node.value === (work.mode === 'pilot' ? 'partial' : work.mode);
    });
    const nativeTextEngine = ['kirikiri', 'renpy', 'tyranoscript'].includes(work?.source_kind);
    const fullYuris = work?.source_kind === 'yuris-479';
    if (work?.deployment_support) {
      const support = work.deployment_support;
      document.querySelector('#deploy-button').disabled = busy || !support.supported;
      if (!support.supported) {
        document.querySelector('#deploy-status').textContent = '此版本暂不支持';
        document.querySelector('#deploy-message').textContent = support.message;
      } else if (work.stages.deploy.status === 'pending') {
        document.querySelector('#deploy-message').textContent = support.message;
      }
    }
    document.querySelector('#translation-rag-details').hidden = fullYuris || nativeTextEngine;
    document.querySelector('#locale-settings-fields').hidden = nativeTextEngine;
    const selectedMode = document.querySelector('input[name="mode"]:checked').value;
    const scopeChanged = work?.mode && (work.mode === 'pilot' ? 'partial' : work.mode) !== selectedMode;
    document.querySelector('#translation-description').textContent = selectedMode === 'partial'
      ? '仅翻译开场剧情前 50 条内容'
      : '全部已提取文本分批提交所选 API。';
    if (scopeChanged && !busy) {
      document.querySelector('#translate-status').textContent = '待开始';
      document.querySelector('#translate-status').className = 'status-pill pending';
      document.querySelector('#translate-message').textContent = '已切换翻译范围，点击“开始翻译”执行；原有译文保持不变。';
      document.querySelector('#translate-percent').textContent = '0%';
      document.querySelector('#translate-progress').setAttribute('aria-valuenow', '0');
      document.querySelector('#translate-progress span').style.width = '0%';
    }
    document.querySelector('#copy-launcher-button').hidden = !work?.launcher_path;
    if (received) document.querySelector('#copy-launcher-button').textContent = '复制启动文件路径';
    document.querySelector('#extract-stop').hidden = active !== 'extract';
    document.querySelector('#stop-button').hidden = active !== 'translate';
    document.querySelector('#extract-stop').disabled = this.pending;
    document.querySelector('#stop-button').disabled = this.pending;
    document.querySelector('#extract-button strong').textContent = work?.stages.extract.status === 'completed' ? '重新提取（新建任务）' : work && work.stages.extract.status !== 'pending' && !active ? '继续提取' : '提取资源';
    document.querySelector('#start-button strong').textContent = !scopeChanged && ['failed', 'cancelled'].includes(work?.stages.translate.status) ? '继续翻译' : '开始翻译';
    const overall = document.querySelector('#game-status');
    overall.textContent = active ? `${this.names[active]}中` : work?.stages.deploy.status === 'completed' ? '汉化入口已生成' : work?.stages.translate.status === 'completed' ? '译文已生成' : work?.stages.extract.status === 'completed' ? '资源已就绪' : '准备开始';
    overall.className = `status-pill ${active ? 'running' : ''}`;
    const outputs = { extract: work?.export_dir, translate: work?.translated_scripts_dir, deploy: work?.launcher_path };
    for (const [stage, path] of Object.entries(outputs)) {
      const node = document.querySelector(`#${stage}-result`);
      node.hidden = !path;
      node.textContent = path ? `${stage === 'deploy' ? '汉化启动文件' : '结果已保存至'}：${path}` : '';
    }
    document.querySelector('#workflow-log-summary').textContent = work ? `任务 ${work.workflow_id} · ${work.output_root}` : '尚未开始 · 原始游戏保持不变';
    const log = document.querySelector('#event-log');
    log.innerHTML = work?.job?.events?.length
      ? work.job.events.map(event => `<p><time>${escapeHtml(event.time)}</time><span>${escapeHtml(event.message)}</span></p>`).join('')
      : '<p>执行记录会显示在这里。</p>';
    if (active && !this.timer) this.timer = setInterval(() => this.poll(), 900);
    if (!active && this.timer) { clearInterval(this.timer); this.timer = null; }
  },
};
