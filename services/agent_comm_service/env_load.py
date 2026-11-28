"""agent_comm_service 配置（风格对齐 agents_redis_streams/env_load.py）。"""
from __future__ import annotations

import json
import os
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()

# ---------------- 服务 ----------------
HOST = os.getenv("HOST") or "0.0.0.0"
PORT = int(os.getenv("PORT") or 8090)

# ---------------- Redis ----------------
REDIS_URL = os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
STREAM_MAXLEN = int(os.getenv("STREAM_MAXLEN") or 10_000)
REPLY_MAXLEN = int(os.getenv("REPLY_MAXLEN") or 200)
DLQ_MAXLEN = int(os.getenv("DLQ_MAXLEN") or 5_000)

# ---------------- 流与消费组 ----------------
STREAM_IN_RAW = os.getenv("STREAM_IN_RAW") or "outside-in-raw"
STREAM_IN = os.getenv("STREAM_IN") or "outside-in-stream"
STREAM_OUT = os.getenv("STREAM_OUT") or "inside-out-stream"
STREAM_DLQ_IN = os.getenv("STREAM_DLQ_IN") or "agent:dlq:in"
STREAM_DLQ_OUT = os.getenv("STREAM_DLQ_OUT") or "agent:dlq:out"

GROUP_GUARD = os.getenv("GROUP_GUARD") or "guard-workers"
GROUP_READER = os.getenv("GROUP_READER") or "outside-in-workers"
GROUP_DISPATCH = os.getenv("GROUP_DISPATCH") or "inside-out-dispatchers"

# ---------------- TTL / 超时 ----------------
REQUEST_TTL = int(os.getenv("REQUEST_TTL") or 3600)   # agent:req:{rid}
REPLY_TTL = int(os.getenv("REPLY_TTL") or 3600)       # agent:reply:{rid}
WAITER_GRACE = int(os.getenv("WAITER_GRACE") or 60)   # 等待标记额外留存的秒数
DEFAULT_TIMEOUT_S = int(os.getenv("DEFAULT_TIMEOUT_S") or 180)
MAX_TIMEOUT_S = int(os.getenv("MAX_TIMEOUT_S") or 1800)

# ---------------- 完整结果（超出单条回包上限的大 JSON） ----------------
# 回包超过 MAX_INLINE_KB 时，完整内容存 Redis 字符串键 agent:result:{rid}，
# 回传消息里只带摘要 + result_ref；客户端用 GET /v1/requests/{rid}/result 取回。
RESULT_TTL = int(os.getenv("RESULT_TTL") or REPLY_TTL)          # 完整结果的存活时间
RESULT_MAX_MB = int(os.getenv("RESULT_MAX_MB") or 16)           # 单条完整结果上限
RESULT_COMPRESS_KB = int(os.getenv("RESULT_COMPRESS_KB") or 32)  # 超过就 gzip+base64
# 取回完整结果后是否**立即删除** agent:result:{rid}（默认否，靠 RESULT_TTL 自然过期）：
# 客户端仍可用 `GET /v1/requests/{rid}/result?purge=true` 单次覆盖。
RESULT_DELETE_ON_FETCH = (os.getenv("RESULT_DELETE_ON_FETCH") or "0").lower() in (
    "1", "true", "yes", "on")

# ---------------- 并发 ----------------
WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY") or 16)
READ_BATCH = int(os.getenv("READ_BATCH") or 16)
BLOCK_MS = int(os.getenv("BLOCK_MS") or 200)
RECLAIM_IDLE_MS = int(os.getenv("RECLAIM_IDLE_MS") or 60_000)
RECLAIM_INTERVAL_S = int(os.getenv("RECLAIM_INTERVAL_S") or 60)

# ---------------- 限额 ----------------
MAX_INLINE_KB = int(os.getenv("MAX_INLINE_KB") or 64)
MAX_PAYLOAD_DEPTH = int(os.getenv("MAX_PAYLOAD_DEPTH") or 8)
QUOTA_QPS = int(os.getenv("QUOTA_QPS") or 20)          # <=0 表示不限

# ---------------- 鉴权 ----------------
# AGENT_SECRETS='{"research-agent-v1":"s3cret"}'；为空则关闭鉴权（仅本地调试）
AGENT_SECRETS: dict[str, str] = json.loads(os.getenv("AGENT_SECRETS") or "{}")
REQUIRE_SIGNATURE = (os.getenv("REQUIRE_SIGNATURE") or "0").lower() in ("1", "true", "yes", "on")
TS_WINDOW = int(os.getenv("TS_WINDOW") or 300)

