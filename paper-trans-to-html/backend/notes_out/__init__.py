"""笔记导出（模块十）子包。

把「笔记导出」相关的代码集中在这里，与 `backend/` 下的解析 / 存储 / Agent
模块分开，避免导出能力（JSON / 图片）散落在根目录：

| 模块 | 作用 |
| --- | --- |
| `note_out.py` | 机械导出 / Agent 导出：论文分节 + 笔记 + 统计报告（纯数据转换） |
| `note_out_image.py` | 机械图片导出：Matplotlib + XKCD 三栏版式 PNG；Agent 绘图技能在沙盒运行生成代码 |

对外只暴露 `note_out` 的公共函数（`app.py` 从这里 import）：

```python
from backend.notes_out import export_paper, export_filename, to_json
from backend.notes_out import render_image
```
"""
from .note_out import (
    EXPORT_FORMAT,
    EXPORT_VERSION,
    MODES,
    build_ai_summary,
    build_mechanical_report,
    build_notes,
    build_sections,
    export_filename,
    export_paper,
    paper_authors,
    to_json,
)
from .note_out_image import THEMES, render as render_image

__all__ = [
    "EXPORT_FORMAT", "EXPORT_VERSION", "MODES",
    "build_sections", "build_notes", "build_mechanical_report",
    "build_ai_summary", "paper_authors",
    "export_paper", "to_json", "export_filename",
    "render_image", "THEMES",
]
