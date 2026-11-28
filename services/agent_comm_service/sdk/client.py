"""科研 agent 侧客户端：提交请求 -> 长轮询等待 -> 拿到完整结果字典。

依赖仅 httpx。用法：

    async with AgentCommClient("http://127.0.0.1:9500", "research-agent-v1", api_key="...") as c:
        rid = (await c.submit(intent="paper.insight.validate", payload={"claim": "..."}))["request_id"]
        result = await c.wait(rid, timeout_s=180)
        final = next(i for i in result["items"] if i["type"] == "final")
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any

import httpx

TERMINAL = {"done", "error", "cancelled", "quarantined", "rejected"}
# 回传项的 kind 比状态更可信：服务端先追加回传、后更新 status，
# 客户端可能在两步之间读到 items，必须凭回传项本身收敛，否则会挂到超时。
_TERMINAL_ITEM = {"final": "done", "error": "error", "cancelled": "cancelled"}


def _sign(secret: str, ts: str, method: str, path: str, nonce: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    slot = "\n".join([ts, method.upper(), path, nonce, body_hash])
    return hmac.new(secret.encode(), slot.encode(), hashlib.sha256).hexdigest()


class AgentCommError(RuntimeError):
    """服务端返回非 2xx。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class AgentCommClient:
    def __init__(
        self,
        base_url: str,
        agent_id: str,
        *,
        api_key: str | None = None,
        secret: str | None = None,
        timeout: float = 30.0,
        limits: httpx.Limits | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.api_key = api_key
        self.secret = secret
        # httpx 0.28 不接受 limits=None，缺省时让它用自己的默认值
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if limits is not None:
            client_kwargs["limits"] = limits
        self._client = httpx.AsyncClient(**client_kwargs)

    # ------------------------------------------------------------------ #
    # 底层
    # ------------------------------------------------------------------ #
    def _headers(self, method: str, path: str, body: bytes = b"") -> dict[str, str]:
        headers = {"X-Agent-Id": self.agent_id}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if self.secret:
            ts = str(int(time.time()))
            nonce = uuid.uuid4().hex
            headers.update(
                {
                    "X-Timestamp": ts,
                    "X-Nonce": nonce,
                    "X-Signature": _sign(self.secret, ts, method, path, nonce, body),
                }
            )
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        body = kwargs.pop("content", b"") or b""
        extra_headers = kwargs.pop("headers", None) or {}
        headers = {**self._headers(method, path, body), **extra_headers}
        if body:                         # 之前只把 body 用于签名，没真正发出去
            kwargs["content"] = body
        resp = await self._client.request(
            method, f"{self.base_url}{path}", headers=headers, **kwargs
        )
        if resp.status_code >= 400:
            raise AgentCommError(resp.status_code, resp.text)
        return resp

    # ------------------------------------------------------------------ #
    # 业务
    # ------------------------------------------------------------------ #
    async def submit(
        self,
        intent: str,
        *,
        payload: dict[str, Any] | None = None,
        transfer_key: str | None = None,
        from_agent: str | None = None,
        wait: bool = True,
        timeout_s: int = 180,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """提交请求（幂等）。

        `transfer_key` 必须是**账号**的 transfer key（`ptk_…`，在 tardigrade 的
        「🔑 转交 Key」里签发）。网关会在入口完成校验，并把归属账号盖章到信封上；
        明文 key 不会随消息进 Redis。旧的 `tk:v2`（`issue_transfer_key()` 签的那种）
        默认已拒收。
        """
        body = {
            "from_agent": from_agent or self.agent_id,
            "intent": intent,
            "payload": payload or {},
            "transfer_key": transfer_key,
            "wait": wait,
            "timeout_s": timeout_s,
            "request_id": request_id,
        }
        raw = httpx.Request("POST", self.base_url, json=body).content
        resp = await self._request("POST", "/v1/requests", content=raw, headers={"Content-Type": "application/json"})
        return resp.json()

    async def poll(self, request_id: str, *, from_id: str = "0-0") -> dict[str, Any]:
        resp = await self._request("GET", f"/v1/requests/{request_id}", params={"from": from_id})
        return resp.json()

    async def fetch_result(self, request_id: str, *, purge: bool = False) -> Any:
        """取回完整结果（reader 回包超 64KB 时，回传消息里只有摘要 + `result_url`）。

        结果存于通信服务 Redis（`agent:result:{rid}`），默认 1 小时后过期；
        `purge=True` 时**读完即删**（只靠 TTL 回收容易把几 MB 的大结果留一小时）；
        结果不存在时抛 `AgentCommError(404, ...)`。
        """
        params = {"purge": "true"} if purge else None
        resp = await self._request(
            "GET", f"/v1/requests/{request_id}/result", params=params
        )
        body = resp.json()
        return body.get("result")

    async def fetch_result_meta(self, request_id: str) -> dict[str, Any]:
        """只看完整结果的元信息（大小 / 摘要 / 编码 / TTL），不下载正文。"""
        resp = await self._request(
            "GET", f"/v1/requests/{request_id}/result", params={"meta": "true"}
        )
        return resp.json()

    async def wait(
        self,
        request_id: str,
        *,
        timeout_s: float = 180,
        window: int = 25,
    ) -> dict[str, Any]:
        """循环长轮询直到终态或超时；返回 {"status", "items", "request_id"}。"""
        deadline = time.time() + timeout_s
        cursor = "0-0"
        collected: list[dict[str, Any]] = []
        status = "queued"

        while time.time() < deadline:
            remain = max(1, min(window, int(deadline - time.time())))
            resp = await self._request(
                "GET",
                f"/v1/requests/{request_id}/wait",
                params={"from": cursor, "timeout": remain},
            )
            if resp.status_code == 204:
                continue
            body = resp.json()
            status = body.get("status", status)
            items = body.get("items") or []
            if items:
                cursor = body.get("next", cursor)
                collected.extend(items)
                for item in items:
                    mapped = _TERMINAL_ITEM.get(str(item.get("type")))
                    if mapped:
                        status = mapped
                        break
            if status in TERMINAL:
                break

        return {"request_id": request_id, "status": status, "items": collected}

    async def ask(
        self,
        intent: str,
        *,
        payload: dict[str, Any] | None = None,
        transfer_key: str | None = None,
        timeout_s: float = 180,
        hydrate: bool = True,
        purge_result: bool = False,
    ) -> dict[str, Any]:
        """提交 + 等待，一步到位；返回最终结果（没有 final 时返回原始 reply）。

        `hydrate=True`（默认）时，若终态消息是「摘要 + 引用」（`result_url`），会
        自动 `fetch_result()` 取回完整结果：返回项的 `data` 是完整内容，
        原始摘要放在 `slim` 里；`hydrate=False` 则原样返回摘要。

        `purge_result=True`：取回完整结果后**立即删除** `agent:result:{rid}`
        （适合「拿到就算完」的一次性调用；默认 false，仍靠 `RESULT_TTL` 过期）。
        """
        submitted = await self.submit(intent, payload=payload, transfer_key=transfer_key, timeout_s=int(timeout_s))
        result = await self.wait(submitted["request_id"], timeout_s=timeout_s)
        for item in reversed(result["items"]):
            if item["type"] in ("final", "error", "cancelled"):
                data = item.get("data")
                has_ref = isinstance(data, dict) and (
                    data.get("result_url") or data.get("result_ref")
                )
                if hydrate and has_ref:
                    try:
                        full = await self.fetch_result(
                            submitted["request_id"], purge=purge_result
                        )
                    except AgentCommError:
                        return item                       # 已过期 / 无权限：退回摘要
                    if full is not None:
                        return {**item, "data": full, "slim": data}
                return item
        return {"type": "timeout", "data": result}

    async def cancel(self, request_id: str) -> dict[str, Any]:
        resp = await self._request("DELETE", f"/v1/requests/{request_id}")
        return resp.json()

    async def issue_transfer_key(
        self, blob_id: str | None = None, ttl: int | None = None, *, label: str = ""
    ) -> str:
        """签发旧的 `tk:v2` key（pepper 摘要，与账号无关）。

        ⚠️ 新模型下默认关闭（服务端 `ALLOW_ENV_TRANSFER_KEYS=0` 时直接 403）：
        访问权只来自账号 transfer key（`ptk_…`），不再由服务按启动环境变量派生。
        """
        body = {"blob_id": blob_id, "ttl": ttl, "label": label}
        raw = httpx.Request("POST", self.base_url, json=body).content
        resp = await self._request(
            "POST", "/v1/transfer-keys", content=raw, headers={"Content-Type": "application/json"}
        )
        return resp.json()["transfer_key"]

    async def list_transfer_keys(self) -> list[dict[str, Any]]:
        """列出自己名下的 key 元数据（不含明文 / 摘要）。"""
        resp = await self._request("GET", "/v1/transfer-keys")
        return resp.json()["items"]

    async def revoke_transfer_key(self, key_id: str, *, purge: bool = False) -> dict[str, Any]:
        """吊销（默认，保留审计）或物理删除自己的一把 key。"""
        resp = await self._request(
            "DELETE", f"/v1/transfer-keys/{key_id}", params={"purge": str(purge).lower()}
        )
        return resp.json()

    # ------------------------------------------------------------------ #
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AgentCommClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
