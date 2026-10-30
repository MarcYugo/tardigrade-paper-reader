# 用 Docker 部署（应用容器）

把应用打包成容器，与宿主机环境隔离；数据仍然在 MongoDB（`mongo_configuration/`）里，
和直接 `python run.py` 用的是同一套库、同一份数据。

## 1. 文件说明

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 应用镜像：python:3.11-slim + 依赖 + 代码，非 root 用户运行 |
| `docker-compose.yml` | 应用服务定义：端口、环境变量、数据卷、健康检查、网络 |
| `.env.example` | 可覆盖的配置（端口 / 存储后端 / 是否允许注册 / 默认 LLM），复制成 `.env` 生效 |
| `../.dockerignore` | 构建上下文排除项（`data/`、`config.json`、`mongo_configuration/.env` 不进镜像） |

## 2. 启动

```bash
# ① 先起 MongoDB（就是 mongo_configuration 里那套，两份 compose 共用网络 paper-mongo-net）
cd mongo_configuration && cp -n .env.example .env && docker compose up -d

# ② 再构建并启动应用
cd ../docker && cp -n .env.example .env
docker compose up -d --build

# ③ 打开 http://127.0.0.1:8000
docker compose ps          # app 应为 healthy
docker compose logs -f app # 看日志（会打印“数据存储：MongoDB mongo:27017 / 库 paper”）
```

首次打开会照常走「创建管理员账号 → 配置 LLM」；账号、论文、笔记等都在 MongoDB 里，
**和宿主机上跑 `python run.py` 看到的是同一份数据**（只要指向同一个库）。

## 3. 常用操作

```bash
cd docker
docker compose logs -f app         # 日志
docker compose restart app         # 重启（改了 .env 后：docker compose up -d 即可）
docker compose down                # 停止并删容器（数据卷 paper-app-data 保留）
docker compose up -d --build       # 改代码后重新构建

docker compose exec app python tools/migrate_to_mongo.py --dry-run   # 容器内跑迁移工具
docker compose exec app python tools/selftest.py                     # 容器内自检解析模块
docker compose exec app sh                                           # 进容器
```

## 4. 数据放在哪

| 内容 | 位置 |
| --- | --- |
| 账号 / 论文元数据 / 版式数据 / **插图** / 标注 / 笔记 / 译文 / LLM 配置 | **MongoDB**（`mongo_configuration` 的卷 `paper-mongo-data`）—— 插图存在 GridFS 桶 `images`，重建容器/换机器都不丢 |
| 上传临时文件；「回退模式」下的 JSON 数据与插图 | 应用卷 **`paper-app-data`** → 容器内 `/app/data` |

```bash
docker volume inspect paper-app-data            # 看卷在宿主机的路径
docker run --rm -v paper-app-data:/d -v $PWD:/b alpine tar czf /b/app-data.tgz -C /d .
```

想直接在宿主机看图片 / 用项目里已有的 `data/`，把 compose 里的目录挂载取消注释：

```yaml
- ../data:/app/data
```
（需保证宿主机目录属主是容器里的 `appuser`：`mkdir -p ../data && sudo chown -R 1000:1000 ../data`）

