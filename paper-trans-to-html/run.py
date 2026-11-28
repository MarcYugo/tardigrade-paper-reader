"""项目启动入口。

用法:
    python run.py                 # 启动服务 http://127.0.0.1:8000
    python run.py --port 9000
    python run.py --host 0.0.0.0  # 监听所有网卡（**容器里必须这样**，否则端口映射不到）
    python run.py --reload        # 开发模式，代码变更自动重启

环境变量：`APP_HOST` / `APP_PORT`（命令行参数优先），方便 docker/ 里直接调。
"""
import os
import sys

import uvicorn


def main() -> None:
    args = sys.argv[1:]
    reload = "--reload" in args

    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8000"))
    if "--host" in args:
        host = args[args.index("--host") + 1]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    uvicorn.run("backend.server.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
