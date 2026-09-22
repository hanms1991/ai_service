'use strict';

/* ═══════════════════════════════════════════════════════════════
 * Agent Playground 前端逻辑
 * 功能：
 *   P1 对话核心：readyz 状态灯 / 配置存 localStorage / capabilities 场景下拉
 *              + inputs JSON 编辑 / 多轮对话（thread_id 复用）/ text+structured 渲染
 *              / 统一错误卡
 *   P2 异步任务：同步 /invoke 与异步 /tasks 切换 / 1s 轮询任务卡 / 状态机
 *              + plan + progress 展示 / 取消任务 / 终态输出展示
 *   P3 中断调试：waiting_human 中断卡 + resume / 每条消息原始 JSON
 *              / trace_id 一键复制 / usage + 耗时 / 会话历史存 localStorage
 * 依赖：原生 fetch + localStorage，无第三方库
 * ═══════════════════════════════════════════════════════════════ */

/* ── 工具函数 ── */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const genTraceId = () => 'pg-' + Date.now().toString(16) + '-' + Math.random().toString(16).slice(2, 8);
const fmtJson = (v) => { const s = JSON.stringify(v, null, 2); return s === undefined ? String(v) : s; };
const nowTime = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });

/* 任务终态集合（与后端 TaskStatus.TERMINAL_STATES 对齐） */
const TERMINAL = new Set(['completed', 'failed', 'timeout', 'cancelled', 'rejected']);

/* 状态徽章配色（planning/running 闪烁） */
const STATUS_COLOR = {
  pending: 'muted', planning: 'blue blink', running: 'blue blink',
  waiting_human: 'yellow', completed: 'green', failed: 'red',
  timeout: 'red', cancelled: 'muted', rejected: 'red',
};

/* file_id：32 位十六进制 */
const FILE_ID_RE = /\b[0-9a-f]{32}\b/;

/* ── 全局状态 ── */
const state = {
  apiBase: localStorage.getItem('pg_api_base') || '',
  apiKey: localStorage.getItem('pg_api_key') || '',
  mode: localStorage.getItem('pg_mode') || 'sync',
  threadId: null,       // 当前会话 ID（服务端返回后复用）
  scenes: [],           // capabilities.scenes
  messages: [],         // 当前会话消息列表
  sending: false,       // 发送防重入
  pollTimer: null,      // 轮询定时器
  pollTaskId: null,
  lastTraceId: '',
  files: [],            // /agent/files 列表
  filesLimits: null,    // {max_upload_mb, allowed_extensions}
  filesCollapsed: localStorage.getItem('pg_files_collapsed') === '1',
};

/* ── URL / fetch 封装 ── */
function apiUrl(path) {
  const base = (state.apiBase || window.location.origin).replace(/\/+$/, '');
  return base + path;
}

/** 统一请求封装：自动带 X-API-Key / X-Trace-Id，返回 {ok,status,data,elapsed} */
async function apiFetch(path, { method = 'GET', body = null, timeoutMs = 0 } = {}) {
  const headers = { 'X-API-Key': state.apiKey, 'X-Trace-Id': genTraceId() };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const ctrl = new AbortController();
  const timer = timeoutMs > 0 ? setTimeout(() => ctrl.abort('timeout'), timeoutMs) : null;
  const started = performance.now();
  try {
    const resp = await fetch(apiUrl(path), {
      method, headers,
      body: body !== null ? JSON.stringify(body) : null,
      signal: ctrl.signal,
    });
    const elapsed = Math.round(performance.now() - started);
    let data = null;
    const text = await resp.text();
    if (text) { try { data = JSON.parse(text); } catch { data = text; } }
    return { ok: resp.ok, status: resp.status, data, elapsed };
  } catch (e) {
    return { ok: false, status: 0, data: null, elapsed: Math.round(performance.now() - started), networkError: String(e) };
  } finally { if (timer) clearTimeout(timer); }
}

/* ═══════════════════════════════════════════════════════════════
 * 轻量安全 Markdown 渲染（无第三方依赖）
 * 支持：# 标题 / **加粗** / `行内代码` / GFM 表格 / > 引用 / 列表 / ---
 * 特殊：行内代码中的 /agent/files/<32hex>/download 渲染为下载按钮
 * ═══════════════════════════════════════════════════════════════ */

function mdInline(text, slots) {
  let s = esc(text);
  /* `code`：下载链接 → 按钮；普通代码 → <code>（用占位符避免被后续规则破坏） */
  s = s.replace(/`([^`\n]+)`/g, (whole, code) => {
    const dm = code.match(/([0-9a-f]{32})\/download\s*[^`]*$/);
    if (dm) {
      slots.push(
        `<button class="btn small dl-btn" data-action="download-file" data-id="${dm[1]}"` +
        ` title="需携带 API Key，已由前端自动附加">⬇ 下载交付物</button>`
      );
      return `\u0000${slots.length - 1}\u0000`;
    }
    slots.push(`<code>${code}</code>`);
    return `\u0000${slots.length - 1}\u0000`;
  });
  /* [text](https?://url) */
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  /* **bold** */
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  /* 裸 32hex file_id：加等宽样式并提供复制（不自动下载） */
  s = s.replace(/(?<![\w/])([0-9a-f]{32})(?!\w)/g,
    '<code class="fid" title="点击复制 file_id">$1</code>');
  /* 还原占位符 */
  s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[+i] || '');
  return s;
}

function splitMdRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

