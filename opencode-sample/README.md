# opencode 技能包

本目录存 opencode 可用的 skill，以及 opencode 的安装脚本。

```text
opencode/
├── opencode_install.sh                # 安装 opencode（binary / npm / official）
└── skills/
    └── tardigrade-paper-reader-comm/   # 与论文阅读器（agent_comm_service）通信
        ├── SKILL.md
        ├── scripts/paper_comm.py      # 零依赖 CLI（仅标准库）
        └── references/export-schema.md
```

## 让 opencode 发现技能

opencode 会自动扫描以下位置（技能 ID 由路径决定，`skills/tardigrade-paper-reader-comm/SKILL.md` → `tardigrade-paper-reader-comm`）：

| 作用域 | 路径 |
|---|---|
| 全局 | `~/.config/opencode/skills/<id>/SKILL.md` |
| 项目 | `<项目>/.opencode/skills/<id>/SKILL.md` |

任选一种注册方式（opencode 可能不跟随符号链接，**推荐直接复制**）：

```bash
# 1) 全局（所有项目都能用）——已执行过，修改后重新同步一次即可
rsync -a --delete /root/workspace/opencode/skills/tardigrade-paper-reader-comm/ \
      ~/.config/opencode/skills/tardigrade-paper-reader-comm/

# 2) 项目内（只在某个仓库生效）
mkdir -p /path/to/project/.opencode/skills
cp -r /root/workspace/opencode/skills/tardigrade-paper-reader-comm \
      /path/to/project/.opencode/skills/
```

或在 `opencode.jsonc` 里把它当成额外的技能源目录（这样就不用复制）：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "skills": ["/root/workspace/opencode"]
}
```

之后在 opencode 里说“用论文阅读器查一下……”即可自动命中，也可显式 `@tardigrade-paper-reader-comm`。
验证：

```bash
~/.opencode/bin/opencode --version
python ~/.config/opencode/skills/tardigrade-paper-reader-comm/scripts/paper_comm.py status
```

## tardigrade-paper-reader-comm 依赖

- `paper_comm.py`：只用 Python 标准库，无需 `pip install`。
- 通信服务需在跑：`http://127.0.0.1:8090`（本地）或 `:9500`（docker compose）。
- **凭据自动从 `scripts/.env` 读取**（也可 `--env-file` 指定或传参覆盖），键名：
  `PAPER_COMM_AGENT_ID` / `PAPER_COMM_API_KEY` / `PAPER_COMM_SECRET` / `PAPER_COMM_URL`，
  兼容服务端的 `AGENT_SECRETS` + `REQUIRE_SIGNATURE` 写法。优先级：
  命令行参数 > 进程环境变量 > `.env`。
- 本地开发默认凭据（来自 `agent_comm_service/.env.example`）：
  `PAPER_COMM_AGENT_ID=research-agent-v1`、`PAPER_COMM_API_KEY=change-me`，生产务必替换。
