#!/bin/bash
# ============================================================
#  mongo 容器自定义入口 —— 让 init/ 下的脚本在【每次启动】都自动执行
#
#  为什么需要它:
#    官方 mongo 镜像只在【数据目录为空】(首次初始化)时执行
#    /docker-entrypoint-initdb.d/*.sh；数据卷已存在时重启不会执行。
#    本入口在官方 entrypoint 之外补上"每次启动都跑一遍"的能力。
#
#  工作方式:
#    1) 后台等 mongod 就绪(带 root 账号认证, 首次启动时官方会先起临时实例再起正式实例)
#    2) 依次执行 /opt/mongo-init/*.sh（脚本是幂等的: 账号不存在就建, 已存在就改密码）
#    3) 主进程仍然交给官方 entrypoint 启动 mongod(前台常驻)
#
#  安全边界:
#    - 初始化脚本失败【不会】影响 mongod 启动(失败只在日志里报错);
#    - 通过 MONGO_INIT_DIR / MONGO_INIT_WAIT_SECONDS / MONGO_INIT_MAX_TRIES 可调。
#
#  查看执行情况:
#    docker compose logs mongo | grep mongo-init
# ============================================================
set -uo pipefail

INIT_DIR="${MONGO_INIT_DIR:-/opt/mongo-init}"
WAIT_SECONDS="${MONGO_INIT_WAIT_SECONDS:-120}"
MAX_TRIES="${MONGO_INIT_MAX_TRIES:-5}"
MONGO_HOST="${MONGO_INIT_HOST:-127.0.0.1}"

log() { echo "[mongo-init] $*"; }

run_init_scripts() {
  local auth=()
  [ -n "${MONGO_INITDB_ROOT_USERNAME:-}" ] && auth+=(--username "$MONGO_INITDB_ROOT_USERNAME")
  [ -n "${MONGO_INITDB_ROOT_PASSWORD:-}" ] && auth+=(--password "$MONGO_INITDB_ROOT_PASSWORD")
  [ "${#auth[@]}" -gt 0 ] && auth+=(--authenticationDatabase admin)

  # ---- 1) 等 mongod 就绪 ----
  local i
  for ((i = 1; i <= WAIT_SECONDS; i++)); do
    if mongosh --quiet --host "$MONGO_HOST" "${auth[@]}" \
         --eval 'db.adminCommand({ ping: 1 }).ok' >/dev/null 2>&1; then
      log "mongod 已就绪（等待 ${i}s）"
      break
    fi
    if [ "$i" -eq "$WAIT_SECONDS" ]; then
      log "警告: 等待 mongod 就绪超时(${WAIT_SECONDS}s), 本次跳过初始化脚本"
      return 1
    fi
    sleep 1
  done

  # ---- 2) 依次执行初始化脚本 ----
  shopt -s nullglob
  local scripts=("$INIT_DIR"/*.sh)
  shopt -u nullglob
  if [ "${#scripts[@]}" -eq 0 ]; then
    log "提示: $INIT_DIR 下没有 *.sh, 跳过"
    return 0
  fi

  local f n ok
  for f in "${scripts[@]}"; do
    ok=0
    for ((n = 1; n <= MAX_TRIES; n++)); do
      log "执行 $f (第 $n/$MAX_TRIES 次)"
      if bash "$f"; then
        ok=1
        break
      fi
      sleep 2
    done
    if [ "$ok" -eq 1 ]; then
      log "完成: $f"
    else
      log "错误: $f 连续 ${MAX_TRIES} 次失败, 已放弃(不影响 mongod 启动, 请检查日志上方报错)"
    fi
  done
  return 0
}

# 后台执行: 只负责初始化, 不阻塞也不影响容器主进程
run_init_scripts &

# 主进程 = 官方 entrypoint（它会 exec mongod 常驻前台）
exec /usr/local/bin/docker-entrypoint.sh "$@"
