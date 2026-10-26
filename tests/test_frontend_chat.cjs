const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const {node: domNode, parse: parseHtml, findAttribute} = require('./helpers/chat-dom.cjs');

const source = readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
const quickSource = readFileSync(path.join(__dirname, '../frontend/quick-answer.js'), 'utf8');
const markedSource = readFileSync(path.join(__dirname, '../frontend/vendor/marked/marked.umd.js'), 'utf8');
const encoder = new TextEncoder();
const encode = event => encoder.encode(JSON.stringify(event) + '\n');

function createApp(sessions = [], data = {}) {
  const elements = new Map();
  const requests = [];
  const apiRequests = [];
  let saved = data.fresh ? null : JSON.stringify({ libraries: data.libraries || [], documents: data.documents || [], activities: [], sessions, settings: { dense: false } });
  let nextId = 0;
  const temporary = new Map();
  const streams = [];
  class FakeEventSource {
    static CONNECTING = 0;
    constructor(url) { this.url = url; this.readyState = 1; streams.push(this); }
    close() { this.readyState = 2; }
    emit(event) { this.onmessage?.({data: JSON.stringify(event)}); }
  }
  const element = selector => {
    const attribute = selector.match(/^\[(data-(?:message|sources)-index)="(\d+)"\]$/);
    if (attribute && elements.has('.chat-thread')) {
      const found = findAttribute(elements.get('.chat-thread'), attribute[1], attribute[2]);
      if (found) return found;
    }
    if (!elements.has(selector)) {
      let text = '';
      let html = '';
      const item = domNode(1, 'DIV');
      Object.assign(item, {value: '', hidden: false, focus() {}, scrollIntoView() {}, showModal() { this.open = true; }, close() { this.open = false; }});
      let scrollTop = 0;
      let scrollHeight = 2000;
      item.clientHeight = 400;
      Object.defineProperty(item, 'scrollHeight', {get: () => scrollHeight, set: height => {
        scrollHeight = height;
        scrollTop = Math.max(0, Math.min(scrollTop, scrollHeight - item.clientHeight));
      }});
      item.getBoundingClientRect = () => ({ top: item.viewportTop || 0, bottom: (item.viewportTop || 0) + item.clientHeight });
      item.querySelectorAll = selector => selector.startsWith('.markdown-body') ? item.blocks || [] : [];
      Object.defineProperty(item, 'scrollTop', { get: () => scrollTop, set: value => { scrollTop = Math.max(0, Math.min(value, item.scrollHeight - item.clientHeight)); } });
      if (selector === '#content-view') Object.defineProperty(item, 'innerHTML', {
        get: () => selector === '#content-view' && elements.has('.chat-thread')
          ? html.replace(/(<div class="chat-thread">)[\s\S]*?(<\/div><form class="chat-composer")/, (_, before, after) => before + element('.chat-thread').innerHTML + after) : html,
        set: value => {
          html = value;
          if (selector === '#content-view') {
            elements.delete('.chat-thread');
            const messages = value.match(/<div class="chat-thread">([\s\S]*?)<\/div><form class="chat-composer"/);
            if (messages) element('.chat-thread').innerHTML = messages[1];
          }
          if (item.onHTML) item.onHTML(value);
        }
      });
      if (selector === '#quick-status') Object.defineProperty(item, 'textContent', { get: () => text, set: value => { text = value; if (item.onText) item.onText(value); } });
      elements.set(selector, item);
    }
    return elements.get(selector);
  };
  const document = { listeners: {}, body: { classList: { toggle() {}, add() {} } }, createElement: name => {
    if (name !== 'template') return element(`created-${name}-${++nextId}`);
    const template = {};
    Object.defineProperty(template, 'innerHTML', {set: html => { template.content = parseHtml(html); }});
    return template;
  }, querySelector: element, querySelectorAll: selector => selector === '[data-chat-model]' || selector === '[data-chat-mode]' ? [element(selector)] : [], addEventListener(name, handler) { this.listeners[name] = handler; } };
  const location = {hash: data.hash || ''};
  const context = vm.createContext({ document, window: { innerWidth: 1200, scrollTo() {}, addEventListener() {}, EventSource: FakeEventSource, location, history: {replaceState(_, __, url) {location.hash = url.includes('#') ? url.slice(url.indexOf('#')) : '';}} }, sessionStorage: {getItem: key => temporary.get(key), setItem: (key,value) => temporary.set(key,value), removeItem: key => temporary.delete(key)}, localStorage: { getItem: () => saved, setItem: (_, value) => { saved = value; } }, crypto: { randomUUID: () => `session-${++nextId}` }, structuredClone, TextDecoder, AbortController, URL, setTimeout: data.setTimeout || setTimeout, clearTimeout: data.clearTimeout || clearTimeout, console, fetch: async (url, options) => { apiRequests.push({ url, options }); if (data.fetch) { const response = await data.fetch(url, options); if (response) return response; } if (url === '/api/auth/me') return new Response(JSON.stringify({ id: 'admin', username: 'admin' }), { status: data.meStatus || 200 }); if (url === '/api/knowledge-bases') return new Response(JSON.stringify(data.serverLibraries || [])); if (url === '/api/sessions') return new Response(JSON.stringify(data.serverSessions || [])); if (url === '/api/chat/model') return new Response(JSON.stringify({ model: 'provider/current-model' })); requests.push({ url, ...JSON.parse(options.body) }); return context.reply(); } });
  vm.runInContext(markedSource, context);
  vm.runInContext(quickSource, context);
  vm.runInContext(source, context);
  const mode = value => document.listeners.change({target: {dataset: {chatMode: ''}, value}});
  // 原有普通对话回归显式切换；产品默认值仍由首页HTML与app.js决定。
  if (data.mode !== 'quick_answer') mode('plain_chat');
  return {
    element, requests, apiRequests, streams, mode,
    state: () => JSON.parse(saved),
    reply: factory => { context.reply = factory; },
    home: value => { if (data.mode !== 'quick_answer') mode('plain_chat'); element('#home-search').value = value; return element('#home-search-form').listeners.submit({ preventDefault() {} }); },
    send: value => document.listeners.submit({ preventDefault() {}, target: { id: 'chat-form', elements: { q: { value } } } }),
    key: event => document.listeners.keydown(event),
    action: (action, id) => document.listeners.click({ target: { closest: () => ({ dataset: { action, id } }) } })
  };
}

