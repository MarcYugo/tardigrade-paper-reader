# 单机版 MongoDB（Docker Compose）

本地 Mongo 部署，用于开发/单机运行（非副本集、非集群）。

## 1. 文件说明

| 文件 | 作用 |
| --- | --- |
| `docker-compose.yml` | 服务定义：`mongo` + 可选 `mongo-express`（图形界面） |
| `.env.example` | 端口/账号密码模板，**复制成 `.env` 后生效** |
| `init/01-init-app-user.sh` | 创建/更新业务库普通账号（**每次启动都会执行**，见下节） |
| `entrypoint.sh` | 自定义入口：把 `init/*.sh` 包成“每次启动都执行” |
| `data/`、`.env` | 运行期数据，已在 `.gitignore` 中忽略 |

## 2. 如何拉取（镜像）

Docker 不会“单独拉配置”，镜像 + compose 文件一起组成运行环境。镜像有三种拿法：

```bash
# 方法一：compose 自动拉（推荐，按 compose 里写的 mongo:7.0 拉取并启动）
cp .env.example .env
docker compose up -d

# 方法二：先手动拉镜像，再起容器
docker pull mongo:7.0
docker compose up -d

# 方法三：只想单独跑数据库（不启 mongo-express）
docker compose up -d mongo
```

只想下载不动配置时可 `docker compose pull`；确认镜像已就位用 `docker images | grep mongo`。

### 镜像加速（国内拉取超时/报 manifest unknown）

`manifest unknown` / `i/o timeout` 基本都是网络问题，给 Docker 配镜像源：

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://mirror.ccs.tencentyun.com"
  ]
}
JSON
sudo systemctl daemon-reload && sudo systemctl restart docker
```

镜像源会失效，报错就换一个可用的。`mongo:7.0` 拉不到时可退而用 `mongo:7` 或 `mongo:8.0`（大版本号会随上游更新，生产建议锁小版本）。

## 3. 连接

`.env` 默认值对应的连接串：

```
# 应用账号（业务库 paper，推荐给后端用）
mongodb://paper:paper123@127.0.0.1:27017/paper?authSource=paper

# 管理员账号（运维/建库用）
mongodb://root:changeme@127.0.0.1:27017/?authSource=admin
```

PyMongo 用法：

```python
from pymongo import MongoClient
client = MongoClient("mongodb://paper:paper123@127.0.0.1:27017/paper?authSource=paper")
db = client["paper"]
```

命令行连：

```bash
# 进容器内的 shell
docker compose exec mongo mongosh -u paper -p paper123 --authenticationDatabase paper paper

