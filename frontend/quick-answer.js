(() => {
  'use strict';
  const STORAGE_KEY = 'knora-quick-pending-runs';

  class QuickAnswerClient {
    constructor({api, onChange, EventSourceClass, storage}) {
      this.api = api;
      this.onChange = onChange;
      this.EventSourceClass = EventSourceClass;
      this.storage = storage;
      this.rows = new Map();
      this.pending = new Map();
      this.stopping = new Map();
      this.statuses = new Map();
      this.submitting = false;
      this.epoch = 0;
      this.listVersion = 0;
    }

    get sessions() {
      return [...this.rows.values()].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    }

    busy(id) { return this.submitting || this.pending.has(id) || this.stopping.has(id); }
    changed(type, sessionId) { this.onChange({type, sessionId}); }
    status(id, text, error) {
      this.statuses.set(id, {text, error});
      this.changed('phase', id);
    }

    async refreshSessions() {
      const epoch = this.epoch;
      const version = ++this.listVersion;
      const rows = await this.api('/api/sessions');
      if (epoch !== this.epoch || version !== this.listVersion) return;
      this.rows = new Map(rows.map(row => [row.id, {...row, updatedAt: row.updated_at,
        mode: this.rows.get(row.id)?.mode || 'quick_answer', messages: this.rows.get(row.id)?.messages || []}]));
      for (const [id, run] of this.pending) {
        if (this.rows.has(id)) run.row = this.rows.get(id);
      }
      this.changed('rows', null);
    }

    async open(id, preserveStatus = false) {
      const epoch = this.epoch;
      const messages = await this.api(`/api/sessions/${encodeURIComponent(id)}/messages`);
      if (epoch !== this.epoch) return;
      const row = this.rows.get(id);
      if (!row) return;
      const stopped = this.stopping.get(id);
      if (stopped && !stopped.reply.id) {
        const saved = messages.find(message => message.run_id === stopped.id && message.role === 'assistant'
          && message.content === stopped.reply.content) || messages.findLast(message => message.run_id === stopped.id
            && message.role === 'assistant' && !message.tool_calls?.length);
        if (saved) Object.assign(stopped.reply, saved);
      }
      const existing = new Map(row.messages.filter(message => message.id).map(message => [message.id, message]));
      row.messages = messages.map(message => existing.has(message.id)
        ? Object.assign(existing.get(message.id), message) : message);
      const run = this.pending.get(id);
      if (run) {
        run.row = row;
        const saved = run.reply.id && row.messages.find(message => message.id === run.reply.id);
        if (saved) run.reply = saved;
        else row.messages.push(run.reply);
      } else if (!preserveStatus) this.status(id, '会话历史已加载。', false);
      this.changed('messages', id);
    }

    async stop(id) {
      const run = this.pending.get(id);
      if (!run || this.stopping.has(id)) return;
      const epoch = this.epoch;
      this.stopping.set(id, run);
      this.status(id, '正在停止并保存…', false);
      try {
        await this.api(`/api/runs/${encodeURIComponent(run.id)}/stop`, {method: 'POST'});
        if (epoch !== this.epoch) return;
        if (this.pending.get(id) === run) {
          run.stream.close();
          this.pending.delete(id);
          this.savePending();
        }
        await this.open(id, true);
        if (epoch === this.epoch && !run.terminalEvent) {
          this.status(id, '已停止，已输出内容已保存，可继续提问。', false);
        }
      } catch (error) {
        if (epoch === this.epoch) this.status(id, error.message, true);
        throw error;
      } finally {
        if (epoch === this.epoch && this.stopping.get(id) === run) {
          this.stopping.delete(id);
          this.changed('finished', id);
        }
      }
    }

    async send(id, query, kbIds, mode = 'quick_answer') {
      if (this.busy(id)) return null;
      const epoch = this.epoch;
      this.submitting = true;
      this.changed('controls', id);
      try {
        if (!id) {
          const row = await this.api('/api/sessions', {method: 'POST',
            body: JSON.stringify({title: [...query].slice(0, 255).join('')})});
          if (epoch !== this.epoch) return null;
          id = row.id;
          this.listVersion++;
          this.rows.set(id, {...row, updatedAt: new Date().toISOString(), messages: []});
          this.changed('rows', id);
        }
        const data = await this.api(`/api/sessions/${encodeURIComponent(id)}/runs`, {method: 'POST',
          body: JSON.stringify({query, mode, kb_ids: kbIds, agent_id: null})});
        if (epoch !== this.epoch) return null;
        const row = this.rows.get(id);
        row.mode = mode;
        row.updatedAt = new Date().toISOString();
        row.messages.push({role: 'user', content: query, status: 'complete'});
        this.connect(id, data.run_id, mode);
        return id;
      } catch (error) {
        if (epoch === this.epoch) {
          error.sessionId = id;
          this.status(id, error.message, true);
        }
        throw error;
      } finally {
        if (epoch === this.epoch) {
          this.submitting = false;
          this.changed('controls', id);
        }
      }
    }

    connect(id, runId, mode = 'quick_answer') {
      const reply = {role: 'assistant', content: '', citations: [], status: 'complete'};
      const row = this.rows.get(id);
      const run = {id: runId, lastSeq: 0, reply, row, mode};
      row.messages.push(reply);
      this.pending.set(id, run);
      this.savePending();
      this.status(id, '正在理解问题…', false);
      this.changed('messages', id);
      const stream = new this.EventSourceClass(`/api/runs/${encodeURIComponent(runId)}/events`);
      run.stream = stream;
      stream.onmessage = event => {
        let envelope;
        try { envelope = JSON.parse(event.data); } catch { return; }
        if (this.pending.get(id) !== run || envelope.run_id !== run.id || envelope.seq <= run.lastSeq) return;
        run.lastSeq = envelope.seq;
        this.receive(id, run, envelope);
      };
      stream.onerror = () => {
        if (this.pending.get(id) !== run) return;
        if (stream.readyState === this.EventSourceClass.CONNECTING) {
          this.status(id, '流连接暂时中断，正在重新连接…', false);
          return;
        }
        stream.close();
        if (this.stopping.has(id)) return;
        this.pending.delete(id);
        if (!run.reply.id && !run.reply.content) {
          const row = this.rows.get(id) || run.row;
          row.messages = row.messages.filter(message => message !== run.reply);
        }
        this.savePending();
        this.status(id, '运行事件已中断、过期或服务已重启；重新打开会话可查看保存的历史。', true);
        this.changed('finished', id);
      };
    }

    receive(id, run, event) {
      const payload = event.payload;
      if (event.type === 'run.started') {
        run.mode = payload.mode || run.mode;
        run.turnId = payload.turn_id;
        this.status(id, '正在理解问题…', false);
      }
      if (event.type === 'round.started') {
        run.reply.content = '';
        this.status(id, `第 ${payload.round_no} 轮：正在处理…`, false);
        this.changed('messages', id);
      }
      if (event.type === 'assistant.tool_calls') {
        run.turnId = payload.turn_id;
        run.reply.turn_id = payload.turn_id;
        if (!run.row.messages.some(message => message.id === payload.message_id)) {
          Object.assign(run.reply, {
            id: payload.message_id, turn_id: payload.turn_id, role: 'assistant',
            content: run.reply.content, status: 'complete', tool_calls: payload.tool_calls.map(call => ({
              id: call.id, type: 'function', function: {name: call.name}}))});
          run.reply = {role: 'assistant', content: '', citations: [], status: 'complete'};
          run.row.messages.push(run.reply);
        }
        run.reply.content = '';
        this.changed('messages', id);
      }
      if (['tool.started', 'tool.finished'].includes(event.type)) {
        let tool = run.row.messages.find(message => message.role === 'tool' &&
          message.turn_id === run.turnId && message.tool_call_id === payload.tool_call_id);
        if (!tool) {
          tool = {role: 'tool', turn_id: run.turnId, tool_call_id: payload.tool_call_id, status: 'complete'};
          run.row.messages.splice(run.row.messages.indexOf(run.reply), 0, tool);
        }
        Object.assign(tool, {tool_name: payload.name,
          tool_summary: payload.summary || '正在执行…', ok: payload.ok});
        this.status(id, `${payload.name}：${tool.tool_summary}`, payload.ok === false);
        this.changed('messages', id);
      }
      if (event.type === 'retrieval.finished') this.status(id, payload.skipped
        ? '正在依据聊天上下文回答…' : `已检索 ${payload.count} 条证据，正在生成…`, false);
      if (event.type === 'model.delta') {
        run.reply.content += payload.text;
        this.changed('delta', id);
      }
      if (event.type === 'answer.finalized') this.finalize(id, run, payload);
      if (['run.completed', 'run.failed'].includes(event.type)) this.finish(id, run, event);
    }

    finalize(id, run, payload) {
      const row = this.rows.get(id) || run.row;
      const existing = row.messages.find(message => message.id === payload.message_id);
      if (existing && existing !== run.reply) {
        row.messages = row.messages.filter(message => message !== run.reply);
        run.reply = existing;
      }
      Object.assign(run.reply, {id: payload.message_id, content: payload.text,
        citations: payload.citations, status: payload.status});
      run.finalized = true;
      this.changed('finalized', id);
    }

    finish(id, run, event) {
      run.terminalEvent = event;
      run.stream.close();
      this.pending.delete(id);
      this.savePending();
      const payload = event.payload;
      const tracingFailed = payload.tracing?.status === 'failed';
      const warning = tracingFailed ? `；追踪失败：${payload.tracing.warnings.join('；')}` : '';
      if (event.type === 'run.failed' && payload.code === 'cancelled') {
        this.status(id, '已停止，已输出内容已保存，可继续提问。' + warning, tracingFailed);
      } else if (event.type === 'run.failed') {
        run.reply.error = payload.message;
        if (!run.finalized) run.reply.status = 'failed';
        this.status(id, payload.message + warning, true);
      } else this.status(id, '回答完成。' + warning, tracingFailed);
      run.row.updatedAt = new Date().toISOString();
      this.changed('finished', id);
      this.refreshSessions().catch(error => this.status(id, error.message, true));
    }

    async remove(id) {
      await this.api(`/api/sessions/${encodeURIComponent(id)}`, {method: 'DELETE'});
      this.listVersion++;
      this.pending.get(id)?.stream.close();
      this.pending.delete(id);
      this.rows.delete(id);
      this.statuses.delete(id);
      this.savePending();
      this.changed('rows', id);
    }

    savePending() {
      try {
        this.storage.setItem(STORAGE_KEY, JSON.stringify([...this.pending].map(([sessionId, run]) => ({sessionId, runId: run.id, mode: run.mode}))));
      } catch {
        this.onChange({type: 'warning', message: '浏览器无法保存临时运行标识，刷新后请从历史查看回答。'});
      }
    }

    restorePending() {
      let items;
      try { items = JSON.parse(this.storage.getItem(STORAGE_KEY) || '[]'); }
      catch { this.storage.removeItem(STORAGE_KEY); return; }
      for (const item of items) {
        if (this.rows.has(item.sessionId) && !this.pending.has(item.sessionId)) this.connect(item.sessionId, item.runId, item.mode);
      }
    }

    detach() {
      for (const run of this.pending.values()) {
        run.stream.onmessage = null;
        run.stream.onerror = null;
        run.stream.close();
      }
    }

    reset() {
      this.epoch++;
      this.detach();
      this.pending.clear();
      this.stopping.clear();
      this.rows.clear();
      this.statuses.clear();
      this.submitting = false;
      this.storage.removeItem(STORAGE_KEY);
    }
  }

  window.QuickAnswerClient = QuickAnswerClient;
})();