function quickApp(options = {}) {
  const rows = options.rows || [];
  let next = 0;
  const app = createApp(options.local || [], {mode: 'quick_answer', hash: options.hash,
    serverLibraries: [{id: 'kb', name: '企业库', status: 'active'}],
    fetch: (url, request = {}) => {
      const response = options.fetch?.(url, request);
      if (response) return response;
      if (url === '/api/sessions') {
        if (request.method === 'POST') {
          const row = {id: `quick-${++next}`, title: JSON.parse(request.body).title, updated_at: new Date().toISOString()};
          rows.push(row); return new Response(JSON.stringify(row), {status: 201});
        }
        return new Response(JSON.stringify(rows));
      }
      if (url.endsWith('/runs')) return new Response(JSON.stringify({run_id: `run-${url.split('/')[3]}`}), {status: 202});
      if (url.endsWith('/messages')) return new Response(JSON.stringify(options.messages || []));
      if (/^\/api\/runs\/[^/]+\/sources\//.test(url)) return new Response(JSON.stringify(options.snapshot || {run_id:'run',handle:'s1',chunk_id:'chunk',document_id:'document',readable:true,original_deleted:false,source_snapshot:{original_name:'SLA.md',page_start:null,page_end:null,excerpt:'历史必要片段',kb_id:'kb'}}));
      if (url.startsWith('/api/chunks/')) return new Response(JSON.stringify({content: '准确来源正文', source: {source_meta: {document_status: '现行', heading_path: ['SLA'], line_ranges: [[2, 8]]}}}));
      return null;
    }});
  return app;
}

for (const sqlFirst of [true, false]) {
  test(`快速模块失效run恢复不保留虚假空回答（${sqlFirst ? 'SQL先返回' : 'SSE先报错'}）`, async () => {
    const context = vm.createContext({window: {}});
    vm.runInContext(quickSource, context);
    let resolveMessages;
    const streams = [];
    class ExpiredEventSource {
      static CONNECTING = 0;
      constructor() { this.readyState = 1; streams.push(this); }
      close() { this.readyState = 2; }
    }
    const storage = new Map([['knora-quick-pending-runs', JSON.stringify([{sessionId: 'saved', runId: 'expired'}])]]);
    const client = new context.window.QuickAnswerClient({
      api: () => new Promise(resolve => { resolveMessages = resolve; }),
      onChange() {}, EventSourceClass: ExpiredEventSource,
      storage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)}
    });
    client.rows.set('saved', {id: 'saved', messages: [], updatedAt: ''});
    client.restorePending();
    const opening = client.open('saved');
    const messages = [{id: 'u', role: 'user', content: '问题'}, {id: 'a', role: 'assistant', content: '完整SQL回答', status: 'complete'}];
    if (sqlFirst) { resolveMessages(messages); await opening; assert.equal(client.rows.get('saved').messages.length, 3); }
    streams[0].readyState = 2;
    streams[0].onerror();
    if (!sqlFirst) { resolveMessages(messages); await opening; }
    assert.equal(client.rows.get('saved').messages.length, 2);
    assert.equal(client.rows.get('saved').messages[1].content, '完整SQL回答');
    assert.equal(client.pending.size, 0);
    assert.equal(storage.get('knora-quick-pending-runs'), '[]');
  });
}

test('智能推理同会话切换、公开增量移入工具步骤，工具JSON不直接展示', async () => {
  const app = quickApp(); await new Promise(setImmediate);
  app.mode('quick_answer');
  await app.home('第一轮');
  const first = app.streams[0];
  first.emit({run_id:'run-quick-1',seq:1,type:'answer.finalized',payload:{message_id:'first',text:'快速回答',citations:[],status:'complete'}});
  first.emit({run_id:'run-quick-1',seq:2,type:'run.completed',payload:{}});
  await new Promise(setImmediate);
  app.mode('smart_reasoning');
  assert.equal(app.element('[data-chat-mode]').value, 'smart_reasoning');
  await app.send('比较两文档');
  const requests = app.apiRequests.filter(request => request.url.endsWith('/runs'));
  assert.equal(requests[0].url, requests[1].url);
  assert.equal(JSON.parse(requests[1].options.body).mode, 'smart_reasoning');
  const stream = app.streams[1];
  const emit = (seq,type,payload) => stream.emit({run_id:'run-quick-1',seq,type,payload});
  emit(1,'round.started',{round_no:1});
  emit(2,'model.delta',{round_no:1,text:'正在核对原文'});
  assert.match(app.element('[data-message-index="3"]').innerHTML,/正在核对原文/);
  emit(3,'assistant.tool_calls',{round_no:1,message_id:'calls',turn_id:'turn',tool_calls:[{id:'call_a',name:'read_document'},{id:'call_b',name:'read_document'}]});
  emit(4,'tool.started',{round_no:1,tool_call_id:'call_a',name:'read_document'});
  emit(5,'tool.finished',{round_no:1,tool_call_id:'call_a',name:'read_document',ok:true,summary:'返回 2 个片段'});
  assert.match(app.element('#content-view').innerHTML,/正在核对原文.*reasoning-step/s);
  assert.match(app.element('#content-view').innerHTML,/返回 2 个片段/);
  assert.doesNotMatch(app.element('#content-view').innerHTML,/raw_result|model_result|tool_call_id/);
  emit(6,'round.started',{round_no:2});
  emit(7,'model.delta',{round_no:2,text:'最终比较'});
  emit(8,'answer.finalized',{message_id:'final',text:'最终比较[s1]',citations:[],status:'complete'});
  emit(9,'run.completed',{});
  assert.match(app.element('#content-view').innerHTML,/工具步骤/);
  assert.equal(app.element('#chat-input').disabled,false);
  app.mode('quick_answer');
  await app.send('继续追问');
  const last = app.apiRequests.filter(request => request.url.endsWith('/runs')).at(-1);
  assert.equal(last.url,requests[0].url);
  assert.equal(JSON.parse(last.options.body).mode,'quick_answer');
});

for (const initialTop of [0, 600, 1552, 1600]) {
  test(`智能推理滚动位置 ${initialTop} 在增量、工具步骤和终态更新后保持阅读或跟随底部`, async () => {
    const app = quickApp(); await new Promise(setImmediate);
    app.mode('smart_reasoning');
    await app.home('滚动验证');
    let height = 2000;
    app.element('.chat-thread').scrollTop = initialTop;
    app.element('.chat-thread').onHTML = () => { app.element('.chat-thread').scrollHeight = height += 150; };
    let seq = 0;
    const emit = (type, payload) => app.streams[0].emit({run_id: 'run-quick-1', seq: ++seq, type, payload});
    const check = () => {
      const thread = app.element('.chat-thread');
      assert.equal(thread.scrollTop, initialTop >= 1552 ? thread.scrollHeight - thread.clientHeight : initialTop);
    };
    emit('round.started', {round_no: 1}); check();
    app.element('[data-message-index="1"]').onHTML = () => { app.element('.chat-thread').scrollHeight = height += 200; };
    app.element('[data-message-index="1"]').scrollIntoView = () => assert.fail('流式输出不能强制滚动消息');
    emit('model.delta', {text: '正在核对原文'}); check();
    emit('assistant.tool_calls', {message_id: 'calls', turn_id: 'turn', tool_calls: [{id: 'call', name: 'read_document'}]}); check();
    emit('tool.started', {tool_call_id: 'call', name: 'read_document'}); check();
    emit('tool.finished', {tool_call_id: 'call', name: 'read_document', ok: true, summary: '返回原文'}); check();
    emit('round.started', {round_no: 2}); check();
    emit('model.delta', {text: '最终回答'}); check();
    emit('answer.finalized', {message_id: 'answer', text: '完整回答', citations: [], status: 'complete'}); check();
    emit('run.completed', {}); check();
  });
}

test('流式输出期间上移停止跟随，回到底部恢复跟随，失败后保留阅读位置', async () => {
  const app = quickApp(); await new Promise(setImmediate);
  await app.home('滚动验证');
  const thread = app.element('.chat-thread');
  thread.scrollTop = thread.scrollHeight - thread.clientHeight;
  app.element('[data-message-index="1"]').onHTML = () => { thread.scrollHeight += 200; };
  let seq = 0;
  const emit = (type, payload) => app.streams[0].emit({run_id: 'run-quick-1', seq: ++seq, type, payload});
  emit('model.delta', {text: '第一段'});
  assert.equal(thread.scrollTop, thread.scrollHeight - thread.clientHeight);
  thread.scrollTop = 600;
  emit('model.delta', {text: '第二段'});
  assert.equal(thread.scrollTop, 600);
  thread.scrollTop = thread.scrollHeight - thread.clientHeight;
  emit('model.delta', {text: '第三段'});
  assert.equal(thread.scrollTop, thread.scrollHeight - thread.clientHeight);
  thread.scrollTop = 600;
  emit('run.failed', {message: '运行失败'});
  assert.equal(app.element('.chat-thread'), thread);
  assert.equal(app.element('.chat-thread').scrollTop, 600);
});

