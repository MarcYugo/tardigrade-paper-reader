/* 首页：上传转换 + 论文阅读记录列表 */
(function () {
  const drop = document.getElementById("drop");
  const fileInput = document.getElementById("file");
  const cardsEl = document.getElementById("cards");
  const sharedWrap = document.getElementById("sharedWrap");
  const sharedEl = document.getElementById("sharedCards");
  const emptyEl = document.getElementById("empty");
  const statsEl = document.getElementById("stats");
  const overlay = document.getElementById("overlay");
  const ovText = document.getElementById("ovText");
  const filterSeg = document.getElementById("filterSeg");
  let filter = "all";
  let allDocs = [];

  /* ---------- 上传 ---------- */
  function pick() { fileInput.click(); }
  drop.addEventListener("click", pick);
  fileInput.addEventListener("change", () => upload(fileInput.files));
  ["dragover", "dragenter"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", e => upload(e.dataTransfer.files));

  async function upload(files) {
    const f = files && files[0];
    if (!f) return;
    if (!/\.pdf$/i.test(f.name)) { toast("请选择 PDF 文件", 3000); return; }
    overlay.classList.add("show");
    ovText.textContent = "正在解析并转换：" + f.name + " …";
    try {
      const fd = new FormData();
      fd.append("file", f);
      const meta = await api("/api/convert", { method: "POST", body: fd });
      toast("转换完成 ✅");
      location.href = "reader.html?id=" + meta.id;
    } catch (err) {
      toast("转换失败：" + err.message, 4000);
    } finally {
      overlay.classList.remove("show");
    }
  }

  /* ---------- 列表 ---------- */
  const ST = { unread: ["未读", "tag unread"], reading: ["在读", "tag reading"], done: ["已读完", "tag done"] };

  function cardHtml(d) {
    const st = ST[d.status] || ST.unread;
    const pct = Math.round((d.progress || 0) * 100);
    const date = (d.last_read_at || d.created_at || "").replace("T", " ").slice(0, 16);
    const mine = !!d.mine;
    // 自己的：显示“共享 N 人”；别人的：显示来自谁 + 我的权限
    const permTag = mine
      ? (d.shared_count ? `<span class="tag share">共享 ${d.shared_count}</span>` : "")
      : `<span class="tag share">来自 ${esc(d.owner)} · ${d.perm === "write" ? "可写" : "只读"}</span>`;
    const acts = mine
      ? `<button class="btn" data-act="share" title="把这篇（含笔记）共享给别的账号">🔗 共享${d.shared_count ? " (" + d.shared_count + ")" : ""}</button>
         <button class="btn danger" data-act="del">删除</button>`
      : `<button class="btn ghost" data-act="leave" title="不再看这篇共享给我的论文">退出共享</button>`;
    return `<div class="card${mine ? "" : " shared-card"}" data-id="${esc(d.id)}">
      <div class="tags"><span class="${st[1]}">${st[0]}</span>
        ${d.notes ? `<span class="tag note">笔记 ${d.notes}</span>` : ""}
        ${d.highlights ? `<span class="tag hl">高亮 ${d.highlights}</span>` : ""}
        ${permTag}</div>
      <h3 title="${esc(d.title)}">${esc(d.title)}</h3>
      <div class="meta">
        <span>${d.num_pages || 0} 页</span>
        <span>${pct}%</span>
        ${date ? `<span>${esc(date)}</span>` : ""}
      </div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="actions">
        <button class="btn primary" data-act="open">阅读</button>
        ${acts}
      </div>
    </div>`;
  }

  function render() {
    const list = allDocs.filter(d => filter === "all" || d.status === filter);
    const mine = list.filter(d => d.mine);
    const others = list.filter(d => !d.mine);
    cardsEl.innerHTML = mine.map(cardHtml).join("");
    sharedEl.innerHTML = others.map(cardHtml).join("");
    sharedWrap.hidden = others.length === 0;
    emptyEl.hidden = list.length > 0;
    renderStats();
  }

  function renderStats() {
    const unread = allDocs.filter(d => d.status === "unread").length;
    const reading = allDocs.filter(d => d.status === "reading").length;
    const done = allDocs.filter(d => d.status === "done").length;
    const others = allDocs.filter(d => !d.mine).length;
    statsEl.innerHTML = `<span>共 ${allDocs.length} 篇</span>
      <span>在读 ${reading}</span><span>读完 ${done}</span><span>未读 ${unread}</span>`
      + (others ? `<span>共享给我 ${others}</span>` : "");
  }

  async function load() {
    try {
      allDocs = await api("/api/docs");
    } catch (err) { allDocs = []; }
    render();
  }

  async function handleAct(btn) {
    const card = btn.closest(".card");
    const id = card.dataset.id;
    const doc = allDocs.find(d => d.id === id) || {};
    if (btn.dataset.act === "open") location.href = "reader.html?id=" + id;
    if (btn.dataset.act === "share") openShareModal(id, doc.title || "", load);
    if (btn.dataset.act === "del") {
      if (!confirm("确定删除该论文及其全部标注、笔记吗？")) return;
      try {
        await api("/api/doc/" + id, { method: "DELETE" });
        toast("已删除");
        load();
      } catch (err) { toast(err.message); }
    }
    if (btn.dataset.act === "leave") {
      if (!confirm("退出共享？你将不再看到这篇论文及其笔记（论文本身不会被删）。")) return;
      try {
        await api("/api/doc/" + id + "/share/leave", { method: "POST" });
        toast("已退出共享");
        load();
      } catch (err) { toast(err.message); }
    }
  }

  /* 自己的卡片和“共享给我的”两块共用一个点击入口 */
  [cardsEl, sharedEl].forEach(grid => grid.addEventListener("click", e => {
    const btn = e.target.closest("button[data-act]");
    if (btn) handleAct(btn);
  }));

  filterSeg.addEventListener("click", e => {
    const b = e.target.closest("button[data-f]");
    if (!b) return;
    filter = b.dataset.f;
    filterSeg.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    render();
  });

  // 从阅读页“后退”回来时页面可能来自 bfcache（没有重新请求），而那边刚改过标题/
  // 进度/笔记数：回到前台就重新拉一次列表，保证和阅读页显示的一致。
  window.addEventListener("pageshow", e => { if (e.persisted) load(); });

  load();
})();
