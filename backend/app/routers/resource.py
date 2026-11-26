"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.services import impact
from app.services.context import ResourceContext
from app.services.dispatch import DispatchError, dispatcher

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.get("/{module}/{entry_id}/impact", response_model=dict)
def get_impact(
    module: str, entry_id: int,
    levels: int = Query(default=3, ge=1, le=10),
) -> dict[str, Any]:
    """影响范围预览：血缘页看逐跳明细，重跑页看可受理边给出的两层范围。

    只读接口，不写入任何记录；已失效的边不参与计算，因此「下一次排期可采用
    的范围」与「已受理排期冻结的范围」可以分别读取、互相区分。
    """
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")

    if module == "lineage_edge":
        start = str(entry.get("上游对象", ""))
        return {
            "module": module,
            "start": start,
            "impact": service.propagate_impact(start, levels=levels),
        }
    if module == "reprocess_task":
        start = str(entry.get("影响事实表", ""))
        current = impact.scope_for(start)
        accepted = entry.get("影响范围") or None
        return {
            "module": module,
            "start": start,
            "current": current,
            "accepted": accepted,
        }
    raise HTTPException(status_code=400, detail=f"模块 {module} 不提供影响范围预览")


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=None)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> dict[str, Any]:
    """动作入口：解析（认领）与执行（提交）分开，原有一步式调用不变。

    路径里的 ``module`` 是目标资源归属；``target_module`` 若声明且不一致即越权，
    解析阶段就会被拒绝，不会写入任何模块的记录。
    """
    service = require_service(module)

    context = ResourceContext.of(
        module, service.ENTITY, entry_id,
        origin_key=payload.origin_module, token=payload.token,
    )
    dispatcher_ = dispatcher()
    try:
        if payload.phase == "commit":
            plan = _claim_plan(dispatcher_, module, entry_id, payload.token)
            if payload.action.strip() != plan.action:
                raise DispatchError(
                    f"认领凭据属于动作「{plan.action}」，不能用于「{payload.action.strip()}」，"
                    f"已拒绝越权分派",
                    409,
                )
            return _action_result(dispatcher_.execute(plan), payload)
        plan = dispatcher_.resolve(
            context, payload.action, target_module=payload.target_module,
        )
        if payload.phase == "resolve":
            return {
                "ok": True,
                "message": f"动作「{plan.action}」已解析，等待执行",
                "entry": plan.service.get_entry(entry_id),
                "module": module,
                "action": plan.action,
                "token": plan.claim.token,
                "origin_module": payload.origin_module or module,
            }
        return _action_result(dispatcher_.execute(plan), payload)
    except DispatchError as error:
        return {"ok": False, "message": error.message, "entry": None}


def _claim_plan(dispatcher_, module: str, entry_id: int, token: str | None):
    """凭在途认领凭据取回解析好的计划，缺失或不属于该记录都视为过期资格。"""
    if not token:
        raise DispatchError("缺少在途认领凭据 token，无法提交已解析的动作", 409)
    plan = dispatcher_.plan_for_token(token)
    if plan is None or plan.module_key != module or plan.entry_id != entry_id:
        raise DispatchError("在途认领凭据已过期或不属于该记录", 409)
    return plan


def _action_result(result, payload: ActionPayload) -> dict[str, Any]:
    """原有契约只返回 ok/message/entry，扩展字段按需补上。"""
    response: dict[str, Any] = {
        "ok": result.ok,
        "message": result.message,
        "entry": result.entry,
    }
    if payload.return_meta or payload.origin_module or payload.phase:
        response["module"] = result.module
        response["action"] = result.action
        response["token"] = result.token
        response["origin_module"] = result.origin
    return response