test('阅读本轮推理正文时，转为工具步骤后同一段落保持屏幕位置', async () => {
  const app = quickApp(); await new Promise(setImmediate);
  app.mode('smart_reasoning');
  await app.home('滚动验证');
  let seq = 0;
  const emit = (type, payload) => app.streams[0].emit({run_id: 'run-quick-1', seq: ++seq, type, payload});
  emit('model.delta', {text: '正在阅读的推理正文'});
  const thread = app.element('.chat-thread');
  thread.scrollTop = 600;
  let paragraphOffset = 700;
  thread.blocks = [
    {tagName: 'P', textContent: '正在阅读的推理正文', getBoundingClientRect: () => ({top: -thread.scrollTop, bottom: 30 - thread.scrollTop})},
    {tagName: 'P', textContent: '正在阅读的推理正文', getBoundingClientRect: () => ({top: paragraphOffset - thread.scrollTop, bottom: paragraphOffset - thread.scrollTop + 30})}
  ];
  thread.onHTML = () => { paragraphOffset = 400; };
  emit('assistant.tool_calls', {message_id: 'calls', turn_id: 'turn', tool_calls: [{id: 'call', name: 'read_document'}]});
  assert.equal(app.element('.chat-thread'), thread);
  assert.equal(thread.blocks[1].getBoundingClientRect().top, 100);
  emit('round.started', {round_no: 2});
  assert.equal(thread.blocks[1].getBoundingClientRect().top, 100);
});

test('内容变短导致滚动位置触底时不会误恢复跟随', async () => {
  const app = quickApp(); await new Promise(setImmediate);
  await app.home('滚动验证');
  app.streams[0].emit({run_id: 'run-quick-1', seq: 1, type: 'model.delta', payload: {text: '本轮文字'}});
  const thread = app.element('.chat-thread');
  thread.scrollTop = 600;
  thread.onHTML = () => { thread.scrollHeight = 800; };
  app.streams[0].emit({run_id: 'run-quick-1', seq: 2, type: 'round.started', payload: {round_no: 1}});
  assert.equal(thread.scrollTop, 400);
  app.element('[data-message-index="1"]').onHTML = () => { thread.scrollHeight = 1600; };
  app.streams[0].emit({run_id: 'run-quick-1', seq: 3, type: 'model.delta', payload: {text: '继续推理'}});
  assert.equal(thread.scrollTop, 400);
});

test('同会话发送下一条消息保留消息区容器和阅读位置', async () => {
  const app = quickApp(); await new Promise(setImmediate);
  await app.home('第一轮');
  app.streams[0].emit({run_id: 'run-quick-1', seq: 1, type: 'answer.finalized', payload: {message_id: 'answer', text: '回答', citations: [], status: 'complete'}});
  app.streams[0].emit({run_id: 'run-quick-1', seq: 2, type: 'run.completed', payload: {}});
  await new Promise(setImmediate);
  const thread = app.element('.chat-thread');
  thread.scrollTop = 600;
  await app.send('继续推理');
  assert.equal(app.element('.chat-thread'), thread);
  assert.equal(thread.scrollTop, 600);
});

test('流式增量与工具事件保留已完成段落节点和用户折叠状态', async () => {
  const app = quickApp(); await new Promise(setImmediate);
  app.mode('smart_reasoning'); await app.home('节点验证');
  let seq = 0;
  const emit = (type, payload) => app.streams[0].emit({run_id:'run-quick-1',seq:++seq,type,payload});
  emit('model.delta', {text:'已完成段落\n\n尚未完成'});
  const body = app.element('[data-message-index="1"]');
  const paragraph = body.firstChild;
  emit('model.delta', {text:'的段落\n\n新段落'});
  assert.equal(body.firstChild, paragraph);
  emit('assistant.tool_calls', {message_id:'calls',turn_id:'turn',tool_calls:[{id:'call',name:'read_document'}]});
  assert.equal(app.element('[data-message-index="1"]'), body);
  assert.equal(body.firstChild, paragraph);
  const details = body.parentNode.children[1];
  details.open = false;
  emit('tool.finished', {tool_call_id:'call',name:'read_document',ok:true,summary:'已读取'});
  assert.equal(body.parentNode.children[1], details);
  assert.equal(details.open, false);
  assert.equal(body.firstChild, paragraph);
});

test('状态栏换行改变消息区起点时保持正文在屏幕上的位置', async () => {
  const app = quickApp(); await new Promise(setImmediate);
  app.mode('smart_reasoning');
  await app.home('滚动验证');
  app.element('.chat-thread').scrollTop = 600;
  let viewportTop = 0;
  app.element('#quick-status').onText = text => {
    viewportTop = text.includes('read_document') ? 40 : 0;
    app.element('.chat-thread').viewportTop = viewportTop;
  };
  let seq = 0;
  const emit = (type, payload) => app.streams[0].emit({run_id: 'run-quick-1', seq: ++seq, type, payload});
  const check = () => assert.equal(app.element('.chat-thread').scrollTop - viewportTop, 600);
  emit('tool.started', {tool_call_id: 'call', name: 'read_document'}); check();
  emit('tool.finished', {tool_call_id: 'call', name: 'read_document', ok: true, summary: '返回原文'}); check();
  emit('round.started', {round_no: 2}); check();
});

test('普通对话增量与完成更新同样保留中间阅读位置', async () => {
  const app = createApp();
  let controller;
  app.reply(() => new Response(new ReadableStream({start(value) { controller = value; }})));
  const sending = app.home('滚动验证');
  await new Promise(setImmediate);
  const thread = app.element('.chat-thread');
  thread.scrollTop = 600;
  app.element('[data-message-index="1"]').onHTML = () => { thread.scrollHeight += 200; };
  app.element('[data-message-index="1"]').scrollIntoView = () => assert.fail('流式输出不能强制滚动消息');
  controller.enqueue(encode({type: 'content', content: '新段落'}));
  await new Promise(setImmediate);
  assert.equal(thread.scrollTop, 600);
  controller.enqueue(encode({type: 'done'}));
  controller.close();
  await sending;
  assert.equal(app.element('.chat-thread'), thread);
  assert.equal(app.element('.chat-thread').scrollTop, 600);
});

test('智能历史刷新保持模式并按完整调用组展示摘要及最终回答', async () => {
  const app=quickApp({hash:'#session=saved&mode=smart_reasoning',rows:[{id:'saved',title:'比较',updated_at:new Date().toISOString()}],
    messages:[{id:'u',turn_id:'turn',role:'user',content:'比较',status:'complete'},
      {id:'a',turn_id:'turn',role:'assistant',content:null,status:'complete',tool_calls:[{id:'c',function:{name:'read_document',arguments:'{"document_id":"uuid"}'}}]},
      {id:'t',turn_id:'turn',role:'tool',tool_call_id:'c',content:'{"raw_result":{"data":"不应展示的完整JSON"}}',tool_name:'read_document',tool_summary:'返回 5 个片段',status:'complete'},
      {id:'f',turn_id:'turn',role:'assistant',content:'依据原文的比较',citations:[],status:'complete'}]});
  await new Promise(setImmediate);
  assert.equal(app.element('[data-chat-mode]').value,'smart_reasoning');
  assert.match(app.element('#content-view').innerHTML,/read_document.*返回 5 个片段/s);
  assert.doesNotMatch(app.element('#content-view').innerHTML,/不应展示|raw_result|document_id/);
  assert.equal(app.element('[data-message-index="3"]').innerHTML,'<p>依据原文的比较</p>\n');
});

