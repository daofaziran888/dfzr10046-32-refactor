"""血缘影响传播引擎：重跑任务与血缘关系共用的「可受理边 → 影响层级」计算。

职责收拢在这一处，两个模块只决定要几层、要不要保留起点：
- 重跑任务：直接/间接两层，供排期时快照影响范围；
- 血缘关系：最多三层逐跳传播，输出明细；
- 边只有处于可受理状态（未被标记失效）时才参与传播：已被标记失效的边不再进入
  下一次排期可采用的影响范围，已经受理的排期由重跑任务自行留存快照。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.store import store

# 已失效的边不再参与任何影响传播；其余登记态都视为可受理。
INVALID_STATUS = "已失效"
ADMISSIBLE_STATUSES = frozenset({"已登记", "待核对", "已核对"})
DEFAULT_MAX_LEVELS = 3


@dataclass(frozen=True)
class ImpactEdge:
    """一条血缘边的传播视图，只保留计算影响范围需要的字段。"""

    edge_id: str
    upstream: str
    downstream: str
    status: str
    propagation: str = ""
    impact_level: str = ""

    @property
    def admissible(self) -> bool:
        return self.status in ADMISSIBLE_STATUSES

    def to_hop(self) -> dict[str, Any]:
        """血缘页返回的逐跳明细，保持原有字段。"""
        return {
            "upstream": self.upstream,
            "downstream": self.downstream,
            "propagation": self.propagation,
            "impact_level": self.impact_level,
        }


def _as_edge(row: dict[str, Any]) -> ImpactEdge:
    return ImpactEdge(
        edge_id=str(row.get("血缘编号", "")),
        upstream=str(row.get("上游对象", "")),
        downstream=str(row.get("下游对象", "")),
        status=str(row.get("status", "")),
        propagation=str(row.get("传递方式", "") or ""),
        impact_level=str(row.get("影响层级", "") or ""),
    )


def load_edges(
    rows: Iterable[dict[str, Any]] | None = None, *, accepted_only: bool = True,
) -> list[ImpactEdge]:
    """读取血缘边；``rows`` 缺省时从存储快照读取，默认只保留可受理的边。"""
    if rows is None:
        with store.lock:
            rows = list(store.rows("lineage_edge"))
    edges = [_as_edge(row) for row in rows]
    if accepted_only:
        edges = [edge for edge in edges if edge.admissible]
    return edges


def _mirror_aliases() -> dict[str, str]:
    """事实表名 → 归属模块 key 的别名。

    血缘边既可以用模块 key 登记，也可以直接用事实表名登记；重跑任务的
    「影响事实表」写的是事实表名，因此当按名字找不到任何直接边时，回落到
    装配该事实表的模块，使影响范围仍来自同一批可受理边。
    """
    aliases: dict[str, str] = {}
    with store.lock:
        rows = list(store.rows("fact_assemble"))
    for row in rows:
        table = str(row.get("所属事实表", "") or "")
        if table:
            aliases.setdefault(table, "fact_assemble")
    return aliases


def _starts(start: str, edges: list[ImpactEdge]) -> list[str]:
    """解析传播起点：优先用原值，完全没有直接边时再尝试事实表别名。"""
    if any(edge.upstream == start for edge in edges):
        return [start]
    alias = _mirror_aliases().get(start)
    if alias and any(edge.upstream == alias for edge in edges):
        return [start, alias]
    return [start]


def two_level_scope(
    edges: list[ImpactEdge], start: str,
) -> dict[str, list[str]]:
    """重跑任务原有语义的两层影响范围：直接全收，间接去重且不回收起点。

    直接层保留原始逐边结果（同一上游的多条边各自登记），
    间接层只保留未在直接层出现过的下游，键名与原有返回一致。
    """
    direct: list[str] = []
    for origin in _starts(start, edges):
        direct.extend(edge.downstream for edge in edges if edge.upstream == origin)
    seen = set(direct)
    indirect: list[str] = []
    for downstream in direct:
        for edge in edges:
            if edge.upstream == downstream and edge.downstream not in seen:
                indirect.append(edge.downstream)
                seen.add(edge.downstream)
    return {"direct_impact": direct, "indirect_impact": indirect}


def propagate_hops(
    edges: list[ImpactEdge], start: str, levels: int = DEFAULT_MAX_LEVELS,
) -> list[dict[str, Any]]:
    """血缘页原有语义的逐层传播：起点不会在回环中再次被收集。"""
    current = [start]
    seen = {start}
    result: list[dict[str, Any]] = []
    for _ in range(levels):
        next_level: list[str] = []
        for upstream in current:
            for edge in edges:
                if edge.upstream == upstream and edge.downstream not in seen:
                    seen.add(edge.downstream)
                    next_level.append(edge.downstream)
                    result.append(edge.to_hop())
        current = next_level
        if not current:
            break
    return result


def scope_for(start: str, *, accepted_only: bool = True) -> dict[str, list[str]]:
    """从存储读取边并计算重跑任务的两层影响范围。"""
    return two_level_scope(load_edges(accepted_only=accepted_only), start)


def hops_for(
    start: str, *, levels: int = DEFAULT_MAX_LEVELS, accepted_only: bool = True,
) -> list[dict[str, Any]]:
    """从存储读取边并计算血缘页的逐跳传播结果。"""
    return propagate_hops(load_edges(accepted_only=accepted_only), start, levels)
