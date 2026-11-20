"""服务配置：全部来自环境变量。

所有数值都有上下限夹取，防止误配把宿主机拖垮（例如把超时配成 0 或把产出上限配成 10GB）。
"""

from __future__ import annotations

import os
from pathlib import Path

# 默认允许回传的产出后缀（不含点）。其余文件留在容器里不采集。
DEFAULT_ALLOWED_EXT = (
    "png,jpg,jpeg,webp,gif,svg,pdf,"
    "csv,tsv,json,txt,md,tex"
)


def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


def _env_int(name: str, default: int, low: int = 0, high: int | None = None) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    value = max(low, value)
    return value if high is None else min(high, value)


def _env_float(name: str, default: float, low: float = 0.0,
               high: float | None = None) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    value = max(low, value)
    return value if high is None else min(high, value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


class Settings:
    """一次性从环境变量读全部配置（进程启动时实例化）。"""

    def __init__(self) -> None:
        self.version = "1.0.0"

        # ---- 运行目录：每个请求建一个 <base_dir>/<run_id>/ 子目录，跑完即删 ----
        self.base_dir = Path(_env_str("SANDBOX_BASE_DIR", "/tmp/sandbox") or "/tmp/sandbox")

        # ---- 鉴权：留空 = 不校验（只适合本机 / 内网回环）----
        self.api_key = _env_str("SANDBOX_API_KEY")

        # ---- 并发 ----
        self.max_concurrency = _env_int("SANDBOX_MAX_CONCURRENCY", 2, 1, 64)
        # 排队等并发槽的上限（秒），0 = 无限等
        self.queue_timeout = _env_float("SANDBOX_QUEUE_TIMEOUT", 30.0, 0.0, 600.0)

        # ---- 执行超时 ----
        self.default_timeout = _env_float("SANDBOX_DEFAULT_TIMEOUT", 20.0, 1.0, 600.0)
        self.max_timeout = _env_float("SANDBOX_MAX_TIMEOUT", 60.0, 1.0, 600.0)
        if self.default_timeout > self.max_timeout:
            self.default_timeout = self.max_timeout

        # ---- 入参上限 ----
        self.max_code_bytes = _env_int(
            "SANDBOX_MAX_CODE_BYTES", 256 * 1024, 1024, 8 * 1024 * 1024)
        self.max_input_files = _env_int("SANDBOX_MAX_INPUT_FILES", 20, 0, 200)
        self.max_input_file_bytes = _env_int(
            "SANDBOX_MAX_INPUT_FILE_BYTES", 8 * 1024 * 1024, 1024, 256 * 1024 * 1024)

        # ---- 产出上限 ----
        self.max_output_files = _env_int("SANDBOX_MAX_OUTPUT_FILES", 20, 1, 200)
        self.max_output_file_bytes = _env_int(
            "SANDBOX_MAX_OUTPUT_FILE_BYTES", 10 * 1024 * 1024, 1024, 512 * 1024 * 1024)
        self.max_output_bytes = _env_int(
            "SANDBOX_MAX_OUTPUT_BYTES", 24 * 1024 * 1024, 1024, 1024 * 1024 * 1024)
        # 单次回传的 stdout / stderr 上限（超出截断，尾部留提示）
        self.max_log_bytes = _env_int("SANDBOX_MAX_LOG_BYTES", 64 * 1024, 4096, 4 * 1024 * 1024)

        # ---- 子进程资源限制（0 = 不设）----
        # ⚠️ RLIMIT_AS 计的是虚拟内存，numpy/OpenBLAS 启动就会预留几百 MB，
        #    设太小会出现"import numpy 就 MemoryError"的假故障；容器层 cgroup
        #    （compose 的 mem_limit）才是主防线，这里默认不设。
        self.mem_limit_mb = _env_int("SANDBOX_MEM_LIMIT_MB", 0, 0, 64 * 1024)
        self.fsize_limit_mb = _env_int("SANDBOX_FSIZE_LIMIT_MB", 64, 0, 4096)
        self.nproc_limit = _env_int("SANDBOX_NPROC_LIMIT", 64, 0, 4096)
        self.nofile_limit = _env_int("SANDBOX_NOFILE_LIMIT", 256, 16, 65536)
        # CPU 时间 = 墙钟超时 + 该余量（防止用户代码卡在纯 CPU 计算里杀不掉）
        self.cpu_extra_seconds = _env_int("SANDBOX_CPU_EXTRA_SECONDS", 5, 0, 600)

        # ---- 产出采集 ----
        exts = _env_str("SANDBOX_ALLOWED_EXT", DEFAULT_ALLOWED_EXT) or DEFAULT_ALLOWED_EXT
        self.allowed_exts = frozenset(
            e.strip().lower().lstrip(".") for e in exts.split(",") if e.strip())
        self.figure_dpi = _env_int("SANDBOX_FIGURE_DPI", 150, 60, 600)

        # ---- 调试：保留运行目录（排查完记得手动清 /tmp/sandbox）----
        self.keep_workdir = _env_bool("SANDBOX_KEEP_WORKDIR", False)

    def describe(self) -> dict:
        """给 GET /v1/info 用（不含 api_key）。"""
        return {
            "max_concurrency": self.max_concurrency,
            "queue_timeout_s": self.queue_timeout,
            "default_timeout_s": self.default_timeout,
            "max_timeout_s": self.max_timeout,
            "max_code_bytes": self.max_code_bytes,
            "max_input_files": self.max_input_files,
            "max_input_file_bytes": self.max_input_file_bytes,
            "max_output_files": self.max_output_files,
            "max_output_file_bytes": self.max_output_file_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_log_bytes": self.max_log_bytes,
            "mem_limit_mb": self.mem_limit_mb,
            "fsize_limit_mb": self.fsize_limit_mb,
            "nproc_limit": self.nproc_limit,
            "nofile_limit": self.nofile_limit,
            "allowed_ext": sorted(self.allowed_exts),
            "figure_dpi": self.figure_dpi,
            "auth_required": bool(self.api_key),
            "keep_workdir": self.keep_workdir,
        }


settings = Settings()
