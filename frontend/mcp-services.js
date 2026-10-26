(() => {
  'use strict';
  const status = document.querySelector('#mcp-status');
  const list = document.querySelector('#mcp-list');
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let services = [];
  function configBody(form) {
    const data = new FormData(form);
    const body = {name:data.get('name'), endpoint:data.get('endpoint'), requires_auth:data.get('requires_auth') === 'on'};
    if (body.requires_auth && data.get('token')) body.token = data.get('token');
    form.querySelector('[name=token]').value = '';
    return body;
  }
  function authChanged(event) {
    if (event.target.name !== 'requires_auth') return;
    const form = event.target.form;
    const input = form.querySelector('[name=token]');
    form.querySelector('[data-token]').hidden = !event.target.checked;
    input.disabled = !event.target.checked;
    input.required = event.target.checked && !form.hasAttribute('data-has-key');
    if (!event.target.checked) input.value = '';
  }
  async function api(path, method = 'GET', body) {
    const response = await fetch(path, {method, credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
    if (response.status === 401) { location.href = '/'; throw new Error('请先登录'); }
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.detail?.message || (Array.isArray(data.detail) ? '服务配置字段无效' : '操作失败'));
    }
    return response.status === 204 ? null : response.json();
  }
  function render() {
    list.innerHTML = services.length ? services.map(service => `<article class="mcp-card" data-id="${escape(service.id)}">
      <form data-edit ${service.auth_env_key ? 'data-has-key' : ''}><label>名称<input name="name" value="${escape(service.name)}" required maxlength="128"></label>
      <label>地址<input name="endpoint" type="url" value="${escape(service.endpoint)}" required maxlength="2048"></label>
      <label><input name="requires_auth" type="checkbox" ${service.auth_env_key ? 'checked' : ''}> 需要认证</label>
      <label data-token ${service.auth_env_key ? '' : 'hidden'}>API Key / Bearer Token<input name="token" type="password" ${service.auth_env_key ? '' : 'disabled'} maxlength="4096" autocomplete="new-password" placeholder="${service.auth_env_key ? '已配置；留空保留，填写新Key即可更换' : '仅填写Key，不加Bearer前缀'}"></label>
      <button class="button" type="submit">保存配置</button></form>
      <div class="mcp-actions"><button class="button" data-action="test">测试连接</button><button class="button" data-action="refresh-tools">刷新目录</button><button class="button" data-action="toggle">${service.is_enabled ? '禁用服务' : '启用服务'}</button><button class="button" data-action="delete">删除服务</button></div>
      <form data-whitelist><h2>只读白名单</h2>${service.tools_catalog.map(tool => `<label class="mcp-tool"><input type="checkbox" name="tool" value="${escape(tool.name)}" ${service.enabled_tools.includes(tool.name) ? 'checked' : ''}> ${escape(tool.name)}<small>${escape(tool.description)}</small><small>远端只读提示：${tool.readOnlyHint === true ? '是' : tool.readOnlyHint === false ? '否' : '未提供'}；勾选表示管理员已确认只读。</small><details><summary>参数schema</summary><pre>${escape(JSON.stringify(tool.inputSchema, null, 2))}</pre></details></label>`).join('') || '<p>目录为空</p>'}<button class="button primary" type="submit">保存只读白名单</button></form></article>`).join('') : '<p>尚未配置MCP服务。</p>';
  }
  async function reload() {
    services = await api('/api/mcp-services'); render();
    document.querySelector('#mcp-count').textContent = String(services.length);
  }
  async function action(button, operation) {
    button.disabled = true;
    status.textContent = '处理中…';
    try { const message = await operation(); await reload(); status.textContent = message || '已保存'; }
    catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  }
  document.querySelector('#mcp-create').addEventListener('change', authChanged);
  list.addEventListener('change', authChanged);
  document.querySelector('#mcp-create').addEventListener('submit', event => {
    event.preventDefault(); const form = event.target; const body = configBody(form);
    action(form.querySelector('button'), async () => {
      await api('/api/mcp-services', 'POST', {...body, enabled_tools:[]});
      form.reset(); form.querySelector('[data-token]').hidden = true;
      form.querySelector('[name=token]').disabled = true;
      return '连接成功，服务已添加；请选择只读工具并保存白名单';
    });
  });
  list.addEventListener('submit', event => {
    event.preventDefault(); const form = event.target; const id = form.closest('[data-id]').dataset.id;
    const data = new FormData(form);
    const body = form.hasAttribute('data-whitelist') ? {enabled_tools:data.getAll('tool')} : configBody(form);
    action(form.querySelector('button[type=submit]'), () => api('/api/mcp-services/' + id, 'PATCH', body).then(() => '已保存；连接配置变化后需重新选择白名单'));
  });
  list.addEventListener('click', event => {
    const button = event.target.closest('[data-action]'); if (!button) return;
    const id = button.closest('[data-id]').dataset.id; const service = services.find(row => row.id === id);
    const kind = button.dataset.action;
    if (kind === 'delete' && !confirm('删除此MCP服务配置？')) return;
    action(button, async () => {
      const path = '/api/mcp-services/' + id;
      if (kind === 'toggle') await api(path, 'PATCH', {is_enabled:!service.is_enabled});
      else if (kind === 'delete') await api(path, 'DELETE');
      else { const result = await api(path + '/' + kind, 'POST'); return kind === 'test' ? `连接成功，发现${result.tool_count}个工具` : '目录已刷新；新增或schema变化的工具需重新批准'; }
      return kind === 'delete' ? '已删除' : '服务状态已更新';
    });
  });
  api('/api/auth/me').then(async () => {
    document.querySelector('#mcp-content').hidden = false;
    const loadSkills = api('/api/skills').then(rows => {
      document.querySelector('#skills-count').textContent = String(rows.length);
      return '';
    }).catch(error => '技能数量加载失败：' + error.message);
    const [, countError] = await Promise.all([reload(), loadSkills]);
    status.textContent = countError;
  }).catch(error => { status.textContent = error.message; });
})();
