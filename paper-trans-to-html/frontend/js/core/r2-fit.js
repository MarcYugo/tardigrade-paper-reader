/* ============ 行宽压缩（本地解析方案的逐行绝对定位）：从 reader2.js 拆出来的模块 ============
   本地解析方案的每一行都按 PDF 行框绝对定位；浏览器的替代字体通常比原排版宽，
   长行会压到栏间空隙/右栏上。这里按行框宽度做**横向压缩**（只缩不放），
   行位与行高不变；公式占位（`.rngap`）的宽度已由解析侧 `gw` 直接撑开。

   无外部依赖、无状态：入参是渲染期缓存的「行数据」`ld`（含 el / ln / S / rot / rwPx）。
   装配：`const { gapStats, fitLineEl, fitItemLines } = R2Fit();`
   ============================================================================ */
window.R2Fit = function () {
  "use strict";

  /* 公式空位（`.rngap`）是**定宽**的：宽度由解析侧按原公式宽度给出，绝不能参与
     「文字超宽」的判定。行内公式的框常常比 PDF 的这条行碎片宽（分式/根号/上下限
     落在别的碎片行上），空位会伸出 `.ln` 的右边 —— 旧写法按整行 scrollWidth 判超宽，
     会把**整行**（含公式前的字）scaleX 压扁，实测压到 0.72。 */
  function gapStats(lineEl, avail) {
    let width = 0, inside = 0;
    for (const sp of lineEl.querySelectorAll(".rngap")) {
      const w = sp.offsetWidth;
      const l = sp.offsetLeft;
      width += w;
      inside += Math.max(0, Math.min(l + w, avail) - Math.max(l, 0));
    }
    return { width, inside };
  }
  function fitLineEl(ld) {
    const lineEl = ld && ld.el;
    if (!lineEl || ld.free) return;
    // 旋转行的 transform 要保留旋转本体，不能整句覆盖成 scaleX
    const rotT = ld.rot ? ("rotate(" + ld.rot + "deg)") : "";
    lineEl.style.transform = rotT;
    // 旋转行的可用宽度是**未旋转时**的宽度（`rw`），不是 bbox 的横向宽度
    const avail = ld.rot ? ld.rwPx : Number(ld.ln && ld.ln.w) * (ld.S || 1);
    if (!(avail > 0)) return;
    const gaps = gapStats(lineEl, avail);
    // 「需要压的宽度」只算正文；「可用的宽度」把落在行框内的空位扣掉
    const need = Math.max(0, lineEl.scrollWidth - gaps.width);
    const availText = Math.max(1, avail - Math.min(gaps.inside, avail));
    if (need <= availText + 0.75) return;
    const k = Math.max(0.72, availText / need);
    lineEl.style.transformOrigin = "left top";
    lineEl.style.transform = rotT + (rotT ? " " : "") + "scaleX(" + k.toFixed(4) + ")";
  }
  function fitItemLines(item) {
    if (!item || item.kind !== "blk") return;
    for (const ld of item.linesData || []) fitLineEl(ld);
  }

  /* ============ 对外接口 ============ */
  return { gapStats, fitLineEl, fitItemLines };
};
