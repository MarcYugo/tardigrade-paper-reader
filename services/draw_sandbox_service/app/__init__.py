"""draw_sandbox_service —— 画图沙盒（Python / matplotlib）。

容器内起一个 HTTP 服务：接收一段 Python 代码 → 在受限子进程里执行 →
把 stdout / stderr 与产出的图片（PNG / SVG / PDF …）回传给调用方。
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
