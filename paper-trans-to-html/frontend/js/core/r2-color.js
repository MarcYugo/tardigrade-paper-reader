/* ============ 颜色工具：HSL 换算 / 暗夜反色 / hex→rgba：从 reader2.js 拆出来的模块 ============
   `pageInk` 需要一个**跨主题复用的缓存**（换主题时由 reader2 的 themechange 监听
   清空，同一份对象也传给 r2-notes.js 共用），所以由外部注入；其余都是纯函数。
   状态判断函数 `isDark`（common.js）也一并注入，方便以后换成别的主题机制。

   装配（reader2.js，`$` 之后）：
     const { hex2rgb, rgb2hsl, hsl2hex, invertInk, pageInk, hexToRgba } =
       R2Color({ inkCache, isDark });
   ============================================================================ */
window.R2Color = function (env) {
  "use strict";
  const { inkCache, isDark } = env;

  function hex2rgb(hex) {
    let s = String(hex || "").replace("#", "").trim();
    if (s.length === 3) s = s.split("").map(ch => ch + ch).join("");
    if (s.length !== 6) return null;
    const n = parseInt(s, 16);
    return isNaN(n) ? null : [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function rgb2hsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
    if (mx === mn) return [0, 0, l];
    const d = mx - mn;
    const s = l > .5 ? d / (2 - mx - mn) : d / (mx + mn);
    let h;
    if (mx === r) h = (g - b) / d + (g < b ? 6 : 0);
    else if (mx === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    return [h / 6, s, l];
  }
  const _h2 = x => Math.round(x * 255).toString(16).padStart(2, "0");
  function hsl2hex(h, s, l) {
    if (s <= 0) { const v = _h2(l); return "#" + v + v + v; }
    const q = l < .5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    const f = t => {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };
    return "#" + _h2(f(h + 1 / 3)) + _h2(f(h)) + _h2(f(h - 1 / 3));
  }
  /* 暗夜下正文颜色“保留色相、反转明度”：PDF 里的字色是写死的（大多是黑），
     直接黑字配枪灰底就看不见了；所以按 HSL 把明度翻过来并夹到可读区间：
     黑字→近白、彩色字→同色系提亮。 */
  function invertInk(hex) {
    const rgb = hex2rgb(hex);
    if (!rgb) return hex;
    const [h, s, l] = rgb2hsl(rgb[0], rgb[1], rgb[2]);
    const nl = Math.min(.98, Math.max(.62, 1 - l));    // 夹住，别变成看不见的灰
    return hsl2hex(h, s <= 0 ? 0 : Math.min(1, s * .95 + .05), nl);
  }
  function pageInk(c) {
    const key = c || "#000";
    if (!isDark()) return key;
    if (!inkCache.has(key)) inkCache.set(key, invertInk(key));
    return inkCache.get(key);
  }
  /* #rrggbb → rgba(r,g,b,a)；解析失败时退回高亮默认黄 */
  function hexToRgba(hex, a) {
    let s = String(hex || "").replace("#", "");
    if (s.length === 3) s = s.split("").map(ch => ch + ch).join("");
    const n = parseInt(s, 16);
    if (s.length !== 6 || isNaN(n)) return "rgba(255,214,74," + a + ")";
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  /* ============ 对外接口 ============ */
  return { hex2rgb, rgb2hsl, hsl2hex, invertInk, pageInk, hexToRgba };
};