# 或从宿主机（需宿主机装了 mongosh）
mongosh "mongodb://paper:paper123@127.0.0.1:27017/paper?authSource=paper"
```

> 注意：端口只映射到 `127.0.0.1`，所以**默认只有本机能连**。容器之间用服务名 `mongo:27017` 互连（见 compose 里的网络 `paper-mongo-net`）。

## 4. 应用侧怎么连（paper-trans-to-html）

后端 `backend/db.py` **直接读本目录的 `.env`**，不需要在项目里再配一遍：

```bash
cd mongo_conf && docker compose up -d     # 1) 先把数据库起来
cd .. && python run.py                    # 2) 再启动应用
# 启动日志会打印：数据存储：MongoDB 127.0.0.1:27017 / 库 paper（配置来源 mongo_conf/.env）
```

连接参数读取顺序（高 → 低）：

1. 环境变量 `MONGO_URI`（完整连接串，含云端 Atlas / 已有实例）
2. 环境变量 `MONGO_HOST` / `MONGO_PORT` / `MONGO_USER` / `MONGO_PASSWORD` / `MONGO_DB` / `MONGO_AUTH_DB`
3. 本目录 `.env`：`MONGO_PORT`、`MONGO_APP_USER`、`MONGO_APP_PASSWORD`、`MONGO_APP_DB`
4. 内置默认：`paper:paper123@127.0.0.1:27017/paper?authSource=paper`（与本目录 `.env.example` 一致）

默认用 `.env` 里的**业务账号** `MONGO_APP_USER`（对本库 readWrite 权限）连接，不用 root；
root 只在运维（备份/清库）时用。

### 应用把什么存在哪

| 集合 | 内容 |
| --- | --- |
| `records` | 论文元数据/阅读进度/状态；**`owner`= 归属账号，论文按账号隔离** |
| `documents` | 版式数据（原先的 `doc.json`） |
| `highlights` / `notes` | 高亮 / 笔记（一条一个文档，增删改都是单文档操作） |
| `translations` | 译文缓存（一段一条，可查可统计） |
| `users` | 账号（口令为 PBKDF2 加盐哈希）；`prefs` 存该账号的偏好与配置（LLM 地址/Key/模型、自定义高亮色、显示译文开关） |
| `sessions` | 登录会话（带 TTL 索引，过期自动清理） |
| `settings` | **仅旧版遗留**：升级前 LLM 配置是全局一份存在这里（`_id="llm"`），第一次启动时会被归到最早创建的账号，之后不再写入 |

**图片仍然是文件**：落在 `data/docs/<论文id>/images/`（二进制大文件放文件系统更合适），
所以备份数据时数据部分用 `mongodump`，图片记得一起打包 `data/docs/`。

### 数据库没起来会怎样

应用默认 `APP_STORAGE=auto`：连不上 MongoDB 时**回退本地 JSON**（`data/*.json`），
网页上会提示“未连接 MongoDB”，点右上角「⚙ LLM 设置 → 重试连接 MongoDB」即可切回数据库，
不用重启服务。想“必须用库、连不上就报错”就设 `APP_STORAGE=mongo`。

### 把已有的本地数据搬进来

```bash
python tools/migrate_to_mongo.py --dry-run   # 先看要迁什么
python tools/migrate_to_mongo.py             # 真正迁移（同 id 覆盖）
```

### 清空/重置数据

应用用的业务账号**没有** `dropDatabase` 权限（这是好事）。要清空请用 root：

```bash
docker compose exec mongo mongosh -u root -p changeme --authenticationDatabase admin \
  --eval 'db.getSiblingDB("paper").dropDatabase()'
```

下次启动应用会自动重建索引；账号也没了，网页上会回到“首次使用 · 创建管理员账号”。

## 5. 常用运维命令

```bash
docker compose ps                       # 状态，STATUS 显示 healthy 才算就绪
docker compose logs -f mongo            # 看日志
docker compose stop                     # 停止（保留数据）
docker compose start                    # 再启动
docker compose down                     # 删除容器+网络（数据卷保留）
docker compose down -v                  # ⚠️ 连数据卷一起删，数据全丢
```

### 备份 / 恢复

```bash
# 备份：导出到宿主机当前目录的 dump/
docker compose exec -T mongo mongodump \
  -u root -p changeme --authenticationDatabase admin --db paper --archive --gzip > dump.gz

# 恢复
cat dump.gz | docker compose exec -T mongo mongorestore \
  -u root -p changeme --authenticationDatabase admin --archive --gzip --drop
```

数据实际存在命名卷 `paper-mongo-data`（路径：`docker volume inspect paper-mongo-data`），所以不需要管宿主机目录权限。

### 初始化时机（很重要）

`init/*.sh` 由 compose 里的自定义入口 `entrypoint.sh` 执行，**每次容器启动都会跑一遍**
（官方镜像自己的 `/docker-entrypoint-initdb.d` 机制只在数据卷为空时跑，这里保留作为首次初始化的兜底）。
因为脚本是**幂等的**（账号不存在就创建，已存在就把密码改成 `.env` 里的值），重复执行不会有副作用，所以：

- 改完 `.env` 里的账号密码 → `docker compose up -d`（或 `docker compose restart mongo`）即会自动生效，
  **不用**删数据卷；
- 自动重启（宿主重启、`restart: unless-stopped` 拉起）时也会自动执行一次。

看执行结果：

```bash
cd mongo_configuration
docker compose logs mongo | grep mongo-init
# [mongo-init] mongod 已就绪（等待 3s）
# [mongo-init] 执行 /opt/mongo-init/01-init-app-user.sh (第 1/5 次)
# [init] created user paper on db paper
# [mongo-init] 完成: /opt/mongo-init/01-init-app-user.sh
```

也可在容器里手动跑一次（或临时排查）：

```bash
docker exec paper-mongo bash /opt/mongo-init/01-init-app-user.sh
```

> 实现细节：`entrypoint.sh` 在后台等 mongod 就绪（带 root 认证）后再跑脚本，
> 主进程仍交给官方 entrypoint 启动 mongod。**脚本失败不会影响 mongod 启动**，
> 只会连续重试 `MONGO_INIT_MAX_TRIES`（默认 5）次后在日志里报错。
> 可用 `MONGO_INIT_DIR` / `MONGO_INIT_WAIT_SECONDS` / `MONGO_INIT_MAX_TRIES` 调整。
>
> 不想要这个行为：把 compose 里 mongo 的 `entrypoint:` 与 `/opt/mongo-init`、
> `/opt/mongo-entrypoint.sh` 两个挂载删掉，就回到官方镜像的“首次初始化一次”行为。
>
> 想彻底重来（丢数据）：`docker compose down -v && docker compose up -d`。

## 6. 图形界面（mongo-express）

启动后访问 <http://127.0.0.1:8081>，账号密码取自 `.env` 的 `MONGO_EXPRESS_USER/PASSWORD`，登录后可增删库表、执行查询。不需要就把 compose 里 `mongo-express` 整段删掉，或只 `docker compose up -d mongo`。
