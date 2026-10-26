const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const source = readFileSync('frontend/mcp-services.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function page() {
  const elements = new Map(), requests = [];
  let rows = [{id:'demo', name:'术语服务', endpoint:'http://localhost/mcp', auth_env_key:null,
    is_enabled:true, enabled_tools:[], tools_catalog:[{name:'lookup_term', description:'<img src=x onerror=bad>', inputSchema:{type:'object'}, readOnlyHint:true}]}];
  const element = name => {
    if (!elements.has(name)) elements.set(name, {hidden:true, listeners:{}, textContent:'', innerHTML:'', addEventListener(kind, fn) {this.listeners[kind]=fn;}});
    return elements.get(name);
  };
  const location = {};
  class FormData { constructor(form) {this.fields=form.fields;} get(key) {return this.fields[key];} getAll(key) {return this.fields[key] || [];} }
  vm.runInNewContext(source, {document:{querySelector:element}, location, FormData, confirm:()=>true,
    fetch:async (url, options) => {
      requests.push({url, ...options});
      let data;
      if (url === '/api/auth/me') data = {id:'owner'};
      else if (url === '/api/skills') data = [{name:'text-report', is_enabled:true}];
      else if (url === '/api/mcp-services/credentials') data = [{auth_env_key:'MCP_TAVILY_API_KEY', configured:false}];
      else if (options.method === 'PUT') data = {auth_env_key:'MCP_TAVILY_API_KEY', configured:true};
      else if (url === '/api/mcp-services' && options.method === 'GET') data = rows;
      else if (options.method === 'PATCH') {Object.assign(rows[0], JSON.parse(options.body)); data=rows[0];}
      else if (options.method === 'DELETE') {rows=[]; return new Response(null, {status:204});}
      else if (url.endsWith('/test')) data={connected:true,tool_count:1};
      else if (url.endsWith('/refresh-tools')) data=rows[0];
      else {rows.push({...rows[0], id:'new'}); data=rows.at(-1);}
      return new Response(JSON.stringify(data));
    }});
  return {element, requests, rows:()=>rows};
}

test('目录描述转义，readOnlyHint不自动勾选', async () => {
  const app=page(); await tick(); await tick();
  const html=app.element('#mcp-list').innerHTML;
  assert.match(html, /&lt;img/); assert.doesNotMatch(html, /value="lookup_term" checked/);
  assert.match(html, /勾选表示管理员已确认只读/);
  assert.equal(app.element('#mcp-content').hidden, false);
  assert.equal(app.element('#mcp-count').textContent,'1');
  assert.equal(app.element('#skills-count').textContent,'1');
});

test('管理员明确提交白名单', async () => {
  const app=page(); await tick();
  const button={disabled:false}; const form={fields:{tool:['lookup_term']}, closest:()=>({dataset:{id:'demo'}}), hasAttribute:()=>true, querySelector:()=>button};
  app.element('#mcp-list').listeners.submit({preventDefault(){},target:form}); await tick();
  const request=app.requests.find(row=>row.method==='PATCH');
  assert.deepEqual(JSON.parse(request.body), {enabled_tools:['lookup_term']});
  assert.equal(button.disabled,false);
});

for (const action of ['test', 'refresh-tools', 'toggle', 'delete']) {
  test(`管理动作${action}发出正确接口并刷新`, async () => {
    const app=page(); await tick();
    const button={dataset:{action}, disabled:false, closest:()=>({dataset:{id:'demo'}})};
    app.element('#mcp-list').listeners.click({target:{closest:()=>button}}); await tick();
    if(action==='toggle') assert.equal(app.rows()[0].is_enabled,false);
    else if(action==='delete') assert.equal(app.rows().length,0);
    else assert.ok(app.requests.some(row=>row.url.endsWith('/'+action) && row.method==='POST'));
    assert.equal(button.disabled,false);
  });
}

test('添加服务一次提交名称地址和Key，默认空白名单', async () => {
  const app=page(); await tick();
  const button={disabled:false}; const input={value:'fixture-service-key'}; const wrapper={hidden:false};
  const form={fields:{name:'术语',endpoint:'http://localhost/mcp',requires_auth:'on',token:input.value}, querySelector:selector=>selector==='[name=token]' ? input : selector==='[data-token]' ? wrapper : button, reset(){}};
  app.element('#mcp-create').listeners.submit({preventDefault(){},target:form}); await tick();
  const request=app.requests.find(row=>row.url==='/api/mcp-services' && row.method==='POST');
  assert.deepEqual(JSON.parse(request.body).enabled_tools,[]);
  assert.equal(JSON.parse(request.body).requires_auth,true);
  assert.equal(JSON.parse(request.body).token,'fixture-service-key');
  assert.equal(input.value,'');
  assert.equal(wrapper.hidden,true);
});

test('需要认证开关控制Key输入，无需认证不提交密钥', async () => {
  const app=page(); await tick();
  const input={value:'old-key'}; const wrapper={hidden:true};
  const form={querySelector:selector=>selector==='[name=token]' ? input : wrapper,hasAttribute:()=>false};
  const checkbox={name:'requires_auth',checked:true,form};
  app.element('#mcp-create').listeners.change({target:checkbox});
  assert.equal(wrapper.hidden,false); assert.equal(input.required,true); assert.equal(input.disabled,false);
  checkbox.checked=false; app.element('#mcp-create').listeners.change({target:checkbox});
  assert.equal(wrapper.hidden,true); assert.equal(input.value,''); assert.equal(input.disabled,true);
  assert.doesNotMatch(app.element('#mcp-list').innerHTML,/认证变量/);
});

test('编辑已有认证服务留空Key保留原密钥', async () => {
  const app=page(); await tick();
  const button={disabled:false}; const input={value:''};
  const form={fields:{name:'已编辑',endpoint:'http://localhost/mcp',requires_auth:'on',token:''},
    closest:()=>({dataset:{id:'demo'}}),hasAttribute:()=>false,querySelector:selector=>selector==='[name=token]' ? input : button};
  app.element('#mcp-list').listeners.submit({preventDefault(){},target:form}); await tick();
  const request=app.requests.find(row=>row.method==='PATCH');
  assert.deepEqual(JSON.parse(request.body),{name:'已编辑',endpoint:'http://localhost/mcp',requires_auth:true});
});
