"""请求 / 回传的数据契约。"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from env_load import DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S


class RequestStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


TERMINAL_STATUSES = {
    RequestStatus.DONE,
    RequestStatus.ERROR,
    RequestStatus.CANCELLED,
    RequestStatus.QUARANTINED,
    RequestStatus.REJECTED,
}


class AuthStamp(BaseModel):
    """网关盖章：账号校验结果 + 防伪 MAC（客户端不得填写，服务端覆盖）。"""

    model_config = ConfigDict(extra="forbid")

    via: str = Field(default="account_key", max_length=32, description="校验途径：account_key / env_key")
    owner: str = Field(default="", max_length=64, description="校验得出的归属账号（权威值）")
    key_id: str = Field(default="", max_length=64, description="用掉的账号 key 的 id（审计用，不含明文）")
    from_agent: str = Field(default="", max_length=64)
    stamped_at: int = 0
    mac: str = Field(default="", max_length=128)


class RequestEnvelope(BaseModel):
    """外部 agent -> 服务 的请求信封；extra=forbid 禁止夹带字段。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, max_length=64, description="客户端幂等键，缺省由服务生成")
    from_agent: str = Field(min_length=1, max_length=64)
    intent: str = Field(min_length=1, max_length=64)
    transfer_key: str | None = Field(
        default=None,
        max_length=512,
        description="账号 transfer key（ptk_…，由应用的「转交 Key」签发）；"
                    "校验通过后服务端会抹掉明文，改在 auth 里盖 key_id",
    )
    owner: str | None = Field(
        default=None,
        max_length=64,
        description="网关盖章字段（归属账号）。客户端**不得**填写，填了直接 422",
    )
    auth: AuthStamp | None = Field(
        default=None,
        description="网关盖章（MAC + 校验途径）。客户端**不得**填写，填了直接 422",
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    wait: bool = True
    timeout_s: int = Field(default=DEFAULT_TIMEOUT_S, ge=1, le=MAX_TIMEOUT_S)


class ReplyItem(BaseModel):
    id: str
    type: str
    data: Any = None


class SubmitResponse(BaseModel):
    request_id: str
    status: RequestStatus
    duplicate: bool = False
    deadline: int
    poll_url: str
    wait_url: str


# 回传流里允许的 kind -> 请求状态
KIND_TO_STATUS: dict[str, RequestStatus] = {
    "ack": RequestStatus.RUNNING,
    "progress": RequestStatus.RUNNING,
    "final": RequestStatus.DONE,
    "error": RequestStatus.ERROR,
    "cancelled": RequestStatus.CANCELLED,
}