function renderMarkdown(src) {
  const text = String(src ?? '').replace(/\r\n/g, '\n');
  if (!text.trim()) return '';
  const lines = text.split('\n');
  const slots = [];
  let html = '';
  let listType = null;
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    /* GFM 表格：当前行含 | 且下一行为 --- 分隔行 */
    if (line.includes('|') && i + 1 < lines.length
        && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      closeList();
      const header = splitMdRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitMdRow(lines[i]));
        i++;
      }
      i--;
      html += '<table class="md-table"><thead><tr>'
        + header.map((h) => `<th>${mdInline(h, slots)}</th>`).join('')
        + '</tr></thead><tbody>';
      for (const r of rows) {
        html += '<tr>' + header.map((_, c) => `<td>${mdInline(r[c] || '', slots)}</td>`).join('') + '</tr>';
      }
      html += '</tbody></table>';
      continue;
    }

    if (!line.trim()) { closeList(); continue; }

    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      closeList();
      const lv = m[1].length;
      html += `<h${lv} class="md-h md-h${lv}">${mdInline(m[2], slots)}</h${lv}>`;
    } else if (/^\s*-{3,}\s*$/.test(line)) {
      closeList(); html += '<hr class="md-hr" />';
    } else if ((m = line.match(/^\s*>\s?(.*)$/))) {
      closeList(); html += `<blockquote class="md-quote">${mdInline(m[1], slots)}</blockquote>`;
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      if (listType !== 'ul') { closeList(); html += '<ul class="md-ul">'; listType = 'ul'; }
      html += `<li>${mdInline(m[1], slots)}</li>`;
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      if (listType !== 'ol') { closeList(); html += '<ol class="md-ol">'; listType = 'ol'; }
      html += `<li>${mdInline(m[1], slots)}</li>`;
    } else {
      closeList();
      html += `<p class="md-p">${mdInline(line, slots)}</p>`;
    }
  }
  closeList();
  return html;
}

/* ═══════════════════════════════════════════════════════════════
 * 渲染
 * ═══════════════════════════════════════════════════════════════ */

function renderMessages() {
  const box = $('messages');
  box.innerHTML = state.messages.map(renderMsg).join('');
  box.scrollTop = box.scrollHeight;
}

function renderMsg(m) {
  if (m.kind === 'user') {
    return `<div class="msg user"><div class="bubble">${esc(m.text)}</div><div class="meta">${esc(m.time || '')}</div></div>`;
  }
  if (m.kind === 'error') return renderErrorMsg(m);
  if (m.kind === 'assistant') return renderAssistantMsg(m);
  if (m.kind === 'task') return renderTaskMsg(m);
  return '';
}

function renderErrorMsg(m) {
  return `<div class="msg error-card">
    <div class="err-head">请求失败 <span class="badge red">${esc(m.code || 'ERROR')}</span></div>
    <div class="err-msg">${esc(m.message || '')}</div>
    ${m.taskId ? `<div class="meta">task_id: <span class="mono">${esc(m.taskId)}</span></div>` : ''}
    ${m.traceId ? `<div class="meta">trace_id: <span class="copyable" data-action="copy-trace" data-trace="${esc(m.traceId)}" title="点击复制">${esc(m.traceId)}</span></div>` : ''}
  </div>`;
}

function renderAssistantMsg(m) {
  const interruptBlock = m.interrupt ? `<div class="interrupt-block">
      <b>命中人机中断（${esc(m.interrupt.type || '?')}）</b>
      ${m.interrupt.message ? `<div>${esc(m.interrupt.message)}</div>` : ''}
      <div class="muted">请直接回复以继续该会话。</div>
    </div>` : '';
  const outBlock = m.output
    ? `<div class="out${m.streaming ? '' : ' md'}">${m.streaming ? esc(m.output) : renderMarkdown(m.output)}</div>`
    : '';
  // 流式进行中且尚无输出时显示"正在生成…"光标
  const streamingBlock = (m.streaming && !m.output)
    ? '<span class="streaming-cursor">正在生成<span class="cursor-dot">…</span></span>'
    : '';
  const structBlock = m.structured
    ? `<details><summary>structured</summary><pre class="debug-pre">${esc(fmtJson(m.structured))}</pre></details>` : '';
  const planBlock = m.plan && m.plan.length
    ? `<details><summary>plan（${m.plan.length} 步）</summary><pre class="debug-pre">${esc(fmtJson(m.plan))}</pre></details>` : '';
  const usageTxt = m.usage && Object.keys(m.usage).length ? ` · usage ${esc(fmtJson(m.usage))}` : '';
  const head = [m.mode, m.scene].filter(Boolean).join(' · ');
  const emptyHint = (interruptBlock || streamingBlock) ? '' : '<span class="muted">（空输出）</span>';
  return `<div class="msg assistant">
    <div class="bubble">${outBlock}${streamingBlock}${emptyHint}${interruptBlock}${structBlock}${planBlock}</div>
    <div class="meta">${esc(head)}${m.elapsed != null ? ` · ${m.elapsed}ms` : ''}${usageTxt}
      ${m.traceId ? ` · <span class="copyable" data-action="copy-trace" data-trace="${esc(m.traceId)}" title="点击复制">trace ${esc(m.traceId)}</span>` : ''}
      <details class="raw"><summary>原始 JSON</summary><pre class="debug-pre">${esc(m.rawJson || '')}</pre></details>
    </div>
  </div>`;
}

