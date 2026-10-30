"""行内 run 合并：**颜色边界不能被抹掉**（链接蓝不许扩散到整行）。

背景（2026-10-29 实测）：`text_layer._merge_line_runs` 原本把每行正文 run 合成
**一个**，样式取「本行字符数最多的 run」。MuPDF 是按「字体/字号/颜色」切 span 的，
`\\cite` / `\\ref` 会被 hyperref 染成链接蓝（实测 `#0064e0`）—— 一旦整行并成一段，
链接段只要不比正文短，**整行正文都会变成链接蓝**（某篇 ACL 论文 38 行中招），
反过来正文更长时引用又丢掉蓝色。

现在按颜色切段合并：字符数守恒、字号/字体仍取本行最长 run（字号不跳动）、
颜色逐段保留。这里覆盖这四条口径 + 公式占位（`keep_gaps=True`）的行为。
"""
from __future__ import annotations

import unittest

from backend.pdf_parser.text_layer import _merge_line_runs

LINK_BLUE = "#0064e0"
BODY = "#000000"


def _run(text, color=BODY, size=10.0, bold=False, fam="serif"):
    return {"t": text, "s": size, "fam": fam, "b": bold, "i": False,
            "up": False, "c": color}


def _blocks(runs):
    return [{"lines": [{"x": 60.0, "y": 100.0, "w": 480.0, "h": 12.0,
                        "runs": runs}]}]


def _line(blocks):
    return blocks[0]["lines"][0]


class MergeLineRunsColorTest(unittest.TestCase):

    def test_link_color_stays_on_its_own_run(self):
        """正文 55 字 + 链接 55 字：合并后仍是两段，只有引用是蓝的。"""
        body = "thus increasing their effective depth during inference "
        cite = "(Wang et al., 2025; Jolicoeur-Martineau, 2025; Movahedi"
        blocks = _blocks([_run(body), _run(cite, LINK_BLUE)])
        _merge_line_runs(blocks)

        runs = _line(blocks)["runs"]
        self.assertEqual([r["c"] for r in runs], [BODY, LINK_BLUE])
        self.assertEqual("".join(r["t"] for r in runs), body + cite)

    def test_longest_run_color_does_not_paint_the_line(self):
        """链接段比正文长时也一样：不许把整行刷成链接色。"""
        blocks = _blocks([_run("see "), _run("(Wang et al., 2025)", LINK_BLUE)])
        _merge_line_runs(blocks)

        runs = _line(blocks)["runs"]
        self.assertEqual([r["c"] for r in runs], [BODY, LINK_BLUE])
        self.assertEqual(runs[0]["t"], "see ")

    def test_single_color_line_still_collapses(self):
        """原文一整行同色同字号 —— 仍然合成一个 run（不给 doc.json 增体积）。"""
        blocks = _blocks([_run("hello "), _run("world")])
        _merge_line_runs(blocks)

        runs = _line(blocks)["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["t"], "hello world")
        self.assertEqual(runs[0]["c"], BODY)

    def test_body_style_wins_over_link_run(self):
        """字号/字体/粗斜体取**本行最长**的 run，不跟着颜色走。"""
        blocks = _blocks([_run("plain body text "),          # 16 字
                          _run("link", LINK_BLUE, size=7.0, bold=True)])
        _merge_line_runs(blocks)

        runs = _line(blocks)["runs"]
        self.assertEqual(len(runs), 2)
        for r in runs:                       # 整行统一 10pt / 非粗体
            self.assertEqual(r["s"], 10.0)
            self.assertFalse(r["b"])
            self.assertFalse(r["up"])
        self.assertEqual(runs[1]["c"], LINK_BLUE)

    def test_char_count_is_conserved(self):
        blocks = _blocks([_run("a b "), _run("c d ", LINK_BLUE), _run("e f")])
        before = "".join(r["t"] for r in _line(blocks)["runs"])
        _merge_line_runs(blocks)
        after = "".join(r["t"] for r in _line(blocks)["runs"])
        self.assertEqual(before, after)

    def test_gap_run_kept_and_colors_split_within_segments(self):
        """`keep_gaps=True`：公式占位单独留，两侧正文各自按颜色分段。"""
        gap = {"t": " ", "s": 10.0, "fam": "serif", "b": False, "i": False,
               "up": False, "c": BODY, "gap": "f1", "gw": 42.0}
        blocks = _blocks([_run("before "), gap,
                          _run("after "), _run("(Smith, 2020)", LINK_BLUE)])
        _merge_line_runs(blocks, keep_gaps=True)

        runs = _line(blocks)["runs"]
        self.assertEqual([r.get("gap") for r in runs], [None, "f1", None, None])
        self.assertEqual([r["c"] for r in runs], [BODY, BODY, BODY, LINK_BLUE])
        self.assertEqual("".join(r["t"] for r in runs),
                         "before  after (Smith, 2020)")


if __name__ == "__main__":
    unittest.main()
