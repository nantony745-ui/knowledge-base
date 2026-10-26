(() => {
  'use strict';
  const status = document.querySelector('#skills-status');
  const list = document.querySelector('#skills-list');
  const refresh = document.querySelector('#skills-refresh');
  const pending = new Set();
  let refreshing = false;
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let skills = [];
  async function api(path, method = 'GET', body) {
    const response = await fetch(path, {method, credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
    if (response.status === 401) { location.href = '/'; throw new Error('请先登录'); }
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.detail?.message || '操作失败');
    }
    return response.json();
  }
  function render() {
    document.querySelector('#skills-count').textContent = String(skills.length);
    list.innerHTML = skills.length ? skills.map(skill => `<article class="skill-card" data-name="${escape(skill.name)}">
      <div class="skill-card-head"><div><h2>${escape(skill.name)}</h2><p>${escape(skill.description)}</p></div>
      <div class="skill-control"><span class="${skill.is_enabled ? 'enabled' : ''}">${skill.is_enabled ? '已启用' : '已停用'}</span><button class="skill-toggle" type="button" role="switch" aria-checked="${skill.is_enabled}" aria-label="启用 ${escape(skill.name)}" data-toggle ${refreshing || pending.has(skill.name) ? 'disabled' : ''}></button></div></div>
      <details class="skill-resources"><summary>资源清单 · ${skill.resources.length}</summary><ul>${skill.resources.map(resource => `<li><code>${escape(resource.path)}</code><small>${resource.type === 'script' ? '脚本' : '参考资料'}</small></li>`).join('') || '<li>暂无资源</li>'}</ul></details></article>`).join('') : '<div class="toolbox-empty"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8h16v12H4zM8 4h8M8 12h8"/></svg><p>暂无技能</p><p>当前没有已登记的技能。</p></div>';
  }
  list.addEventListener('click', async event => {
    const button = event.target.closest('[data-toggle]');
    if (!button) return;
    const name = button.closest('[data-name]').dataset.name;
    if (refreshing || pending.has(name)) return;
    const skill = skills.find(item => item.name === name);
    pending.add(name); button.disabled = true; refresh.disabled = true; status.textContent = '正在保存…';
    try {
      const updated = await api('/api/skills/' + encodeURIComponent(name), 'PATCH', {is_enabled:!skill.is_enabled});
      skills = skills.map(item => item.name === name ? updated : item);
      status.textContent = '已保存，对新启动的智能推理生效';
    } catch (error) { status.textContent = error.message; }
    finally { pending.delete(name); button.disabled = false; refresh.disabled = pending.size > 0; render(); }
  });
  refresh.addEventListener('click', async () => {
    if (refreshing || pending.size) return;
    refreshing = true; refresh.disabled = true; render(); status.textContent = '正在刷新目录…';
    try {
      skills = await api('/api/skills/refresh', 'POST');
      status.textContent = '目录已刷新，对新启动的智能推理生效';
    } catch (error) { status.textContent = error.message; }
    finally { refreshing = false; refresh.disabled = false; render(); }
  });
  api('/api/auth/me').then(async () => {
    document.querySelector('#skills-content').hidden = false;
    status.textContent = '正在加载技能…';
    const loadSkills = api('/api/skills').then(rows => { skills = rows; render(); });
    const loadMcp = api('/api/mcp-services').then(rows => {
      document.querySelector('#mcp-count').textContent = String(rows.length);
      return '';
    }).catch(error => 'MCP服务数量加载失败：' + error.message);
    const [, countError] = await Promise.all([loadSkills, loadMcp]);
    status.textContent = countError;
  }).catch(error => { status.textContent = error.message; });
})();
