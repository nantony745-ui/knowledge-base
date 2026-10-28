(() => {
  'use strict';
  const content = document.querySelector('#settings-content');
  const status = document.querySelector('#settings-status');
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const services = [
    ['chat', 'Chat 对话模型', '用于普通对话、快速问答和智能推理。', 'OpenAI 兼容 Base URL'],
    ['embedding', 'Embedding 向量模型', '用于文档入库和语义检索。更换模型后，已有文档需重新索引；新模型需与现有向量维度兼容。', 'OpenAI 兼容 Base URL'],
    ['reranker', 'Reranker 重排模型', '用于检索结果重排，URL 请填写完整重排接口地址。', '完整接口 URL'],
  ];
  async function api(path, method = 'GET', body) {
    const response = await fetch(path, {method, credentials:'same-origin', headers:{'Content-Type':'application/json'},
      body:body === undefined ? undefined : JSON.stringify(body)});
    if (response.status === 401) { location.href = '/'; throw new Error('请先登录'); }
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail?.message || '模型配置字段无效，请检查输入');
    return result;
  }
  function keyPlaceholder(config) {
    return config.key_configured ? '已配置，留空保留；填写新密钥即可更换' : '尚未配置，请填写 API Key';
  }
  function render(configs) {
    content.innerHTML = services.map(([service, title, description, urlLabel]) => {
      const config = configs[service];
      return `<form class="model-card" data-service="${service}"><h2>${title}</h2><p class="model-description">${description}</p>
        <div class="model-fields"><label>模型名称<input name="model" value="${escape(config.model)}" required maxlength="256" autocomplete="off"></label>
        <label>API Key<input name="api_key" type="password" maxlength="4096" autocomplete="new-password" placeholder="${keyPlaceholder(config)}"></label>
        <label class="model-url">${urlLabel}<input name="url" type="url" value="${escape(config.url)}" required maxlength="2048" autocomplete="off"></label></div>
        <div class="model-actions"><button class="button primary" type="submit" data-action="save">保存配置</button><button class="button" type="submit" data-action="test">检测连接</button></div>
        <p role="status" aria-live="polite"></p></form>`;
    }).join('');
    content.hidden = false;
  }
  content.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.target;
    if (form.dataset.busy) return;
    const data = new FormData(form);
    const body = {model:data.get('model').trim(), url:data.get('url').trim()};
    const key = data.get('api_key');
    if (key) body.api_key = key;
    const testing = event.submitter?.dataset.action === 'test';
    const message = form.querySelector('[role=status]');
    const controls = form.querySelectorAll('input,button');
    form.dataset.busy = 'true';
    controls.forEach(control => { control.disabled = true; });
    message.dataset.state = '';
    message.textContent = testing ? '正在检测连接…' : '正在保存…';
    try {
      const result = await api('/api/settings/models/' + form.dataset.service + (testing ? '/test' : ''), testing ? 'POST' : 'PUT', body);
      if (!testing) {
        form.querySelector('[name=model]').value = result.model;
        form.querySelector('[name=url]').value = result.url;
        form.querySelector('[name=api_key]').value = '';
        form.querySelector('[name=api_key]').placeholder = keyPlaceholder(result);
      }
      message.dataset.state = 'success';
      message.textContent = testing ? result.message : '已保存，新请求和新入库任务将使用此配置。';
    } catch (error) {
      message.dataset.state = 'error';
      message.textContent = error.message;
    } finally {
      delete form.dataset.busy;
      controls.forEach(control => { control.disabled = false; });
    }
  });
  api('/api/settings/models').then(configs => {
    render(configs);
    status.textContent = '';
  }).catch(error => { status.textContent = error.message; });
})();
