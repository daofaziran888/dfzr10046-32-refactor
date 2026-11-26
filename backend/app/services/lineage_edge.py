"""血缘关系领域服务：维护维度事实汇总层血缘边数据集，校验依赖传递和影响层级。"""
from __future__ import annotations

from typing import Any

from app.services import impact
from app.services.base import BaseModuleService, fingerprint
from app.store import store


class LineageEdgeService(BaseModuleService):
    MODULE = "lineage_edge"
    ENTITY = "血缘边"
    LIST_FIELDS = ["血缘编号", "上游对象", "下游对象", "传递方式", "重算触发", "影响层级", "血缘版本", "最近更新"]
    REQUIRED_FIELDS = ["血缘编号", "上游对象", "下游对象"]
    STATUS_ORDER = ["已登记", "待核对", "已核对", "已失效"]
    ACTION_RULES = {"提交核对": "待核对", "确认核对": "已核对", "标记失效": "已失效"}
    NEGATIVE_ACTIONS = []

    # 已失效的血缘边不再接受任何推进/复核动作，避免把失效边重新拉回影响范围。
    INVALID_ACTIONS = frozenset({"提交核对", "确认核对"})

    def issue_write_claim(
        self, entry_id: int, action: str,
    ) -> tuple[str | None, str | None]:
        """目标资源侧的写入资格：已失效的边拒绝重新核对。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        if entry.get("status") == impact.INVALID_STATUS and action in self.INVALID_ACTIONS:
            return None, f"血缘边 {entry_id} 已失效，不能再{action}"
        return fingerprint(entry), None

    def register_edge(
        self, edge_id: str, upstream: str, downstream: str,
        propagation: str, recalc_trigger: str, impact_level: int,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """登记血缘边：上游对象→下游对象的传递方式与重算触发。"""
        rows = store.rows(self.MODULE)
        entry = {
            "血缘编号": edge_id, "上游对象": upstream,
            "下游对象": downstream, "传递方式": propagation,
            "重算触发": recalc_trigger, "影响层级": impact_level,
            "血缘版本": "v1", "最近更新": "2026-11-21",
            "status": "已登记", "pending": False, "abnormal": False,
        }
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)
        return True, f"血缘边 {upstream}→{downstream} 已登记", entry

    def propagate_impact(
        self, upstream_id: str, levels: int = impact.DEFAULT_MAX_LEVELS,
    ) -> list[dict[str, Any]]:
        """沿血缘边向下传递重跑影响范围，多层传播；已失效的边不参与传播。"""
        return impact.hops_for(upstream_id, levels=levels)

    def admissible_scope(self, upstream_id: str) -> dict[str, list[str]]:
        """当前可受理边给出的两层影响范围，供重跑页排期前预览。"""
        return impact.scope_for(upstream_id)
