"""在途动作分派：把「谁发起的、属于哪个模块、哪个动作、哪个记录」解析成可执行计划。

工作台页面的在途请求认领与业务动作执行在这里分开：
- ``resolve`` 只做解析：确认模块存在、动作属于该模块、记录存在，并生成认领令牌，
  此时尚未写入任何模块记录，可供并发请求介入；
- ``execute`` 才是真正的写入阶段，并把结果按发起时的模块/动作归属接回。

解析阶段由目标资源给出写入资格（``WriteClaim``），执行阶段重新核对资格没被别的
请求改写；同号记录分属不同模块时，越权（模块与动作不匹配）的请求在执行前就被拒绝，
两个模块的归属互不影响。
"""
from __future__ import annotations

import copy
import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any

from app.services import service_for
from app.services.base import BaseModuleService
from app.services.context import ResourceContext
from app.store import store


class DispatchError(Exception):
    """动作分派失败：保留可读原因与状态码，路由层只负责转成 HTTP 响应。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class WriteClaim:
    """写入资格：解析动作时由目标资源签发，执行前再核验一次。"""

    module: str
    entry_id: int
    fingerprint: str
    token: str


@dataclass(frozen=True)
class ActionPlan:
    """已解析、未执行的动作计划。"""

    context: ResourceContext
    service: BaseModuleService
    action: str
    claim: WriteClaim
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def module_key(self) -> str:
        return self.context.module.key

    @property
    def entry_id(self) -> int:
        return self.context.entry_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module_key,
            "entity": self.service.ENTITY,
            "entry_id": self.entry_id,
            "action": self.action,
            "token": self.claim.token,
            "origin": self.context.origin.key if self.context.origin else self.module_key,
        }


@dataclass(frozen=True)
class DispatchResult:
    """执行结果：按发起时的模块与动作归属接回。"""

    module: str
    action: str
    token: str
    entry: dict[str, Any] | None
    message: str
    ok: bool
    origin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "entry": self.entry,
            "module": self.module,
            "action": self.action,
            "token": self.token,
            "origin": self.origin,
        }


def _claim_token(module: str, entry_id: int, action: str, fingerprint: str) -> str:
    """认领凭据绑定「模块 + 记录 + 动作 + 记录快照」，不同动作不会互相顶掉。"""
    digest = hashlib.sha1(
        f"{module}:{entry_id}:{action}:{fingerprint}".encode("utf-8")
    ).hexdigest()
    return f"{module}:{entry_id}:{digest[:12]}"


class ActionDispatcher:
    """动作分派器：解析与执行分离，解析出的计划可被并发请求观察/改写。"""

    def __init__(self) -> None:
        self._inflight: dict[str, ActionPlan] = {}
        self._lock = threading.RLock()

    # ---- 解析阶段 -------------------------------------------------------
    def resolve(
        self, context: ResourceContext, action: str,
        *, target_module: str | None = None,
    ) -> ActionPlan:
        """解析动作：不写入任何记录，只确认归属并签发写入资格。"""
        service = service_for(context.module.key)
        if service is None:
            raise DispatchError(f"未知业务模块：{context.module.key}", 404)

        action = action.strip()
        target = service.ACTION_RULES.get(action)
        if target is None:
            raise DispatchError(
                f"动作「{action}」不属于{context.module.key}可执行范围", 409,
            )
        if target_module and target_module != context.module.key:
            raise DispatchError(
                f"动作目标 {target_module} 与发起模块 {context.module.key} 不一致，"
                f"已拒绝越权分派",
                409,
            )
        origin = context.origin.key if context.origin else context.module.key
        if origin != context.module.key:
            raise DispatchError(
                f"发起方 {origin} 不能把动作写入 {context.module.key}，已拒绝越权分派",
                409,
            )

        with store.lock:
            fingerprinted, rejected = service.issue_write_claim(
                context.entry_id, action)
            if rejected or fingerprinted is None:
                raise DispatchError(rejected or "无法签发写入资格", 404)
            entry = store.find(context.module.key, context.entry_id)
            claim = WriteClaim(
                module=context.module.key,
                entry_id=context.entry_id,
                fingerprint=fingerprinted,
                token=_claim_token(
                    context.module.key, context.entry_id, action, fingerprinted),
            )
            snapshot = copy.deepcopy(entry)

        plan = ActionPlan(
            context=context,
            service=service,
            action=action,
            claim=claim,
            payload={"expected": target, "entry": snapshot},
        )
        with self._lock:
            self._inflight[claim.token] = plan
        return plan

    # ---- 执行阶段 -------------------------------------------------------
    def execute(self, plan: ActionPlan) -> DispatchResult:
        """执行已解析的计划：重新向目标资源核对写入资格后写入。

        这里是「真正执行时重新检查资格」：解析阶段签发的资格若已过期
        （记录被并发请求改写、或不再满足动作准入），在执行前就被拒绝。
        """
        claim = plan.claim
        if plan.module_key != claim.module:
            raise DispatchError("动作计划与写入资格不属于同一模块，已拒绝", 409)
        with store.lock:
            fingerprinted, rejected = plan.service.issue_write_claim(
                claim.entry_id, plan.action)
            if rejected or fingerprinted is None:
                raise DispatchError(rejected or "无法核对写入资格", 404)
            if fingerprinted != claim.fingerprint:
                raise DispatchError(
                    f"{plan.service.ENTITY} {claim.entry_id} 在动作解析后已被其他请求修改，"
                    f"写入资格已过期",
                    409,
                )
            if plan.service.MODULE != claim.module:
                raise DispatchError("动作写入了非目标模块，已拒绝", 409)
            extra = {"claim": claim, "origin": plan.context.origin}
            updated, message = plan.service.run_action_for(
                claim.entry_id, plan.action, extra,
            )
            result = copy.deepcopy(updated)

        with self._lock:
            self._inflight.pop(claim.token, None)

        return DispatchResult(
            module=claim.module,
            action=plan.action,
            token=claim.token,
            entry=result,
            message=message,
            ok=result is not None,
            origin=plan.context.origin.key if plan.context.origin else claim.module,
        )

    # ---- 一步完成（兼容原接口） -----------------------------------------
    def dispatch(self, context: ResourceContext, action: str) -> DispatchResult:
        return self.execute(self.resolve(context, action))

    # ---- 在途请求观察（供并发用例断言） --------------------------------
    def inflight(self) -> list[dict[str, Any]]:
        with self._lock:
            return [plan.to_dict() for plan in self._inflight.values()]

    def inflight_for(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [
                plan.to_dict() for plan in self._inflight.values()
                if plan.module_key == module and plan.entry_id == entry_id
            ]

    def claim_for(self, module: str, entry_id: int) -> WriteClaim | None:
        with self._lock:
            for plan in self._inflight.values():
                if plan.module_key == module and plan.entry_id == entry_id:
                    return plan.claim
        return None

    def plan_for_token(self, token: str) -> ActionPlan | None:
        with self._lock:
            return self._inflight.get(token)

    def discard(self, token: str) -> bool:
        """放弃一个在途计划（不写入），返回是否确实存在。"""
        with self._lock:
            return self._inflight.pop(token, None) is not None

    def discard_all(self) -> int:
        """清空全部在途计划（测试或本地重置用），返回清掉的条数。"""
        with self._lock:
            count = len(self._inflight)
            self._inflight.clear()
            return count


_dispatcher: ActionDispatcher | None = None
_dispatcher_lock = threading.Lock()


def dispatcher() -> ActionDispatcher:
    """进程级分派器：路由与测试共用同一份在途计划表。"""
    global _dispatcher
    if _dispatcher is None:
        with _dispatcher_lock:
            if _dispatcher is None:
                _dispatcher = ActionDispatcher()
    return _dispatcher
