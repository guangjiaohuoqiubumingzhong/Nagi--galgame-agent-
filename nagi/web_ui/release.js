"use strict";
(async () => {
  try {
    const config = await fetch('/api/config').then(r => r.json());
    const release = config.release;
    if (!release) return;
    const button = document.createElement('button');
    button.className = 'settings-button';
    button.textContent = `Nagi ${release.version} · 使用说明`;
    document.querySelector('.sidebar-footer').append(button);
    const dialog = document.createElement('dialog');
    dialog.className = 'release-dialog';
    dialog.innerHTML = `<h2>欢迎使用 Nagi</h2>
      <p>程序不附带账户、密钥、游戏或检索模型。打开程序和保存配置不会调用模型。</p>
      <ol><li>在「设置 → 模型」填写自己的 API 服务地址、模型 ID 和密钥。</li>
      <li>确认服务价格、账户额度与数据发送范围。测试连接、Agent 对话和翻译可能产生费用。</li>
      <li>选择有权处理的游戏；可先提取文本，再确认费用并选择开场前 50 条、全文翻译或查缺补漏。</li>
      <li>游戏翻译现支持 QLIE、YU-RIS 479、KiriKiri/KAG、Ren'Py 与 TyranoScript；部署始终生成独立可玩副本。</li></ol>
      <p>部署前请在翻译页选择本机 Locale Emulator。<a href="https://github.com/xupefei/Locale-Emulator/releases" target="_blank" rel="noreferrer">官方获取入口</a></p>
      <p>默认 BM25。语义检索需自行安装 rag 依赖并主动运行模型下载命令，翻译过程中不会下载。</p>
      <p id="release-features"></p><p id="release-recovery" role="alert"></p>
      <details><summary>数据与日志位置</summary><pre id="release-paths"></pre><p>移动旧任务后若无法恢复，请保留原文件并按升级说明恢复原路径；不会自动重译或覆盖游戏。</p></details>
      <label><input id="release-consent" type="checkbox"> 我了解需使用自己的账户，并自行确认 API 费用和游戏处理权限</label>
      <p id="release-error" role="alert"></p><div class="release-actions"><button id="release-close" class="primary-button">开始使用</button><button id="release-stop" class="secondary-button">退出 Nagi 服务</button></div>`;
    document.body.append(dialog);
    const f = release.features;
    dialog.querySelector('#release-features').textContent = `可用组件：基础 Agent / 翻译 / BM25；MCP ${f.mcp ? '已安装（需自行配置服务）' : '未安装'}；Frida 部署 ${f.deployment ? '已安装（还需 LE）' : '不可用'}；语义检索运行库 ${f.semantic_runtime ? '已安装（还需模型）' : '未安装'}。`;
    dialog.querySelector('#release-paths').textContent = Object.entries(release.paths).map(([k,v]) => `${k}: ${v}`).join('\n');
    dialog.querySelector('#release-recovery').textContent = config.recovery_warning || '';
    dialog.querySelector('#release-consent').checked = !release.first_run;
    const post = async (url, body) => {
      const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...body, csrf_token:config.csrf_token})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '操作失败');
    };
    dialog.querySelector('#release-close').onclick = async () => {
      try {
        await post('/api/settings/onboarding', {acknowledged:dialog.querySelector('#release-consent').checked});
        dialog.close();
      } catch(e) { dialog.querySelector('#release-error').textContent = e.message; }
    };
    dialog.querySelector('#release-stop').onclick = async () => {
      if (!confirm('退出 Nagi 本机服务？关闭网页本身不会停止服务。')) return;
      try {
        await post('/api/shutdown', {});
        document.body.textContent = 'Nagi 服务已退出，可以关闭此网页。';
      } catch(e) { dialog.querySelector('#release-error').textContent = e.message; }
    };
    button.onclick = () => dialog.showModal();
    if (release.first_run || config.recovery_warning) dialog.showModal();
  } catch (error) { console.warn('Nagi 使用说明暂时不可用', error); }
})();
