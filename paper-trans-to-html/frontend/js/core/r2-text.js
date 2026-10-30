/* ============ 文本工具：句子切分 / 字符区间 / 可复制文本 / 句子 key：从 reader2.js
   拆出来的模块 ============
   与 r2-math.js / r2-notes.js / r2-nav.js 同一套路，但这个模块**没有任何外部依赖、
   也不持有状态**：入参都是块对象（`item`）本身，出参是纯数据（区间 / 字符串 / 数字）。
   所以装配时不用传 env，`const { … } = R2Text();` 即可。

   注意：这里的句子切分口径必须与后端 `pdf_parser/sentences.py::split_sentences`
   完全一致（见 splitSentences 里的说明）—— 数据层已经「一个块 = 恰好一句」，
   两边不一致的话会把一个块在界面上切回两句，笔记/高亮的句号就对不上了。
   ============================================================================ */
window.R2Text = function () {
  "use strict";

  /* ================= 句子分割(学术英文) ================= */
  const _ABBR = new Set(["fig", "figs", "eq", "eqs", "sec", "sect", "ref", "refs", "no", "nos",
    "tab", "tabs", "ch", "chaps", "app", "pp", "vol", "vols", "approx", "et", "al", "dept", "univ"]);
  const _TITLE = new Set(["dr", "prof", "mr", "mrs", "ms", "st", "mt", "fr", "jr", "sr",
    "rev", "gen", "col", "lt", "capt", "sgt", "adm", "rep", "sen", "gov", "corp", "inc", "ltd", "co"]);

  function splitSentences(text) {
    const n = text.length, ends = [];
    let i = 0;
    while (i < n) {
      const c = text[i];
      if (c === "." || c === "!" || c === "?") {
        let j = i + 1;
        while (j < n && /[.!?…]/.test(text[j])) j++;
        while (j < n && /[)\]}”’»"'’]/.test(text[j])) j++;
        const ws = j;
        while (j < n && /\s/.test(text[j])) j++;
        if (j >= n) { ends.push(ws); break; }
        const next = text[j];
        const mm = text.slice(0, i).match(/([A-Za-z]+)\s*$/);
        const tok = mm ? mm[1].toLowerCase() : "";
        let boundary = false;
        if (/[A-Z0-9"'“’(]/.test(next)) {
          const numeral = /^\d/.test(next) && _ABBR.has(tok);
          const nameLike = _TITLE.has(tok);
          const etAl = tok === "al" && /(^|\s)et\s*$/i.test(text.slice(0, ws));
          boundary = !numeral && !nameLike && !etAl;
        }
        // 小数点/编号不切（`1.1`、`3.14`）：后端 `pdf_parser.sentences.split_sentences`
        // 是同一口径 —— 数据层现在已经「一个块 = 恰好一句」，两边不一致的话会把一个块
        // 在界面上切回两句（笔记/高亮的句号就对不上了）。
        if (boundary && i > 0 && /\d/.test(text[i - 1]) && /\d/.test(next)) {
          boundary = false;
        }
        if (boundary) ends.push(ws);
        i = j;
      } else { i++; }
    }
    const sents = [];
    let start = 0;
    for (const e of ends) {
      if (e <= start) continue;
      sents.push({ s: start, e });
      start = e;
    }
    // ⚠️ 句子区间必须**铺满整块文本**：句末标点后的空白、结尾的换行、以及最后一行
    // 被擦成空格的公式空位，都归**前一句**。否则这些字符「既不是上一句的也不是下一句的」——
    // 永远选不中(sentOf 只能回落到相邻句)，选中高亮/笔记下划线会在行中间留出空洞，
    // 看上去像排版坏了。文本抽取侧不用管：sentenceText/selFragText 都会 trim+压缩空白。
    if (start < n) {
      if (/\S/.test(text.slice(start))) sents.push({ s: start, e: n });
      else if (sents.length) sents[sents.length - 1].e = n;   // 尾巴整段是空白(含换行) → 并进前一句
    }
    return sents;
  }
  function sentOf(item, idx) {
    const L = item.canonical.length;
    let i = idx == null ? 0 : idx;
    i = Math.max(0, Math.min(L - 1, i));
    const ss = item.sents;
    if (!ss.length) return 0;
    if (i < ss[0].s) return 0;
    for (let k = 0; k < ss.length; k++) if (i >= ss[k].s && i < ss[k].e) return k;
    return ss.length - 1;
  }
  function sentRange(item, si) {
    const ss = item.sents[si];
    return ss ? { a: ss.s, b: ss.e } : { a: 0, b: Math.max(0, item.canonical.length - 1) };
  }
  /* 字符下标 → 所在行（`item.linesData` 的下标）。段落/表格块没有行数据时回落 0。 */
  function lineOfChar(item, idx) {
    let li = 0;
    const lines = item.linesData || [];
    for (let i = 0; i < lines.length; i++) {
      if (idx >= lines[i].gs) li = i;
      else break;
    }
    return li;
  }

  /* ================= 句子 key / 复制文本 ================= */
  function sentKey(item, si) { return item.id + "#" + si; }
  /* 句子 key("p0b3#1") → { id: "p0b3", si: 1 }；不是句子 key 时 si = -1 */
  function keyParts(key) {
    const k = String(key || "");
    const p = k.lastIndexOf("#");
    return p > 0 ? { id: k.slice(0, p), si: parseInt(k.slice(p + 1), 10) || 0 } : { id: k, si: -1 };
  }
  /* 字符区间 → **可复制 / 可翻译**的文本：公式占位换成 `$latex$`，空白压成单空格。
     公式与文字的位置关系由 attachFormulaBoxes 从解析侧 `copy[]` 区间建立
     （`box._copyRange`，按 canonical 计字符数）。 */
  function copyRangeText(item, a, b) {
    let text = item.canonical.slice(a, b);
    const formulas = (item.formulaBoxes || [])
      .filter(box => box._copyRange && box._copyRange.start < b && box._copyRange.end > a)
      .sort((x, y) => y._copyRange.start - x._copyRange.start);
    for (const box of formulas) {
      const s = Math.max(0, box._copyRange.start - a);
      const e = Math.min(text.length, box._copyRange.end - a);
      text = text.slice(0, s) + "$" + (box.dataset.latex || "") + "$" + text.slice(e);
    }
    return text.replace(/\s+/g, " ").trim();
  }
  function copySentenceText(item, si) {
    const ss = item.sents[si];
    return ss ? copyRangeText(item, ss.s, ss.e) : "";
  }
  /* 整块文本：**「复制」和「送翻译」都用它**，保证两边看到的文本一模一样 ——
     带 `$latex$` 公式占位、句间只有一个空格（换行留着的话译文卡是 pre-wrap，
     会撑出巨大空行；模型也容易按行硬断句）。 */
  function itemCopyText(item) {
    if (!item) return "";
    if (item.kind !== "blk" || !item.canonical) return (item.text || "").trim();
    const ss = item.sents || [];
    if (ss.length > 1) {
      return ss.map((_, si) => copySentenceText(item, si)).filter(Boolean).join(" ");
    }
    if (ss.length === 1) return copySentenceText(item, 0);
    return copyRangeText(item, 0, item.canonical.length);
  }
  /* 译文文本规范化：模型偶尔会在句间插空行，卡片是 pre-wrap，空行会撑成巨大间隙 */
  function normTrans(s) {
    return String(s || "").replace(/\r\n?/g, "\n")
      .replace(/[ \t]+\n/g, "\n").replace(/\n{2,}/g, "\n").trim();
  }

  /* ============ 对外接口 ============ */
  return {
    splitSentences, sentOf, sentRange, lineOfChar,
    sentKey, keyParts, copyRangeText, copySentenceText, itemCopyText, normTrans,
  };
};
