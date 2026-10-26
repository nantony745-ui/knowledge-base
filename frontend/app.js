(() => {
  'use strict';

  const STORAGE_KEY = 'weknora-aaa-knowledge-space-v1';
  const $ = selector => document.querySelector(selector);
  const escape = value => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
  const markdown = new marked.Marked({
    gfm: true,
    breaks: true,
    renderer: {
      html: ({ text }) => escape(text),
      text(token) {
        return token.escaped ? escape(token.text) : false;
      },
      link({ href, title, text, tokens, autolink }) {
        const label = autolink ? escape(text) : this.parser.parseInline(tokens);
        const url = markdownUrl(href, ['http:', 'https:', 'mailto:']);
        return url ? `<a href="${escape(url)}"${title ? ` title="${escape(title)}"` : ''} target="_blank" rel="noopener noreferrer">${label}</a>` : label;
      },
      image({ href, title, text }) {
        const url = markdownUrl(href, ['http:', 'https:']);
        return url ? `<img src="${escape(url)}" alt="${escape(text)}"${title ? ` title="${escape(title)}"` : ''} loading="lazy">` : escape(text);
      }
    }
  });

  function markdownUrl(value, protocols) {
    try {
      const url = new URL(value);
      return protocols.includes(url.protocol) ? url.href : null;
    } catch {
      return null;
    }
  }
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const button = (label, action, symbol, className = '', extra = '') => `<button class="button ${className}" data-action="${action}" ${extra}>${symbol ? icon(symbol) : ''}${label}</button>`;
  const initialState = {
    libraries: [],
    documents: [],
    activities: [],
    sessions: [],
    settings: { dense: false }
  };
  let state = structuredClone(initialState);
  let storageAvailable = true;
  let serverLibraries = [];
  let serverDocuments = [];
  let pollTimer;
  let semanticResults = [];
  let semanticQuery = '';
  let signedIn = false;
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) state = JSON.parse(saved);
  } catch (error) {
    storageAvailable = false;
    console.warn('本地知识空间存储不可用：', error.message);
  }
  if (!Array.isArray(state.sessions)) state.sessions = [];
  if (!Array.isArray(state.libraries)) state.libraries = [];
  if (!Array.isArray(state.documents)) state.documents = [];
  if (!Array.isArray(state.activities)) state.activities = [];
  state.sessions.forEach(session => {
    if (!Array.isArray(session.messages)) session.messages = session.started ? [{ role: 'user', content: session.title }] : [];
  });
  const pendingSessions = new Map();
  const chatScrollStates = new WeakMap();
  const chatMessageKeys = new WeakMap();
  const chatSourceCards = new WeakMap();
  let nextChatMessageKey = 0;
  let currentPage = 'home';
  let currentLibrary = null;
  let currentDocument = null;
  let currentSession = null;
  let documentFilter = 'all';
  let searchFilter = 'all';
  let searchResults = [];
  let selectedResult = -1;
  let toastTimer;
  let modelName = '正在加载…';
  let chatMode = 'smart_reasoning';
  let pageVersion = 0;
  let kbSelectionInitialized = false;
  const selectedKbIds = new Set();
  const quick = new window.QuickAnswerClient({
    api: (url, options) => api(url, {...options, headers: {'Content-Type': 'application/json', ...options?.headers}}),
    onChange: quickChanged, EventSourceClass: window.EventSource, storage: sessionStorage
  });

  const activeLibraries = () => serverLibraries;
  const serverMode = mode => mode === 'quick_answer' || mode === 'smart_reasoning';
  const libraryById = id => serverLibraries.find(library => library.id === id);
  const documentById = id => serverDocuments.find(document => document.id === id);
  const activeSessions = () => [...state.sessions.filter(session => !session.deleted).map(session => ({...session, mode: 'plain_chat'})),
    ...quick.sessions.map(session => ({...session, mode: session.mode || 'quick_answer'}))].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  const sessionById = id => serverMode(chatMode) ? quick.rows.get(id) : state.sessions.find(session => session.id === id);
  const visibleDocuments = () => serverDocuments;
  const dateLabel = value => new Date(value).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
  const typeLabel = type => ({ file: '文档', note: '笔记', faq: '问答' })[type];
  const documentIcon = type => ({ file: 'file', note: 'note', faq: 'chat' })[type];
  const plainSearch = value => value.toLocaleLowerCase();

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
      storageAvailable = true;
      return true;
    } catch (error) {
      storageAvailable = false;
      toast('浏览器存储失败，本次改动仅在当前页面保留。');
      console.warn('本地知识空间保存失败：', error.message);
      return false;
    }
  }

  function toast(message) {
    clearTimeout(toastTimer);
    $('#toast').textContent = message;
    $('#toast').hidden = false;
    toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 3300);
  }

  function recordActivity(message) {
    state.activities.unshift({ message, time: new Date().toISOString() });
    state.activities = state.activities.slice(0, 30);
  }

  function closeDialogs() {
    document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  }

  function setPage(page, id) {
    pageVersion++;
    clearTimeout(pollTimer);
    currentPage = page;
    currentLibrary = page === 'library' ? id : null;
    currentDocument = page === 'document' ? id : null;
    currentSession = page === 'chat' ? id : null;
    window.history.replaceState(null, '', page === 'chat' && serverMode(chatMode)
      ? `/#session=${id}${chatMode === 'smart_reasoning' ? '&mode=smart_reasoning' : ''}` : '/');
    documentFilter = 'all';
    closeDialogs();
    render();
    window.scrollTo({ top: 0 });
    if (window.innerWidth <= 600) document.body.classList.add('collapsed');
  }

  function startNewChat() {
    chatMode = 'smart_reasoning';
    $('#home-search').value = '';
    setPage('home');
    $('#home-search').focus();
  }

  function sessionBucket(value) {
    const dayStart = date => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    const days = Math.round((dayStart(new Date()) - dayStart(new Date(value))) / 86400000);
    if (days <= 0) return 'today';
    if (days === 1) return 'yesterday';
    if (days <= 7) return 'week';
    return 'earlier';
  }

  function renderHistory() {
    const sessions = activeSessions();
    $('#history-count').textContent = sessions.length || '';
    const list = $('#history-list');
    if (!sessions.length) {
      list.innerHTML = '<p class="history-empty">还没有对话记录。点击「新对话」开始。</p>';
      return;
    }
    const labels = { today: '今天', yesterday: '昨天', week: '近 7 天', earlier: '更早' };
    let html = '';
    Object.keys(labels).forEach(bucket => {
      const items = sessions.filter(session => sessionBucket(session.updatedAt) === bucket);
      if (!items.length) return;
      html += `<div class="history-group">${labels[bucket]}</div>`;
      html += items.map(session => `<button class="history-item ${currentSession === session.id && chatMode === session.mode ? 'selected' : ''}" data-action="open-session" data-mode="${session.mode}" data-id="${escape(session.id)}" title="${session.mode === 'smart_reasoning' ? '智能推理' : session.mode === 'quick_answer' ? '快速问答' : '普通对话'} · ${escape(session.title)}">${icon('chat')}<span>${escape(session.title)}</span></button>`).join('');
    });
    list.innerHTML = html;
  }

  function renderSidebar() {
    renderHistory();
    document.querySelectorAll('.primary-action[data-action]').forEach(action => {
      const active = (action.dataset.action === 'new-chat' && ['home', 'chat'].includes(currentPage)) || (action.dataset.action === 'open-knowledge' && ['libraries', 'library', 'document'].includes(currentPage));
      action.classList.toggle('active', active);
      if (active) action.setAttribute('aria-current', 'page'); else action.removeAttribute('aria-current');
    });
    document.body.classList.toggle('dense', state.settings.dense);
  }

  function heading(title, description, actions = '') {
    const eyebrow = currentPage === 'chat' ? '对话' : ['library', 'document', 'libraries'].includes(currentPage) ? '知识库' : '我的知识空间';
    return `<header class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${escape(title)}</h1><p class="page-description">${escape(description)}</p></div>${actions ? `<div class="heading-actions">${actions}</div>` : ''}</header>`;
  }

  function emptyState(symbol, title, description, action = '') {
    return `<div class="empty-state">${icon(symbol)}<h2>${escape(title)}</h2><p>${escape(description)}</p>${action}</div>`;
  }

  const statusLabel = status => ({ queued: '排队中', parsing: '解析中', indexing: '索引中', ready: '可检索', failed: '失败', deleting: '清理中' })[status] || status;

  function libraryCard(library) {
    return `<article class="library-card"><div class="card-tools">${button('修改', 'edit-library', 'note', '', `data-id="${escape(library.id)}"`)}${button(library.status === 'deleting' ? '继续删除' : '删除', 'delete-library', 'trash', '', `data-id="${escape(library.id)}"`)}</div><button class="card-open" data-action="open-library" data-id="${escape(library.id)}"><span class="card-icon">${icon('folder')}</span><h2>${escape(library.name)}</h2><p>${escape(library.description || '')}</p><div class="card-footer">${library.status === 'deleting' ? '正在清理' : '文档知识库'}</div></button></article>`;
  }

  function documentRow(item) {
    const resource = `data-id="${escape(item.id)}"`;
    const controls = (item.parse_status === 'failed' ? button('重试', 'retry-document', 'restore', '', resource) : '') +
      (item.parse_status === 'ready' ? button('重新索引', 'reindex-document', 'restore', '', resource) : '') +
      (item.parse_status !== 'deleting' ? `<a class="button" href="/api/documents/${escape(item.id)}/download">下载</a>` : '') +
      button(item.parse_status === 'deleting' ? '继续删除' : '删除', 'delete-document', 'trash', '', resource);
    return `<article class="document-row"><div class="document-symbol file">${icon('file')}</div><button class="document-main" data-action="open-document" ${resource}><strong>${escape(item.title)}</strong><small>${escape(statusLabel(item.parse_status))} · ${escape(item.stage)} · ${dateLabel(item.updatedAt)}</small>${item.error_message ? `<small class="chat-error">${escape(item.error_message)}</small>` : ''}</button><div class="document-actions">${controls}</div></article>`;
  }

  function searchHit(hit) {
    const meta = hit.source_meta;
    const location = (meta.line_ranges || []).map(([a, b]) => a === b ? `第 ${a} 行` : `第 ${a}–${b} 行`).join('、');
    return `<article class="search-hit"><strong>${escape(hit.document_name)}</strong><small>${escape((meta.heading_path || []).join(' / '))} · 无页码 · ${escape(location)} · COSINE ${Number(hit.score).toFixed(4)}</small>${meta.document_status ? `<span class="source-status">${escape(meta.document_status)}</span>` : ''}<pre>${escape(hit.content)}</pre><a href="${escape(hit.download_url)}">下载原文件</a></article>`;
  }

  function libraryView() {
    const library = libraryById(currentLibrary);
    if (!library) return '<p>正在加载知识库…</p>';
    const active = library.status === 'active';
    const actions = button('返回知识库列表', 'open-knowledge', 'book') + (active ? button('上传文档', 'upload', 'upload', 'primary') : button('继续删除', 'delete-library', 'trash', '', `data-id="${escape(library.id)}"`));
    const items = visibleDocuments().filter(item => item.libraryId === library.id);
    const search = active ? `<form id="semantic-search-form" class="semantic-search"><input id="semantic-query" name="query" value="${escape(semanticQuery)}" placeholder="输入问题，检索相关片段" aria-label="语义检索问题" required maxlength="8192"><input name="top_k" type="number" value="5" min="1" max="20" aria-label="结果数量"><button class="button primary" type="submit">语义检索</button></form><div id="semantic-results">${semanticResults.map(searchHit).join('')}</div>` : '<p>知识库正在清理，不能上传或检索。</p>';
    return heading(library.name, library.description || '', actions) + search + (items.length ? `<div class="documents">${items.map(documentRow).join('')}</div>` : emptyState('file', '暂无文档', '上传TXT或MD，每个文件最多20 MiB。'));
  }

  function documentView() {
    const item = documentById(currentDocument);
    if (!item) return '<p>文档不存在或正在加载…</p>';
    return heading(item.title, `${statusLabel(item.parse_status)} · ${item.stage}`, button('返回知识库', 'open-library', 'folder', '', `data-id="${escape(item.libraryId)}"`)) + documentRow(item) + `<div class="detail-meta">无页码 · ${formatSize(item.size_bytes)}</div><pre class="document-content">${escape(item.content || '通过下载入口查看原文件；检索结果提供标题及原文行号。')}</pre>`;
  }

  function conversationView() {
    const session = sessionById(currentSession);
    if (!session) return '';
    const isQuick = serverMode(chatMode);
    const pending = isQuick ? quick.busy(session.id) : pendingSessions.has(session.id);
    const messages = conversationMessages(session, isQuick, pending);
    const composer = `<form class="chat-composer" id="chat-form"><textarea id="chat-input" name="q" rows="2" placeholder="输入问题或描述任务…" aria-label="输入问题" ${pending ? 'disabled' : ''}></textarea><div class="composer-footer">${chatOptions()}<span class="chat-model" data-chat-model title="${escape(modelName)}">${escape(modelName)}</span><button class="chat-send" type="submit" aria-label="发送消息" title="${pending ? '正在回复…' : '发送消息'}" ${pending ? 'disabled' : ''}>${icon('arrow')}</button></div></form>`;
    const bar = isQuick ? `<div class="quick-session-bar"><span id="quick-status" role="status"></span>${button('删除会话', 'delete-session', 'trash')}</div>` : '';
    return bar + `<div class="chat-thread">${messages}</div>` + composer;
  }

  function conversationMessages(session, isQuick, pending) {
    return session.messages.map((message, index) => {
      if (message.role === 'tool') return '';
      if (!chatMessageKeys.has(message)) chatMessageKeys.set(message, ++nextChatMessageKey);
      const key = chatMessageKeys.get(message);
      return message.tool_calls?.length ? toolStep(session, message, index, key)
      : message.role === 'user'
      ? `<div class="chat-message user" data-chat-key="${key}">${escape(message.content)}</div>`
      : `<div class="chat-message assistant" data-chat-key="${key}"><div class="${isQuick ? 'quick-text markdown-body' : 'markdown-body'}" data-message-index="${index}">${markdown.parse(message.content || (message.stopped ? '已停止生成。' : message.error ? '' : pending ? '正在回复…' : '回复未完成。'))}</div>${isQuick ? `<div class="quick-sources" data-sources-index="${index}"></div>` : ''}${message.error ? `<p class="chat-error" role="status">${escape(message.error)}</p>` : ''}${message.stopped ? '<p class="chat-stop-note">已停止，可继续提问。</p>' : isQuick && message.status !== 'complete' && message.content && !message.error ? `<p class="${message.status === 'partial' ? 'chat-stop-note' : 'chat-error'}">本条回答未完成</p>` : ''}</div>`;
    }).join('');
  }

  function chatOptions() {
    return '<div class="chat-options"><select class="chat-mode-select" data-chat-mode aria-label="对话模式"><option value="quick_answer">快速问答</option><option value="smart_reasoning">智能推理</option><option value="plain_chat">普通对话</option></select><details class="chat-scope-picker" data-chat-scope><summary>知识库</summary></details></div>';
  }

  function toolStep(session, message, index, key) {
    const tools = message.tool_calls.map(call => {
      const result = session.messages.find(row => row.role === 'tool' && row.turn_id === message.turn_id && row.tool_call_id === call.id);
      return `<li><strong>${escape(call.function.name)}</strong> · ${escape(result?.tool_summary || '等待结果…')}</li>`;
    }).join('');
    const step = `<details class="reasoning-step" data-message-index="${index}" open><summary>工具步骤 · ${message.tool_calls.length} 次调用</summary><ul>${tools}</ul></details>`;
    return `<div class="chat-message assistant" data-chat-key="${key}"><div class="quick-text markdown-body" data-message-index="${index}">${markdown.parse(message.content || '')}</div>${step}</div>`;
  }

  function renderChatControls() {
    document.querySelectorAll('[data-chat-mode]').forEach(node => { node.value = chatMode; });
    document.querySelectorAll('[data-chat-scope]').forEach(node => {
      node.hidden = !serverMode(chatMode);
      const libraries = serverLibraries.filter(library => library.status === 'active');
      const label = libraries.filter(library => selectedKbIds.has(library.id)).map(library => library.name).join('、') || '未选择知识库';
      node.innerHTML = `<summary title="${escape(label)}">${escape(label)}</summary><div class="chat-scope-options">${libraries.length ? libraries.map(library => `<label><input type="checkbox" data-chat-kb="${escape(library.id)}" ${selectedKbIds.has(library.id) ? 'checked' : ''}>${escape(library.name)}</label>`).join('') : '<p>先在知识库中创建并上传资料。</p>'}</div>`;
    });
    const homeSend = $('#home-search-form [type="submit"]');
    homeSend.disabled = serverMode(chatMode) && quick.submitting;
    if (currentPage === 'chat' && sessionById(currentSession)) {
      const busy = serverMode(chatMode) ? quick.busy(currentSession) : pendingSessions.has(currentSession);
      const run = serverMode(chatMode) ? quick.pending.get(currentSession) || quick.stopping.get(currentSession) : pendingSessions.get(currentSession);
      const stopping = serverMode(chatMode) ? quick.stopping.has(currentSession) : !!run?.signal.aborted;
      const send = $('#chat-form .chat-send');
      $('#chat-input').disabled = busy;
      send.type = run ? 'button' : 'submit';
      send.dataset.action = run ? 'stop-chat' : '';
      send.disabled = stopping || (busy && !run);
      send.title = stopping ? '正在停止…' : run ? '打断生成' : busy ? '正在发送…' : '发送消息';
      send.setAttribute('aria-label', send.title);
      if (send.dataset.running !== String(!!run)) {
        send.innerHTML = icon(run ? 'stop' : 'arrow');
        send.dataset.running = String(!!run);
      }
      if (!serverMode(chatMode)) return;
      const phase = quick.statuses.get(currentSession);
      $('#quick-status').textContent = phase?.text || (chatMode === 'smart_reasoning' ? '智能推理' : '快速问答');
      $('#quick-status').classList.toggle('error', !!phase?.error);
    }
  }

  function renderQuickMessages() {
    const session = quick.rows.get(currentSession);
    session.messages.forEach((message, index) => {
      if (message.role !== 'assistant' || message.tool_calls?.length) return;
      const sources = $(`[data-sources-index="${index}"]`);
      const cards = JSON.stringify(message.citations || []);
      if (chatSourceCards.get(sources) === cards) return;
      chatSourceCards.set(sources, cards);
      sources.replaceChildren();
      for (const card of message.citations || []) {
        if (!card.run_id || !card.source_id) continue;
        const source = document.createElement('button');
        source.className = 'source-card';
        source.textContent = `[${card.source_id}] ${card.original_name} · ${card.page_start == null ? '无页码' : `第${card.page_start}页`}`;
        source.addEventListener('click', () => openQuickSource(card).catch(error => toast(error.message)));
        sources.append(source);
      }
    });
  }

  function renderChatMessages() {
    const session = sessionById(currentSession);
    const isQuick = serverMode(chatMode);
    const thread = $('.chat-thread');
    updateChatMarkup(thread, conversationMessages(session, isQuick, isQuick ? quick.busy(session.id) : pendingSessions.has(session.id)));
    if (isQuick) renderQuickMessages();
    renderHistory();
    renderChatControls();
  }

  function updateChatMarkup(node, html) {
    const template = document.createElement('template');
    template.innerHTML = html;
    syncChatNodes(node, template.content);
  }

  function syncChatNodes(parent, incoming) {
    const keyed = new Map([...parent.children].filter(node => node.dataset.chatKey).map(node => [node.dataset.chatKey, node]));
    let current = parent.firstChild;
    for (const next of [...incoming.childNodes]) {
      const key = next.nodeType === 1 && next.dataset.chatKey;
      const existing = key ? keyed.get(key) : current;
      if (!existing || existing.nodeType !== next.nodeType || existing.nodeName !== next.nodeName ||
          (!key && existing.nodeType === 1 && existing.dataset.chatKey)) {
        parent.insertBefore(next, current);
        continue;
      }
      if (existing !== current) parent.insertBefore(existing, current);
      if (existing.nodeType === 1) {
        for (const attr of [...existing.attributes]) {
          if (!(existing.tagName === 'DETAILS' && attr.name === 'open') && !next.hasAttribute(attr.name)) existing.removeAttribute(attr.name);
        }
        for (const attr of [...next.attributes]) {
          if (!(existing.tagName === 'DETAILS' && attr.name === 'open') && existing.getAttribute(attr.name) !== attr.value) existing.setAttribute(attr.name, attr.value);
        }
        // 来源按钮由 renderQuickMessages 绑定事件，只在引用内容改变时更新。
        if (!existing.classList.contains('quick-sources')) syncChatNodes(existing, next);
      } else if (existing.nodeValue !== next.nodeValue) existing.nodeValue = next.nodeValue;
      current = existing.nextSibling;
    }
    while (current) {
      const next = current.nextSibling;
      current.remove();
      current = next;
    }
  }

  function updateChatViewport(update) {
    const thread = $('.chat-thread');
    const top = thread.scrollTop;
    const viewport = thread.getBoundingClientRect();
    const previous = chatScrollStates.get(thread);
    const follow = previous && Math.abs(previous.top - top) < 1 ? previous.follow : thread.scrollHeight - thread.clientHeight - top <= 48;
    const selector = '.markdown-body p, .markdown-body li, .markdown-body pre, .markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4, .markdown-body h5, .markdown-body h6, .markdown-body blockquote, .markdown-body tr';
    const blocks = [...thread.querySelectorAll(selector)];
    const visible = follow ? null : blocks.find(node => {
      const bounds = node.getBoundingClientRect();
      return bounds.bottom > viewport.top && bounds.top < viewport.bottom;
    });
    const anchor = visible && {tag: visible.tagName, text: visible.textContent.slice(0, 80), top: visible.getBoundingClientRect().top};
    if (anchor) anchor.occurrence = blocks.slice(0, blocks.indexOf(visible)).filter(node => node.tagName === anchor.tag && node.textContent.startsWith(anchor.text)).length;
    update();
    const updatedThread = $('.chat-thread');
    if (follow) updatedThread.scrollTop = updatedThread.scrollHeight;
    else if (anchor) {
      const match = [...updatedThread.querySelectorAll(selector)].filter(node => node.tagName === anchor.tag && node.textContent.startsWith(anchor.text))[anchor.occurrence];
      const shift = match ? match.getBoundingClientRect().top - anchor.top : 0;
      if (Math.abs(shift) > 0.5) updatedThread.scrollTop += shift;
    } else {
      const shift = updatedThread.getBoundingClientRect().top - viewport.top;
      if (Math.abs(shift) > 0.5) updatedThread.scrollTop += shift;
    }
    chatScrollStates.set(updatedThread, {top: updatedThread.scrollTop, follow});
  }

  function quickChanged(event) {
    if (event.type === 'warning') { toast(event.message); return; }
    const visible = currentPage === 'chat' && serverMode(chatMode) && currentSession === event.sessionId && quick.rows.has(currentSession);
    if (visible) {
      updateChatViewport(() => {
        if (event.type === 'delta') {
          const messages = quick.rows.get(currentSession).messages;
          const index = messages.indexOf(quick.pending.get(currentSession).reply);
          updateChatMarkup($(`[data-message-index="${index}"]`), markdown.parse(messages[index].content));
        } else if (['messages', 'finalized', 'finished'].includes(event.type)) renderChatMessages();
        else renderChatControls();
      });
      return;
    }
    if (event.type === 'rows') {
      if (currentPage === 'chat' && serverMode(chatMode) && !quick.rows.has(currentSession) && !quick.pending.has(currentSession)) startNewChat();
      else renderHistory();
    }
    renderChatControls();
  }

  async function openQuickSource(card) {
    const source = await api(`/api/runs/${encodeURIComponent(card.run_id)}/sources/${encodeURIComponent(card.source_id)}`);
    const snapshot = source.source_snapshot;
    $('#source-name').textContent = snapshot.original_name;
    const download = $('#source-download');
    download.hidden = !source.readable;
    download.removeAttribute('href');
    if (!source.readable) {
      $('#source-meta').textContent = source.original_deleted ? '原文已删除，仅历史快照' : '原文当前不可用，仅历史快照';
      $('#source-text').textContent = snapshot.excerpt;
      closeDialogs(); $('#source-dialog').showModal();
      return;
    }
    const chunk = await api(`/api/chunks/${encodeURIComponent(source.chunk_id)}`);
    const meta = chunk.source.source_meta;
    $('#source-meta').textContent = [meta.document_status, ...(meta.heading_path || []),
      snapshot.page_start == null ? '无页码' : `第${snapshot.page_start}页`,
      (meta.line_ranges || []).map(([a, b]) => `原文第${a}–${b}行`).join('；')].filter(Boolean).join(' · ');
    $('#source-text').textContent = chunk.content;
    download.href = `/api/documents/${encodeURIComponent(source.document_id)}/download`;
    closeDialogs(); $('#source-dialog').showModal();
  }

  async function sendQuickMessage(value) {
    const ticket = pageVersion;
    const sessionId = currentPage === 'chat' ? currentSession : null;
    try {
      const id = await quick.send(sessionId, value, [...selectedKbIds], chatMode);
      if (id && pageVersion === ticket && serverMode(chatMode)) {
        $('#home-search').value = '';
        if (currentPage !== 'chat' || currentSession !== id) setPage('chat', id);
        else $('#chat-input').value = '';
      }
    } catch (error) {
      if (pageVersion === ticket && serverMode(chatMode) && signedIn && error.sessionId) {
        if (currentPage !== 'chat' || currentSession !== error.sessionId) setPage('chat', error.sessionId);
        $('#chat-input').value = value;
      }
      toast(error.message);
    }
  }

  async function openSession(element) {
    chatMode = element.dataset.mode || (quick.rows.has(element.dataset.id) ? 'quick_answer' : 'plain_chat');
    setPage('chat', element.dataset.id);
    if (!serverMode(chatMode)) return;
    quick.rows.get(element.dataset.id).mode = chatMode;
    const ticket = pageVersion;
    try { await quick.open(element.dataset.id); }
    catch (error) { toast(error.message); if (pageVersion === ticket) startNewChat(); }
  }

  function renderModel() {
    document.querySelectorAll('[data-chat-model]').forEach(node => {
      node.textContent = modelName;
      node.title = modelName;
    });
  }

  async function loadChatModel() {
    try {
      const response = await fetch('/api/chat/model');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      modelName = data.model;
    } catch (error) {
      modelName = '模型暂不可用';
    }
    renderModel();
  }

  async function* streamReply(messages, signal) {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages }),
      signal
    });
    if (response.status === 401) showLogin();
    if (!response.ok) throw new Error(`对话请求失败（HTTP ${response.status}）。`);
    const reader = response.body.getReader();
    const cancelRead = () => { reader.cancel().catch(() => {}); };
    signal.addEventListener('abort', cancelRead, {once: true});
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      signal.throwIfAborted();
      while (true) {
        const { value, done } = await reader.read();
        signal.throwIfAborted();
        buffer += decoder.decode(value, { stream: !done });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          signal.throwIfAborted();
          if (!line.trim()) continue;
          const event = JSON.parse(line);
          if (event.type === 'content') yield event.content;
          if (event.type === 'error') throw new Error(event.message);
          if (event.type === 'done') return;
        }
        if (done) throw new Error('回复连接已中断，请重新提问。');
      }
    } finally {
      signal.removeEventListener('abort', cancelRead);
      try { await reader.cancel(); } finally { reader.releaseLock(); }
    }
  }

  async function sendMessage(session, value) {
    if (!value || pendingSessions.has(session.id)) return;
    if (!session.started) session.title = value;
    session.started = true;
    session.updatedAt = new Date().toISOString();
    session.messages.push({ role: 'user', content: value });
    const messages = session.messages.filter(message => message.content && !message.error).map(({ role, content }) => ({ role, content }));
    const reply = { role: 'assistant', content: '' };
    session.messages.push(reply);
    const replyIndex = session.messages.length - 1;
    const controller = new AbortController();
    pendingSessions.set(session.id, controller);
    recordActivity(`发送问题：${value}`);
    persist();
    if (currentPage === 'chat' && currentSession === session.id) updateChatViewport(renderChatMessages);
    else setPage('chat', session.id);
    try {
      for await (const content of streamReply(messages, controller.signal)) {
        reply.content += content;
        if (currentPage === 'chat' && currentSession === session.id) {
          const node = $(`[data-message-index="${replyIndex}"]`);
          updateChatViewport(() => { updateChatMarkup(node, markdown.parse(reply.content)); });
        }
      }
    } catch (error) {
      if (controller.signal.aborted) {
        reply.stopped = true;
        reply.status = 'partial';
      } else reply.error = error.message;
    } finally {
      pendingSessions.delete(session.id);
      session.updatedAt = new Date().toISOString();
      persist();
      if (currentPage === 'chat' && currentSession === session.id) updateChatViewport(renderChatMessages); else renderHistory();
    }
  }

  function render() {
    renderSidebar();
    const content = $('#content-view');
    if (currentPage === 'chat' && content.dataset.sessionId === currentSession && content.dataset.chatMode === chatMode) {
      updateChatViewport(renderChatMessages);
      return;
    }
    content.dataset.sessionId = currentPage === 'chat' ? currentSession : '';
    content.dataset.chatMode = chatMode;
    document.body.classList.toggle('chat-mode', currentPage === 'chat');
    $('.window-bar').hidden = currentPage === 'chat';
    $('#home-view').hidden = currentPage !== 'home';
    $('#content-view').hidden = currentPage === 'home';
    const docs = visibleDocuments();
    let html = '';
    if (currentPage === 'libraries') html = heading('知识库', '分门别类地收集资料，让知识有迹可循。', button('新建知识库', 'new-library', 'plus', 'primary')) + (activeLibraries().length ? `<h2 class="section-label">全部知识库 <span class="count">${activeLibraries().length}</span></h2><div class="library-grid">${activeLibraries().map(libraryCard).join('')}</div>` : emptyState('book', '暂无知识库', '点击「新建知识库」创建。'));
    if (currentPage === 'library') html = libraryView();
    if (currentPage === 'document') html = documentView();
    if (currentPage === 'chat') html = conversationView();
    $('#content-view').innerHTML = html;
    if (currentPage === 'chat' && serverMode(chatMode) && quick.rows.has(currentSession)) renderQuickMessages();
    renderChatControls();
    renderModel();
  }

  function showGeneral(title, body) {
    closeDialogs();
    $('#general-dialog').innerHTML = `<div class="dialog-heading"><h2 id="general-heading">${escape(title)}</h2><button class="icon-button" data-action="close-dialog" aria-label="关闭">${icon('close')}</button></div><div class="dialog-body">${body}</div>`;
    $('#general-dialog').showModal();
  }

  function openLibraryEditor(id) {
    const library = id ? libraryById(id) : null;
    showGeneral(library ? '修改知识库' : '新建知识库', `<form id="library-form" ${library ? `data-id="${escape(id)}"` : ''}><label class="form-field"><span>知识库名称</span><input name="name" maxlength="128" required value="${escape(library?.name || '')}" autofocus></label><label class="form-field"><span>描述</span><textarea name="description" class="short-textarea">${escape(library?.description || '')}</textarea></label><div class="dialog-footer">${button('取消', 'close-dialog')}<button class="button primary" type="submit">${library ? '保存' : '创建知识库'}</button></div></form>`);
  }

  function openUpload() {
    const libraries = activeLibraries().filter(library => library.status === 'active');
    if (!libraries.length) return toast('先创建一个可用知识库。');
    showGeneral('上传文档', `<form id="upload-form"><label class="form-field"><span>保存到知识库</span><select name="libraryId">${libraries.map(library => `<option value="${escape(library.id)}" ${library.id === currentLibrary ? 'selected' : ''}>${escape(library.name)}</option>`).join('')}</select></label><label class="upload-zone" id="upload-zone">${icon('upload')}<strong>选择或拖拽文件</strong><span>TXT / MD，每个文件最多20 MiB</span><input id="file-input" name="files" type="file" accept=".txt,.md" multiple required aria-label="选择上传文档"></label><div id="file-selection"></div><p class="form-error" id="upload-error" hidden></p><div class="dialog-footer">${button('取消', 'close-dialog')}<button class="button primary" type="submit">上传到知识库</button></div></form>`);
    const input = $('#file-input');
    const zone = $('#upload-zone');
    input.addEventListener('change', () => { $('#file-selection').textContent = [...input.files].map(file => file.name).join('、'); });
    zone.addEventListener('dragover', event => { event.preventDefault(); zone.classList.add('dragging'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragging'));
    zone.addEventListener('drop', event => { event.preventDefault(); zone.classList.remove('dragging'); input.files = event.dataTransfer.files; input.dispatchEvent(new Event('change')); });
  }

  function formatSize(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function openSearch(value = '') {
    closeDialogs();
    searchFilter = 'all';
    $('#command-input').value = value;
    document.querySelectorAll('[data-filter]').forEach(filter => {
      filter.classList.toggle('active', filter.dataset.filter === 'all');
      filter.setAttribute('aria-pressed', String(filter.dataset.filter === 'all'));
    });
    renderSearch();
    $('#search-dialog').showModal();
    $('#command-input').focus();
  }

  function highlighted(value, query) {
    const position = plainSearch(value).indexOf(plainSearch(query));
    if (position === -1 || !query) return escape(value);
    return `${escape(value.slice(0, position))}<mark>${escape(value.slice(position, position + query.length))}</mark>${escape(value.slice(position + query.length))}`;
  }

  function renderSearch() {
    const query = $('#command-input').value.trim();
    const matching = value => plainSearch(value).includes(plainSearch(query));
    const libraries = activeLibraries().filter(library => ['all', 'library'].includes(searchFilter) && (!query || matching(library.name + ' ' + library.description))).map(library => ({ id: library.id, type: 'library', title: library.name, description: '文档知识库', symbol: 'folder' }));
    const documents = visibleDocuments().filter(item => (searchFilter === 'all' || searchFilter === item.type) && (!query || matching(item.title + ' ' + item.content))).map(item => ({ id: item.id, type: item.type, title: item.title, description: `${libraryById(item.libraryId).name} · ${item.content ? item.content.replace(/\s+/g, ' ').slice(0, 90) : '文件信息'}`, symbol: documentIcon(item.type) }));
    searchResults = [...libraries, ...documents].slice(0, 50);
    selectedResult = -1;
    $('#search-results').innerHTML = searchResults.length ? `<p class="result-caption">${query ? `找到 ${searchResults.length} 项内容` : '知识库与最近内容'}</p>${searchResults.map((item, index) => `<button class="search-result" data-result-index="${index}">${icon(item.symbol)}<span><strong>${highlighted(item.title, query)}</strong><small>${highlighted(item.description, query)}</small></span>${icon('chevron')}</button>`).join('')}` : `<div class="search-hint">${query ? `没有找到“${escape(query)}”相关内容<br>试试其他关键词，或先添加一份内容。` : '还没有相关内容。'}</div>`;
  }

  function chooseSearchResult(index) {
    const result = searchResults[index];
    if (!result) return;
    if (result.type === 'library') openLibrary(result.id);
    else {
      setPage('document', result.id);
      pollDocuments(documentById(result.id).libraryId);
    }
  }

  function showLogin() {
    signedIn = false;
    pageVersion++;
    quick.reset();
    currentPage = 'home'; currentSession = null;
    currentLibrary = null; currentDocument = null;
    chatMode = 'smart_reasoning';
    serverLibraries = []; serverDocuments = [];
    selectedKbIds.clear(); kbSelectionInitialized = false;
    clearTimeout(pollTimer);
    closeDialogs();
    $('.app-shell').hidden = true;
    $('#login-view').hidden = false;
    render();
  }

  async function api(url, options = {}) {
    const response = await fetch(url, { credentials: 'same-origin', ...options });
    if (response.status === 401 && url !== '/api/auth/login') showLogin();
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail; } catch { detail = null; }
      const error = new Error(detail?.message || (typeof detail === 'string' ? detail : `请求失败（HTTP ${response.status}）`));
      error.status = response.status;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  }

  async function refreshLibraries() {
    serverLibraries = await api('/api/knowledge-bases');
    const available = serverLibraries.filter(library => library.status === 'active');
    if (!kbSelectionInitialized) {
      for (const library of available) selectedKbIds.add(library.id);
      kbSelectionInitialized = true;
    }
    for (const id of selectedKbIds) if (!available.some(library => library.id === id)) selectedKbIds.delete(id);
    renderChatControls();
  }

  async function refreshDocuments(id) {
    const documents = await api(`/api/knowledge-bases/${id}/documents`);
    serverDocuments = [...serverDocuments.filter(item => item.libraryId !== id), ...documents.map(item => ({ ...item, libraryId: item.kb_id, title: item.original_name, type: 'file', content: '', updatedAt: item.updated_at }))];
  }

  async function openLibraries() {
    setPage('libraries');
    try { await refreshLibraries(); if (currentPage === 'libraries') render(); } catch (error) { toast(error.message); }
  }

  async function openLibrary(id) {
    semanticResults = []; semanticQuery = '';
    setPage('library', id);
    await loadLibraryDocuments(id);
  }

  async function loadLibraryDocuments(id) {
    try { await refreshDocuments(id); if (currentPage === 'library' && currentLibrary === id) { render(); pollDocuments(id); } }
    catch (error) {
      toast(error.message);
      if (transientError(error) && signedIn && currentPage === 'library' && currentLibrary === id) {
        clearTimeout(pollTimer);
        pollTimer = setTimeout(() => loadLibraryDocuments(id), 2000);
      }
    }
  }

  function transientError(error) {
    return error.status === undefined || error.status === 408 || error.status === 429 || error.status >= 500;
  }

  function pollDocuments(id) {
    clearTimeout(pollTimer);
    const pending = serverDocuments.some(item => item.libraryId === id && ['queued', 'parsing', 'indexing'].includes(item.parse_status));
    const inLibrary = currentPage === 'library' && currentLibrary === id;
    const inDocument = currentPage === 'document' && documentById(currentDocument)?.libraryId === id;
    if (!signedIn || !pending || !(inLibrary || inDocument)) return;
    pollTimer = setTimeout(async () => {
      try { await refreshDocuments(id); if ((currentPage === 'library' && currentLibrary === id) || (currentPage === 'document' && documentById(currentDocument)?.libraryId === id)) { render(); pollDocuments(id); } } catch (error) { toast(error.message); if (transientError(error)) pollDocuments(id); }
    }, 2000);
  }

  async function documentOperation(id, operation) {
    const item = documentById(id);
    if (operation === 'delete' && !window.confirm('删除原文件、片段和向量，是否继续？')) return;
    try {
      await api(`/api/documents/${id}${operation === 'delete' ? '' : '/'+operation}`, { method: operation === 'delete' ? 'DELETE' : 'POST' });
      await openLibrary(item.libraryId);
    } catch (error) {
      toast(error.message);
      await refreshDocuments(item.libraryId);
      if (currentPage === 'library' || currentPage === 'document') render();
    }
  }

  async function deleteLibrary(id) {
    if (!window.confirm('删除知识库及全部文档，是否继续？')) return;
    try { await api(`/api/knowledge-bases/${id}`, { method: 'DELETE' }); await openLibraries(); }
    catch (error) { toast(error.message); await refreshLibraries(); render(); }
  }

  async function boot() {
    const [target, modeHint] = window.location.hash.replace(/^#(?:session=)?/, '').split('&mode=');
    const ticket = pageVersion;
    try {
      const user = await api('/api/auth/me');
      $('#user-status').textContent = `已登录：${user.username}`;
      signedIn = true;
      $('#login-view').hidden = true;
      $('.app-shell').hidden = false;
      await refreshLibraries();
      await quick.refreshSessions();
      await loadChatModel();
      quick.restorePending();
      if (pageVersion === ticket && quick.rows.has(target)) await openSession({dataset: {id: target,
        mode: modeHint === 'smart_reasoning' ? 'smart_reasoning' : 'quick_answer'}});
    } catch (error) { showLogin(); }
  }

  const actions = {
    'new-chat': startNewChat,
    'open-knowledge': openLibraries,
    'open-session': openSession,
    'stop-chat': async () => {
      const id = currentSession;
      if (serverMode(chatMode)) {
        try { await quick.stop(id); } catch (error) { toast(error.message); }
      } else {
        pendingSessions.get(id)?.abort();
        updateChatViewport(renderChatControls);
      }
    },
    'delete-session': async () => {
      const id = currentSession;
      if (!window.confirm('删除这个会话及历史，是否继续？')) return;
      try { await quick.remove(id); if (currentPage === 'chat' && currentSession === id && serverMode(chatMode)) startNewChat(); }
      catch (error) { toast(error.message); }
    },
    'open-library': element => openLibrary(element.dataset.id),
    'open-document': element => { setPage('document', element.dataset.id); pollDocuments(documentById(element.dataset.id).libraryId); },
    'new-library': () => openLibraryEditor(),
    'edit-library': element => openLibraryEditor(element.dataset.id),
    'retry-document': element => documentOperation(element.dataset.id, 'retry'),
    'reindex-document': element => documentOperation(element.dataset.id, 'reindex'),
    'logout': async () => { try { await api('/api/auth/logout', { method: 'POST' }); showLogin(); } catch (error) { toast(error.message); } },
    'upload': openUpload,
    'search': () => openSearch(),
    'close-dialog': closeDialogs,
    'delete-library': element => deleteLibrary(element.dataset.id),
    'delete-document': element => documentOperation(element.dataset.id, 'delete'),
    'notifications': () => showGeneral('最近动态', state.activities.length ? `<div class="documents">${state.activities.map(activity => `<div class="document-row"><div class="document-main"><strong>${escape(activity.message)}</strong><small>${new Date(activity.time).toLocaleString('zh-CN')}</small></div></div>`).join('')}</div>` : '<div class="search-hint">暂时没有新动态<br>你对知识空间的修改会记录在这里。</div>'),
    'collapse': () => {
      const collapsed = document.body.classList.toggle('collapsed');
      document.querySelectorAll('[data-action="collapse"]').forEach(button => button.setAttribute('aria-pressed', String(collapsed)));
    },
    'fullscreen': async () => {
      try {
        if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen();
      } catch (error) { toast('当前浏览器暂时不支持全屏显示。'); }
    }
  };

  document.addEventListener('click', event => {
    const element = event.target.closest('button');
    if (!element || element.disabled) return;
    if (element.dataset.page) setPage(element.dataset.page);
    else if (element.dataset.action) actions[element.dataset.action]?.(element);
    else if (element.dataset.filter) {
      searchFilter = element.dataset.filter;
      document.querySelectorAll('[data-filter]').forEach(filter => {
        filter.classList.toggle('active', filter.dataset.filter === searchFilter);
        filter.setAttribute('aria-pressed', String(filter.dataset.filter === searchFilter));
      });
      renderSearch();
    } else if (element.dataset.documentFilter) {
      documentFilter = element.dataset.documentFilter;
      render();
    } else if (element.dataset.resultIndex != null) chooseSearchResult(Number(element.dataset.resultIndex));
  });

  $('#home-search-form').addEventListener('submit', async event => {
    event.preventDefault();
    const value = $('#home-search').value.trim();
    if (!value) return;
    if (serverMode(chatMode)) { await sendQuickMessage(value); return; }
    let session = currentPage === 'chat' ? sessionById(currentSession) : null;
    if (!session) session = state.sessions.find(item => !item.deleted && !item.started);
    if (!session) {
      session = { id: crypto.randomUUID(), title: '新对话', started: false, messages: [], createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() };
      state.sessions.unshift(session);
    }
    $('#home-search').value = '';
    await sendMessage(session, value);
  });
  $('#command-input').addEventListener('input', renderSearch);
  $('#command-input').addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!searchResults.length) return;
      selectedResult = (selectedResult + (event.key === 'ArrowDown' ? 1 : -1) + searchResults.length) % searchResults.length;
      document.querySelectorAll('.search-result').forEach((result, index) => result.classList.toggle('keyboard-selected', index === selectedResult));
      $(`[data-result-index="${selectedResult}"]`).scrollIntoView({ block: 'nearest' });
    } else if (event.key === 'Enter') { event.preventDefault(); chooseSearchResult(selectedResult < 0 ? 0 : selectedResult); }
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && ['home-search', 'chat-input'].includes(event.target.id)) { event.preventDefault(); event.target.form.requestSubmit(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); openSearch(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && $('#editor-dialog').open) { event.preventDefault(); $('#content-form').requestSubmit(); }
  });

  document.addEventListener('input', event => {
    if (event.target.id === 'semantic-query') { semanticQuery = event.target.value; return; }
    if (event.target.id !== 'list-filter') return;
    const query = plainSearch(event.target.value.trim());
    document.querySelectorAll('#document-list .document-row').forEach(row => { row.hidden = !plainSearch(row.querySelector('strong').textContent).includes(query); });
  });

  document.addEventListener('change', event => {
    if (event.target.dataset.chatMode !== undefined) {
      const sameSession = currentPage === 'chat' && serverMode(chatMode) && serverMode(event.target.value);
      chatMode = event.target.value;
      $('#home-search').value = '';
      if (sameSession) {
        quick.rows.get(currentSession).mode = chatMode;
        setPage('chat', currentSession);
      } else setPage('home');
    }
    if (event.target.dataset.chatKb !== undefined) {
      if (event.target.checked) selectedKbIds.add(event.target.dataset.chatKb); else selectedKbIds.delete(event.target.dataset.chatKb);
      document.querySelectorAll('[data-chat-scope] summary').forEach(node => {
        const text = serverLibraries.filter(library => selectedKbIds.has(library.id)).map(library => library.name).join('、') || '未选择知识库';
        node.textContent = text; node.title = text;
      });
    }
    if (event.target.id === 'dense-setting') { state.settings.dense = event.target.checked; persist(); document.body.classList.toggle('dense', state.settings.dense); }
  });

  document.addEventListener('submit', async event => {
    const form = event.target;
    if (form.id === 'chat-form') {
      event.preventDefault();
      const session = sessionById(currentSession);
      const value = form.elements.q.value.trim();
      if (!session || !value) return;
      if (serverMode(chatMode)) { await sendQuickMessage(value); return; }
      await sendMessage(session, value);
      return;
    }
    if (!['login-form', 'library-form', 'upload-form', 'semantic-search-form'].includes(form.id)) return;
    event.preventDefault();
    const data = new FormData(form);
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      if (form.id === 'login-form') {
        await api('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: data.get('username'), password: data.get('password') }) });
        form.reset(); await boot(); render();
      }
      if (form.id === 'library-form') {
        const library = await api('/api/knowledge-bases'+(form.dataset.id ? '/'+form.dataset.id : ''), { method: form.dataset.id ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: data.get('name'), description: data.get('description') }) });
        await refreshLibraries(); closeDialogs(); await openLibrary(library.id);
      }
      if (form.id === 'upload-form') {
        const libraryId = data.get('libraryId');
        for (const file of [...$('#file-input').files]) {
          const upload = new FormData(); upload.append('file', file);
          await api(`/api/knowledge-bases/${libraryId}/documents`, { method: 'POST', body: upload });
        }
        closeDialogs(); await openLibrary(libraryId);
      }
      if (form.id === 'semantic-search-form') {
        const libraryId = currentLibrary;
        semanticQuery = data.get('query');
        const results = await api(`/api/knowledge-bases/${libraryId}/search`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: semanticQuery, top_k: Number(data.get('top_k')) }) });
        if (currentPage === 'library' && currentLibrary === libraryId) {
          semanticResults = results; render();
          if (!results.length) $('#semantic-results').textContent = '没有找到可检索片段。';
        }
      }
    } catch (error) {
      if (form.id === 'login-form') $('#login-error').textContent = error.message;
      else if (form.id === 'upload-form') { $('#upload-error').hidden = false; $('#upload-error').textContent = error.message; }
      else toast(error.message);
    } finally { submit.disabled = false; }
  });

  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  }));

  if (window.innerWidth <= 600) document.body.classList.add('collapsed');

  window.addEventListener('pagehide', () => { clearTimeout(pollTimer); quick.detach(); });

  render();
  boot();
})();
