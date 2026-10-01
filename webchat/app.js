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
    uploads: [],   // 当前用户待发送的上传文件
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

  // ── 会话列表（分组：置顶 / 今天 / 最近7天 / 最近30天 / 更早） ──
  function parseConvDate(s) {
    // 后端时间为本地时区 "YYYY-MM-DD HH:MM:SS"，补 T 让浏览器按本地时间解析
    const d = new Date(String(s || "").replace(" ", "T"));
    return isNaN(d.getTime()) ? Date.now() : d.getTime();
  }

  function convGroups(list) {
    const now = new Date();
    const today0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const DAY = 86400000;
    const groups = [
      { key: "pinned", label: "置顶", items: [] },
      { key: "today", label: "今天", items: [] },
      { key: "week", label: "最近7天", items: [] },
      { key: "month", label: "最近30天", items: [] },
      { key: "older", label: "更早", items: [] },
    ];
    list.forEach((c) => {
      if (c.pinned) { groups[0].items.push(c); return; }
      const days = Math.floor((today0 - parseConvDate(c.updated_at)) / DAY);
      if (days <= 0) groups[1].items.push(c);
      else if (days <= 7) groups[2].items.push(c);
      else if (days <= 30) groups[3].items.push(c);
      else groups[4].items.push(c);
    });
    return groups.filter((g) => g.items.length);
  }

  function renderConvList() {
    const ul = $("conv-list");
    if (!state.conversations.length) {
      ul.innerHTML = '<div class="conv-empty">暂无会话</div>';
      return;
    }
    ul.innerHTML = "";
    convGroups(state.conversations).forEach((g) => {
      const h = document.createElement("div");
      h.className = "conv-group-label";
      h.textContent = g.label;
      ul.appendChild(h);
      g.items.forEach((c) => ul.appendChild(buildConvItem(c)));
    });
  }

  function buildConvItem(c) {
    const div = document.createElement("div");
    div.className = "conv-item" + (c.id === state.currentId ? " active" : "")
      + (c.pinned ? " pinned" : "");
    div.innerHTML =
      `<span class="conv-title" title="${esc(c.title)}">
         ${c.pinned ? '<span class="pin-flag" title="已置顶">📌</span>' : ""}${esc(c.title)}
       </span>
       <span class="conv-ops">
         <button class="icon-btn act-pin${c.pinned ? " on" : ""}" title="${c.pinned ? "取消置顶" : "置顶"}">📌</button>
         <button class="icon-btn act-rename" title="重命名">✎</button>
         <button class="icon-btn act-del" title="删除">✕</button>
       </span>`;
    div.querySelector(".conv-title").onclick = () => selectConv(c.id);
    div.querySelector(".act-pin").onclick = (e) => {
      e.stopPropagation();
      togglePin(c);
    };
    div.querySelector(".act-rename").onclick = (e) => {
      e.stopPropagation();
      renameConv(c, div);
    };
    div.querySelector(".act-del").onclick = (e) => {
      e.stopPropagation();
      deleteConv(c, div);
    };
    return div;
  }

  // ── 锚点弹层：确认删除 / 重命名输入（定位在被操作会话项附近，非浏览器顶部） ──
  let popoverState = null;
  function closeItemPopover() {
    if (!popoverState) return;
    popoverState.backdrop.remove();
    popoverState.pop.remove();
    document.removeEventListener("keydown", popoverState.onKey, true);
    popoverState = null;
  }

  function showItemPopover(anchorEl, innerHTML) {
    closeItemPopover();
    const backdrop = document.createElement("div");
    backdrop.className = "pop-backdrop";
    const pop = document.createElement("div");
    pop.className = "item-popover";
    pop.innerHTML = innerHTML;
    document.body.appendChild(backdrop);
    document.body.appendChild(pop);

    const close = () => closeItemPopover();
    backdrop.onclick = close;
    pop.onclick = (e) => e.stopPropagation();
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey, true);
    popoverState = { backdrop, pop, onKey };

    // 定位：优先在会话项下方靠左；空间不足翻到上方；横向不超出视口
    const r = anchorEl.getBoundingClientRect();
    pop.style.visibility = "hidden";
    const w = pop.offsetWidth;
    const h = pop.offsetHeight;
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    let left = r.left;
    if (left + w > vw - 8) left = Math.max(8, vw - w - 8);
    let top = r.bottom + 6;
    if (top + h > vh - 8) top = Math.max(8, r.top - h - 6);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    pop.style.visibility = "visible";
    return { pop, close };
  }

  async function togglePin(c) {
    const next = !c.pinned;
    try {
      await api(`/ui/api/conversations/${c.id}/pin`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pinned: next }),
      });
      const d = await api("/ui/api/conversations");
      state.conversations = d.conversations || [];
      renderConvList();
    } catch (err) {
      alert("操作失败：" + err.message);
    }
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
    renderChips();
  }

  async function selectConv(id) {
    if (state.sending) stopSend(true); // 离开当前会话时取消正在进行的流
    state.currentId = id;
    renderConvList();
    const conv = state.conversations.find((c) => c.id === id);
    $("topbar-title").textContent = conv ? conv.title : "";
    $("scene-select").value = (conv && conv.scene) || "";

    const msgD = await api(`/ui/api/conversations/${id}/messages`);
    const msgs = $("messages");
    msgs.innerHTML = "";
    (msgD.messages || []).forEach((m) =>
      appendMsgEl(m.role, m.content, !!m.is_error, m.attachments || null,
                  m.id || null, m.feedback || 0));
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

  function renameConv(c, anchorEl) {
    const { pop, close } = showItemPopover(anchorEl,
      `<div class="pop-title">重命名会话</div>
       <input class="pop-input" type="text" maxlength="100" value="${esc(c.title)}">
       <div class="pop-actions">
         <button class="pop-btn" data-act="cancel">取消</button>
         <button class="pop-btn pop-btn-primary" data-act="ok">确定</button>
       </div>`);
    const input = pop.querySelector(".pop-input");
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    const submit = async () => {
      const title = input.value.trim();
      if (!title) { input.focus(); return; }
      close();
      try {
        await api(`/ui/api/conversations/${c.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title }),
        });
        await loadConvs(state.currentId);
      } catch (err) {
        alert("重命名失败：" + err.message);
      }
    };
    pop.querySelector('[data-act="ok"]').onclick = submit;
    pop.querySelector('[data-act="cancel"]').onclick = close;
    input.onkeydown = (e) => {
      e.stopPropagation();
      if (e.key === "Enter") submit();
    };
  }

  function deleteConv(c, anchorEl) {
    if (state.sending && state.currentId === c.id) stopSend(true);
    const { pop, close } = showItemPopover(anchorEl,
      `<div class="pop-title">删除会话</div>
       <div class="pop-text">确定删除会话「${esc(c.title)}」？历史消息将一并删除。</div>
       <div class="pop-actions">
         <button class="pop-btn" data-act="cancel">取消</button>
         <button class="pop-btn pop-btn-danger" data-act="ok">删除</button>
       </div>`);
    pop.querySelector('[data-act="cancel"]').onclick = close;
    pop.querySelector('[data-act="ok"]').onclick = async () => {
      close();
      try {
        await api(`/ui/api/conversations/${c.id}`, { method: "DELETE" });
        if (state.currentId === c.id) state.currentId = null;
        await loadConvs();
      } catch (err) {
        alert("删除失败：" + err.message);
      }
    };
  }

  // ── 消息渲染 ────────────────────────────────────────────────
  // 扩展名 → 卡片图标/类型标签/配色
  const FILE_TYPES = {
    doc: { icon: "W", label: "Word", cls: "ft-word" },
    docx: { icon: "W", label: "Word", cls: "ft-word" },
    xls: { icon: "X", label: "Excel", cls: "ft-excel" },
    xlsx: { icon: "X", label: "Excel", cls: "ft-excel" },
    csv: { icon: "X", label: "Excel", cls: "ft-excel" },
    ppt: { icon: "P", label: "PowerPoint", cls: "ft-ppt" },
    pptx: { icon: "P", label: "PowerPoint", cls: "ft-ppt" },
    pdf: { icon: "PDF", label: "PDF", cls: "ft-pdf" },
    txt: { icon: "T", label: "文本", cls: "ft-text" },
    md: { icon: "T", label: "Markdown", cls: "ft-text" },
    json: { icon: "{ }", label: "JSON", cls: "ft-text" },
    html: { icon: "< >", label: "网页", cls: "ft-text" },
    htm: { icon: "< >", label: "网页", cls: "ft-text" },
  };

  // 文件卡片行：用户附件在文字气泡上方（右侧），AI 产物在回复下方（左侧），点击下载
  function appendFileRows(attachments, role) {
    const who = role === "user" ? state.user.username.slice(0, 1) : "AI";
    attachments.forEach((f) => {
      const name = f.filename || f.file_id;
      const ext = (name.includes(".") ? name.split(".").pop() : "").toLowerCase();
      const t = FILE_TYPES[ext] || { icon: "•", label: ext ? ext.toUpperCase() : "文件", cls: "ft-file" };
      const row = document.createElement("div");
      row.className = `msg-row ${role} attach-row`;
      row.innerHTML =
        `<div class="msg-avatar">${esc(who)}</div>
         <div class="msg-body">
           <a class="file-bubble" href="/ui/api/files/${esc(f.file_id)}/download"
              target="_blank" rel="noopener" title="点击下载 ${esc(name)}">
             <span class="file-icon ${t.cls}">${t.icon}</span>
             <span class="file-meta">
               <span class="file-name">${esc(name)}</span>
               <span class="file-type">${t.label}</span>
             </span>
           </a>
         </div>`;
      $("messages").appendChild(row);
    });
    scrollBottom();
  }

  function appendMsgEl(role, content, isError = false, attachments = null,
                       msgId = null, feedback = 0) {
    const empty = $("empty-state");
    if (empty) empty.remove();
    // 用户附件先以卡片输出（文字在上一条消息之后、文字气泡之前）
    if (role === "user" && attachments && attachments.length) {
      appendFileRows(attachments, "user");
    }
    const row = document.createElement("div");
    row.className = `msg-row ${role}`;
    if (msgId) row.dataset.msgId = String(msgId);
    const who = role === "user" ? state.user.username.slice(0, 1) : "AI";
    row.innerHTML =
      `<div class="msg-avatar">${esc(who)}</div>
       <div class="msg-body ${isError ? "error" : ""}">
         <div class="msg-bubble">${fmt(content)}</div>
         ${buildMsgActions(role, feedback)}
       </div>`;
    $("messages").appendChild(row);
    bindMsgActions(row, role);
    // AI 产物卡片挂在回复气泡下方（左侧），点击可下载
    if (role === "assistant" && attachments && attachments.length) {
      appendFileRows(attachments, "assistant");
    }
    scrollBottom();
    return row.querySelector(".msg-bubble");
  }

  // 操作栏 SVG 图标（lucide 风格，16x16，currentColor 继承）
  const ICONS = {
    copy: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    redo: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>',
    up: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>',
    down: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/></svg>',
  };

  function buildMsgActions(role, feedback = 0) {
    const upOn = feedback === 1 ? " on" : "";
    const downOn = feedback === -1 ? " on" : "";
    if (role === "user") {
      return `<div class="msg-actions">
        <button class="act-btn act-copy" title="复制">${ICONS.copy}</button>
      </div>`;
    }
    return `<div class="msg-actions">
      <button class="act-btn act-copy" title="复制">${ICONS.copy}</button>
      <button class="act-btn act-redo" title="重新生成">${ICONS.redo}</button>
      <button class="act-btn act-up${upOn}" title="赞">${ICONS.up}</button>
      <button class="act-btn act-down${downOn}" title="踩">${ICONS.down}</button>
    </div>`;
  }

  function bindMsgActions(row, role) {
    const bubble = row.querySelector(".msg-bubble");
    const actions = row.querySelector(".msg-actions");
    if (!actions) return;

    // 复制：取气泡纯文本（保留换行）
    const copyBtn = actions.querySelector(".act-copy");
    if (copyBtn) {
      copyBtn.addEventListener("click", async () => {
        const text = bubble ? bubble.innerText : "";
        if (!text.trim()) return;
        try {
          await navigator.clipboard.writeText(text);
        } catch {
          const ta = document.createElement("textarea");
          ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
          document.body.appendChild(ta); ta.select();
          try { document.execCommand("copy"); } catch {}
          ta.remove();
        }
        flashIcon(copyBtn, "已复制");
      });
    }

    if (role !== "assistant") return;

    // 重新生成：重发该助手消息的上一条用户消息
    const redoBtn = actions.querySelector(".act-redo");
    if (redoBtn) {
      redoBtn.addEventListener("click", () => {
        const rows = Array.from($("messages").querySelectorAll(".msg-row"));
        const idx = rows.indexOf(row);
        const prevUser = [...rows.slice(0, idx)].reverse()
          .find((r) => r.classList.contains("user"));
        if (!prevUser) return;
        const text = (prevUser.querySelector(".msg-bubble") || {}).innerText || "";
        if (!text.trim()) return;
        send(text.trim());
      });
    }

    // 赞 / 踩：互斥（再点取消），失败回滚
    const upBtn = actions.querySelector(".act-up");
    const downBtn = actions.querySelector(".act-down");
    const setFeedback = async (btn, value) => {
      const msgId = row.dataset.msgId;
      if (!msgId) return;
      const wasUp = upBtn.classList.contains("on");
      const wasDown = downBtn.classList.contains("on");
      const toggle = (value === 1 && wasUp) || (value === -1 && wasDown);
      const next = toggle ? 0 : value;
      // 乐观更新
      upBtn.classList.toggle("on", next === 1);
      downBtn.classList.toggle("on", next === -1);
      try {
        await api(`/ui/api/conversations/${state.currentId}/messages/${msgId}/feedback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: next }),
        });
      } catch {
        // 回滚
        upBtn.classList.toggle("on", wasUp);
        downBtn.classList.toggle("on", wasDown);
      }
    };
    if (upBtn) upBtn.addEventListener("click", () => setFeedback(upBtn, 1));
    if (downBtn) downBtn.addEventListener("click", () => setFeedback(downBtn, -1));
  }

  // 图标按钮短暂的反馈态（复制成功提示）
  function flashIcon(btn, label) {
    const orig = btn.innerHTML;
    btn.classList.add("ok");
    btn.innerHTML = `<span class="act-tip">${esc(label)}</span>`;
    setTimeout(() => { btn.innerHTML = orig; btn.classList.remove("ok"); }, 1200);
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
      `<div class="status-main">
         <span class="dots"><span></span><span></span><span></span></span>
         <span class="stage">${HINTS[0]}</span>
         <span class="elapsed"></span>
       </div>
       <div class="stage-history"></div>`;
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

  async function send(prefillText) {
    const input = $("input");
    const message = (typeof prefillText === "string" ? prefillText : input.value).trim();
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
    let assistantRow = null;

    const fadeStage = () => {
      if (stageFaded) return;
      stageFaded = true;
      if (statusLine) statusLine.classList.add("fading");
      stopStatus();
      if (heartbeat) heartbeat.arm(); // 正文阶段改由静默心跳值守
    };

    try {
      // 本次发送携带的附件（快照）：渲染成附件气泡后即从输入框暂存区移除
      const sentFiles = state.uploads.slice();
      const sentIds = new Set(sentFiles.map((f) => f.file_id));

      if (!state.currentId) await createConv();
      state.abortCtrl = new AbortController();
      const convId = state.currentId;
      const scene = $("scene-select").value;
      if (typeof prefillText !== "string") { input.value = ""; autoGrow(); }
      appendMsgEl("user", message, false, sentFiles);
      state.uploads = state.uploads.filter((f) => !sentIds.has(f.file_id));
      renderChips();
      const bubble = appendMsgEl("assistant", "");
      assistantRow = bubble.closest(".msg-row");
      renderer = createStreamRenderer(bubble);
      statusLine = startStatus(bubble);
      heartbeat = startHeartbeat(bubble);

      const _fids = sentFiles.map((f) => f.file_id);
      const r = await fetch(`/ui/api/conversations/${convId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          message,
          scene,
          // 自动携带当前所有已上传文档（用户不可见），后端校验归属后注入上下文
          file_ids: _fids,
        }),
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
            // 真实步骤推进：旧阶段收进已完成列表（✓，最多留 3 条），显示新阶段文案
            const st = statusLine && statusLine.querySelector(".stage");
            if (st && st.textContent !== evt.stage) {
              if (st.dataset.live === "1") {
                const hist = statusLine.querySelector(".stage-history");
                if (hist) {
                  const item = document.createElement("div");
                  item.className = "stage-done";
                  item.innerHTML =
                    `<span class="tick">✓</span><span>${esc(st.textContent)}</span>`;
                  hist.appendChild(item);
                  while (hist.children.length > 3) hist.removeChild(hist.firstChild);
                }
              }
              st.textContent = evt.stage;
              st.dataset.live = "1";
            }
          } else if (evt.type === "done") {
            finalized = true;
            full = evt.output || full;
            renderer.finalize(full);
            scrollBottom();
          } else if (evt.type === "artifacts") {
            // AI 产物：以文件卡片挂在助手回复气泡下方，点击下载
            if (Array.isArray(evt.files) && evt.files.length) {
              appendFileRows(evt.files, "assistant");
            }
          } else if (evt.type === "msg_saved") {
            // 助手消息已落库，记录 msg_id 供赞/踩反馈使用
            if (assistantRow && evt.msg_id) {
              assistantRow.dataset.msgId = String(evt.msg_id);
            }
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
        refreshUploads(),
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
    // 仅展示「待发送」的上传文档；AI 产物以文件卡片挂在助手回复气泡下方
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
      `${icon} <span class="chip-name">${esc(f.filename || f.file_id)}</span>`;
    // 已上传文档在发送时自动携带，无需手动引用
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