function renderTaskMsg(m) {
  const color = STATUS_COLOR[m.status] || 'muted';
  const isTerminal = TERMINAL.has(m.status);
  const p = m.progress || {};
  const progressTxt = (p.step_total ? `步骤 ${p.step_index ?? '?'} / ${p.step_total}` : '')
    + (p.current_skill ? ` · ${p.current_skill}` : '');
  const it = m.interrupt;
  const interruptBlock = (m.status === 'waiting_human' && it) ? `
    <div class="interrupt-block">
      <b>人机中断（${esc(it.type || '?')}）</b>
      ${it.message ? `<div>${esc(it.message)}</div>` : ''}
      ${(it.tool || it.skill) ? `<div class="muted">tool=${esc(it.tool || '-')} · skill=${esc(it.skill || '-')}</div>` : ''}
      <div class="interrupt-row">
        <input class="interrupt-input" data-task="${esc(m.taskId)}" value="${esc(m.replyDraft || '')}"
               placeholder="${it.type === 'confirm' ? '通常回复 yes / no' : '补充信息…'}" />
        <button class="btn small" data-action="resume" data-task="${esc(m.taskId)}">恢复执行</button>
      </div>
    </div>` : '';
  const outBlock = m.output
    ? `<div class="out md">${renderMarkdown(m.output)}</div>` : '';
  const structBlock = m.structured
    ? `<details><summary>structured</summary><pre class="debug-pre">${esc(fmtJson(m.structured))}</pre></details>` : '';
  const planBlock = m.plan && m.plan.length
    ? `<details><summary>plan（${m.plan.length} 步）</summary><pre class="debug-pre">${esc(fmtJson(m.plan))}</pre></details>` : '';
  const errBlock = m.error ? `<div class="err-msg">${esc(m.error)}</div>` : '';
  const cancelBtn = (!isTerminal && m.status !== 'pending')
    ? `<button class="btn small danger" data-action="cancel" data-task="${esc(m.taskId)}">取消任务</button>` : '';
  const elapsedTxt = m.elapsed != null ? ` · 耗时 ${(m.elapsed / 1000).toFixed(1)}s` : '';
  const usageTxt = m.usage && Object.keys(m.usage).length ? ` · usage ${esc(fmtJson(m.usage))}` : '';
  return `<div class="msg task-card">
    <div class="task-head">任务 <span class="mono">${esc(m.taskId)}</span>
      <span class="badge ${color}">${esc(m.status)}</span>${cancelBtn}</div>
    ${m.scene ? `<div class="meta">scene=${esc(m.scene)}${m.mode ? ' · mode=' + esc(m.mode) : ''}</div>` : ''}
    ${progressTxt ? `<div class="meta">${esc(progressTxt)}</div>` : ''}
    ${planBlock}${outBlock}${structBlock}${errBlock}${interruptBlock}
    <div class="meta">${esc(m.updatedTime || '')}${elapsedTxt}${usageTxt}
      ${m.traceId ? ` · <span class="copyable" data-action="copy-trace" data-trace="${esc(m.traceId)}" title="点击复制">trace ${esc(m.traceId)}</span>` : ''}
      <details class="raw"><summary>原始 JSON</summary><pre class="debug-pre">${esc(m.rawJson || '')}</pre></details>
    </div>
  </div>`;
}

/* ═══════════════════════════════════════════════════════════════
 * 发送 / 同步 invoke / 异步 tasks / 轮询 / resume / cancel
 * ═══════════════════════════════════════════════════════════════ */

function pushError(r) {
  const err = (r.data && r.data.error) || {};
  const code = err.code || (r.status ? 'HTTP_' + r.status : 'NETWORK_ERROR');
  let msg = err.message || (typeof r.data === 'string' ? r.data : '') || r.networkError || '网络错误或服务不可达';
  if (r.status === 401) msg = '鉴权失败（401）：请检查顶栏 X-API-Key 配置';
  if (r.status === 413) msg = '响应超过 50KB 限制：' + msg;
  state.messages.push({
    kind: 'error', code, message: msg,
    traceId: err.trace_id || '', taskId: err.task_id || null,
  });
}

function setSendDisabled(disabled) {
  const btn = $('btn-send');
  btn.disabled = disabled;
  btn.classList.toggle('running', disabled);
  btn.textContent = disabled ? '执行中…' : '发送';
}

async function send() {
  if (state.sending) return;
  const text = $('user-input').value.trim();
  const scene = $('scene-select').value;
  let inputs = null;
  const raw = $('inputs-json').value.trim();
  if (raw) {
    try { inputs = JSON.parse(raw); }
    catch (e) {
      state.messages.push({ kind: 'error', code: 'INPUTS_INVALID', message: 'inputs 不是合法 JSON：' + e.message });
      renderMessages();
      return;
    }
  }
  if (!text && !scene) { alert('请输入消息或选择场景'); return; }

  /* 组装请求体（AgentRequest 契约） */
  const body = {};
  if (text) body.message = text;
  if (scene) body.scene = scene;
  if (inputs) body.inputs = inputs;
  if (state.threadId) body.thread_id = state.threadId;

  state.messages.push({ kind: 'user', text: text || `（场景 ${scene} 直达调用）`, time: nowTime() });
  $('user-input').value = '';
  renderMessages();

  state.sending = true;
  setSendDisabled(true);
  try {
    if (state.mode === 'stream') await invokeStream(body);
    else if (state.mode === 'sync') await invokeSync(body);
    else await submitTask(body);
  } finally {
    state.sending = false;
    setSendDisabled(false);
  }
}

