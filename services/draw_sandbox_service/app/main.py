"""画图沙盒服务 · HTTP 入口（FastAPI）。

场景：论文阅读器需要"让模型画一张图"时，把生成的 Python 代码（通常是 matplotlib）
送进来，在容器内的受限子进程里跑一遍，再把 stdout / stderr 与产出的图片回传出去。

接口：
    POST /v1/run              执行代码，返回 JSON（产出文件 base64 内联）
    POST /v1/run?archive=1    同样执行，但产出打包成 zip 直接下载
    GET  /healthz             健康检查
    GET  /v1/info             版本与当前限制
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from importlib import metadata
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import sandbox
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("draw_sandbox")


# =====================================================================
#  请求 / 响应模型
# =====================================================================
class InputFile(BaseModel):
    name: str = Field(..., description="相对路径，例如 data.csv")
    content: str = Field(..., description="文件内容")
    encoding: Literal["utf8", "base64"] = Field("utf8", description="内容编码")


class RunRequest(BaseModel):
    code: str = Field(..., description="要执行的 Python 代码")
    timeout: float | None = Field(
        None, gt=0, le=3600,
        description="执行超时（秒），超过服务端上限会被夹到上限")
    files: list[InputFile] = Field(
        default_factory=list, description="额外的输入文件（可选）")


# =====================================================================
#  应用
# =====================================================================
_sem = asyncio.Semaphore(settings.max_concurrency)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        settings.base_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:                       # 只读根文件系统且 /tmp 不可写
        log.error("工作目录 %s 不可用：%s", settings.base_dir, exc)
        raise
    log.info("draw-sandbox %s 启动，python=%s，限制=%s",
             settings.version, sys.version.split()[0], settings.describe())
    yield


app = FastAPI(
    title="Draw Sandbox Service",
    description="接收 Python / matplotlib 代码，在受限子进程里执行并回传产出文件。",
    version=settings.version,
    lifespan=lifespan,
)


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """配置了 SANDBOX_API_KEY 时才校验，留空表示不鉴权。"""
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="无效的 X-API-Key")


def _pkg_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


# =====================================================================
#  路由
# =====================================================================
@app.get("/healthz", summary="健康检查")
async def healthz() -> dict:
    return {"status": "ok", "service": "draw-sandbox",
            "version": settings.version, "ts": int(time.time())}


@app.get("/v1/info", summary="版本与限制")
async def info() -> dict:
    return {
        "service": "draw-sandbox",
        "version": settings.version,
        "python": sys.version.split()[0],
        "packages": {
            name: _pkg_version(name)
            for name in ("fastapi", "uvicorn", "matplotlib", "numpy")
        },
        "limits": settings.describe(),
    }


@app.post("/v1/run", summary="执行一段代码并回传产出")
async def run(
    req: RunRequest,
    archive: bool = Query(False, description="true 时产出打包成 zip 下载"),
    _: None = Depends(require_api_key),
) -> Any:
    # ---- 1) 参数校验（不占用并发槽）----
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="code 不能为空")
    if len(req.code.encode("utf-8")) > settings.max_code_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"代码超过上限 {settings.max_code_bytes} 字节")
    if len(req.files) > settings.max_input_files:
        raise HTTPException(
            status_code=413,
            detail=f"输入文件数超过上限 {settings.max_input_files}")

    # ---- 2) 抢并发槽 ----
    try:
        await asyncio.wait_for(_sem.acquire(), timeout=settings.queue_timeout or None)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=429, detail="沙盒繁忙，请稍后重试") from None

    run_id = uuid.uuid4().hex
    run_dir = settings.base_dir / run_id

    # ---- 3) 准备 + 执行 ----
    try:
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法创建运行目录：{exc}") from exc

        try:
            workdir, seeded = sandbox.prepare_run(run_dir, req.code, req.files)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"输入文件非法：{exc}") from exc

        timeout = sandbox.resolve_timeout(req.timeout)
        log.info("run %s 开始：timeout=%.1fs files=%d code=%d字节",
                 run_id, timeout, len(req.files), len(req.code.encode("utf-8")))

        result = await asyncio.to_thread(
            sandbox.execute, run_dir, workdir, timeout, seeded)

        log.info("run %s 结束：status=%s exit=%s %.0fms outputs=%d",
                 run_id, result.status, result.exit_code,
                 result.duration_ms, len(result.outputs))
    except HTTPException:
        sandbox.cleanup(run_dir, settings.keep_workdir)
        raise
    except Exception as exc:                    # noqa: BLE001 - 兜底，别把栈给调用方
        log.exception("run %s 内部错误", run_id)
        sandbox.cleanup(run_dir, settings.keep_workdir)
        raise HTTPException(status_code=500, detail=f"沙盒内部错误：{exc}") from exc
    finally:
        _sem.release()

    # ---- 4) 回传 ----
    try:
        if archive:
            zip_path = sandbox.build_archive(run_dir, result)
            # 响应发完再删目录
            return FileResponse(
                zip_path,
                media_type="application/zip",
                filename=f"outputs-{run_id}.zip",
                headers={"X-Run-Id": run_id, "X-Sandbox-Status": result.status},
                background=BackgroundTask(sandbox.cleanup, run_dir, settings.keep_workdir),
            )

        payload = sandbox.to_json(result)
        payload["run_id"] = run_id
        return JSONResponse(payload)
    except Exception:
        sandbox.cleanup(run_dir, settings.keep_workdir)
        raise
    finally:
        if not archive:
            sandbox.cleanup(run_dir, settings.keep_workdir)


@app.get("/", include_in_schema=False)
async def index() -> dict:
    return {
        "service": "draw-sandbox",
        "version": settings.version,
        "docs": "/docs",
        "endpoints": ["POST /v1/run", "GET /healthz", "GET /v1/info"],
    }
