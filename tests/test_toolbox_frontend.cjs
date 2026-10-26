const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const source = readFileSync('frontend/skills.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function page({empty=false, unauthorized=false, failRead=false, failCount=false, failWrite=false, holdWrite=false, failRefresh=false, holdRefresh=false} = {}) {
  const elements = new Map(), requests = [], location = {};
  let rows = empty ? [] : [{name:'text-report', description:'<img src=x onerror=bad>', is_enabled:true,
    resources:[{path:'references/<format>.md',type:'reference'}, {path:'scripts/report.py',type:'script'}]}];
  let release;
  const gate = new Promise(resolve => { release=resolve; });
  const element = name => {
    if (!elements.has(name)) elements.set(name, {hidden:true, textContent:'', innerHTML:'', listeners:{},
      addEventListener(kind, handler) {this.listeners[kind]=handler;}});
    return elements.get(name);
  };
  vm.runInNewContext(source, {document:{querySelector:element}, location, fetch:async (url, options) => {
    requests.push({url,...options});
    if (unauthorized) return new Response('{}',{status:401});
    if ((url === '/api/skills' && failRead) || (url === '/api/mcp-services' && failCount))
      return new Response(JSON.stringify({detail:{message:'受控读取失败'}}),{status:503});
    if (url === '/api/auth/me') return new Response('{"id":"owner"}');
    if (url === '/api/mcp-services') return new Response('[{"id":"demo"},{"id":"second"}]');
    if (url === '/api/skills/refresh') {
      if (holdRefresh) await gate;
      if (failRefresh) return new Response(JSON.stringify({detail:{message:'目录校验失败'}}),{status:503});
      rows=[...rows, {name:'kb-retriever', description:'本地检索', is_enabled:true, resources:[]}];
      return new Response(JSON.stringify(rows));
    }
    if (options.method === 'PATCH') {
      if (holdWrite) await gate;
      if (failWrite) return new Response(JSON.stringify({detail:{message:'保存失败，原状态已保留'}}),{status:503});
      rows=[{...rows[0],...JSON.parse(options.body)}];
      return new Response(JSON.stringify(rows[0]));
    }
    return new Response(JSON.stringify(rows));
  }});
  function toggle() {
    const button={disabled:false, closest:()=>({dataset:{name:'text-report'}})};
    const promise=element('#skills-list').listeners.click({target:{closest:()=>button}});
    return {button,promise};
  }
  return {element, requests, location, toggle, release};
}

test('技能列表转义简介与资源，数量显示所有已登记条目', async () => {
  const app=page(); await tick();
  const html=app.element('#skills-list').innerHTML;
  assert.match(html,/&lt;img/); assert.doesNotMatch(html,/<img/);
  assert.match(html,/references\/&lt;format&gt;\.md/); assert.match(html,/脚本/);
  assert.match(html,/aria-checked="true"/); assert.match(html,/资源清单 · 2/);
  assert.equal(app.element('#skills-count').textContent,'1');
  assert.equal(app.element('#mcp-count').textContent,'2');
  assert.equal(app.element('#skills-content').hidden,false);
});

test('启停等待保存，防止重复请求；成功后更新状态和提示', async () => {
  const app=page({holdWrite:true}); await tick();
  const {button,promise}=app.toggle();
  assert.equal(button.disabled,true);
  assert.match(app.element('#skills-list').innerHTML,/aria-checked="true"/);
  app.toggle();
  assert.equal(app.requests.filter(request=>request.method==='PATCH').length,1);
  const request=app.requests.find(request=>request.method==='PATCH');
  assert.equal(request.url,'/api/skills/text-report');
  assert.equal(request.credentials,'same-origin');
  assert.deepEqual(JSON.parse(request.body),{is_enabled:false});
  app.release(); await promise;
  assert.equal(button.disabled,false);
  assert.match(app.element('#skills-list').innerHTML,/aria-checked="false"/);
  assert.match(app.element('#skills-status').textContent,/对新启动的智能推理生效/);
  assert.equal(app.element('#skills-count').textContent,'1');
  await app.toggle().promise;
  assert.match(app.element('#skills-list').innerHTML,/aria-checked="true"/);
});

test('保存失败保持原启用状态并恢复开关', async () => {
  const app=page({failWrite:true}); await tick();
  const {button,promise}=app.toggle(); await promise;
  assert.equal(button.disabled,false);
  assert.match(app.element('#skills-list').innerHTML,/aria-checked="true"/);
  assert.match(app.element('#skills-status').textContent,/保存失败/);
});

test('空列表有明确提示', async () => {
  const app=page({empty:true}); await tick();
  assert.match(app.element('#skills-list').innerHTML,/暂无技能/);
  assert.equal(app.element('#skills-count').textContent,'0');
});

test('未登录跳转主页且不请求管理数据', async () => {
  const app=page({unauthorized:true}); await tick();
  assert.equal(app.location.href,'/');
  assert.equal(app.requests.length,1);
  assert.equal(app.element('#skills-content').hidden,true);
});

test('技能读取失败显示错误，不伪造空列表', async () => {
  const app=page({failRead:true}); await tick();
  assert.match(app.element('#skills-status').textContent,/受控读取失败/);
  assert.equal(app.element('#skills-list').innerHTML,'');
});

test('MCP计数失败不阻塞技能页面并明确提示', async () => {
  const app=page({failCount:true}); await tick();
  assert.match(app.element('#skills-list').innerHTML,/text-report/);
  assert.match(app.element('#skills-status').textContent,/MCP服务数量加载失败/);
  assert.equal(app.element('#mcp-count').textContent,'');
});

test('刷新目录阻止重复请求和启停，成功更新列表数量', async () => {
  const app=page({holdRefresh:true}); await tick();
  const refresh=app.element('#skills-refresh');
  const promise=refresh.listeners.click();
  assert.equal(refresh.disabled,true);
  assert.match(app.element('#skills-list').innerHTML,/data-toggle disabled/);
  refresh.listeners.click(); app.toggle();
  assert.equal(app.requests.filter(request=>request.url==='/api/skills/refresh').length,1);
  assert.equal(app.requests.filter(request=>request.method==='PATCH').length,0);
  assert.equal(app.requests.at(-1).method,'POST');
  app.release(); await promise;
  assert.equal(refresh.disabled,false);
  assert.match(app.element('#skills-list').innerHTML,/kb-retriever/);
  assert.equal(app.element('#skills-count').textContent,'2');
  assert.match(app.element('#skills-status').textContent,/目录已刷新/);
});

test('刷新失败保留列表状态并恢复操作', async () => {
  const app=page({failRefresh:true}); await tick();
  const before=app.element('#skills-list').innerHTML;
  await app.element('#skills-refresh').listeners.click();
  assert.equal(app.element('#skills-list').innerHTML,before);
  assert.equal(app.element('#skills-count').textContent,'1');
  assert.equal(app.element('#skills-refresh').disabled,false);
  assert.match(app.element('#skills-status').textContent,/目录校验失败/);
});

test('启停保存期间不能刷新目录', async () => {
  const app=page({holdWrite:true}); await tick();
  const toggle=app.toggle();
  assert.equal(app.element('#skills-refresh').disabled,true);
  await app.element('#skills-refresh').listeners.click();
  assert.equal(app.requests.filter(request=>request.url==='/api/skills/refresh').length,0);
  app.release(); await toggle.promise;
  assert.equal(app.element('#skills-refresh').disabled,false);
});