test('智能运行中刷新列表和SQL历史后，后续工具事件仍更新当前会话', async () => {
  const context = vm.createContext({window: {}});
  vm.runInContext(quickSource, context);
  const stored = [{id:'u',turn_id:'turn',role:'user',content:'比较'},
    {id:'a',turn_id:'turn',role:'assistant',content:null,tool_calls:[{id:'c',function:{name:'read_document'}}]}];
  class Stream { close() {} }
  const client = new context.window.QuickAnswerClient({api: async url => url.endsWith('/messages') ? stored :
    [{id:'saved',updated_at:new Date().toISOString()}], onChange() {}, EventSourceClass:Stream,
    storage:{setItem() {}}});
  client.rows.set('saved',{id:'saved',updatedAt:'',messages:[]});
  client.connect('saved','run','smart_reasoning');
  const run = client.pending.get('saved');
  await client.refreshSessions();
  await client.open('saved');
  client.receive('saved',run,{type:'run.started',payload:{turn_id:'turn'}});
  client.receive('saved',run,{type:'tool.finished',payload:{tool_call_id:'c',name:'read_document',ok:true,summary:'返回 2 个片段'}});
  const current = client.rows.get('saved');
  assert.equal(run.row,current);
  assert.equal(current.messages.filter(message => message.role==='tool').length,1);
  assert.equal(current.messages.find(message => message.role==='tool').tool_summary,'返回 2 个片段');
  client.receive('saved',run,{type:'answer.finalized',payload:{message_id:'final',text:'依据资料',citations:[],status:'complete'}});
  assert.equal(current.messages.at(-1).content,'依据资料');
});

test('模拟DOM主页面默认智能推理调用正式会话与run接口，SSE去重并替换最终正文', async () => {
  const app = quickApp(); await new Promise(setImmediate);
  assert.equal(app.element('[data-chat-mode]').value, 'smart_reasoning');
  await app.home('安装要求');
  const request = app.apiRequests.find(r => r.url.endsWith('/runs'));
  assert.deepEqual(JSON.parse(request.options.body), {query: '安装要求', mode: 'smart_reasoning', kb_ids: ['kb'], agent_id: null});
  assert.equal(request.options.headers['Content-Type'], 'application/json');
  assert.equal(app.apiRequests.some(r => r.url === '/api/chat'), false);
  const stream = app.streams[0];
  const emit = (seq,type,payload,run_id='run-quick-1') => stream.emit({seq,type,payload,run_id});
  emit(1,'model.delta',{text:'错误会话'},'other-run');
  emit(1,'model.delta',{text:'<script>临时正文</script>'});
  emit(1,'model.delta',{text:'重复增量'});
  assert.match(app.element('[data-message-index="1"]').innerHTML, /&lt;script&gt;临时正文&lt;\/script&gt;/);
  assert.doesNotMatch(app.element('[data-message-index="1"]').innerHTML, /<script>|重复增量|错误会话/);
  emit(2,'answer.finalized',{message_id:'answer',text:'校验后的全文',citations:[],status:'complete'});
  emit(3,'run.completed',{});
  assert.equal(app.element('[data-message-index="1"]').innerHTML, '<p>校验后的全文</p>\n');
  assert.equal(app.element('#chat-input').disabled, false);
  assert.deepEqual(app.state().sessions, [], '快速历史不写到普通对话localStorage');
});

