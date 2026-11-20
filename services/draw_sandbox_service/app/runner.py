#!/usr/bin/env python3
"""沙盒子进程入口（由父进程用 `python -I -B runner.py ...` 启动）。

只做四件事：

1. 落地资源限制（CPU 时间 / 内存 / 文件大小 / 进程数 / 句柄数）；
2. 把 matplotlib 固定到无界面 ``Agg`` 后端，并拦截用户代码里切换后端的尝试；
3. 执行用户代码（cwd = 工作目录，``__name__`` 为 ``"__main__"``）；
4. 把 ``plt.show()`` 与执行结束时仍开着的图存成 ``figure_N.png``，
   并把执行结果写到 ``result.json`` 交给父进程。

⚠️ 资源限制放在本文件里而不是父进程的 ``preexec_fn``：父进程是多线程的，
``fork`` 之后跑 Python 有死锁风险；子进程是全新单线程进程，``setrlimit`` 更安全。
"""

from __future__ import annotations

import argparse
import builtins
import json
import os
import sys
import time
import traceback

try:                                                       # 容器里一定有；本地 Windows 联调没有
    import resource
    _HAS_RESOURCE = True
except ImportError:                                        # pragma: no cover
    resource = None                                        # type: ignore[assignment]
    _HAS_RESOURCE = False


# =====================================================================
#  资源限制
# =====================================================================
def apply_limits(cpu_seconds: int, mem_mb: int, fsize_mb: int,
                 nproc: int, nofile: int) -> None:
    """尽量都设上；受限环境里设不了就跳过，不阻断执行。"""
    os.umask(0o077)
    if not _HAS_RESOURCE:
        return
    limits: list[tuple[int, int, int]] = []
    if cpu_seconds > 0:
        limits.append((resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1))
    if mem_mb > 0:
        size = mem_mb * 1024 * 1024
        limits.append((resource.RLIMIT_AS, size, size))
    if fsize_mb > 0:
        size = fsize_mb * 1024 * 1024
        limits.append((resource.RLIMIT_FSIZE, size, size))
    if nproc > 0 and hasattr(resource, "RLIMIT_NPROC"):
        limits.append((resource.RLIMIT_NPROC, nproc, nproc))
    if nofile > 0:
        limits.append((resource.RLIMIT_NOFILE, nofile, nofile))

    for kind, soft, hard in limits:
        try:
            resource.setrlimit(kind, (soft, hard))
        except (ValueError, OSError):
            pass


# =====================================================================
#  图形落盘
# =====================================================================
class FigureSaver:
    """把 pyplot 里还开着的图存成 ``figure_N.png``（不覆盖用户自己的文件）。"""

    def __init__(self, workdir: str, dpi: int,
                 default_figure_dpi: float = 100.0) -> None:
        self.workdir = workdir
        self.dpi = dpi
        self.default_figure_dpi = default_figure_dpi
        self.index = 0
        self.saved: list[str] = []
        self._used: set[str] = set()

    def _dpi_for(self, fig) -> float:
        """用户显式改过 figure.dpi（≠ matplotlib 默认 100）就尊重它，否则用沙盒统一 DPI。"""
        try:
            value = float(fig.dpi)
            if abs(value - self.default_figure_dpi) > 1e-6:
                return value
        except Exception:
            pass
        return float(self.dpi)

    def _next_name(self) -> str:
        while True:
            self.index += 1
            name = f"figure_{self.index}.png"
            if name not in self._used and not os.path.exists(
                    os.path.join(self.workdir, name)):
                self._used.add(name)
                return name

    def save_open(self, plt) -> list[str]:
        """保存当前所有未关闭的 figure，然后统一 close。"""
        names: list[str] = []
        try:
            numbers = list(plt.get_fignums())
        except Exception:
            return names

        for number in numbers:
            try:
                fig = plt.figure(number)
                name = self._next_name()
                fig.savefig(os.path.join(self.workdir, name), dpi=self._dpi_for(fig),
                            bbox_inches="tight", pad_inches=0.1)
                names.append(name)
                self.saved.append(name)
            except Exception as exc:                     # 单张失败不影响其它图
                print(f"[sandbox] 保存图形失败：{exc}", file=sys.stderr)

        try:
            plt.close("all")
        except Exception:
            pass
        return names


