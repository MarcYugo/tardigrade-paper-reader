/* ============ 句子笔记（正文内嵌卡片 + 右侧“全部笔记”抽屉）：从 reader2.js 拆出来的模块 ============
   与 r2-math.js / r2-ai.js / r2-nav.js 同一套路：整段**按原样搬过来**（缩进没动），
   改动只有一类：跨模块共享的**可变状态**改成 `env.xxx`，因为本段会读写它们：
     · anno        —— 读句子笔记与独立笔记，并且**整体重新赋值**→ 需要 setter
     · editingKey  —— 正在编辑哪条笔记（`块id#句号`），段内多次赋值 → setter
     · noteDraft   —— 编辑框草稿，段内多次赋值 → setter
     · showNotesOn —— “显示笔记”开关，段内赋值 → setter
     · sel         —— 只读 → getter
     · DOC         —— 只读 → getter
     · myName      —— 当前登录账号（只读，用来区分“我写的”笔记）→ getter
   其余（$ / itemById / inkCache / docId 等 const，以及 20 多个函数声明）装配时取一次即可。

   装配（reader2.js 原位置）：
     const { notesOfBlock, notesCount, renderAllNotes, openNoteEditor, refreshSideList,
             openSidePanel, jumpToNoteId } =
       R2Notes({ … });
     `canWrite` / `renderTocAi` 声明在**本段之后**（共享权限段 / AI 段装配处），
     装配这一刻还在 TDZ → 包一层转发，调用时才去读。

   对外接口在文件末尾的 return，包含笔记列表、编辑与 Agent 定位操作。
   ============================================================================ */
