# draw_sandbox_service · 画图沙盒

给论文阅读器用的**代码执行沙盒**：容器启动后对外提供一个 HTTP 接口，接收一段 Python 代码
（通常是模型生成的 matplotlib 画图代码），在受限子进程里执行完，把 **stdout / stderr**
和**产出的图片等文件**回传出去。

- 基础镜像：`python:3.11-slim`
- 必装依赖：`matplotlib`（附 `numpy`）、`fastapi`、`uvicorn`
- 中文字体：`fonts-wqy-microhei`（约 2MB），中文标题/坐标轴不会显示成方框
- 执行方式：每个请求独立临时目录 + 独立进程组，超时整组 `SIGKILL`，跑完即删

## 目录结构

```
draw_sandbox_service/
├── app/
│   ├── main.py       # FastAPI 入口：/v1/run、/healthz、/v1/info
│   ├── sandbox.py    # 执行核心：建目录 → 跑子进程 → 采集产出 → 打包 / 清理
│   ├── runner.py     # 子进程入口：上资源限制、固定 Agg 后端、跑用户代码、图落盘
│   └── config.py     # 环境变量配置（带上下限夹取）
├── Dockerfile
├── docker-compose.yaml   # 带隔离加固（只读根文件系统 / tmpfs / cap_drop / cgroup 限制）
├── requirements.txt
├── example_client.py     # 本地联调示例客户端
└── .env.example
```

## 快速开始

```bash
cd draw_sandbox_service

# 1) 起服务（首次会构建镜像）
docker compose up -d --build

# 2) 健康检查
curl http://127.0.0.1:8090/healthz

# 3) 跑一段示例 matplotlib 代码，产出的 PNG 会存到 ./out
python example_client.py
```

日志：`docker compose logs -f draw-sandbox`

## 接口

### `POST /v1/run` — 执行代码

请求体（JSON）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `code` | string | 是 | 要执行的 Python 代码 |
| `timeout` | number | 否 | 执行超时（秒），超过服务端 `SANDBOX_MAX_TIMEOUT` 会被夹到上限 |
| `files` | array | 否 | 额外输入文件 `[{name, content, encoding}]`，`encoding` 取 `utf8`/`base64`，`name` 为相对路径 |

响应体（HTTP 200 即"请求处理完成"，成功与否看 `ok`）：

```json
{
  "ok": true,
  "status": "success",
  "exit_code": 0,
  "timed_out": false,
  "duration_ms": 812,
  "stdout": "",
  "stderr": "",
  "error": null,
  "outputs": [
    {
      "name": "figure_1.png",
      "size": 24680,
      "mime": "image/png",
      "sha256": "…",
      "encoding": "base64",
      "data": "iVBORw0KGgo…"
    }
  ],
  "outputs_truncated": false,
  "run_id": "3f2a…"
}
```

`status` 取值：

| 值 | 含义 |
| --- | --- |
| `success` | 代码正常跑完（`plt.show()` / 结束时还开着的图已自动存盘） |
| `error` | 代码抛异常或 `sys.exit(非 0)`，看 `error` / `stderr` |
| `timeout` | 超时被整组杀死 |
| `killed` | 被信号终止（常见 `SIGKILL` 内存超限 / `SIGXFSZ` 单文件超限） |

只有**请求本身有问题**才用非 200：`400` 参数不合法、`401` Key 不对、`413` 超出上限、
`429` 并发已满、`500` 服务内部错误。

### `POST /v1/run?archive=1` — 直接下载 zip

同样执行，但产出打包成 zip（`manifest.json` + `stdout.txt` + `stderr.txt` + `outputs/…`），
响应的 `Content-Type` 为 `application/zip`。适合图片多 / 需要原始文件的场景。

### 其它

- `GET /healthz` — 健康检查（Docker HEALTHCHECK 用它）
- `GET /v1/info` — 版本、Python 与依赖版本、当前全部限制
- `GET /docs` — 自动生成的交互式接口文档

## 调用示例

### PowerShell（注意：内联 JSON 的双引号会被吞，先写文件再 `-d @file`）

```powershell
'{"code":"import matplotlib.pyplot as plt\nplt.plot([1,2,3],[1,4,9])\nplt.title(\"demo\")","timeout":20}' |
  Set-Content -Encoding ascii req.json
curl.exe -s -X POST "http://127.0.0.1:8090/v1/run" -H "Content-Type: application/json" -d "@req.json"
```

### Python（阅读器后端集成）

```python
import base64, requests

resp = requests.post(
    "http://draw-sandbox:8090/v1/run",
    json={"code": llm_generated_code, "timeout": 30},
    headers={"X-API-Key": "你的KEY"},
    timeout=90,
)
data = resp.json()
if data["ok"]:
    png = base64.b64decode(data["outputs"][0]["data"])
```

## 沙盒内约定的行为

