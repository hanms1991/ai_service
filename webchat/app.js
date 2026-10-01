/* AutoAgent 对话页（原生 JS，无第三方依赖） */
(() => {
  "use strict";

  const state = {
    user: null,
    conversations: [],
    currentId: null,
    scenes: [],
    sending: false,
    abortCtrl: null,
    elapsedTimer: null,
    uploads: [],   // 当前用户的上传文件
    artifacts: [], // 当前会话的交付物
  };

  const HINTS = [
    "正在分析你的请求…",
    "正在读取文档与知识库…",
    "正在规划执行步骤…",
    "模型生成中，长任务可能需要数分钟…",
    "仍在执行中，请耐心等待…",
  ];

  const $ = (id) => document.getElementById(id);

  // ── 请求封装（同源 Cookie 鉴权；401 回登录页） ────────────────
  async function api(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", ...opts });
    if (r.status === 401) {
      location.href = "/chat/login.html";
      throw new Error("unauthorized");
    }
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error((d.error && d.error.message) || `HTTP ${r.status}`);
    }
    return r.json();
  }

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ── Markdown 渲染（done 后一次性执行；先 esc 再放标签，防 XSS） ──
  function inlineMd(s) {
    return s
      .replace(/`([^`\n]+)`/g, (_, c) => `<code>${c}</code>`)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|\/[^)\s]*|#[^)\s]*)\)/g,
        (_, t, u) => `<a href="${u}" target="_blank" rel="noopener">${t}</a>`);
  }

  function fmtTable(rows) {
    // rows: 原始表格行数组（含表头、--- 分隔行）
    const cells = (ln) =>
      ln.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
    const head = cells(rows[0]);
    const body = rows.slice(2).map(cells);
    const th = head.map((c) => `<th>${inlineMd(esc(c))}</th>`).join("");
    const trs = body.map(
      (r) => "<tr>" + r.map((c) => `<td>${inlineMd(esc(c))}</td>`).join("") + "</tr>"
    );
    return `<table><thead><tr>${th}</tr></thead><tbody>${trs.join("")}</tbody></table>`;
  }

  function fmt(text) {
    const blocks = String(text ?? "").split(/```/);
    const out = [];
    for (let i = 0; i < blocks.length; i++) {
      if (i % 2 === 1) {
        out.push(`<pre><code>${esc(blocks[i].replace(/^[a-zA-Z0-9_+-]*\n/, ""))}</code></pre>`);
        continue;
      }
      const lines = blocks[i].replace(/\r/g, "").split("\n");
      let html = "";
      let para = [];
      let list = null; // {tag, items:[]}
      const flushPara = () => {
        if (para.length) {
          html += `<p>${inlineMd(esc(para.join(" ")))}</p>`;
          para = [];
        }
      };
      const flushList = () => {
        if (list) {
          const lis = list.items.map((it) => `<li>${inlineMd(esc(it))}</li>`).join("");
          html += `<${list.tag}>${lis}</${list.tag}>`;
          list = null;
        }
      };
      let i2 = 0;
      while (i2 < lines.length) {
        const t = lines[i2].trim();
        // 表格：当前行像表头且下一行为分隔行
        if (/^\|.*\|\s*$/.test(t)
            && /^\|[\s:|-]+\|\s*$/.test((lines[i2 + 1] || "").trim())) {
          flushPara(); flushList();
          const tbl = [t, lines[i2 + 1].trim()];
          let j = i2 + 2;
          while (j < lines.length && /^\|.*\|\s*$/.test(lines[j].trim())) {
            tbl.push(lines[j].trim()); j++;
          }
          html += fmtTable(tbl);
          i2 = j;
          continue;
        }
        let m;
        if ((m = t.match(/^(#{1,4})\s+(.*)$/))) {
          flushPara(); flushList();
          html += `<h${m[1].length}>${inlineMd(esc(m[2]))}</h${m[1].length}>`;
        } else if (/^([-*_])\1{2,}\s*$/.test(t)) {
          flushPara(); flushList();
          html += "<hr>";
        } else if ((m = t.match(/^>\s?(.*)$/))) {
          flushPara(); flushList();
          html += `<blockquote>${inlineMd(esc(m[1]))}</blockquote>`;
        } else if ((m = t.match(/^[-*+]\s+(.*)$/))) {
          flushPara();
          if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
          list.items.push(m[1]);
        } else if ((m = t.match(/^\d+[.、]\s+(.*)$/))) {
          flushPara();
          if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
          list.items.push(m[1]);
        } else if (t === "") {
          flushPara(); flushList();
        } else {
          flushList();
          para.push(t);
        }
        i2++;
      }
      flushPara(); flushList();
      out.push(html);
    }
    return out.join("");
  }

  // ── 流式增量 Markdown 渲染器 ────────────────────────────────
  // 后端 token 实时到达（PRD 实测中位间隔 ~3ms），渲染策略：
  // 已闭合的块（标题/段落/列表/表格/代码围栏）立即按 Markdown 定稿并 append，
  // 未完成的尾部保持转义纯文本逐字显示，未闭合代码围栏整体降级为 <pre>。
  // 每帧只重绘「尾部」（通常几十字），长文档下成本与总长度无关，不冻结页面。
  function createStreamRenderer(bubble) {
    bubble.innerHTML =
      '<div class="md-done"></div>' +
      '<div class="md-tail" hidden><span class="tail-text"></span>' +
      '<span class="stream-caret" aria-hidden="true"></span></div>';
    const doneWrap = bubble.querySelector(".md-done");
    const tailWrap = bubble.querySelector(".md-tail");
    const tailText = bubble.querySelector(".tail-text");

    let rendered = 0;      // 已定稿 append 的块数
    let lastHead = null;
    let lastBlocks = [];
    let finalized = false;

    // 切出「块边界完整的前缀」+ 尾部；尾部可能是未闭合代码围栏
    function split(text) {
      const fences = [];
      const re = /^\u0060{3}/gm;
      let m;
      while ((m = re.exec(text))) fences.push(m.index);
      if (fences.length % 2 === 1) {
        // 未闭合围栏：围栏起点之后全部按代码态尾部处理
        return { head: text.slice(0, fences[fences.length - 1]),
                 tail: text.slice(fences[fences.length - 1]), code: true };
      }
      const idx = text.lastIndexOf("\n\n");
      if (idx < 0) return { head: "", tail: text, code: false };
      return { head: text.slice(0, idx + 2), tail: text.slice(idx + 2), code: false };
    }

    // head 的块边界已完整：按空行切块，代码围栏（可能含内部空行）整体一块
    function splitBlocks(head) {
      if (!head) return [];
      const lines = head.split("\n");
      const blocks = [];
      let buf = [];
      const flush = () => {
        if (buf.length && buf.some((l) => l.trim())) blocks.push(buf.join("\n"));
        buf = [];
      };
      for (let i = 0; i < lines.length; i++) {
        if (lines[i].trim().startsWith("```")) {
          flush();
          const cb = [lines[i]];
          i++;
          while (i < lines.length && !lines[i].trim().startsWith("```")) {
            cb.push(lines[i]); i++;
          }
          if (i < lines.length) cb.push(lines[i]); // 闭合围栏行
          blocks.push(cb.join("\n"));
        } else if (lines[i].trim() === "") {
          flush();
        } else {
          buf.push(lines[i]);
        }
      }
      flush();
      return blocks;
    }

    return {
      update(text) {
        if (finalized) return;
        const { head, tail, code } = split(text);
        if (head !== lastHead) {
          lastBlocks = splitBlocks(head);
          lastHead = head;
          // 理论上块前缀只增不变；异常收缩时整体重建
          if (lastBlocks.length < rendered) {
            doneWrap.innerHTML = "";
            rendered = 0;
          }
          for (let i = rendered; i < lastBlocks.length; i++) {
            const d = document.createElement("div");
            d.className = "md-block";
            d.innerHTML = fmt(lastBlocks[i]);
            doneWrap.appendChild(d);
          }
          rendered = lastBlocks.length;
        }
        if (tail) {
          tailWrap.hidden = false;
          tailWrap.className = code ? "md-tail tail-code" : "md-tail";
          tailText.textContent = tail;
        } else {
          tailWrap.hidden = true;
        }
      },
      finalize(text) {
        if (finalized) return;
        finalized = true;
        bubble.innerHTML = fmt(text) || "（无回复内容）";
      },
      fail(text) {
        if (finalized) return;
        finalized = true;
        bubble.textContent = text;
      },
    };
  }

  // ── 会话列表 ────────────────────────────────────────────────
  function renderConvList() {
    const ul = $("conv-list");
    if (!state.conversations.length) {
      ul.innerHTML = '<div class="conv-empty">暂无会话</div>';
      return;
    }
    ul.innerHTML = "";
    state.conversations.forEach((c) => {
      const div = document.createElement("div");
      div.className = "conv-item" + (c.id === state.currentId ? " active" : "");
      div.innerHTML =
        `<span class="conv-title" title="${esc(c.title)}">${esc(c.title)}</span>
         <span class="conv-ops">
           <button class="icon-btn act-rename" title="重命名">✎</button>
           <button class="icon-btn act-del" title="删除">✕</button>
         </span>`;
      div.querySelector(".conv-title").onclick = () => selectConv(c.id);
      div.querySelector(".act-rename").onclick = (e) => {
        e.stopPropagation();
        renameConv(c);
      };
      div.querySelector(".act-del").onclick = (e) => {
        e.stopPropagation();
        deleteConv(c);
      };
      ul.appendChild(div);
    });
  }

  async function loadConvs(selectId = null) {
    const d = await api("/ui/api/conversations");
    state.conversations = d.conversations || [];
    if (selectId) state.currentId = selectId;
    if (!state.currentId && state.conversations[0]) {
      state.currentId = state.conversations[0].id;
    }
    renderConvList();
    if (state.currentId) await selectConv(state.currentId);
    else showEmpty();
  }

  function showEmpty() {
    state.currentId = null;
    $("messages").innerHTML =
      `<div class="empty-state">
         <div class="big">AutoAgent</div>
         <div>输入问题开始对话；可上传文档并发起 HARA 等功能分析</div>
       </div>`;
    $("topbar-title").textContent = "";
    state.artifacts = [];
    renderChips();
  }

  async function selectConv(id) {
    if (state.sending) stopSend(true); // 离开当前会话时取消正在进行的流
    state.currentId = id;
    renderConvList();
    const conv = state.conversations.find((c) => c.id === id);
    $("topbar-title").textContent = conv ? conv.title : "";
    $("scene-select").value = (conv && conv.scene) || "";

    const [msgD, artD] = await Promise.all([
      api(`/ui/api/conversations/${id}/messages`),
      api(`/ui/api/files?conversation_id=${id}&kind=artifact`),
    ]);
    state.artifacts = artD.files || [];
    const msgs = $("messages");
    msgs.innerHTML = "";
    (msgD.messages || []).forEach((m) => appendMsgEl(m.role, m.content, !!m.is_error));
    scrollBottom();
    renderChips();
  }

  async function createConv() {
    const scene = $("scene-select").value;
    const d = await api("/ui/api/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scene }),
    });
    await loadConvs(d.conversation.id);
  }

  async function renameConv(c) {
    const title = prompt("会话标题", c.title);
    if (title === null || !title.trim()) return;
    await api(`/ui/api/conversations/${c.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title.trim() }),
    });
    await loadConvs(state.currentId);
  }

  async function deleteConv(c) {
    if (state.sending && state.currentId === c.id) stopSend(true);
    if (!confirm(`确定删除会话「${c.title}」？历史消息将一并删除。`)) return;
    await api(`/ui/api/conversations/${c.id}`, { method: "DELETE" });
    if (state.currentId === c.id) state.currentId = null;
    await loadConvs();
  }

  // ── 消息渲染 ────────────────────────────────────────────────
  function appendMsgEl(role, content, isError = false) {
    const empty = $("empty-state");
    if (empty) empty.remove();
    const row = document.createElement("div");
    row.className = `msg-row ${role}`;
    const who = role === "user" ? state.user.username.slice(0, 1) : "AI";
    row.innerHTML =
      `<div class="msg-avatar">${esc(who)}</div>
       <div class="msg-body ${isError ? "error" : ""}">
         <div class="msg-bubble">${fmt(content)}</div>
       </div>`;
    $("messages").appendChild(row);
    scrollBottom();
    return row.querySelector(".msg-bubble");
  }

  function scrollBottom() {
    const box = $("messages");
    box.scrollTop = box.scrollHeight;
  }

  // ── 进度状态条 + 计时器 ─────────────────────────────────────
  function startStatus(bubble) {
    let t0 = Date.now();
    let hintIdx = 0;
    const line = document.createElement("div");
    line.className = "status-line";
    line.innerHTML =
      `<span class="dots"><span></span><span></span><span></span></span>
       <span class="stage">${HINTS[0]}</span>
       <span class="elapsed"></span>`;
    bubble.after(line);
    const tick = () => {
      const sec = Math.floor((Date.now() - t0) / 1000);
      line.querySelector(".elapsed").textContent = `${sec}s`;
      // 没有真实阶段事件推进时，本地轮播提示，缓解长时间等待焦虑
      if (sec > 0 && sec % 8 === 0) {
        hintIdx = (hintIdx + 1) % HINTS.length;
        const st = line.querySelector(".stage");
        if (!st.dataset.live) st.textContent = HINTS[hintIdx];
      }
    };
    tick();
    state.elapsedTimer = setInterval(tick, 1000);
    return line;
  }

  function stopStatus() {
    if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
  }

  // ── 静默心跳：正文已开始但 token 中断超过 8s（上游慢/异常处理中）时提示，
  // 避免光标空闪让用户误以为页面卡死；新 token 到达立即收起 ──
  function startHeartbeat(bubble) {
    const line = document.createElement("div");
    line.className = "status-line heartbeat";
    line.hidden = true;
    line.innerHTML =
      `<span class="dots"><span></span><span></span><span></span></span>
       <span class="stage">模型仍在生成中，内容较长请耐心等待…</span>
       <span class="elapsed"></span>`;
    bubble.after(line);
    let last = Date.now();
    let armed = false; // 首字落地前由「思考中」状态条负责，心跳不参与
    const timer = setInterval(() => {
      if (!armed) return;
      const gap = Math.floor((Date.now() - last) / 1000);
      if (gap >= 8) {
        line.hidden = false;
        line.querySelector(".elapsed").textContent = `已等待 ${gap}s`;
      } else if (!line.hidden) {
        line.hidden = true;
      }
    }, 1000);
    return {
      // 首字落地：接管进度提示
      arm() { armed = true; last = Date.now(); },
      // token 到达：重置静默计时并收起提示
      poke() { if (armed) { last = Date.now(); line.hidden = true; } },
      stop() { clearInterval(timer); if (line.parentNode) line.remove(); },
    };
  }

  // ── 发送 / 停止 / 流式接收 ──────────────────────────────────
  function setSendingUI(on) {
    state.sending = on;
    const btn = $("btn-send");
    btn.textContent = on ? "停止" : "发送";
    btn.classList.toggle("stop", on);
    btn.disabled = false;
  }

  function stopSend(silent = false) {
    if (state.abortCtrl) {
      state.abortCtrl._silent = silent;
      state.abortCtrl.abort();
    }
  }

  async function send() {
    const input = $("input");
    const message = input.value.trim();
    if (!message || state.sending) return;

    setSendingUI(true);
    // 注意：AbortController 必须在 createConv() 之后创建。
    // 首次会话时 createConv → loadConvs → selectConv，而 selectConv 会在
    // state.sending=true 时调用 stopSend() 取消「上一个」流；若提前建好
    // controller，会被这条初始化链路误 abort，导致 fetch 立即失败显示「已停止生成」。
    let statusLine = null;
    let heartbeat = null;
    let renderer = null;
    let full = "";
    let stageFaded = false;

    const fadeStage = () => {
      if (stageFaded) return;
      stageFaded = true;
      if (statusLine) statusLine.classList.add("fading");
      stopStatus();
      if (heartbeat) heartbeat.arm(); // 正文阶段改由静默心跳值守
    };

    try {
      if (!state.currentId) await createConv();
      state.abortCtrl = new AbortController();
      const convId = state.currentId;
      const scene = $("scene-select").value;
      input.value = "";
      autoGrow();
      appendMsgEl("user", message);
      const bubble = appendMsgEl("assistant", "");
      renderer = createStreamRenderer(bubble);
      statusLine = startStatus(bubble);
      heartbeat = startHeartbeat(bubble);

      const r = await fetch(`/ui/api/conversations/${convId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ message, scene }),
        signal: state.abortCtrl.signal,
      });
      if (r.status === 401) { location.href = "/chat/login.html"; return; }
      if (!r.ok || !r.body) {
        renderer.fail(`请求失败（HTTP ${r.status}）`);
        return;
      }

      // rAF 节流：每帧至多一次增量渲染（只重绘未完成尾部，长文档不卡）
      let scheduled = false;
      const paint = () => {
        scheduled = false;
        renderer.update(full);
        scrollBottom();
      };
      let finalized = false;

      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          let evt;
          try { evt = JSON.parse(line); } catch { continue; }
          if (evt.type === "token") {
            full += evt.content || "";
            fadeStage(); // 首字落地：思考中状态条淡出，进入正文流式
            heartbeat.poke();
            if (!scheduled) { scheduled = true; requestAnimationFrame(paint); }
          } else if (evt.type === "status" && evt.stage && !stageFaded) {
            const st = statusLine && statusLine.querySelector(".stage");
            if (st) { st.textContent = evt.stage; st.dataset.live = "1"; }
          } else if (evt.type === "done") {
            finalized = true;
            full = evt.output || full;
            renderer.finalize(full);
            scrollBottom();
          } else if (evt.type === "error") {
            finalized = true;
            bubble.parentElement.classList.add("error");
            renderer.fail(`[${evt.code || "ERROR"}] ${evt.message || "执行失败"}`);
            scrollBottom();
          }
        }
      }
      if (!finalized && !state.abortCtrl.signal.aborted) {
        // 流结束但没收到 done/error（异常断流）
        if (full) renderer.finalize(full);
        else renderer.fail("（连接异常中断）");
      }

      await Promise.all([
        loadConvsKeep(convId),
        refreshArtifacts(convId),
      ]);
    } catch (err) {
      if (err.name === "AbortError") {
        // 用户主动停止：服务端会把已输出部分落库，本地只做展示收尾
        if (renderer) {
          if (full.trim()) {
            renderer.finalize(full.replace(/\s*$/, "") + "\n\n*（已停止生成）*");
          } else {
            renderer.fail("（已停止生成）");
          }
        }
      } else {
        appendMsgEl("assistant", `[NETWORK_ERROR] ${err.message}`, true);
      }
    } finally {
      stopStatus();
      if (heartbeat) heartbeat.stop();
      if (statusLine && statusLine.parentNode) statusLine.remove();
      state.abortCtrl = null;
      setSendingUI(false);
      $("input").focus();
    }
  }

  // 刷新列表但保持当前会话消息区不重载（避免打断渲染）
  async function loadConvsKeep(keepId) {
    const d = await api("/ui/api/conversations");
    state.conversations = d.conversations || [];
    state.currentId = keepId;
    renderConvList();
    const conv = state.conversations.find((c) => c.id === keepId);
    if (conv) $("topbar-title").textContent = conv.title;
  }

  // ── 文件：上传 / 引用 / 下载 / 删除 / 交付物 ────────────────
  function renderChips() {
    const box = $("chips");
    box.innerHTML = "";
    if (state.artifacts.length) {
      const label = document.createElement("span");
      label.className = "chips-label";
      label.textContent = "本会话交付物：";
      box.appendChild(label);
      state.artifacts.forEach((f) => box.appendChild(fileChip(f, "artifact")));
    }
    if (state.uploads.length) {
      const label = document.createElement("span");
      label.className = "chips-label";
      label.textContent = "我的文档：";
      box.appendChild(label);
      state.uploads.slice(0, 10).forEach((f) => box.appendChild(fileChip(f, "upload")));
    }
  }

  function fileChip(f, kind) {
    const span = document.createElement("span");
    span.className = "chip" + (kind === "artifact" ? " artifact" : "");
    const icon = kind === "artifact" ? "📊" : "📄";
    span.innerHTML =
      `${icon} <span class="chip-name">${esc(f.filename || f.file_id)}</span>
       <a href="/ui/api/files/${esc(f.file_id)}/download" title="下载">下载</a>`;
    if (kind === "upload") {
      const ins = document.createElement("button");
      ins.textContent = "引用";
      ins.title = "把 file_id 插入输入框";
      ins.onclick = () => {
        const ta = $("input");
        ta.value += (ta.value && !ta.value.endsWith(" ") ? " " : "") + f.file_id;
        ta.focus();
      };
      span.appendChild(ins);
    }
    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "✕";
    del.title = "删除文件";
    del.onclick = async () => {
      if (!confirm("删除该文件？")) return;
      await api(`/ui/api/files/${f.file_id}`, { method: "DELETE" });
      await refreshUploads();
    };
    span.appendChild(del);
    return span;
  }

  async function refreshUploads() {
    const d = await api("/ui/api/files?kind=upload");
    state.uploads = d.files || [];
    renderChips();
  }

  async function refreshArtifacts(convId) {
    const d = await api(`/ui/api/files?conversation_id=${convId}&kind=artifact`);
    state.artifacts = d.files || [];
    renderChips();
  }

  async function uploadFile(file) {
    const fd = new FormData();
    fd.append("file", file);
    try {
      await api("/ui/api/files/upload", { method: "POST", body: fd });
      await refreshUploads();
    } catch (err) {
      alert("上传失败：" + err.message);
    }
  }

  // ── 输入框自适应 ────────────────────────────────────────────
  function autoGrow() {
    const ta = $("input");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
  }

  // ── 初始化 ──────────────────────────────────────────────────
  async function init() {
    const me = await api("/ui/api/me").catch(() => null);
    if (!me) { location.href = "/chat/login.html"; return; }
    state.user = me.user;
    $("user-name").textContent = state.user.username;
    $("user-avatar").textContent = state.user.username.slice(0, 1).toUpperCase();

    const sc = await api("/ui/api/scenes").catch(() => ({ scenes: [] }));
    state.scenes = sc.scenes || [];
    const sel = $("scene-select");
    state.scenes.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.scene;
      opt.textContent = s.description && s.description !== s.scene
        ? `${s.scene} · ${s.description}` : s.scene;
      sel.appendChild(opt);
    });

    $("btn-new").onclick = () => {
      if (state.sending) stopSend(true);
      state.currentId = null; showEmpty(); $("input").focus();
    };
    $("btn-logout").onclick = async () => {
      if (!confirm("退出登录？")) return;
      await api("/ui/api/logout", { method: "POST" });
      location.href = "/chat/login.html";
    };
    $("btn-send").onclick = () => (state.sending ? stopSend() : send());
    $("input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        state.sending ? stopSend() : send();
      }
    });
    $("input").addEventListener("input", autoGrow);
    $("btn-attach").onclick = () => $("file-input").click();
    $("file-input").addEventListener("change", (e) => {
      Array.from(e.target.files || []).forEach(uploadFile);
      e.target.value = "";
    });

    await Promise.all([loadConvs(), refreshUploads()]);
    $("input").focus();
  }

  init();
})();
