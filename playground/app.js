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
  const outBlock = m.output ? `<div class="out">${esc(m.output)}</div>` : '';
  const structBlock = m.structured
    ? `<details><summary>structured</summary><pre class="debug-pre">${esc(fmtJson(m.structured))}</pre></details>` : '';
  const planBlock = m.plan && m.plan.length
    ? `<details><summary>plan（${m.plan.length} 步）</summary><pre class="debug-pre">${esc(fmtJson(m.plan))}</pre></details>` : '';
  const usageTxt = m.usage && Object.keys(m.usage).length ? ` · usage ${esc(fmtJson(m.usage))}` : '';
  const head = [m.mode, m.scene].filter(Boolean).join(' · ');
  return `<div class="msg assistant">
    <div class="bubble">${outBlock || (interruptBlock ? '' : '<span class="muted">（空输出）</span>')}${interruptBlock}${structBlock}${planBlock}</div>
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
  const outBlock = m.output ? `<div class="out">${esc(m.output)}</div>` : '';
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
    if (state.mode === 'sync') await invokeSync(body);
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
  } else {
    pushError(r);
  }
  saveSession();
  renderMessages();
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
  updateTaskCard(taskId, r.data);
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
  });
  $('mode-select').addEventListener('change', (e) => {
    state.mode = e.target.value;
    localStorage.setItem('pg_mode', state.mode);
  });

  /* 事件委托：所有 data-action 按钮 / 可点元素 */
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const act = el.dataset.action;
    if (act === 'send') send();
    else if (act === 'toggle-inputs') $('inputs-json').classList.toggle('hidden');
    else if (act === 'new-session') newSession();
    else if (act === 'check-ready') checkReady();
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
  renderSessions();
  renderMessages();
  checkReady();
  loadCapabilities();
  setInterval(checkReady, 30000);
  /* 恢复最近一次会话 */
  const sessions = loadSessions();
  if (sessions.length && sessions[0].messages && sessions[0].messages.length) openSession(0);
}

init();