1. **`plt.show()` 不会阻塞**：被替换成"保存当前所有图 → `figure_N.png`"。
2. **用户没保存的图也会落盘**：代码跑完后收尾再存一次，文件名同样是 `figure_N.png`（不覆盖已有文件）。
3. **后端固定 `Agg`**：用户代码里写 `matplotlib.use("TkAgg")` / `plt.switch_backend(...)` 会被忽略并在 stderr 留一行提示。
4. **中文字体已配好**：优先使用镜像安装的 WenQuanYi Micro Hei（普通文本不会可靠地逐字回退到后续字体），其余字体作为后备，`axes.unicode_minus=False`。
5. **工作目录**：用户代码的 cwd 就是临时工作目录，可以 `import` 同目录的辅助模块、读写 `files` 传入的数据文件。
6. **产出采集**：只回传 `SANDBOX_ALLOWED_EXT` 里的后缀（默认 png/jpg/jpeg/webp/gif/svg/pdf/csv/tsv/json/txt/md/tex），
   跳过隐藏文件、`__pycache__`、符号链接，并受文件数 / 单文件 / 总量上限约束。

## 安全模型（重要）

- **进程级**：每次执行独立临时目录 + 独立 session，超时 `killpg`，父进程退出后再补一刀清理孙进程。
- **资源级**：`RLIMIT_CPU / FSIZE / NPROC / NOFILE` 在子进程里设置；容器层再叠 `mem_limit=2g`、`cpus=2`、`pids_limit=256`。
  > ⚠️ `SANDBOX_MEM_LIMIT_MB`（`RLIMIT_AS`）默认 **0 不设**：它计虚拟内存，numpy/OpenBLAS 启动就会预留几百 MB，
  > 设小了会出现"import numpy 就 MemoryError"的假故障。内存主防线用 cgroup（compose 的 `mem_limit`）。
- **容器级**：`read_only` 根文件系统 + 只有 `/tmp` tmpfs 可写、`cap_drop: ALL`、`no-new-privileges`、非 root（uid 10001）。
- **网络**：容器仍能出网（服务要收 HTTP 请求）。**不要把执行任意代码的沙盒暴露到公网**；
  生产建议把它放在只允许后端访问的内网/独立网络里，并设置 `SANDBOX_API_KEY`。
  需要完全断网时，可把沙盒放到 `internal: true` 的 Docker 网络，仅由后端同网络访问。
- 这层隔离防的是"模型写错的代码"（死循环、爆内存、疯狂存盘），**不防恶意逃逸**；
  真要跑不受信任的代码，请再套 gVisor / Kata / 独立虚拟机。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SANDBOX_PORT` | `8090` | compose 映射到宿主机的端口（仅 127.0.0.1）；容器内固定监听 8090 |
| `SANDBOX_API_KEY` | 空 | 非空时请求必须带 `X-API-Key` |
| `SANDBOX_BASE_DIR` | `/tmp/sandbox` | 运行目录根 |
| `SANDBOX_MAX_CONCURRENCY` | `2` | 同时执行的任务数 |
| `SANDBOX_QUEUE_TIMEOUT` | `30` | 排队等并发槽的上限（秒），0 = 无限 |
| `SANDBOX_DEFAULT_TIMEOUT` | `20` | 请求未带 `timeout` 时的默认值 |
| `SANDBOX_MAX_TIMEOUT` | `60` | `timeout` 的上限 |
| `SANDBOX_MAX_CODE_BYTES` | `262144` | 代码最大字节数 |
| `SANDBOX_MAX_OUTPUT_FILES` | `20` | 最多回传文件数 |
| `SANDBOX_MAX_OUTPUT_FILE_BYTES` | `10485760` | 单文件上限 |
| `SANDBOX_MAX_OUTPUT_BYTES` | `25165824` | 回传总量上限 |
| `SANDBOX_MAX_LOG_BYTES` | `65536` | stdout / stderr 各截断到多少 |
| `SANDBOX_ALLOWED_EXT` | 见上 | 允许回传的后缀，逗号分隔 |
| `SANDBOX_MEM_LIMIT_MB` | `0` | 子进程 `RLIMIT_AS`（默认关，见上文说明） |
| `SANDBOX_FSIZE_LIMIT_MB` | `64` | 子进程单个文件最大写入量 |
| `SANDBOX_NPROC_LIMIT` | `64` | 子进程可创建的进程/线程数 |
| `SANDBOX_NOFILE_LIMIT` | `256` | 子进程文件句柄数 |
| `SANDBOX_CPU_EXTRA_SECONDS` | `5` | `RLIMIT_CPU = 超时 + 该余量` |
| `SANDBOX_FIGURE_DPI` | `150` | 自动保存 `figure_N.png` 的 DPI |
| `SANDBOX_KEEP_WORKDIR` | `0` | 1 = 保留运行目录用于排障 |

## 不用 compose 时

```bash
docker build -t draw-sandbox:1.0.0 .

docker run -d --name draw-sandbox \
  -p 127.0.0.1:8090:8090 \
  -e SANDBOX_API_KEY=change-me \
  --memory=2g --cpus=2 --pids_limit=256 \
  --read-only --tmpfs /tmp:rw,nosuid,noexec,size=256m \
  --cap-drop=ALL --security-opt no-new-privileges \
  draw-sandbox:1.0.0
```