test('快速Markdown跨片段渲染表格代码并安全替换最终回答', async () => {
  const app=quickApp();await new Promise(setImmediate);await app.home('格式展示');
  const stream=app.streams[0];
  const emit=(seq,type,payload)=>stream.emit({run_id:'run-quick-1',seq,type,payload});
  emit(1,'model.delta',{text:'**加'});
  emit(2,'model.delta',{text:'粗**\n\n```js\nconst text = "<script>";\n```\n\n| 项目 |\n| --- |\n| 内容 |\n\n<img src=x onerror=alert(1)>\n\n[恶意](javascript:alert%281%29)'});
  const node=app.element('[data-message-index="1"]');
  assert.match(node.innerHTML,/<strong>加粗<\/strong>/);
  assert.match(node.innerHTML,/<pre><code class="language-js">/);
  assert.match(node.innerHTML,/<table>/);
  assert.doesNotMatch(node.innerHTML,/<(?:script|img)\b|href="javascript:/i);
  assert.match(node.innerHTML,/&lt;img/);
  emit(3,'answer.finalized',{message_id:'answer',text:'## 最终回答\n\n- **已校验**\n- 第二项[s1]',citations:[],status:'complete'});
  emit(4,'run.completed',{});
  assert.match(node.innerHTML,/<h2>最终回答<\/h2>/);
  assert.match(node.innerHTML,/<ul>/);
  assert.match(node.innerHTML,/<strong>已校验<\/strong>/);
  assert.match(node.innerHTML,/\[s1\]/);
  assert.doesNotMatch(node.innerHTML,/<table>|const text/);
});

test('模拟DOM切换新对话后旧快速SSE不会写入当前会话', async () => {
  const app = quickApp();await new Promise(setImmediate);
  await app.home('问题A');const a=app.streams[0];
  app.action('new-chat');await app.home('问题B');const b=app.streams[1];
  b.emit({run_id:'run-quick-2',seq:1,type:'model.delta',payload:{text:'回答B'}});
  a.emit({run_id:'run-quick-1',seq:1,type:'model.delta',payload:{text:'回答A'}});
  assert.equal(app.element('[data-message-index="1"]').innerHTML,'<p>回答B</p>\n');
  for (const [stream,id,text] of [[a,'run-quick-1','回答A'],[b,'run-quick-2','回答B']]) {
    stream.emit({run_id:id,seq:2,type:'answer.finalized',payload:{message_id:id,text,citations:[],status:'complete'}});
    stream.emit({run_id:id,seq:3,type:'run.completed',payload:{}});
  }
});

test('模拟DOM刷新主页面从SQL历史显示来源，来源正文仅通过鉴权片段接口加载', async () => {
  const card={run_id:'run',source_id:'s1',chunk_id:'chunk',document_id:'document',original_name:'SLA.md',page_start:null,page_end:null,excerpt:'准确来源正文'};
  const app=quickApp({hash:'#session=saved',rows:[{id:'saved',title:'资料问答',updated_at:new Date().toISOString()}],
    messages:[{id:'u',role:'user',content:'问题',status:'complete'},{id:'a',role:'assistant',content:'**已保存答案**',status:'complete',citations:[card]}]});
  await new Promise(setImmediate);
  assert.equal(app.element('[data-message-index="1"]').innerHTML,'<p><strong>已保存答案</strong></p>\n');
  assert.match(app.element('#content-view').innerHTML,/class="quick-text markdown-body"/);
  const button=app.element('[data-sources-index="1"]').children[0];assert.match(button.textContent,/SLA.md.*无页码/);
  await button.listeners.click();
  assert.equal(app.element('#source-dialog').open,true);
  assert.equal(app.element('#source-text').textContent,'准确来源正文');
  assert.match(app.element('#source-meta').textContent,/现行.*原文第2–8行/);
  assert.ok(app.apiRequests.some(r=>r.url==='/api/chunks/chunk'));
});

test('模拟DOM快速失败显示终态、恢复输入且晚到事件被忽略', async () => {
  const app=quickApp();await new Promise(setImmediate);await app.home('问题');const stream=app.streams[0];
  stream.emit({run_id:'run-quick-1',seq:1,type:'answer.finalized',payload:{message_id:'partial',text:'部分正文',citations:[],status:'partial'}});
  stream.emit({run_id:'run-quick-1',seq:2,type:'run.failed',payload:{partial_text:'不应覆盖的终态正文',message:'重排失败'}});
  stream.emit({run_id:'run-quick-1',seq:3,type:'model.delta',payload:{text:'晚到污染'}});
  assert.equal(app.element('[data-message-index="1"]').innerHTML,'<p>部分正文</p>\n');
  assert.match(app.element('#quick-status').textContent,/重排失败/);
  assert.equal(app.element('#chat-input').disabled,false);
  assert.doesNotMatch(app.element('.chat-thread').innerHTML,/本条回答未完成/);
});

for (const message of ['问题改写结果不符合三意图协议', '模型输出达到长度上限（8192 tokens），回答未完成']) {
  test(`快速问答空正文失败只显示具体原因：${message}`, async () => {
    const app = quickApp(); await new Promise(setImmediate); await app.home('问题');
    const stream = app.streams[0];
    stream.emit({run_id:'run-quick-1',seq:1,type:'answer.finalized',payload:{message_id:'failed',text:'',citations:[],status:'failed'}});
    stream.emit({run_id:'run-quick-1',seq:2,type:'run.failed',payload:{message}});
    assert.equal(app.element('[data-message-index="1"]').innerHTML, '');
    assert.match(app.element('.chat-thread').innerHTML, /class="chat-error" role="status"/);
    assert.ok(app.element('.chat-thread').innerHTML.includes(message));
    assert.doesNotMatch(app.element('.chat-thread').innerHTML, /回复未完成。|本条回答未完成/);
    assert.equal(app.element('#chat-input').disabled, false);
  });
}

test('重新加载未完成历史时保留状态提示', async () => {
  const app = quickApp({hash:'#session=saved',rows:[{id:'saved',updated_at:new Date().toISOString()}],
    messages:[{id:'partial',role:'assistant',content:'部分正文',status:'partial',citations:[]},
      {id:'failed',role:'assistant',content:'',status:'failed',citations:[]}]});
  await new Promise(setImmediate);
  assert.match(app.element('.chat-thread').innerHTML, /部分正文/);
  assert.equal((app.element('.chat-thread').innerHTML.match(/本条回答未完成/g) || []).length, 1);
  assert.equal((app.element('.chat-thread').innerHTML.match(/回复未完成。/g) || []).length, 1);
});

test('模拟DOM删除后的历史来源展示快照并关闭下载入口', async () => {
  const card={run_id:'run',source_id:'s1',original_name:'资料.md'};
  const app=quickApp({hash:'#session=saved',rows:[{id:'saved',updated_at:new Date().toISOString()}],
    snapshot:{run_id:'run',handle:'s1',chunk_id:null,document_id:null,readable:false,original_deleted:true,
      source_snapshot:{original_name:'资料.md',excerpt:'仅历史必要片段',page_start:null}},
    messages:[{id:'a',role:'assistant',content:'历史依据[s1]',citations:[card],status:'complete'}]});
  await new Promise(setImmediate);
  await app.element('[data-sources-index="0"]').children[0].listeners.click();
  assert.equal(app.element('#source-text').textContent,'仅历史必要片段');
  assert.equal(app.element('#source-meta').textContent,'原文已删除，仅历史快照');
  assert.equal(app.element('#source-download').hidden,true);
  assert.equal(app.apiRequests.some(request=>request.url.startsWith('/api/chunks/')),false);
});

test('模拟DOM旧消息无run来源映射时只展示原文', async () => {
  const app=quickApp({hash:'#session=saved',rows:[{id:'saved',updated_at:new Date().toISOString()}],
    messages:[{id:'a',role:'assistant',content:'旧正文[s1]',citations:[{source_id:'s1',chunk_id:'old',original_name:'旧资料.md'}],status:'complete'}]});
  await new Promise(setImmediate);
  assert.equal(app.element('[data-sources-index="0"]').children.length,0);
  assert.match(app.element('[data-message-index="0"]').innerHTML,/旧正文\[s1\]/);
});

test('模拟DOM普通模式继续旧聊天接口和本地历史，快速SQL历史仍独立', async () => {
  const app=quickApp({rows:[{id:'saved',title:'快速历史',updated_at:new Date().toISOString()}]});await new Promise(setImmediate);
  app.mode('plain_chat');app.reply(()=>new Response(encode({type:'done'})));await app.home('普通问题');
  assert.equal(app.requests[0].url,'/api/chat');assert.equal(app.state().sessions.length,1);
  assert.equal(app.apiRequests.some(r=>r.url.endsWith('/runs')),false);
  app.action('open-session','saved');await new Promise(setImmediate);
  assert.equal(app.element('[data-chat-mode]').value,'quick_answer');
  assert.equal(app.state().sessions.length,1);
});

test('首页发起请求，中文跨字节分块实时显示，重复发送被阻止，完成后保存上下文', async () => {
  const app = createApp();
  let controller;
  app.reply(() => new Response(new ReadableStream({ start(value) { controller = value; } })));
  const sending = app.home('第一个问题');
  assert.equal(app.element('#chat-input').disabled, true);
  assert.equal(app.element('#chat-form .chat-send').disabled, false);
  assert.equal(app.element('#chat-form .chat-send').type, 'button');
  assert.equal(app.element('#chat-form .chat-send').getAttribute('aria-label'), '打断生成');
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].url, '/api/chat');
  assert.deepEqual(app.requests[0].messages, [{ role: 'user', content: '第一个问题' }]);
  await app.send('重复问题');
  assert.equal(app.requests.length, 1);
  const node = app.element('[data-message-index="1"]');
  const firstUpdate = new Promise(resolve => { node.onHTML = resolve; });
  const firstText = '你好\n**第一段**\n<script>1</script>';
  for (const byte of encode({ type: 'content', content: firstText })) controller.enqueue(Uint8Array.of(byte));
  await firstUpdate;
  const firstHTML = node.innerHTML;
  assert.match(firstHTML, /<strong>第一段<\/strong>/);
  assert.match(firstHTML, /&lt;script&gt;1&lt;\/script&gt;/);
  assert.doesNotMatch(firstHTML, /<script>/);
  assert.equal(app.state().sessions[0].messages[1].content, '');
  controller.enqueue(encode({ type: 'content', content: '，第二段。' }));
  controller.enqueue(encode({ type: 'done' }));
  controller.close();
  await sending;
  const answer = firstText + '，第二段。';
  assert.equal(app.state().sessions[0].messages[1].content, answer);
  assert.equal(app.element('#chat-input').disabled, false);
  assert.equal(app.element('#chat-form .chat-send').disabled, false);
  assert.match(app.element('#content-view').innerHTML, /&lt;script&gt;1&lt;\/script&gt;/);
  app.reply(() => new Response(encode({ type: 'done' })));
  await app.send('继续问题');
  assert.deepEqual(app.requests[1].messages, [{ role: 'user', content: '第一个问题' }, { role: 'assistant', content: answer }, { role: 'user', content: '继续问题' }]);
});