## 5. 关键环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_PORT` | `8000` | 宿主机映射端口（只绑 127.0.0.1） |
| `APP_HOST` / `APP_PORT`(容器内) | `0.0.0.0:8000` | 容器内监听地址，**必须是 0.0.0.0**，否则端口映射不到 |
| `MONGO_HOST` | `mongo` | 模板网络里的服务名（不是 `127.0.0.1`） |
| `MONGO_URI` | 空 | 给了就用整串（云端 Atlas / 外部实例），优先级最高 |
| `MONGO_APP_USER` / `_PASSWORD` / `_DB` | 从 `../mongo_configuration/.env` 继承 | 数据库账号，与 compose 里那份保持一致 |
| `APP_STORAGE` | `auto` | `auto` 连不上库回退本地 JSON；`mongo` 连不上就启动失败；`json` 只用本地 |
| `PDF_PARSER_BACKEND` | 空 | **留空 = 用网页「⚙ 设置」里保存的解析后端**（可随时切换）；填 `pymupdf`/`auto`/`detection_service_group`（本地解析方案）或 `surya`/`unlimited_ocr` 则强制锁定，会覆盖面板值（页面会提示）。旧值 `ftgroup` 仍被接受 |
| `DETECTION_SERVICE_GROUP_URL` | `http://host.docker.internal:9003/v1` | detection-service-group（formula_table_service_group 的 router_service）地址；Figure 裁图与公式框 + LaTeX 由它提供 |
| `DETECTION_SERVICE_GROUP_TIMEOUT` | `300` | 一次预测（检测+识别）整体超时（秒） |
| `DETECTION_SERVICE_GROUP_KEEP_CROPS` | `true` | 是否保留公式兜底裁图；Figure 显示所需裁图始终开启 |
| `UNLIMITED_OCR_URL` / `UNLIMITED_OCR_MODEL` / `UNLIMITED_OCR_TIMEOUT` / `UNLIMITED_OCR_API_KEY` | 见 compose | Unlimited-OCR 整页 OCR 推理服务（vLLM 的 OpenAI 兼容接口，`backend=unlimited_ocr` 时使用）：文字 / 公式 / 表格 / 插图全用它 |
| `PADDLE_OCR_URL` / `PADDLE_OCR_MODEL` / `PADDLE_OCR_API_KEY` | 见 compose | **已删除**：PaddleOCR-VL 的旧客户端与配对链路已随代码移除，这些变量不再被读取（仅作历史记录保留） |
| `ALLOW_SIGNUP` | `1` | 未登录自助注册；**暴露到公网请设 0** |
| `DEEPSEEK_API_KEY` 等 | 空 | 安装级默认 LLM；每个账号还能在网页里各自覆盖 |
| `MEM_LIMIT` / `MEM_RESERVATION` / `MEMSWAP_LIMIT` | 空（不限） | 容器内存硬上限 / 软保障 / “上限+swap”总配额（见第 8 节） |
| `SHM_SIZE` | `512m` | `/dev/shm` 大小；Docker 默认只给 64MB，PDF 位图渲染容易撞上限 |

## 6. 与 `mongo_configuration` 的关系

两份 compose 通过**同名网络** `paper-mongo-net` 打通：谁先起谁创建，后起的直接加入。
所以顺序上先起 `mongo_configuration` 更稳妥（否则应用会因连不上库而回退本地 JSON，
日志与网页上都会有提示，起来后在「⚙ 设置」里点「重试连接 MongoDB」即可切回）。

只想跑应用、不要数据库：

```bash
APP_STORAGE=json docker compose up -d --build     # 数据落在 paper-app-data 卷的 JSON 文件里
```

## 7. 注意事项

- 镜像里**不含** `config.json` 与 `mongo_configuration/.env`（避免把密钥打进镜像）。要用项目里的
  `config.json`（含 `deepseek_api_key`、`parser` 段）请把 compose 里那行挂载取消注释 —— 
  宿主机上该文件必须先存在，否则 Docker 会建出一个同名目录；**不要加 `:ro`**（网页里保存
  解析后端 / LLM Key 时要写回它），属主给 uid 1000：
  `touch ../config.json && sudo chown 1000:1000 ../config.json`。
  不挂载也能跑，但网页里改的解析后端只存在容器内，`docker compose up -d --build` 重建后会回到默认。
- 解析后端面板切换不生效时，先看页面上的 `⚠️ 已设置环境变量 PDF_PARSER_BACKEND` 提示：
  环境变量优先级高于 `config.json`，删掉 `docker/.env` 里的那行再 `docker compose up -d` 重建即可。
- `data/` 目录用的是**命名卷**，所以不会出现宿主机权限问题；要用目录挂载见第 4 节。
- 服务默认只绑 `127.0.0.1`，容器与宿主机一致：**别直接暴露到公网**；要对外请加 HTTPS 反向代理，
  并把 `ALLOW_SIGNUP` 设为 `0`。
- 容器里的 `appuser` 是 uid 1000；`/app/data` 已按该属主创建。

