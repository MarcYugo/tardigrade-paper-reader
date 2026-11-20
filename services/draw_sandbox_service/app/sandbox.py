"""沙盒执行核心：准备运行目录 → 起子进程跑用户代码 → 采集产出 → 打包。

这里全是同步代码（会被 FastAPI 用 ``asyncio.to_thread`` 丢到线程池里跑），
因为 ``subprocess.Popen().wait(timeout=...)`` 必须阻塞调用方。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import settings

RUNNER = Path(__file__).with_name("runner.py")

_HIDDEN_DIRS = {"__pycache__"}


# =====================================================================
#  入参准备
# =====================================================================
def safe_relpath(name: str) -> str:
    """把用户给的相对路径洗干净；绝对路径 / ``..`` / 空名一律拒绝。"""
    cleaned = str(name).replace("\\", "/").strip()
    if not cleaned or "\x00" in cleaned:
        raise ValueError(f"非法的文件名：{name!r}")
    if cleaned.startswith("/"):
        raise ValueError(f"不允许绝对路径：{name!r}")

    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"非法的文件名：{name!r}")
    if any(p == ".." for p in parts):
        raise ValueError(f"不允许 .. 越界：{name!r}")
    if any(len(p) > 120 for p in parts):
        raise ValueError(f"路径片段过长：{name!r}")
    return "/".join(parts)


def resolve_timeout(requested: float | None) -> float:
    value = settings.default_timeout if requested is None else float(requested)
    return max(1.0, min(value, settings.max_timeout))


def prepare_run(run_dir: Path, code: str, files: Iterable[Any]) -> tuple[Path, set[str]]:
    """在 run_dir 下建 work/ 与 home/，写入用户代码与输入文件。

    返回 ``(workdir, seeded)``；``seeded`` 是"属于输入、不算产出"的相对路径集合。
    """
    workdir = run_dir / "work"
    workdir.mkdir(parents=True, exist_ok=False)
    home = run_dir / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)

    (workdir / "main.py").write_text(code, encoding="utf-8", newline="\n")
    seeded: set[str] = {"main.py"}

    for item in files:
        rel = safe_relpath(item.name)
        if rel in seeded:
            raise ValueError(f"输入文件名重复：{rel}")

        if getattr(item, "encoding", "utf8") == "base64":
            try:
                data = base64.b64decode(item.content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"输入文件 {rel} 的 base64 无法解码") from exc
        else:
            data = item.content.encode("utf-8")

        if len(data) > settings.max_input_file_bytes:
            raise ValueError(
                f"输入文件 {rel} 超过上限 {settings.max_input_file_bytes} 字节")

        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        seeded.add(rel)

    return workdir, seeded


# =====================================================================
#  执行
# =====================================================================
@dataclass(slots=True)
class OutputFile:
    name: str
    path: Path
    size: int
    mime: str
    sha256: str


@dataclass(slots=True)
class RunResult:
    status: str                     # success / error / timeout / killed
    exit_code: int | None
    duration_ms: int
    stdout: str
    stderr: str
    outputs: list[OutputFile] = field(default_factory=list)
    error: str | None = None
    outputs_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _mpl_cache_dir(home: Path) -> str:
    """优先复用镜像里预热好的字体缓存目录。

    运行时根文件系统可能只读——目录只要存在就继续用（只读也能读缓存）；
    不存在时退回每个请求自己的 home 目录（会多花 1~3s 扫系统字体）。
    """
    candidate = os.getenv("MPLCONFIGDIR")
    if candidate and Path(candidate).is_dir():
        return candidate
    return str(home / ".mpl")


def _build_env(home: Path, workdir: Path) -> dict[str, str]:
    """子进程环境：白名单式构造，不继承父进程的 PYTHONPATH 等。"""
    mpl_cache = _mpl_cache_dir(home)
    return {
        "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        # BLAS/OpenMP 限 1 线程：多请求并发时不至于把 CPU 全吃满
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": mpl_cache,
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "SANDBOX_FIGURE_DPI": str(settings.figure_dpi),
        "SANDBOX_WORKDIR": str(workdir),
    }


def _kill_tree(proc: subprocess.Popen) -> None:
    """子进程是 session leader（``start_new_session=True``），pid 即 pgid。

    整组 kill 之后可能还有漏网的孙进程，再补一刀。Windows 本地联调没有
    ``killpg``，退回普通 ``kill``（容器里永远走 Linux 分支）。
    """
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _read_limited(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            data = fh.read(limit)
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    if size > limit:
        text += f"\n… [已截断，日志共 {size} 字节，上限 {limit} 字节]"
    return text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def collect_outputs(workdir: Path, seeded: set[str]) -> tuple[list[OutputFile], bool]:
    """遍历 workdir，收集允许后缀的普通文件（跳过输入文件 / 隐藏文件 / 符号链接）。"""
    outputs: list[OutputFile] = []
    total = 0
    truncated = False

    for root, dirs, files in os.walk(workdir, followlinks=False):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in _HIDDEN_DIRS)
        for filename in sorted(files):
            path = Path(root) / filename
            try:
                rel = path.relative_to(workdir).as_posix()
            except ValueError:                            # pragma: no cover
                continue
            if rel in seeded or filename.startswith("."):
                continue

            try:
                st = path.lstat()
            except OSError:
                continue
            # lstat + S_ISREG：符号链接 / 管道 / 设备一律跳过，避免读穿工作目录
            if not stat.S_ISREG(st.st_mode):
                continue

            ext = path.suffix.lower().lstrip(".")
            if settings.allowed_exts and ext not in settings.allowed_exts:
                continue

            if (st.st_size > settings.max_output_file_bytes
                    or st.st_size > settings.max_output_bytes):
                truncated = True
                continue
            if len(outputs) >= settings.max_output_files:
                truncated = True
                continue
            if total + st.st_size > settings.max_output_bytes:
                truncated = True
                continue

            try:
                digest = _sha256(path)
            except OSError:
                continue

            outputs.append(OutputFile(name=rel, path=path, size=st.st_size,
                                      mime=_guess_mime(path), sha256=digest))
            total += st.st_size

    return outputs, truncated


def execute(run_dir: Path, workdir: Path, timeout: float, seeded: set[str]) -> RunResult:
    """同步执行用户代码。调用方负责并发限流。"""
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    result_path = run_dir / "result.json"
    home = run_dir / "home"

    cmd = [
        sys.executable, "-I", "-B", str(RUNNER),
        "--code", "main.py",
        "--workdir", str(workdir),
        "--result", str(result_path),
        "--dpi", str(settings.figure_dpi),
        "--cpu-seconds", str(int(timeout) + settings.cpu_extra_seconds),
        "--mem-mb", str(settings.mem_limit_mb),
        "--fsize-mb", str(settings.fsize_limit_mb),
        "--nproc", str(settings.nproc_limit),
        "--nofile", str(settings.nofile_limit),
    ]

    started = time.monotonic()
    timed_out = False

    with open(stdout_path, "wb") as out_f, open(stderr_path, "wb") as err_f:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            env=_build_env(home, workdir),
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            start_new_session=True,        # 新 session → 超时可整组 kill
            close_fds=True,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            proc.wait()
        finally:
            # 主进程退出后可能还有孙进程（用户代码自己 fork 的），一并清掉
            _kill_tree(proc)

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_text = _read_limited(stdout_path, settings.max_log_bytes)
    stderr_text = _read_limited(stderr_path, settings.max_log_bytes)

    info: dict = {}
    try:
        info = json.loads(result_path.read_text("utf-8"))
    except (OSError, ValueError):
        info = {}

    exit_code = proc.returncode
    if timed_out:
        status = "timeout"
    elif exit_code == 0 and info.get("ok", True) is not False:
        status = "success"
    elif exit_code is not None and exit_code < 0:
        status = "killed"
    else:
        status = "error"

    error = info.get("error")
    if status == "timeout":
        error = f"执行超时（>{timeout:g}s），已强制终止整个进程组"
    elif status == "killed" and not error:
        signum = -exit_code if exit_code else 0
        error = f"进程被信号 {signum} 终止"
        if signum == signal.SIGKILL:
            error += "（可能是内存超限，或被超时/资源限制强制杀死）"
        elif signum == getattr(signal, "SIGXFSZ", None):
            error += "（写出文件超过大小限制）"
    elif status == "error" and not error:
        error = f"退出码 {exit_code}"

    outputs, truncated = collect_outputs(workdir, seeded)

    return RunResult(
        status=status,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout=stdout_text,
        stderr=stderr_text,
        outputs=outputs,
        error=error,
        outputs_truncated=truncated,
    )


# =====================================================================
#  回传
# =====================================================================
def to_json(result: RunResult) -> dict:
    """转成 JSON 响应体：产出文件 base64 内联。"""
    outputs = []
    remaining = settings.max_output_bytes
    truncated = result.outputs_truncated

    for item in result.outputs:
        if item.size > remaining:
            truncated = True
            continue
        try:
            data = item.path.read_bytes()
        except OSError:
            truncated = True
            continue
        remaining -= len(data)
        outputs.append({
            "name": item.name,
            "size": item.size,
            "mime": item.mime,
            "sha256": item.sha256,
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
        })

    return {
        "ok": result.ok,
        "status": result.status,
        "exit_code": result.exit_code,
        "timed_out": result.status == "timeout",
        "duration_ms": result.duration_ms,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "error": result.error,
        "outputs": outputs,
        "outputs_truncated": truncated,
    }


def build_archive(run_dir: Path, result: RunResult) -> Path:
    """产出打成 zip：manifest.json + stdout.txt + stderr.txt + outputs/。"""
    zip_path = run_dir / "outputs.zip"
    manifest = {
        "ok": result.ok,
        "status": result.status,
        "exit_code": result.exit_code,
        "timed_out": result.status == "timeout",
        "duration_ms": result.duration_ms,
        "error": result.error,
        "outputs_truncated": result.outputs_truncated,
        "files": [
            {"name": item.name, "size": item.size,
             "mime": item.mime, "sha256": item.sha256}
            for item in result.outputs
        ],
    }

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("stdout.txt", result.stdout)
        zf.writestr("stderr.txt", result.stderr)
        for item in result.outputs:
            try:
                zf.write(item.path, arcname=f"outputs/{item.name}")
            except OSError:
                continue

    return zip_path


# =====================================================================
#  清理
# =====================================================================
def cleanup(run_dir: Path, keep: bool = False) -> None:
    if keep:
        return
    shutil.rmtree(run_dir, ignore_errors=True)