# ---------------- transfer_key / MongoDB ----------------
# 复用同级 mongo_configuration/ 的单机实例：业务账号 paper，对 paper 库只有 readWrite，
# 所以所有集合都放这个库里（注意与业务集合错开名字）。
# 连接串优先级：MONGO_URI > MONGO_URL > 用下面拆开的字段拼默认值。
# 容器内用服务名 mongo:27017（同 paper-mongo-net 网络）；宿主机本地跑用 127.0.0.1。
MONGO_DB = os.getenv("MONGO_DB") or os.getenv("MONGO_APP_DB") or "paper"
MONGO_AUTH_DB = os.getenv("MONGO_AUTH_DB") or MONGO_DB
KEY_COLLECTION = os.getenv("KEY_COLLECTION") or "agent_transfer_keys"

_MONGO_USER = os.getenv("MONGO_APP_USER") or os.getenv("MONGO_USER") or "paper"
_MONGO_PASSWORD = os.getenv("MONGO_APP_PASSWORD") or os.getenv("MONGO_PASSWORD") or "paper123"
_MONGO_HOST = os.getenv("MONGO_HOST") or "127.0.0.1"
_MONGO_PORT = int(os.getenv("MONGO_PORT") or 27017)

MONGO_URL = os.getenv("MONGO_URI") or os.getenv("MONGO_URL") or (
    f"mongodb://{quote(_MONGO_USER, safe='')}:{quote(_MONGO_PASSWORD, safe='')}"
    f"@{_MONGO_HOST}:{_MONGO_PORT}/{MONGO_DB}?authSource={MONGO_AUTH_DB}"
)

# 存储摘要用的 pepper：只做 HMAC 域分离，不可逆，也不存在"解密密钥"。
# ⚠️ 旧模型（tk:v2）遗留：新模型里 `POST /v1/transfer-keys` 默认关闭，
#    `ALLOW_ENV_TRANSFER_KEYS=0` 时信封里的 tk:v2 key 一律拒收。
KEY_HASH_PEPPER = os.getenv("KEY_HASH_PEPPER") or "dev-key-hash-pepper"

# 有效期：默认 1 天，上限 90 天
KEY_DEFAULT_TTL = int(os.getenv("KEY_DEFAULT_TTL") or 86_400)
KEY_MAX_TTL = int(os.getenv("KEY_MAX_TTL") or 90 * 86_400)

# ---------------- 账号 transfer_key（ptk_…）：网关侧唯一准入凭据 ----------------
# 账号 key 由 tardigrade 应用签发，落在同一个 Mongo 的 `transfer_keys`；本服务直读校验。
ACCOUNT_KEY_PREFIX = os.getenv("ACCOUNT_KEY_PREFIX") or "ptk_"
ACCOUNT_KEY_COLLECTION = os.getenv("ACCOUNT_KEY_COLLECTION") or "transfer_keys"
USER_COLLECTION = os.getenv("USER_COLLECTION") or "users"
GATEWAY_META_COLLECTION = os.getenv("GATEWAY_META_COLLECTION") or "agent_gateway_meta"

# 是否强制每个请求都必须带**账号** transfer key（默认 1 = 只能用它通信）。
# 置 0 只给本地调试：缺 key 时退回旧行为（owner 由 reader 自己推断）。
ACCOUNT_KEY_REQUIRED = (os.getenv("ACCOUNT_KEY_REQUIRED") or "1").lower() in (
    "1", "true", "yes", "on")

# 是否还接受/签发旧的 tk:v2（pepper 摘要）key。默认 0：这类 key 与账号无关，
# 等于"启动环境变量里的 pepper 就能换到访问权"，正是要关掉的路径。
ALLOW_ENV_TRANSFER_KEYS = (os.getenv("ALLOW_ENV_TRANSFER_KEYS") or "0").lower() in (
    "1", "true", "yes", "on")

# 网关盖章（auth stamp）：
#   * 密钥优先取 AUTH_STAMP_SECRET；为空则本服务在 Mongo `agent_gateway_meta` 里
#     自动 get-or-create 一次并长期复用（不依赖容器启动环境变量）。
#   * STAMP_WINDOW 是盖章的有效时间窗（秒），默认与请求存活时间一致。
AUTH_STAMP_SECRET = os.getenv("AUTH_STAMP_SECRET") or ""
STAMP_WINDOW = int(os.getenv("STAMP_WINDOW") or REQUEST_TTL)

# reader 回包是否必须带签名（`sig = HMAC(stamp_secret, reply|rid|kind|owner|sha256(data))`）。
# 默认 1：拦住“能写 Redis 就能伪造回包”这条路；兼容旧 reader 时可置 0（不推荐）。
REQUIRE_READER_SIG = (os.getenv("REQUIRE_READER_SIG") or "1").lower() in (
    "1", "true", "yes", "on")

# ---------------- 意图白名单 ----------------
ALLOWED_INTENTS: set[str] = {
    x.strip()
    for x in (
        os.getenv("ALLOWED_INTENTS")
        or "paper.insight.validate,paper.section.query,paper.note.export"
    ).split(",")
    if x.strip()
}