/** P1 同步调用 POST /api/v1/agent/invoke */
async function invokeSync(body) {
  const r = await apiFetch('/api/v1/agent/invoke', { method: 'POST', body, timeoutMs: 600000 });
  showDebug(body, r);
  if (r.ok && r.data && !r.data.error) {
    if (r.data.thread_id) state.threadId = r.data.thread_id;
    state.messages.push({
      kind: 'assistant',
      output: r.data.output, structured: r.data.structured, plan: r.data.plan,
      interrupt: r.data.interrupt, usage: r.data.usage, mode: r.data.mode, scene: r.data.scene,
      traceId: r.data.trace_id, elapsed: r.elapsed, rawJson: fmtJson(r.data), time: nowTime(),
    });
    /* 技能可能产出交付物（xlsx 等），刷新文件列表 */
    loadFiles();
  } else {
    pushError(r);
  }
  saveSession();
  renderMessages();
}

/** 流式调用 POST /api/v1/agent/invoke/stream（NDJSON 逐 token 渲染） */
async function invokeStream(body) {
  const started = performance.now();
  // 先插入一条空 assistant 消息占位
  const msgIdx = state.messages.length;
  const assistantMsg = {
    kind: 'assistant', output: '', structured: null, plan: null,
    interrupt: null, usage: {}, mode: 'orchestrated', scene: body.scene || null,
    traceId: '', elapsed: null, rawJson: '', time: nowTime(), streaming: true,
  };
  state.messages.push(assistantMsg);
  renderMessages();

  const headers = {
    'Content-Type': 'application/json',
    'X-API-Key': state.apiKey,
    'X-Trace-Id': genTraceId(),
  };

  try {
    const resp = await fetch(apiUrl('/api/v1/agent/invoke/stream'), {
      method: 'POST', headers,
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const text = await resp.text();
      let data = null;
      try { data = JSON.parse(text); } catch { data = text; }
      showDebug(body, { ok: false, status: resp.status, data, elapsed: Math.round(performance.now() - started) });
      // 移除占位消息，显示错误
      state.messages.splice(msgIdx, 1);
      pushError({ ok: false, status: resp.status, data });
      saveSession();
      renderMessages();
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullOutput = '';
    let traceId = '';
    let scene = body.scene || null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // 保留未完整的行

      for (const line of lines) {
        if (!line.trim()) continue;
        let evt;
        try { evt = JSON.parse(line); } catch { continue; }

        if (evt.type === 'token') {
          fullOutput += evt.content;
          assistantMsg.output = fullOutput;
          // 直接更新 DOM，避免全量重绘闪烁
          updateStreamingOutput(msgIdx, fullOutput);
        } else if (evt.type === 'done') {
          fullOutput = evt.output || fullOutput;
          assistantMsg.output = fullOutput;
          if (evt.trace_id) { traceId = evt.trace_id; assistantMsg.traceId = traceId; }
          if (evt.scene != null) scene = evt.scene;
          assistantMsg.scene = scene;
          assistantMsg.taskId = evt.task_id;
          if (evt.thread_id) state.threadId = evt.thread_id;
        } else if (evt.type === 'error') {
          state.messages.splice(msgIdx, 1);
          state.messages.push({
            kind: 'error', code: evt.code || 'STREAM_ERROR',
            message: evt.message || '流式输出出错', traceId,
          });
          saveSession();
          renderMessages();
          return;
        }
      }
    }

    assistantMsg.streaming = false;
    assistantMsg.elapsed = Math.round(performance.now() - started);
    assistantMsg.rawJson = fmtJson({ output: fullOutput, trace_id: traceId, scene });
    /* 终态后刷新交付物列表 */
    loadFiles();
    showDebug(body, {
      ok: true, status: 200,
      data: { output: fullOutput, trace_id: traceId, scene },
      elapsed: assistantMsg.elapsed,
    });
    saveSession();
    renderMessages();
  } catch (e) {
    assistantMsg.streaming = false;
    state.messages.splice(msgIdx, 1);
    pushError({ ok: false, status: 0, data: null, networkError: String(e) });
    saveSession();
    renderMessages();
  }
}

/** 流式渲染：直接更新第 idx 条 assistant 消息的输出文本，避免全量重绘 */
function updateStreamingOutput(idx, text) {
  const box = $('messages');
  const msgEls = box.querySelectorAll('.msg.assistant');
  // 找到第 idx 条 assistant 消息（按 DOM 顺序）
  const target = msgEls[idx - countNonAssistantBefore(idx)];
  if (!target) return;

  const out = target.querySelector('.out');
  if (out) {
    out.textContent = text;
  } else {
    // 首次输出：移除"正在生成…"占位，插入 .out 元素
    const cursor = target.querySelector('.streaming-cursor');
    if (cursor) cursor.remove();
    const bubble = target.querySelector('.bubble');
    if (bubble) {
      const div = document.createElement('div');
      div.className = 'out';
      div.textContent = text;
      bubble.insertBefore(div, bubble.firstChild);
    }
  }
  box.scrollTop = box.scrollHeight;
}

/** 计算 idx 之前非 assistant 消息的数量（用于定位 DOM 中的 assistant 元素） */
function countNonAssistantBefore(idx) {
  let count = 0;
  for (let i = 0; i < idx; i++) {
    if (state.messages[i] && state.messages[i].kind !== 'assistant') count++;
  }
  return count;
}

/** P2 异步提交 POST /api/v1/agent/tasks（202）→ 启动轮询 */
async function submitTask(body) {
  const r = await apiFetch('/api/v1/agent/tasks', { method: 'POST', body });
  showDebug(body, r);
  if (r.ok && r.data && r.data.task_id) {
    if (r.data.thread_id) state.threadId = r.data.thread_id;
    state.messages.push({
      kind: 'task', taskId: r.data.task_id, threadId: r.data.thread_id,
      status: r.data.status || 'pending', submittedAt: Date.now(), updatedTime: nowTime(),
    });
    saveSession();
    renderMessages();
    startPolling(r.data.task_id);
  } else {
    pushError(r);
    saveSession();
    renderMessages();
  }
}

function startPolling(taskId) {
  stopPolling();
  state.pollTaskId = taskId;
  state.pollTimer = setInterval(() => pollOnce(taskId), 1000);
  pollOnce(taskId);
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  state.pollTaskId = null;
}

async function pollOnce(taskId) {
  const r = await apiFetch('/api/v1/agent/tasks/' + encodeURIComponent(taskId));
  if (r.status === 404) {
    stopPolling();
    updateTaskCard(taskId, { status: 'failed', error: '任务不存在或已过 TTL 清理' });
    return;
  }
  if (!r.ok || !r.data) return; /* 瞬时抖动，等下一轮 */
  if (r.data.thread_id && !state.threadId) state.threadId = r.data.thread_id;
  const wasTerminal = TERMINAL.has(
    (state.messages.find((x) => x.kind === 'task' && x.taskId === taskId) || {}).status || ''
  );
  updateTaskCard(taskId, r.data);
  /* 进入终态（成功）时刷新交付物列表 */
  if (TERMINAL.has(r.data.status) && r.data.status === 'completed' && !wasTerminal) loadFiles();
  if (TERMINAL.has(r.data.status)) stopPolling();
}

function updateTaskCard(taskId, t) {
  const m = state.messages.find((x) => x.kind === 'task' && x.taskId === taskId);
  if (!m) return;
  const wasTerminal = TERMINAL.has(m.status);
  Object.assign(m, {
    status: t.status ?? m.status,
    mode: t.mode ?? m.mode,
    scene: t.scene ?? m.scene,
    progress: t.progress ?? m.progress,
    plan: t.plan ?? m.plan,
    output: t.output ?? m.output,
    structured: t.structured ?? m.structured,
    interrupt: t.interrupt ?? m.interrupt,
    error: t.error ?? m.error,
    usage: t.usage ?? m.usage,
    traceId: t.trace_id ?? m.traceId,
    rawJson: fmtJson(t),
    updatedTime: nowTime(),
  });
  if (TERMINAL.has(m.status) && !wasTerminal) {
    m.elapsed = Date.now() - (m.submittedAt || Date.now());
    saveSession();
  }
  renderMessages();
}

/** P3 中断恢复：POST /tasks/{id}/resume */
async function resumeTask(taskId) {
  const m = state.messages.find((x) => x.kind === 'task' && x.taskId === taskId);
  const reply = (m && m.replyDraft || '').trim();
  if (!reply) { alert('请先填写回复内容'); return; }
  const r = await apiFetch(`/api/v1/agent/tasks/${encodeURIComponent(taskId)}/resume`, { method: 'POST', body: { reply } });
  showDebug({ reply }, r);
  if (r.ok) {
    if (m) { m.replyDraft = ''; m.interrupt = null; }
    pollOnce(taskId);
  } else {
    pushError(r);
    renderMessages();
  }
}

/** P2 取消：POST /tasks/{id}/cancel */
async function cancelTask(taskId) {
  if (!confirm('确认取消任务 ' + taskId + '？')) return;
  const r = await apiFetch(`/api/v1/agent/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' });
  showDebug(null, r);
  if (r.ok && r.data) updateTaskCard(taskId, r.data);
  else { pushError(r); renderMessages(); }
}

/* ═══════════════════════════════════════════════════════════════
 * 文档与交付物（/agent/files）
 *   上传（multipart）/ 列表 / 插入 file_id / 一键 HARA / 复制 /
 *   带 API Key 的鉴权下载（blob 落盘）/ 删除 / 拖拽上传
 * ═══════════════════════════════════════════════════════════════ */

function fmtSize(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}

function fmtFileTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ` +
    `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function renderFiles() {
  const ul = $('files-list');
  $('files-count').textContent = state.files.length;
  if (!state.files.length) {
    ul.innerHTML = '<li class="file-empty">暂无文件。上传 docx/xlsx/pdf 等相关项文档后，'
      + '点「插入 ID」或「HARA 分析」即可测试文档技能；技能产出的 xlsx 也会出现在这里。</li>';
    return;
  }
  ul.innerHTML = state.files.map((f) => {
    const isArtifact = f.kind === 'artifact';
    const kindBadge = isArtifact
      ? '<span class="badge green">交付物</span>'
      : '<span class="badge blue">上传</span>';
    const actions = [
      `<button class="btn ghost tiny" data-action="insert-fid" data-id="${esc(f.file_id)}" title="把 file_id 插入输入框">插入ID</button>`,
      !isArtifact
        ? `<button class="btn ghost tiny" data-action="quick-hara" data-id="${esc(f.file_id)}" title="填入 HARA 分析指令">HARA 分析</button>` : '',
      `<button class="btn ghost tiny" data-action="copy-fid" data-id="${esc(f.file_id)}" title="复制 file_id">复制</button>`,
      `<button class="btn ghost tiny" data-action="download-file" data-id="${esc(f.file_id)}" title="下载（自动带 API Key）">下载</button>`,
      `<button class="btn ghost tiny danger-text" data-action="delete-file" data-id="${esc(f.file_id)}" title="删除文件">删除</button>`,
    ].join('');
    return `<li class="file-item ${isArtifact ? 'is-artifact' : ''}">
      <div class="file-line1">${kindBadge}
        <span class="file-name" title="${esc(f.original_name)}">${esc(f.original_name)}</span>
      </div>
      <div class="file-line2">
        <span class="mono" title="${esc(f.file_id)}">${esc(f.file_id.slice(0, 8))}…</span>
        <span class="muted">${esc(fmtSize(f.size))} · ${esc(fmtFileTime(f.uploaded_at))}</span>
        <span class="file-actions">${actions}</span>
      </div>
    </li>`;
  }).join('');
}

async function loadFiles() {
  const r = await apiFetch('/api/v1/agent/files');
  if (r.ok && r.data) {
    state.files = r.data.files || [];
    state.filesLimits = r.data.limits || null;
    const lim = state.filesLimits;
    $('files-hint').textContent = lim
      ? `允许 ${(lim.allowed_extensions || []).join(' / ')}，单文件 ≤ ${lim.max_upload_mb}MB` : '';
    renderFiles();
  } else {
    $('files-hint').textContent = r.status === 401 ? '文件列表加载失败：API Key 无效' : '文件列表加载失败';
  }
}

function pickFile() {
  if (!state.apiKey) { alert('请先在顶栏填写 X-API-Key'); return; }
  $('file-input').click();
}

async function handleFileUpload(file) {
  /* 前端预校验（与后端沙箱白名单一致，以后端返回的 limits 为准） */
  const ext = (file.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
  const lim = state.filesLimits;
  if (lim && lim.allowed_extensions && !lim.allowed_extensions.includes(ext)) {
    state.messages.push({
      kind: 'error', code: 'FILE_TYPE_NOT_ALLOWED',
      message: `不支持的文件类型：${ext || '（无扩展名）'}；允许：${lim.allowed_extensions.join(' / ')}`,
    });
    renderMessages();
    return;
  }
  if (lim && file.size > lim.max_upload_mb * 1024 * 1024) {
    state.messages.push({
      kind: 'error', code: 'FILE_TOO_LARGE',
      message: `文件 ${file.name} 大小 ${fmtSize(file.size)} 超过上限 ${lim.max_upload_mb}MB`,
    });
    renderMessages();
    return;
  }

  const btn = $('btn-upload');
  btn.disabled = true;
  btn.textContent = '上传中…';
  const fd = new FormData();
  fd.append('file', file);
  const started = performance.now();
  try {
    const resp = await fetch(apiUrl('/api/v1/agent/files/upload'), {
      method: 'POST',
      headers: { 'X-API-Key': state.apiKey, 'X-Trace-Id': genTraceId() },
      body: fd,
    });
    const text = await resp.text();
    let data = null;
    try { data = JSON.parse(text); } catch { data = text; }
    showDebug({ upload: file.name, size: file.size }, { ok: resp.ok, status: resp.status, data, elapsed: Math.round(performance.now() - started) });
    if (resp.ok && data && data.file) {
      await loadFiles();
      /* 上传成功后自动把 file_id 放入输入框，便于直接补指令发送 */
      insertAtCursor($('user-input'), data.file.file_id);
    } else {
      const err = (data && data.error) || {};
      state.messages.push({
        kind: 'error', code: err.code || ('HTTP_' + resp.status),
        message: err.message || '上传失败', traceId: err.trace_id || '',
      });
      renderMessages();
    }
  } catch (e) {
    state.messages.push({ kind: 'error', code: 'NETWORK_ERROR', message: '上传请求失败：' + String(e) });
    renderMessages();
  } finally {
    btn.disabled = false;
    btn.textContent = '⬆ 上传文档';
  }
}

function insertAtCursor(el, text) {
  const start = el.selectionStart ?? el.value.length;
  const end = el.selectionEnd ?? el.value.length;
  const before = el.value.slice(0, start);
  const after = el.value.slice(end);
  const gap = before && !/\s$/.test(before) ? ' ' : '';
  el.value = before + gap + text + after;
  el.focus();
  const pos = (before + gap + text).length;
  el.setSelectionRange(pos, pos);
}

function quickHara(fileId) {
  $('user-input').value =
    `请基于我上传的相关项文档（file_id: ${fileId}）完成功能安全危害分析（HARA）：`
    + `按 ISO 26262 枚举功能、M01-M14 失效过滤、S/E/C 评级并生成 HARA xlsx 工作簿。`;
  $('user-input').focus();
}

async function copyText(text, el) {
  try {
    if (navigator.clipboard) await navigator.clipboard.writeText(text);
    if (el) flashCopied(el);
  } catch { /* 非安全上下文时静默：用户可手动从输入框复制 */ }
}

/** 下载需携带 X-API-Key，不能用普通 <a href>，走 fetch → blob 保存 */
async function downloadFile(fileId, triggerEl) {
  const old = triggerEl ? triggerEl.textContent : '';
  if (triggerEl) { triggerEl.disabled = true; triggerEl.textContent = '下载中…'; }
  try {
    const resp = await fetch(
      apiUrl(`/api/v1/agent/files/${encodeURIComponent(fileId)}/download`),
      { headers: { 'X-API-Key': state.apiKey, 'X-Trace-Id': genTraceId() } }
    );
    if (!resp.ok) {
      const text = await resp.text();
      let msg = `下载失败（HTTP ${resp.status}）`;
      try { msg = JSON.parse(text).error.message || msg; } catch { /* 保留默认 */ }
      state.messages.push({ kind: 'error', code: 'DOWNLOAD_FAILED', message: msg });
      renderMessages();
      return;
    }
    const blob = await resp.blob();
    const filename = (resp.headers.get('Content-Disposition') || '')
      .match(/filename\*?=(?:UTF-8'')?["']?([^;"']+)/i)?.[1]
      || (state.files.find((f) => f.file_id === fileId)?.original_name)
      || fileId;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = decodeURIComponent(filename);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (e) {
    state.messages.push({ kind: 'error', code: 'NETWORK_ERROR', message: '下载请求失败：' + String(e) });
    renderMessages();
  } finally {
    if (triggerEl) { triggerEl.disabled = false; triggerEl.textContent = old || '下载'; }
  }
}

async function deleteFile(fileId) {
  const f = state.files.find((x) => x.file_id === fileId);
  if (!confirm(`确认删除文件「${f ? f.original_name : fileId}」？`)) return;
  const r = await apiFetch('/api/v1/agent/files/' + encodeURIComponent(fileId), { method: 'DELETE' });
  showDebug({ delete: fileId }, r);
  if (r.ok) await loadFiles();
  else pushError(r);
  renderMessages();
}

function toggleFiles() {
  state.filesCollapsed = !state.filesCollapsed;
  localStorage.setItem('pg_files_collapsed', state.filesCollapsed ? '1' : '0');
  applyFilesCollapse();
}

function applyFilesCollapse() {
  $('files-panel').classList.toggle('hidden', state.filesCollapsed);
}

/* 拖拽上传：拖入整个对话区时显示提示层，放下即上传 */
function bindDragUpload() {
  const panel = $('files-panel');
  const drop = $('files-drop');
  let depth = 0;

  const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes('Files');
  const onEnter = (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    if (state.filesCollapsed) { state.filesCollapsed = false; applyFilesCollapse(); }
    drop.classList.remove('hidden');
  };
  const onOver = (e) => { if (hasFiles(e)) e.preventDefault(); };
  const onLeave = (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) drop.classList.add('hidden');
  };
  const onDrop = (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    drop.classList.add('hidden');
    if (!state.apiKey) { alert('请先在顶栏填写 X-API-Key'); return; }
    for (const file of Array.from(e.dataTransfer.files)) handleFileUpload(file);
  };

  for (const target of [panel, $('user-input'), $('messages')]) {
    target.addEventListener('dragenter', onEnter);
    target.addEventListener('dragover', onOver);
    target.addEventListener('dragleave', onLeave);
    target.addEventListener('drop', onDrop);
  }
}

/* ═══════════════════════════════════════════════════════════════
 * 调试面板 / capabilities / readyz
 * ═══════════════════════════════════════════════════════════════ */

function showDebug(req, r) {
  const trace = (r && r.data && (r.data.trace_id || (r.data.error && r.data.error.trace_id))) || '';
  state.lastTraceId = trace;
  $('debug-meta').innerHTML =
    `trace: <span class="copyable" data-action="copy-trace" data-trace="${esc(trace)}" title="点击复制">${esc(trace || '(无)')}</span>` +
    ` · HTTP ${r ? r.status : '-'}` +
    ` · ${r ? r.elapsed : '-'}ms`;
  $('debug-req').textContent = req ? fmtJson(req) : '// -';
  $('debug-res').textContent = r ? fmtJson(r.data ?? r.status) : '// -';
}

function fillScenes() {
  const sel = $('scene-select');
  sel.innerHTML = '<option value="">（智能编排：不指定场景）</option>' +
    state.scenes.map((s) =>
      `<option value="${esc(s.scene)}" title="${esc(s.description)}">${esc(s.scene)} — ${esc(s.description)}</option>`
    ).join('');
}

async function loadCapabilities() {
  const r = await apiFetch('/api/v1/agent/capabilities');
  if (r.ok && r.data && r.data.scenes) {
    state.scenes = r.data.scenes;
    fillScenes();
  } else {
    $('scene-select').innerHTML = '<option value="">（capabilities 加载失败，可改用智能编排）</option>';
  }
}

async function checkReady() {
  const dot = $('ready-dot'), txt = $('ready-text');
  dot.className = 'dot gray';
  txt.textContent = '检测中…';
  try {
    const resp = await fetch(apiUrl('/readyz'), { cache: 'no-store' });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      dot.className = 'dot green';
      txt.textContent = '服务就绪';
    } else {
      dot.className = 'dot red';
      const bad = Object.entries(data.checks || {})
        .filter(([, v]) => v !== 'ok').map(([k, v]) => `${k}: ${v}`).join('; ');
      txt.textContent = '未就绪' + (bad ? `（${bad}）` : '');
    }
  } catch {
    dot.className = 'dot red';
    txt.textContent = '服务不可达';
  }
}

/* ═══════════════════════════════════════════════════════════════
 * 会话历史（localStorage）
 * ═══════════════════════════════════════════════════════════════ */

function loadSessions() {
  try { return JSON.parse(localStorage.getItem('pg_sessions') || '[]'); }
  catch { return []; }
}

function saveSession() {
  if (!state.threadId && !state.messages.length) return;
  const sessions = loadSessions();
  const firstUser = state.messages.find((m) => m.kind === 'user');
  const snap = {
    threadId: state.threadId,
    title: (firstUser ? firstUser.text : '会话').slice(0, 24),
    savedAt: Date.now(),
    messages: state.messages,
  };
  const idx = sessions.findIndex((s) => s.threadId === snap.threadId);
  if (idx >= 0) sessions[idx] = snap; else sessions.unshift(snap);
  try {
    localStorage.setItem('pg_sessions', JSON.stringify(sessions.slice(0, 20)));
  } catch {
    /* 超出配额：丢弃到最近 5 条再试 */
    try { localStorage.setItem('pg_sessions', JSON.stringify(sessions.slice(0, 5))); } catch { /* 放弃持久化 */ }
  }
  renderSessions();
}

function renderSessions() {
  const sessions = loadSessions();
  const ul = $('session-list');
  if (!sessions.length) {
    ul.innerHTML = '<li class="empty">暂无历史会话</li>';
    return;
  }
  ul.innerHTML = sessions.map((s, i) => {
    const d = new Date(s.savedAt);
    const time = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${nowTimeOf(d)}`;
    return `<li data-action="open-session" data-idx="${i}" title="thread_id: ${esc(s.threadId || '(未生成)')}">
      <span class="sess-title">${esc(s.title || '会话')}</span>
      <span class="sess-time">${time}</span>
      <button class="sess-del" data-action="del-session" data-idx="${i}" title="删除">×</button>
    </li>`;
  }).join('');
}

