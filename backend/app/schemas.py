"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    """动作回包：前三个字段是原有契约，其余字段只在调用方要求归属信息时出现。"""

    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    module: str | None = None
    action: str | None = None
    token: str | None = None
    origin_module: str | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    """动作请求：原有调用方只传 action 即可，其余字段都是可选的分派扩展。

    - ``phase``：``resolve`` 只解析并在途认领、``commit`` 凭 token 执行、留空则一步完成；
    - ``token``：解析阶段签发的在途认领凭据，执行前据此重新核对写入资格；
    - ``origin_module``：发起动作的页面归属，用于把回包接回原模块；
    - ``target_module``：调用方声明的目标模块，与路径不一致即视为越权；
    - ``return_meta``：要求回包带上模块/动作/凭据等归属信息。
    """

    action: str
    remark: str | None = None
    phase: str | None = None
    token: str | None = None
    origin_module: str | None = None
    target_module: str | None = None
    return_meta: bool = False