def setup_matplotlib(workdir: str, dpi: int):
    """固定 Agg 后端并把 plt.show() 改成"存文件"。返回 (plt, saver)，失败返回 (None, None)。"""
    try:
        import matplotlib
    except Exception as exc:                             # 理论上不会发生（镜像已装）
        print(f"[sandbox] matplotlib 不可用：{exc}", file=sys.stderr)
        return None, None

    matplotlib.use("Agg", force=True)

    import matplotlib as mpl
    from matplotlib import pyplot as plt

    original_use = mpl.use

    def guarded_use(backend=None, *args, **kwargs):
        # 用户代码常写 matplotlib.use("TkAgg")，在无界面容器里会直接抛错
        if backend is not None and "agg" not in str(backend).lower():
            print(f"[sandbox] 忽略后端切换 {backend!r}，固定使用 Agg",
                  file=sys.stderr)
            return None
        return original_use(backend, *args, **kwargs)

    # ⚠️ 不能一律吞掉 switch_backend：pyplot 第一次画图时会自己调
    #    switch_backend("agg") 来初始化 _backend_mod，吞了会变成 None → AttributeError。
    original_switch = plt.switch_backend

    def guarded_switch(backend=None, *args, **kwargs):
        if backend is not None and "agg" not in str(backend).lower():
            print(f"[sandbox] 忽略 switch_backend({backend!r})，固定使用 Agg",
                  file=sys.stderr)
            return original_switch("Agg")
        return original_switch(backend, *args, **kwargs)

    mpl.use = guarded_use
    plt.switch_backend = guarded_switch

    # Matplotlib 不会为普通文本可靠地逐字回退字体，中文字体必须放在首位。
    mpl.rcParams.update({
        "figure.max_open_warning": 0,
        "axes.unicode_minus": False,
        "font.sans-serif": [
            "WenQuanYi Micro Hei", "Noto Sans CJK SC", "Microsoft YaHei",
            "SimHei", "Arial Unicode MS", "DejaVu Sans",
        ],
    })

    saver = FigureSaver(workdir, dpi,
                        float(mpl.rcParamsDefault.get("figure.dpi", 100)))
    plt.show = lambda *args, **kwargs: saver.save_open(plt)
    return plt, saver


# =====================================================================
#  执行用户代码
# =====================================================================
def run_code(code_path: str, workdir: str, result_path: str, dpi: int) -> int:
    started = time.monotonic()
    info: dict = {
        "ok": False,
        "error": None,
        "error_type": None,
        "figures": [],
        "duration_ms": 0,
    }

    plt, saver = setup_matplotlib(workdir, dpi)

    try:
        with open(code_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        code = compile(source, "main.py", "exec")
        namespace = {
            "__name__": "__main__",
            "__file__": os.path.abspath(code_path),
            "__package__": None,
            "__spec__": None,
            "__builtins__": builtins,
        }
        exec(code, namespace)
        info["ok"] = True
    except SystemExit as exc:
        if exc.code in (None, 0):
            info["ok"] = True
        else:
            info["ok"] = False
            info["error_type"] = "SystemExit"
            info["error"] = f"代码调用了 sys.exit({exc.code!r})"
            print(info["error"], file=sys.stderr)
    except BaseException as exc:                          # noqa: BLE001 - 用户代码什么都可能抛
        info["ok"] = False
        info["error_type"] = type(exc).__name__
        info["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        if saver is not None and plt is not None:
            try:
                saver.save_open(plt)                      # 收尾：没 show 过的图也落盘
            except Exception as exc:
                print(f"[sandbox] 收尾保存图形失败：{exc}", file=sys.stderr)
            info["figures"] = list(saver.saved)
        info["duration_ms"] = int((time.monotonic() - started) * 1000)
        try:
            with open(result_path, "w", encoding="utf-8") as fh:
                json.dump(info, fh, ensure_ascii=False)
        except OSError as exc:
            print(f"[sandbox] 无法写入 result.json：{exc}", file=sys.stderr)

    return 0 if info["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox-runner")
    parser.add_argument("--code", default="main.py", help="用户代码文件（相对 workdir）")
    parser.add_argument("--workdir", default=".", help="工作目录")
    parser.add_argument("--result", default="result.json", help="结果 JSON 路径")
    parser.add_argument("--dpi", type=int,
                        default=int(os.getenv("SANDBOX_FIGURE_DPI", "150") or "150"))
    parser.add_argument("--cpu-seconds", type=int, default=0)
    parser.add_argument("--mem-mb", type=int, default=0)
    parser.add_argument("--fsize-mb", type=int, default=0)
    parser.add_argument("--nproc", type=int, default=0)
    parser.add_argument("--nofile", type=int, default=0)
    args = parser.parse_args(argv)

    # 1) 先上限制，再 import 任何重依赖 / 跑用户代码
    apply_limits(args.cpu_seconds, args.mem_mb, args.fsize_mb,
                 args.nproc, args.nofile)

    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(args.result)) or "."),
                exist_ok=True)

    # 2) 工作目录进 sys.path：用户代码可以 import 同目录的辅助模块 / 读自己的数据文件
    if workdir not in sys.path:
        sys.path.insert(0, workdir)
    sys.dont_write_bytecode = True

    code_path = args.code if os.path.isabs(args.code) else os.path.join(workdir, args.code)
    return run_code(code_path, workdir, args.result, args.dpi)


if __name__ == "__main__":
    raise SystemExit(main())