function nowTimeOf(d) {
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function openSession(idx) {
  const s = loadSessions()[idx];
  if (!s) return;
  stopPolling();
  state.threadId = s.threadId || null;
  state.messages = s.messages || [];
  renderMessages();
}

function deleteSession(idx) {
  const sessions = loadSessions();
  sessions.splice(idx, 1);
  localStorage.setItem('pg_sessions', JSON.stringify(sessions));
  renderSessions();
}

function newSession() {
  stopPolling();
  state.threadId = null;
  state.messages = [];
  renderMessages();
}

/* ═══════════════════════════════════════════════════════════════
 * 事件绑定 + 初始化
 * ═══════════════════════════════════════════════════════════════ */

function flashCopied(el) {
  const old = el.textContent;
  el.textContent = '已复制';
  setTimeout(() => { el.textContent = old; }, 800);
}

function bindEvents() {
  $('btn-send').addEventListener('click', send);
  $('user-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  /* 配置变更即时持久化 */
  $('api-base').addEventListener('change', (e) => {
    state.apiBase = e.target.value.trim();
    localStorage.setItem('pg_api_base', state.apiBase);
    checkReady();
    loadCapabilities();
  });
  $('api-key').addEventListener('change', (e) => {
    state.apiKey = e.target.value.trim();
    localStorage.setItem('pg_api_key', state.apiKey);
    loadCapabilities();
    loadFiles();
  });
  $('mode-select').addEventListener('change', (e) => {
    state.mode = e.target.value;
    localStorage.setItem('pg_mode', state.mode);
  });

  /* 选择文件后立即上传 */
  $('file-input').addEventListener('change', (e) => {
    for (const file of Array.from(e.target.files || [])) handleFileUpload(file);
    e.target.value = '';   /* 允许再次选择同一文件 */
  });

  /* 事件委托：所有 data-action 按钮 / 可点元素 */
  document.addEventListener('click', (e) => {
    /* Markdown 输出里的裸 file_id 代码块：点击复制（无 data-action） */
    const fidEl = e.target.closest('code.fid');
    if (fidEl) { copyText(fidEl.textContent, fidEl); return; }
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const act = el.dataset.action;
    if (act === 'send') send();
    else if (act === 'toggle-inputs') $('inputs-json').classList.toggle('hidden');
    else if (act === 'new-session') newSession();
    else if (act === 'check-ready') checkReady();
    else if (act === 'pick-file') pickFile();
    else if (act === 'refresh-files') loadFiles();
    else if (act === 'toggle-files') toggleFiles();
    else if (act === 'insert-fid') insertAtCursor($('user-input'), el.dataset.id || '');
    else if (act === 'quick-hara') quickHara(el.dataset.id || '');
    else if (act === 'copy-fid') copyText(el.dataset.id || '', el);
    else if (act === 'download-file') downloadFile(el.dataset.id || '', el);
    else if (act === 'delete-file') deleteFile(el.dataset.id || '');
    else if (act === 'copy-trace') {
      const val = el.dataset.trace || '';
      if (navigator.clipboard) navigator.clipboard.writeText(val).catch(() => {});
      flashCopied(el);
    }
    else if (act === 'resume') resumeTask(el.dataset.task);
    else if (act === 'cancel') cancelTask(el.dataset.task);
    else if (act === 'open-session') openSession(+el.dataset.idx);
    else if (act === 'del-session') { e.stopPropagation(); deleteSession(+el.dataset.idx); }
  });

  /* 中断回复草稿：避免全量重绘丢失输入 */
  document.addEventListener('input', (e) => {
    const t = e.target;
    if (t.classList && t.classList.contains('interrupt-input')) {
      const m = state.messages.find((x) => x.kind === 'task' && x.taskId === t.dataset.task);
      if (m) m.replyDraft = t.value;
    }
  });
}

function init() {
  $('api-base').value = state.apiBase;
  $('api-key').value = state.apiKey;
  $('mode-select').value = state.mode;
  bindEvents();
  bindDragUpload();
  applyFilesCollapse();
  renderSessions();
  renderMessages();
  checkReady();
  loadCapabilities();
  loadFiles();
  setInterval(checkReady, 30000);
  /* 恢复最近一次会话 */
  const sessions = loadSessions();
  if (sessions.length && sessions[0].messages && sessions[0].messages.length) openSession(0);
}

init();