for (const [name, terminal, expectedError] of [
  ['服务端错误', { type: 'error', message: '服务请求超时' }, '服务请求超时'],
  ['流未正常结束', null, '回复连接已中断']
]) {
  test(`${name}保留部分回复、显示错误并恢复发送`, async () => {
    const app = createApp();
    const chunks = [encode({ type: 'content', content: '部分回复' })];
    if (terminal) chunks.push(encode(terminal));
    app.reply(() => new Response(new ReadableStream({ start(controller) { chunks.forEach(chunk => controller.enqueue(chunk)); controller.close(); } })));
    await app.home('问题');
    const reply = app.state().sessions[0].messages[1];
    assert.equal(reply.content, '部分回复');
    assert.match(reply.error, new RegExp(expectedError));
    assert.equal(app.element('#chat-input').disabled, false);
    assert.equal(app.element('#chat-form .chat-send').disabled, false);
    app.reply(() => new Response(encode({ type: 'done' })));
    await app.send('再次提问');
    assert.deepEqual(app.requests[1].messages, [{ role: 'user', content: '问题' }, { role: 'user', content: '再次提问' }]);
  });
}

test('普通对话打断保留正文和阅读位置，刷新后可继续使用上下文', async () => {
  const app = createApp();
  let stream, cancelled = false;
  app.reply(() => new Response(new ReadableStream({start(controller) {stream = controller;}, cancel() {cancelled = true;}})));
  const sending = app.home('列出步骤');
  stream.enqueue(encode({type:'content', content:'第一点：已输出的正文。'}));
  await new Promise(setImmediate);
  const thread = app.element('.chat-thread');
  thread.scrollTop = 600;
  const paragraph = app.element('[data-message-index="1"]').firstChild;
  await app.action('stop-chat');
  await sending;
  const saved = app.state().sessions[0];
  assert.equal(cancelled, true);
  assert.equal(saved.messages[1].content, '第一点：已输出的正文。');
  assert.equal(saved.messages[1].status, 'partial');
  assert.equal(saved.messages[1].error, undefined);
  assert.equal(app.element('.chat-thread'), thread);
  assert.equal(thread.scrollTop, 600);
  assert.equal(app.element('[data-message-index="1"]').firstChild, paragraph);
  assert.equal(app.element('#chat-input').disabled, false);
  assert.equal(app.element('#chat-form .chat-send').type, 'submit');
  assert.match(app.element('#chat-form .chat-send').innerHTML, /#i-arrow/);
  assert.equal(app.apiRequests.find(request => request.url === '/api/chat').options.signal.aborted, true);
  const refreshed = createApp([saved]);
  refreshed.action('open-session', saved.id);
  assert.match(refreshed.element('#content-view').innerHTML, /第一点：已输出的正文/);
  refreshed.reply(() => new Response(encode({type:'done'})));
  await refreshed.send('继续');
  assert.deepEqual(refreshed.requests[0].messages, [
    {role:'user',content:'列出步骤'}, {role:'assistant',content:'第一点：已输出的正文。'}, {role:'user',content:'继续'}
  ]);
});

test('普通对话在请求返回前打断，不显示请求错误且可再次发送', async () => {
  const app = createApp();
  let resolve;
  app.reply(() => new Promise(done => {resolve = done;}));
  const sending = app.home('问题');
  app.action('stop-chat');
  resolve(new Response(new ReadableStream({start() {}})));
  await sending;
  assert.equal(app.state().sessions[0].messages[1].stopped, true);
  assert.equal(app.state().sessions[0].messages[1].error, undefined);
  assert.equal(app.element('#chat-input').disabled, false);
  app.reply(() => new Response(encode({type:'done'})));
  await app.send('再次提问');
  assert.deepEqual(app.requests[1].messages, [{role:'user',content:'问题'}, {role:'user',content:'再次提问'}]);
});

for (const mode of ['quick_answer', 'smart_reasoning']) for (const terminalDelivered of [true, false]) {
  test(`${mode}打断保存并重载历史，${terminalDelivered ? 'SSE先结束' : 'SSE丢失终态'}时保持正文和控件`, async () => {
    let resolveStop;
    const messages = [];
    const rows = [];
    const app = quickApp({rows, messages, fetch: url => url.endsWith('/stop')
      ? new Promise(resolve => {resolveStop = resolve;}) : null});
    await new Promise(setImmediate);
    app.mode(mode);
    await app.home('说明步骤');
    const stream = app.streams[0];
    const emit = (seq,type,payload) => stream.emit({run_id:'run-quick-1',seq,type,payload});
    emit(1,'run.started',{mode,turn_id:'user'});
    emit(2,'model.delta',{text:'第一点已输出。'});
    const thread = app.element('.chat-thread');
    thread.scrollTop = 600;
    const paragraph = app.element('[data-message-index="1"]').firstChild;
    const send = app.element('#chat-form .chat-send');
    assert.equal(send.disabled, false);
    assert.equal(send.type, 'button');
    assert.match(send.innerHTML, /#i-stop/);
    app.action('stop-chat');
    app.action('stop-chat');
    assert.equal(app.apiRequests.filter(request => request.url.endsWith('/stop')).length, 1);
    assert.equal(send.disabled, true);
    assert.equal(send.title, '正在停止…');
    await app.send('保存前不能发送');
    assert.equal(app.apiRequests.filter(request => request.url.endsWith('/runs')).length, 1);
    messages.push({id:'user', role:'user',content:'说明步骤',status:'complete',run_id:'run-quick-1'},
      {id:'partial',role:'assistant',content:'第一点已输出。',status:'partial',citations:[],run_id:'run-quick-1'});
    if (terminalDelivered) {
      emit(3,'answer.finalized',{message_id:'partial',text:'第一点已输出。',citations:[],status:'partial'});
      emit(4,'run.failed',{code:'cancelled',message:'运行已取消'});
      assert.equal(send.disabled, true);
    } else {
      stream.readyState = 2;
      stream.onerror();
      assert.equal(send.disabled, true);
    }
    resolveStop(new Response(null,{status:204}));
    await new Promise(setImmediate);
    assert.equal(stream.readyState, 2);
    assert.equal(send.disabled, false);
    assert.equal(send.type, 'submit');
    assert.equal(app.element('#chat-input').disabled, false);
    assert.equal(app.element('.chat-thread'), thread);
    assert.equal(thread.scrollTop, 600);
    assert.equal(app.element('[data-message-index="1"]').firstChild, paragraph);
    assert.match(app.element('#quick-status').textContent, /已停止/);
    assert.doesNotMatch(app.element('#content-view').innerHTML, /运行已取消/);
    emit(5,'model.delta',{text:'晚到的旧运行正文'});
    assert.doesNotMatch(app.element('#content-view').innerHTML, /晚到/);
    const refreshed = quickApp({rows,messages,hash:`#session=quick-1&mode=${mode}`});
    await new Promise(setImmediate);
    assert.match(refreshed.element('#content-view').innerHTML, /第一点已输出/);
    await app.send('继续');
    assert.equal(app.apiRequests.filter(request => request.url.endsWith('/runs')).length, 2);
  });
}

test('停止请求失败保留当前流和打断入口，保存失败不显示保存成功', async () => {
  const app = quickApp({fetch: url => url.endsWith('/stop') ? new Response(JSON.stringify({
    detail:{code:'message_persistence_failed',message:'消息未能保存'}
  }),{status:503}) : null});
  await new Promise(setImmediate);
  await app.home('问题');
  app.action('stop-chat');
  await new Promise(setImmediate);
  assert.equal(app.streams[0].readyState, 1);
  assert.equal(app.element('#chat-form .chat-send').disabled, false);
  assert.equal(app.element('#chat-input').disabled, true);
  assert.match(app.element('#quick-status').textContent, /消息未能保存/);
  assert.doesNotMatch(app.element('#quick-status').textContent, /已保存/);
});

test('打断目标会话后切换新会话，保存结果不会改写新会话或释放其控件', async () => {
  let resolveStop;
  const messages = [];
  const app = quickApp({messages, fetch: url => url.endsWith('/stop')
    ? new Promise(resolve => {resolveStop = resolve;}) : null});
  await new Promise(setImmediate);
  await app.home('问题A');
  app.streams[0].emit({run_id:'run-quick-1',seq:1,type:'model.delta',payload:{text:'回答A'}});
  app.action('stop-chat');
  app.action('new-chat');
  await app.home('问题B');
  app.streams[1].emit({run_id:'run-quick-2',seq:1,type:'model.delta',payload:{text:'回答B'}});
  messages.push({id:'a',role:'assistant',content:'回答A',run_id:'run-quick-1',status:'partial',citations:[]});
  resolveStop(new Response(null,{status:204}));
  await new Promise(setImmediate);
  assert.match(app.element('#content-view').innerHTML, /回答B/);
  assert.doesNotMatch(app.element('#content-view').innerHTML, /回答A/);
  assert.equal(app.element('#chat-input').disabled, true);
  assert.equal(app.element('#chat-form .chat-send').type, 'button');
});

test('HTTP 失败显示错误并恢复输入', async () => {
  const app = createApp();
  app.reply(() => new Response('failed', { status: 503 }));
  await app.home('问题');
  assert.match(app.state().sessions[0].messages[1].error, /503/);
  assert.equal(app.element('#chat-input').disabled, false);
  assert.equal(app.element('#chat-form .chat-send').disabled, false);
});

test('切换会话时流式回复不会写入当前新会话，旧记录可以继续提问', async () => {
  const app = createApp([{ id: 'legacy', title: '早期问题', started: true, updatedAt: new Date().toISOString() }]);
  app.action('open-session', 'legacy');
  let controller;
  app.reply(() => new Response(new ReadableStream({ start(value) { controller = value; } })));
  const sending = app.send('继续');
  assert.deepEqual(app.requests[0].messages, [{ role: 'user', content: '早期问题' }, { role: 'user', content: '继续' }]);
  app.action('new-chat');
  const view = app.element('#content-view').innerHTML;
  controller.enqueue(encode({ type: 'content', content: '旧会话回复' }));
  controller.enqueue(encode({ type: 'done' }));
  controller.close();
  await sending;
  assert.equal(app.element('#content-view').innerHTML, view);
  app.action('open-session', 'legacy');
  assert.match(app.element('#content-view').innerHTML, /旧会话回复/);
});

test('纯对话页面与输入框显示当前服务端模型', async () => {
  const app = createApp([{ id: 'existing', title: '问题', started: true, messages: [{ role: 'user', content: '问题' }], updatedAt: new Date().toISOString() }]);
  await new Promise(setImmediate);
  app.action('open-session', 'existing');
  const view = app.element('#content-view').innerHTML;
  assert.match(view, /<textarea/);
  assert.match(view, /composer-footer/);
  assert.match(view, /provider\/current-model/);
  assert.doesNotMatch(view, /page-heading|eyebrow|heading-actions|data-action="search"|Knowledge Base 知识助手/);
  assert.equal(app.element('.window-bar').hidden, true);
  assert.equal(app.element('.ambient-background').hidden, false);
  app.action('open-knowledge');
  assert.equal(app.element('.window-bar').hidden, false);
});

test('新对话回到初始首页，首次发送才创建独立会话', async () => {
  const session = { id: 'existing', title: '之前的问题', started: true, messages: [{ role: 'user', content: '之前的问题' }, { role: 'assistant', content: '之前的回复' }], updatedAt: new Date().toISOString() };
  const app = createApp([session]);
  app.action('open-session', 'existing');
  app.element('#home-search').value = '未发送的草稿';
  app.action('new-chat');
  assert.equal(app.element('#home-view').hidden, false);
  assert.equal(app.element('[data-chat-mode]').value, 'smart_reasoning');
  assert.equal(app.element('#content-view').hidden, true);
  assert.equal(app.element('#home-search').value, '');
  assert.equal(app.element('.ambient-background').hidden, false);
  assert.deepEqual(app.state().sessions, [session]);
  app.action('new-chat');
  assert.deepEqual(app.state().sessions, [session]);
  app.reply(() => new Response(encode({ type: 'done' })));
  await app.home('新的问题');
  assert.deepEqual(app.requests[0].messages, [{ role: 'user', content: '新的问题' }]);
  assert.equal(app.state().sessions.length, 2);
  assert.deepEqual(app.state().sessions.find(item => item.id === 'existing'), session);
});

test('首次打开知识库呈现真实空列表', () => {
  const app = createApp([], { fresh: true });
  app.action('open-knowledge');
  const view = app.element('#content-view').innerHTML;
  assert.match(view, /暂无知识库/);
  assert.doesNotMatch(view, /library-card/);
});

test('原本地知识库保留，不自动迁移到服务器', () => {
  const libraries = ['work', 'design', 'tech', 'growth', 'user-created'].map(id => ({ id, name: id, description: '', type: 'document' }));
  const documents = [{ id: 'document-1', libraryId: 'tech', content: '真实内容' }];
  const app = createApp([], { libraries, documents });
  assert.deepEqual(app.state().libraries.map(library => library.id), ['work', 'design', 'tech', 'growth', 'user-created']);
  assert.deepEqual(app.state().documents, documents);
});

test('输入框 Enter 发送，Shift+Enter 与中文输入确认保持正常输入', () => {
  const app = createApp();
  let submits = 0;
  let prevented = 0;
  const event = { key: 'Enter', target: { id: 'chat-input', form: { requestSubmit() { submits++; } } }, preventDefault() { prevented++; } };
  app.key({ ...event, shiftKey: true });
  app.key({ ...event, isComposing: true });
  assert.equal(submits, 0);
  app.key(event);
  assert.equal(submits, 1);
  assert.equal(prevented, 1);
});

function markdownSession(content) {
  return { id: 'markdown', title: '格式展示', started: true, messages: [{ role: 'user', content: '**用户原文**' }, { role: 'assistant', content }], updatedAt: new Date().toISOString() };
}

test('历史回复渲染常用 Markdown，用户输入及存储保留原文', () => {
  const content = '# 标题\n\n**加粗**、*斜体*、~~删除线~~和 `行内代码`\n\n- 项目一\n  - 子项目\n\n1. 步骤一\n2. 步骤二\n\n> 引用\n\n---\n\n```python\nprint("<hello>")\n```\n\n| 名称 | 数量 |\n| --- | ---: |\n| 内容 | 2 |\n\n[链接](https://example.com "说明")\n\n![图片](https://example.com/image.png)';
  const session = markdownSession(content);
  const app = createApp([session]);
  app.action('open-session', session.id);
  const html = app.element('#content-view').innerHTML;
  for (const pattern of [/<h1>标题<\/h1>/, /<strong>加粗<\/strong>/, /<em>斜体<\/em>/, /<del>删除线<\/del>/, /<code>行内代码<\/code>/, /<ul>/, /<ol>/, /<blockquote>/, /<hr>/, /<pre><code class="language-python">/, /&lt;hello&gt;/, /<table>/, /<th/, /<td/, /href="https:\/\/example.com\/"/, /rel="noopener noreferrer"/, /<img src="https:\/\/example.com\/image.png"/]) assert.match(html, pattern);
  assert.match(html, /class="chat-message user" data-chat-key="\d+">\*\*用户原文\*\*/);
  assert.deepEqual(app.state().sessions[0], session);
});

test('原始 HTML、脚本、事件属性、危险链接与图片不会生成可执行标记', () => {
  const content = '<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\ninline <svg onload=alert(1)>x</svg>\n\n<script>raw < notatag onerror=alert(1)</script>\n\n[脚本](javascript:alert%281%29) [实体](javascript&#58;alert%281%29) [数据](data:text/html,test) [控制字符](java\tscript:alert%281%29)\n\n![恶意图片](javascript:alert%281%29)\n\n[属性](https://example.com "\" onmouseover=alert(1)")';
  const app = createApp([markdownSession(content)]);
  app.action('open-session', 'markdown');
  const html = app.element('#content-view').innerHTML.match(/class="markdown-body" data-message-index="1">([\s\S]*?)<\/div>/)[1];
  assert.doesNotMatch(html, /<(?:script|img|svg|iframe)\b/i);
  assert.doesNotMatch(html, /(?:href|src)="(?:javascript|data|vbscript):/i);
  assert.doesNotMatch(html, /["']\s+on\w+=/i);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /raw &lt; notatag/);
  assert.match(html, /脚本/);
});

test('流式跨片段 Markdown、未闭合代码块和表格实时更新，历史重开结果一致', async () => {
  const app = createApp();
  let controller;
  app.reply(() => new Response(new ReadableStream({ start(value) { controller = value; } })));
  const sending = app.home('展示 Markdown');
  const node = app.element('[data-message-index="1"]');
  const chunks = ['**加', '粗**\n\n```js\nconst text = "<script>";', '\n```\n\n| 名称 |\n| --- |\n| 值 |\n\n[引用][1]', '\n\n[1]: https://example.com'];
  let raw = '';
  let html;
  for (const [index, chunk] of chunks.entries()) {
    const update = new Promise(resolve => { node.onHTML = resolve; });
    controller.enqueue(encode({ type: 'content', content: chunk }));
    raw += chunk;
    await update;
    html = node.innerHTML;
    assert.doesNotMatch(html, /<script>/);
    if (index >= 1) { assert.match(html, /<strong>加粗<\/strong>/); assert.match(html, /<pre><code class="language-js">/); }
    if (index >= 2) assert.match(html, /<table>/);
  }
  assert.match(html, /href="https:\/\/example.com\/"/);
  controller.enqueue(encode({ type: 'done' }));
  controller.close();
  await sending;
  const session = app.state().sessions[0];
  assert.equal(session.messages[1].content, raw);
  const restored = createApp([session]);
  restored.action('open-session', session.id);
  assert.ok(restored.element('#content-view').innerHTML.includes(html));
  app.reply(() => new Response(encode({ type: 'done' })));
  await app.send('继续');
  assert.equal(app.requests[1].messages[1].content, raw);
});

test('本地 Markdown 脚本先于页面脚本加载', () => {
  const html = readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
  assert.ok(html.indexOf('/static/vendor/marked/marked.umd.js') < html.indexOf('/static/app.js'));
  assert.doesNotMatch(html, /<script[^>]+src="https?:/);
});


test('未登录启动只显示登录页，不读取业务知识库', async () => {
  const app = createApp([], { meStatus: 401 });
  await new Promise(setImmediate);
  assert.equal(app.element('#login-view').hidden, false);
  assert.equal(app.element('.app-shell').hidden, true);
  assert.deepEqual(app.apiRequests.map(r => r.url), ['/api/auth/me']);
});

test('知识库列表以服务端为准，名称安全转义，旧本地资料仍保留', async () => {
  const old = [{ id: 'old', name: '旧本地库' }];
  const app = createApp([], { libraries: old, serverLibraries: [{ id: 'server', name: '<img onerror=alert(1)>企业库', description: '来自服务端', status: 'active', created_at: '2026-10-03T00:00:00Z', updated_at: '2026-10-03T00:00:00Z' }] });
  await new Promise(setImmediate);
  app.action('open-knowledge');
  const view = app.element('#content-view').innerHTML;
  assert.match(view, /来自服务端/);
  assert.match(view, /&lt;img onerror=alert\(1\)&gt;企业库/);
  assert.doesNotMatch(view, /旧本地库|<img onerror/);
  assert.deepEqual(app.state().libraries, old);
  assert.equal(app.apiRequests.find(r => r.url === '/api/knowledge-bases').options.credentials, 'same-origin');
});

for (const phase of ['首次列表', '既有轮询']) for (const terminal of ['ready', '401', '404', 'leave']) {
  test(`模拟DOM${phase}临时失败后2秒续查，${terminal}后停止`, async () => {
    const timers = new Map();
    let timerId = 0;
    const library = { id: 'server', name: '测试库', status: 'active' };
    const document = { id: 'doc', kb_id: 'server', original_name: 'probe.md', parse_status: 'queued', stage: 'parse', size_bytes: 12, updated_at: '2026-10-03T00:00:00Z' };
    let reply = () => phase === '首次列表' ? new Response('{}', { status: 503 }) : new Response(JSON.stringify([document]));
    const app = createApp([], {
      serverLibraries: [library],
      setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
      clearTimeout: id => timers.delete(id),
      fetch: url => url.endsWith('/documents') ? reply() : null
    });
    const takePoll = () => {
      const entry = [...timers].find(([, item]) => item.delay === 2000);
      assert.ok(entry, '应安排2秒后再次查询');
      timers.delete(entry[0]);
      return entry[1].callback;
    };
    const polls = () => [...timers.values()].filter(item => item.delay === 2000);
    await new Promise(setImmediate);
    app.action('open-library', 'server');
    await new Promise(setImmediate);
    if (phase === '既有轮询') {
      reply = () => new Response('{}', { status: 503 });
      await takePoll()();
    }
    assert.equal(polls().length, 1);
    if (terminal === 'leave') {
      let finish;
      reply = () => new Promise(resolve => { finish = resolve; });
      const pending = takePoll()();
      app.action('new-chat');
      finish(new Response('{}', { status: 503 }));
      await pending;
    } else {
      if (terminal === 'ready') {
        reply = () => new Response(JSON.stringify([document]));
        await takePoll()();
        assert.equal(polls().length, 1, '首次恢复后待处理文档继续轮询');
      }
      reply = () => ['401', '404'].includes(terminal) ? new Response('{}', { status: Number(terminal) }) : new Response(JSON.stringify([{ ...document, parse_status: 'ready', stage: 'done' }]));
      await takePoll()();
      if (terminal === '401') assert.equal(app.element('#login-view').hidden, false);
      else if (terminal === 'ready') assert.match(app.element('#content-view').innerHTML, /可检索/);
    }
    assert.equal(polls().length, 0);
  });
}
