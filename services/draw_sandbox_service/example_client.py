#!/usr/bin/env python3
"""本地联调示例：把一段 matplotlib 代码送进沙盒，把产出图片存到本地 ./out。

用法：
    python example_client.py                     # 跑内置示例
    python example_client.py --code-file my.py   # 跑自己的代码
    python example_client.py --key 你的KEY       # 服务端设了 SANDBOX_API_KEY 时
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import urllib.error
import urllib.request

SAMPLE_CODE = r'''
import numpy as np
import matplotlib.pyplot as plt

x = np.linspace(-3, 3, 400)

fig, ax = plt.subplots(figsize=(5.6, 3.6), dpi=150)
ax.plot(x, np.sin(x), label=r"$\sin(x)$")
ax.plot(x, np.exp(-x ** 2), label=r"$e^{-x^2}$")
ax.set_title("沙盒绘图示例 / sandbox demo")
ax.set_xlabel("x")
ax.set_ylabel("y")
ax.grid(alpha=0.3)
ax.legend()
plt.tight_layout()
'''


def main() -> int:
    parser = argparse.ArgumentParser(description="draw-sandbox 示例客户端")
    parser.add_argument("--url", default="http://127.0.0.1:8090", help="服务地址")
    parser.add_argument("--key", default="", help="X-API-Key（服务端未设则留空）")
    parser.add_argument("--code-file", default="", help="要执行的 .py 文件")
    parser.add_argument("--timeout", type=float, default=30.0, help="执行超时（秒）")
    parser.add_argument("--out", default="out", help="产出保存目录")
    args = parser.parse_args()

    if args.code_file:
        code = pathlib.Path(args.code_file).read_text(encoding="utf-8")
    else:
        code = SAMPLE_CODE

    body = json.dumps({"code": code, "timeout": args.timeout}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if args.key:
        headers["X-API-Key"] = args.key

    request = urllib.request.Request(
        args.url.rstrip("/") + "/v1/run", data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=args.timeout + 60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}")
        return 1
    except urllib.error.URLError as exc:
        print(f"连不上沙盒服务：{exc}")
        return 1

    summary = {k: v for k, v in payload.items() if k != "outputs"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for item in payload.get("outputs", []):
        # 产出名是相对路径：只取文件名落盘，避免写出 .. 到本地
        target = out_dir / pathlib.PurePosixPath(item["name"]).name
        target.write_bytes(base64.b64decode(item["data"]))
        print(f"已保存 {target}（{item['size']} 字节，{item['mime']}）")

    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