## 8. 内存不够怎么办

**先搞清一个前提**：Linux 上 Docker **默认不给容器任何内存上限**，容器能用到整台机器。
所以 `docker stats` 里那行 `MEM USAGE / LIMIT` 显示的 15.58GiB 之类的数字，就是宿主机的总内存，
不是“分给容器的配额”。如果你觉得内存吃紧，先判断是下面哪种情况。

### ① 先确认到底是不是内存问题

```bash
cd docker
docker stats --no-stream                     # 实时看 app / mongo 各自占多少

docker inspect paper-app --format '{{.State.OOMKilled}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}'
# false 0 0  → 没被 OOM 杀，且没有设上限

dmesg -T | grep -i "killed process"          # 内核有没有杀过进程（OOM killer）
journalctl -k --since '1 day ago' | grep -i oom
docker logs --tail 50 paper-app              # 有没有 MemoryError / 被 SIGKILL
free -h                                      # 宿主机还剩多少
```

- 显示 `OOMKilled=true` 或 `dmesg` 里有 `Killed process ... (python)` → 确实是内存爆了，走 ②。
- 显示 `Memory=0` 且没被 OOM 杀 → **不是“分少了”**，是宿主机 RAM 本身不够（走 ③）或撞了 `/dev/shm`（走 ④）。

### ② 显式给容器分配内存（限制 / 保障）

在 `docker/.env` 里放开（模板见 `.env.example` 末尾），然后 `docker compose up -d`：

```dotenv
MEM_LIMIT=10g          # 硬上限：超过就被内核 OOM 杀掉
MEM_RESERVATION=2g     # 软保障：调度时预留，不限制实际使用
MEMSWAP_LIMIT=12g      # 限制+swap 的总配额；写成 = MEM_LIMIT 表示禁用 swap；-1 = 不限
```

几个要点：

- **不填 = 不限制**（compose 里是 `0`），这是“能给的最多”，比任何数值都大。
- 硬上限是**天花板不是保证**。设得比峰值需求低，只会让进程更容易被杀，别的服务抢内存的情况才需要它。
- 想要 swap 兜底，宿主机得先有 swap（`free -h` 看 `Swap` 行；没有就 `fallocate` + `mkswap` + `swapon`）。
- 单机部署想让 app 不被 mongo 挤到，也可以反过来给 mongo 设上限（那边已有 `MONGO_CACHE_GB` 控制 WiredTiger 缓存）。
- 等价写法（swarm 风格，`docker compose` 也认）：

```yaml
deploy:
  resources:
    limits:   { memory: 10g }
    reservations: { memory: 2g }
```

### ③ 宿主机内存不够 → 加内存 / 加 swap

```bash
free -h                     # total 只有几 G、available 很小，就是宿主本身紧张
```

容器是进程，不是虚拟机，**给容器“多分内存”本质就是给宿主机加内存**。
临时缓解可以加 swap（会被内核 OOM 杀的排序靠后），但解析大 PDF 还是建议加物理内存。
关掉不用的容器、别同时跑多个 mongo-express 也能省一点。

### ④ 别忽略 `/dev/shm`（默认只有 64MB）

Docker 每个容器的 `/dev/shm` 默认 **64MB**。PyMuPDF 渲染整页位图、OpenCV / 多线程库会用到它，
撞上时报错信息往往像是“内存不够”。compose 里已默认放大：

```yaml
shm_size: ${SHM_SIZE:-512m}   # 想再大就设 SHM_SIZE=1g
```

这是 tmpfs，**按用到的量分配、不常驻**，调大基本没有副作用。验证：

```bash
docker compose exec app df -h /dev/shm
```

### ⑤ 用 Docker Desktop（macOS / Windows）才需要改“虚拟机内存”

Docker Desktop 跑在一个 Linux 虚拟机里，默认内存上限是固定的（常见 2~8GB）。
这时无论怎么改 compose 都突破不了 → 打开 **Settings → Resources → Memory** 调大，Reset & Restart。
（本机是原生 Linux，Docker 直接看到 15.58GiB，不存在这一层。）