window.R2Notes = function (env) {
  "use strict";
  const { $, itemById, docId, toScrollDelta, inkCache,
          keyParts, relayoutAll, placeBar, updateBar, relayoutPage, needWrite, findItem,
          sentKey, sentenceText, noteOf, selCount, selAnchor, isSelRef, pageOfItem,
          render, renderHmenuSwatches, setAnnoRanges, rebuildItem, selectSentence, syncExp,
          canWrite, renderTocAi } = env;
  let freeDraft = null;

  /* ================= 句子笔记(内嵌在正文里) =================
     一条笔记 = 一个句子，卡片直接插在该句所属文字块下方（与译文同一套“顶开版式”），
     默认全部展开，读正文时就能同时看到对应笔记；右侧面板只做“全部笔记”总览。 */
  function notesOfBlock(item) {
    return (env.anno.notes || [])
      .filter(x => keyParts(x.block_id).id === item.id)
      .sort((a, b) => keyParts(a.block_id).si - keyParts(b.block_id).si);
  }
  function notesCount() { return (env.anno.notes || []).length; }

  /* 笔记作者标签：**只有共享论文才有** ——
     后端只在论文有共享名单时才下发 `note.by`（没共享时字段直接被摘掉，见
     backend/records.py::anno_with_authors），所以这里“有 by 就画”即可，
     不需要前端再判一次权限/共享。自己写的加 `.me`，一眼能分出哪些是我写的。 */
  function authorChip(note) {
    if (!note || !note.by) return null;
    const who = el("span", "nwho", note.by);
    if (env.myName && note.by === env.myName) who.classList.add("me");
    return who;
  }

  function renderNotes(item) {
    if (!item || item.kind !== "blk" || !item.nbox) return;
    const box = item.nbox;
    box.textContent = "";
    const list = notesOfBlock(item);
    const editing = env.editingKey ? keyParts(env.editingKey) : null;
    for (const n of list) {
      const si = keyParts(n.block_id).si;
      const isEditing = !!editing && editing.id === item.id && editing.si === si;
      if (!env.showNotesOn && !isEditing) continue;      // 关掉“显示笔记”时只留正在编辑的那条
      box.appendChild(noteCardEl(item, n, si, isEditing));
    }
    // 正在新写、但还没有存下来的那条：先放一个空的编辑卡
    if (editing && editing.id === item.id &&
        !list.some(n => keyParts(n.block_id).si === editing.si)) {
      box.appendChild(noteCardEl(item, null, editing.si, true));
    }
    box.hidden = box.childElementCount === 0;
    syncExp(item);
  }
  function renderAllNotes() {
    for (const id in itemById) renderNotes(itemById[id]);
    $("notesCount").textContent = notesCount();
  }

  function noteCardEl(item, note, si, editing) {
    const key = note ? note.block_id : sentKey(item, si);
    const card = el("div", "ncard" + (editing ? " editing" : ""));
    const sentTxt = sentenceText(item, si) || "（原文句已不存在）";

    const head = el("div", "nhead");
    head.appendChild(el("span", "nhk", "📝 笔记"));
    const who = authorChip(note);
    if (who) head.appendChild(who);
    head.appendChild(el("span", "nsent",
      "「" + (sentTxt.length > 60 ? sentTxt.slice(0, 60) + "…" : sentTxt) + "」"));
    const acts = el("span", "nact");
    // 只读共享：不画写入口（不然点了才报 403，体验很差）
    if (!editing && canWrite()) {
      const bEdit = el("button", "", note ? "编辑" : "写笔记");
      bEdit.addEventListener("click", () => {
        env.editingKey = key; env.noteDraft = note ? note.text : "";
        renderNotes(item); relayoutPage(item.pi); focusNoteBox(item);
      });
      acts.appendChild(bEdit);
      if (note) {
        const bDel = el("button", "del", "删除");
        bDel.addEventListener("click", () => deleteNote(note, item));
        acts.appendChild(bDel);
      }
    }
    head.appendChild(acts);
    card.appendChild(head);

    if (editing) {
      const ta = el("textarea", "ntext");
      ta.placeholder = "写下对这一句的理解、疑问或批注…（Ctrl+Enter 保存，Esc 取消）";
      ta.value = env.noteDraft;
      ta.addEventListener("input", () => { env.noteDraft = ta.value; relayoutPage(item.pi); });
      ta.addEventListener("keydown", e => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveNote(item, key); }
        else if (e.key === "Escape") { e.preventDefault(); cancelNoteEdit(item); }
      });
      card.appendChild(ta);
      const row = el("div", "nrow");
      const bSave = el("button", "btn primary", "保存笔记");
      bSave.addEventListener("click", () => saveNote(item, key));
      const bCancel = el("button", "btn ghost", "取消");
      bCancel.addEventListener("click", () => cancelNoteEdit(item));
      row.append(bSave, bCancel);
      card.appendChild(row);
    } else {
      card.appendChild(el("div", "nbody", note ? note.text : ""));
    }
    return card;
  }

  function focusNoteBox(item) {
    setTimeout(() => {
      const ta = item.nbox.querySelector("textarea");
      if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
    }, 40);
  }

  // 选中句子 → 在它下面打开(或激活)内嵌笔记卡片
  function openNoteEditor(forceFocus) {
    if (!env.sel) { toast("请先选中一个句子"); return; }
    if (!needWrite()) return;
    const anchor = selAnchor();
    const key = sentKey(anchor.item, anchor.si);
    const note = noteOf(key);
    env.editingKey = key;
    env.noteDraft = note ? note.text : "";
    if (!env.showNotesOn) { env.showNotesOn = true; $("chkShowNotes").checked = true; }
    if (selCount() > 1) toast("多选时笔记挂在所选的第 1 句上");
    renderNotes(anchor.item);
    relayoutAll();          // 首条笔记会触发“预留右侧批注栏”(可能需要整体重排)
    updateBar(); placeBar(true);
    if (forceFocus) focusNoteBox(anchor.item);
  }

  function cancelNoteEdit(item) {
    env.editingKey = null; env.noteDraft = "";
    if (item) renderNotes(item);
    relayoutAll();          // 草稿取消后如果一条笔记都没了就收回预留
    updateBar(); if (env.sel) placeBar(true);
  }

  async function saveNote(item, key) {
    const ta = item.nbox.querySelector("textarea");
    const text = (ta ? ta.value : env.noteDraft).trim();
    if (!text) { toast("笔记内容为空"); return; }
    if (!needWrite()) return;
    try {
      env.anno = await api(`/api/doc/${docId}/anno`, {
        method: "POST", json: { action: "add_note", block_id: key, text },
      });
      env.editingKey = null; env.noteDraft = "";
      toast("笔记已保存 ✓");
      applyAnnoRefresh();
      env.markAiStale();
    } catch (err) { toast(err.message); }
  }

  async function deleteNote(note, item) {
    if (!needWrite()) return;
    if (!confirm("删除这条笔记？")) return;
    try {
      env.anno = await api(`/api/doc/${docId}/anno`, {
        method: "POST", json: { action: "remove_note", note_id: note.id },
      });
      if (env.editingKey === note.block_id) { env.editingKey = null; env.noteDraft = ""; }
      toast("笔记已删除");
      applyAnnoRefresh();
      env.markAiStale();
      if (item) relayoutPage(item.pi);
    } catch (err) { toast(err.message); }
  }

  $("chkShowNotes").addEventListener("change", e => {
    env.showNotesOn = e.target.checked;
    if (!env.showNotesOn) { env.editingKey = null; env.noteDraft = ""; }
    renderAllNotes();
    relayoutAll();
  });

  /* ---- 右侧笔记栏：句子笔记总览 + 独立笔记编辑 ---- */
  function sideOpen() { document.body.classList.add("side-open"); $("side").classList.add("open"); relayoutAll(); }
  function sideClose() { document.body.classList.remove("side-open"); $("side").classList.remove("open"); relayoutAll(); }
  $("btnNotes").addEventListener("click", () => {
    const isOpen = $("side").classList.contains("open");
    if (isOpen) sideClose(); else { sideOpen(); refreshSideList(); }
  });
  $("closeSide").addEventListener("click", sideClose);
  $("newFreeNote").addEventListener("click", () => openFreeEditor());

  function refreshSideList() {
    const list = $("spList");
    $("newFreeNote").hidden = !canWrite();
    list.textContent = "";
    const notes = (env.anno.notes || []).slice().reverse();
    const freeNotes = (env.anno.free_notes || []).slice().reverse();
    const total = notes.length + freeNotes.length;
    $("spCount").textContent = total ? total + " 条" : "";
    $("notesCount").textContent = total;
    if (freeDraft) renderFreeEditor(list);
    if (!total) {
      const d = el("div", "sp-empty");
      d.innerHTML = '<div class="big">💡</div>可以新建独立笔记，或选中论文中的句子添加关联笔记';
      list.appendChild(d);
    }
    for (const n of freeNotes) renderFreeNoteCard(list, n);
    for (const n of notes) {
      const p = keyParts(n.block_id);
      const item = findItem(p.id);
      const sent = (item && p.si >= 0 && item.sents[p.si]) ? item.sents[p.si].text : "（原文句已不存在）";
      const card = el("div", "note-card");
      if (item && isSelRef(item, p.si)) card.classList.add("cur");
      const src = el("div", "sent-src", "第 " + (item ? pageOfItem(item) : "?") + " 页 · " + (sent.length > 90 ? sent.slice(0, 90) + "…" : sent));
      const body = el("div", "body", n.text);
      const row = el("div", "row");
      const b1 = el("button", "", "定位"); b1.style.color = "var(--accent)";
      b1.addEventListener("click", () => jumpToNote(p));
      row.appendChild(b1);
      if (canWrite()) {                    // 只读共享：抽屉里也不给写入口
        const b2 = el("button", "", "编辑");
        const b3 = el("button", "del", "删除");
        b2.addEventListener("click", () => { jumpToNote(p, false); openNoteEditor(true); });
        b3.addEventListener("click", () => { if (item) deleteNote(n, item); });
        row.append(b2, b3);
      }
      const who = authorChip(n);
      if (who) card.appendChild(who);      // 共享论文：先标「谁写的」
      card.append(src, body, row);
      list.appendChild(card);
    }
  }
  function openFreeEditor(note = null) {
    if (!needWrite()) return;
    freeDraft = note
      ? { id: note.id, title: note.title || "", text: note.text || "" }
      : { id: "", title: "", text: "" };
    sideOpen();
    refreshSideList();
    const input = $("spList").querySelector(".free-note-editor textarea");
    if (input) input.focus();
  }
  function renderFreeEditor(list) {
    const box = el("div", "free-note-editor");
    box.appendChild(el("b", "", freeDraft.id ? "编辑独立笔记" : "新建独立笔记"));
    const title = el("input");
    title.type = "text";
    title.maxLength = 200;
    title.placeholder = "标题（可选）";
    title.value = freeDraft.title;
    title.addEventListener("input", () => { freeDraft.title = title.value; });
    const text = el("textarea");
    text.maxLength = 12000;
    text.placeholder = "写下要保存的内容…";
    text.value = freeDraft.text;
    text.addEventListener("input", () => { freeDraft.text = text.value; });
    const row = el("div", "row");
    const save = el("button", "btn primary", "保存");
    save.addEventListener("click", saveFreeNote);
    const cancel = el("button", "btn ghost", "取消");
    cancel.addEventListener("click", () => { freeDraft = null; refreshSideList(); });
    row.append(save, cancel);
    box.append(title, text, row);
    list.appendChild(box);
  }
  async function saveFreeNote() {
    if (!needWrite()) return;
    const draft = freeDraft;
    if (!draft || !draft.text.trim()) { toast("笔记内容不能为空"); return; }
    try {
      env.anno = await api(`/api/doc/${docId}/anno`, {
        method: "POST",
        json: { action: draft.id ? "update_free_note" : "add_free_note",
          note_id: draft.id || undefined, title: draft.title.trim(), text: draft.text.trim() },
      });
      freeDraft = null;
      applyAnnoRefresh();
      env.markAiStale();
      toast("独立笔记已保存 ✓");
    } catch (err) { toast(err.message); }
  }
  function renderFreeNoteCard(list, note) {
    const card = el("div", "note-card free-note-card");
    card.dataset.noteId = note.id;
    if (note.title) card.appendChild(el("b", "free-note-title", note.title));
    const who = authorChip(note);
    if (who) card.appendChild(who);
    card.appendChild(el("div", "sent-src", "独立笔记"));
    card.appendChild(el("div", "body", note.text || ""));
    const row = el("div", "row");
    const locate = el("button", "", "定位");
    locate.addEventListener("click", () => jumpToNoteId(note.id));
    row.appendChild(locate);
    if (canWrite()) {
      const edit = el("button", "", "编辑");
      edit.addEventListener("click", () => openFreeEditor(note));
      const remove = el("button", "del", "删除");
      remove.addEventListener("click", () => deleteFreeNote(note));
      row.append(edit, remove);
    }
    card.appendChild(row);
    list.appendChild(card);
  }
  async function deleteFreeNote(note) {
    if (!needWrite()) return;
    if (!confirm("删除这条独立笔记？")) return;
    try {
      env.anno = await api(`/api/doc/${docId}/anno`, {
        method: "POST", json: { action: "remove_free_note", note_id: note.id },
      });
      applyAnnoRefresh();
      env.markAiStale();
      toast("独立笔记已删除");
    } catch (err) { toast(err.message); }
  }
  function jumpToNote(p, scroll = true) {
    const item = findItem(p.id);
    if (!item) return;
    selectSentence(item, p.si >= 0 ? p.si : 0);
    if (scroll) {
      const r = item.el.getBoundingClientRect();
      window.scrollBy({ top: toScrollDelta(r.top - 130), behavior: "smooth" });
    }
  }
  function jumpToNoteId(noteId) {
    const anchored = (env.anno.notes || []).find(n => n.id === noteId);
    if (anchored) {
      const p = keyParts(anchored.block_id);
      if (!findItem(p.id)) return false;
      jumpToNote(p);
      return true;
    }
    const free = (env.anno.free_notes || []).find(n => n.id === noteId);
    if (!free) return false;
    sideOpen();
    refreshSideList();
    requestAnimationFrame(() => {
      const card = Array.from($("spList").querySelectorAll("[data-note-id]"))
        .find(node => node.dataset.noteId === noteId);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    return true;
  }
  function applyAnnoRefresh() {
    for (const id in itemById) { const it = itemById[id]; setAnnoRanges(it); rebuildItem(it); }
    renderAllNotes();
    relayoutAll();
    refreshSideList();
    renderTocAi();                     // 笔记条数变了：目录侧栏那块也刷新一下
    if (env.sel) { updateBar(); placeBar(true); }
  }

  /* 切主题：正文颜色与高亮的荧光样式都要重算一遍（全是 inline 样式，CSS 盖不住） */
  window.addEventListener("themechange", () => {
    inkCache.clear();
    renderHmenuSwatches();
    if (!env.DOC) return;
    render();
    applyAnnoRefresh();
  });

  /* ============ 对外接口 ============ */
  return { notesOfBlock, notesCount, renderAllNotes, openNoteEditor, refreshSideList,
    openSidePanel: () => { sideOpen(); refreshSideList(); }, jumpToNoteId };
};
