#!/bin/bash
# 仅在「数据目录为空、首次初始化」时由官方镜像自动执行（见 README 初始化时机）。
# 作用：在业务库里创建一个普通读写账号，应用用它连接，不用 root 超管账号。
#
# 本脚本是幂等的：账号不存在就创建，已存在就把密码改成 .env 里的值。
# 因此数据卷已经初始化过、想补跑一次时可以直接在容器里执行：
#     docker exec paper-mongo bash /docker-entrypoint-initdb.d/01-init-app-user.sh
set -e

mongosh --quiet \
  --host 127.0.0.1 \
  --username "$MONGO_INITDB_ROOT_USERNAME" \
  --password "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin <<EOF
db = db.getSiblingDB("${MONGO_APP_DB}");
if (db.getUser("${MONGO_APP_USER}")) {
  db.updateUser("${MONGO_APP_USER}", { pwd: "${MONGO_APP_PASSWORD}" });
  print("[init] 账号已存在, 已更新密码: ${MONGO_APP_USER} @ ${MONGO_APP_DB}");
} else {
  db.createUser({
    user: "${MONGO_APP_USER}",
    pwd: "${MONGO_APP_PASSWORD}",
    roles: [{ role: "readWrite", db: "${MONGO_APP_DB}" }]
  });
  print("[init] created user ${MONGO_APP_USER} on db ${MONGO_APP_DB}");
}
EOF
