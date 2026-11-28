"""启动 agent_comm_service（API + 深检消费者 + 回传分发消费者）。"""
from __future__ import annotations

import uvicorn

from env_load import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")
