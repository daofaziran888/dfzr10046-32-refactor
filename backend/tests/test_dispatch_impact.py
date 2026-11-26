"""跨模块动作分派与血缘影响传播测试。

覆盖：
- 工作台在途认领与服务端动作分派分离（解析不写入、执行才写入）；
- 重跑任务/血缘关系共用影响范围计算，且只用可受理边；
- 同号记录分属两页时动作互不串号，越权分派被拒绝且不写入任何记录；
- 已受理排期与下一次排期可采用范围的区分；
- 原有动作/未知动作/查无记录的返回结论保持不变。
"""
from __future__ import annotations

import pytest

from app.schemas import ActionPayload
from app.services import impact, service_for
from app.services.context import ResourceContext
from app.services.dispatch import (
    ActionDispatcher, DispatchError, WriteClaim, dispatcher,
)
from app.store import store


# --------------------------------------------------------------------------
# 影响范围：两页共用同一套边状态语义
# --------------------------------------------------------------------------
class TestSharedImpact:
    def test_reprocess_two_level_scope_matches_existing_shape(self):
        scope = impact.scope_for("fact_assemble")
        assert scope == {
            "direct_impact": ["summary_layer", "quality_gate"],
            "indirect_impact": ["metric_bind"],
        }

    def test_lineage_hops_match_existing_shape(self):
        hops = impact.hops_for("fact_assemble")
        assert [(h["upstream"], h["downstream"]) for h in hops] == [
            ("fact_assemble", "summary_layer"),
            ("fact_assemble", "quality_gate"),
            ("summary_layer", "metric_bind"),
        ]
        assert set(hops[0]) == {"upstream", "downstream", "propagation", "impact_level"}

    def test_reprocess_fact_table_resolves_to_its_owning_edges(self):
        """重跑页写的是事实表名，影响范围仍来自血缘页那条可受理的边。"""
        assert impact.scope_for("销售事实表") == impact.scope_for("fact_assemble")

    def test_invalidated_edge_drops_out_of_next_scope(self):
        before = impact.scope_for("fact_assemble")
        assert "summary_layer" in before["direct_impact"]

        edge = service_for("lineage_edge")
        entry, message = edge.run_action(4, "标记失效")
        assert entry is not None
        assert entry["status"] == "已失效"

        after = impact.scope_for("fact_assemble")
        assert after["direct_impact"] == ["quality_gate"]
        assert after["indirect_impact"] == []
        # 血缘页逐跳传播同样只剩仍可受理的边。
        hops = impact.hops_for("fact_assemble")
        assert all(h["downstream"] != "summary_layer" for h in hops)

    def test_verified_edge_status_gates_scope(self):
        """在血缘页核对一条下游边后，它的状态决定重跑页可采用的边。"""
        edge = service_for("lineage_edge")
        pending = store.rows("lineage_edge")[5]
        assert pending["status"] == "待核对"

        verified, _ = edge.run_action(pending["id"], "确认核对")
        assert verified is not None and verified["status"] == "已核对"
        # 待核对本身已经可受理，核对后仍留在范围内。
        hops = impact.hops_for("summary_layer")
        assert any(h["downstream"] == "metric_bind" for h in hops)


# --------------------------------------------------------------------------
# 解析/执行分离：工作台在途认领 vs 服务端动作分派
# --------------------------------------------------------------------------
class TestResolveThenCommit:
    def test_resolve_does_not_write(self):
        d = ActionDispatcher()
        plan = d.resolve(ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑")
        assert store.find("reprocess_task", 2)["status"] == "待排期"
        assert d.inflight_for("reprocess_task", 2)

        result = d.execute(plan)
        assert result.ok
        assert store.find("reprocess_task", 2)["status"] == "重跑中"

    def test_claim_token_is_scoped_to_module_and_record(self):
        d = ActionDispatcher()
        plan = d.resolve(ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑")
        assert plan.claim.module == "reprocess_task"
        assert plan.claim.entry_id == 2
        assert plan.claim.token.startswith("reprocess_task:2:")

    def test_stale_claim_after_other_request_rewrites_record(self):
        d = ActionDispatcher()
        plan = d.resolve(ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑")

        # 解析之后，另一个请求直接改写了这条重跑记录。
        target = store.find("reprocess_task", 2)
        target["触发原因"] = "被并发请求改写"

        with pytest.raises(DispatchError) as caught:
            d.execute(plan)
        assert "写入资格已过期" in caught.value.message
        assert store.find("reprocess_task", 2)["status"] == "待排期"

    def test_stale_claim_does_not_touch_the_lineage_module(self):
        """过期资格的执行失败，不能连带写入同号的血缘记录。"""
        d = ActionDispatcher()
        plan = d.resolve(ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑")
        store.find("reprocess_task", 2)["覆盖策略"] = "并存留痕"

        with pytest.raises(DispatchError):
            d.execute(plan)
        assert store.find("lineage_edge", 2)["status"] == "已核对"


# --------------------------------------------------------------------------
# 同号记录分属两页：各自动作、各自归属
# --------------------------------------------------------------------------
class TestSameIdAcrossPages:
    def test_same_numeric_id_lives_in_both_modules(self):
        assert store.find("reprocess_task", 2) is not None
        assert store.find("lineage_edge", 2) is not None
        assert store.find("reprocess_task", 2) is not store.find("lineage_edge", 2)

    def test_same_id_actions_hit_their_own_module(self):
        d = ActionDispatcher()
        reprocess = d.execute(
            d.resolve(ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑"))
        lineage = d.execute(
            d.resolve(ResourceContext.of("lineage_edge", "血缘边", 2), "标记失效"))

        assert reprocess.module == "reprocess_task"
        assert lineage.module == "lineage_edge"
        assert store.find("reprocess_task", 2)["status"] == "重跑中"
        assert store.find("lineage_edge", 2)["status"] == "已失效"

    def test_cross_route_is_rejected_and_writes_nothing(self):
        """同号错路由：拿重跑的动作去打血缘记录，解析阶段即拒绝。"""
        d = ActionDispatcher()
        with pytest.raises(DispatchError) as caught:
            d.resolve(ResourceContext.of("lineage_edge", "血缘边", 2), "排期重跑")
        assert "不属于lineage_edge可执行范围" in caught.value.message

        for module in ("reprocess_task", "lineage_edge"):
            assert store.find(module, 2)["status"] in {"待排期", "已核对"}

    def test_declared_target_module_mismatch_rejected(self):
        d = ActionDispatcher()
        with pytest.raises(DispatchError) as caught:
            d.resolve(
                ResourceContext.of("lineage_edge", "血缘边", 2), "标记失效",
                target_module="reprocess_task",
            )
        assert "越权分派" in caught.value.message
        assert store.find("lineage_edge", 2)["status"] == "已核对"

    def test_origin_module_mismatch_rejected(self):
        d = ActionDispatcher()
        with pytest.raises(DispatchError) as caught:
            d.resolve(
                ResourceContext.of(
                    "reprocess_task", "重跑任务", 2, origin_key="lineage_edge"),
                "排期重跑",
            )
        assert "越权分派" in caught.value.message
        assert store.find("reprocess_task", 2)["status"] == "待排期"

    def test_reply_cannot_close_another_modules_claim(self):
        """晚到的回包归属到另一模块时，不结算本模块的认领。"""
        d = ActionDispatcher()
        reprocess = d.execute(
            d.resolve(ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑"))
        lineage = d.execute(
            d.resolve(ResourceContext.of("lineage_edge", "血缘边", 2), "提交核对"))

        assert reprocess.module == "reprocess_task"
        assert lineage.module == "lineage_edge"
        assert reprocess.token != lineage.token
        # 串号的回包（用重跑的 token 认领血缘的动作）对应的计划并不存在。
        assert d.plan_for_token(reprocess.token) is None
        assert d.plan_for_token(lineage.token) is None


# --------------------------------------------------------------------------
# 已受理排期 vs 下一次排期可采用的范围
# --------------------------------------------------------------------------
class TestAcceptedVersusNextScope:
    def test_accepted_scope_is_frozen_while_next_scope_shrinks(self):
        d = ActionDispatcher()
        plan = d.resolve(ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑")
        accepted = d.execute(plan).entry["影响范围"]
        assert accepted["direct_impact"] == ["summary_layer", "quality_gate"]

        # 边被标记失效后，下一次排期不能再采用它。
        service_for("lineage_edge").run_action(4, "标记失效")
        current = impact.scope_for("销售事实表")
        assert current["direct_impact"] == ["quality_gate"]

        frozen = store.find("reprocess_task", 2)["影响范围"]
        assert frozen["direct_impact"] == ["summary_layer", "quality_gate"]
        assert frozen != current

    def test_second_schedule_reuses_only_admissible_edges(self):
        d = ActionDispatcher()
        service_for("lineage_edge").run_action(4, "标记失效")

        plan = d.resolve(ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑")
        scope = d.execute(plan).entry["影响范围"]
        assert scope["direct_impact"] == ["quality_gate"]
        assert scope["indirect_impact"] == []
        assert scope["受理序号"] == 1


# --------------------------------------------------------------------------
# 原有结论保持不变
# --------------------------------------------------------------------------
class TestExistingConclusions:
    def test_existing_action_still_succeeds(self):
        d = ActionDispatcher()
        result = d.dispatch(ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑")
        assert result.ok
        assert result.message == "重跑任务已排期重跑"

    def test_unknown_action_message_unchanged(self):
        d = ActionDispatcher()
        with pytest.raises(DispatchError) as caught:
            d.dispatch(ResourceContext.of("reprocess_task", "重跑任务", 2), "未知动作")
        assert caught.value.message == "动作「未知动作」不属于reprocess_task可执行范围"

    def test_missing_record_message_unchanged(self):
        d = ActionDispatcher()
        with pytest.raises(DispatchError) as caught:
            d.dispatch(ResourceContext.of("reprocess_task", "重跑任务", 99), "排期重跑")
        assert caught.value.message == "重跑任务 99 不存在或已归档"

    def test_missing_record_reported_for_lineage_too(self):
        d = ActionDispatcher()
        with pytest.raises(DispatchError) as caught:
            d.dispatch(ResourceContext.of("lineage_edge", "血缘边", 99), "确认核对")
        assert caught.value.message == "血缘边 99 不存在或已归档"

    def test_invalidated_edge_refuses_reverification(self):
        """失效边由目标资源在解析阶段拒绝，资格在执行前也复核一次。"""
        d = ActionDispatcher()
        d.dispatch(ResourceContext.of("lineage_edge", "4", 4), "标记失效")
        with pytest.raises(DispatchError) as caught:
            d.dispatch(ResourceContext.of("lineage_edge", "血缘边", 4), "确认核对")
        assert caught.value.message == "血缘边 4 已失效，不能再确认核对"
        assert store.find("lineage_edge", 4)["status"] == "已失效"

    def test_legacy_service_run_action_untouched(self):
        """直接调用服务层老方法的调用方行为不变。"""
        entry, message = service_for("reprocess_task").run_action(1, "确认完成")
        assert entry is not None
        assert message == "重跑任务已确认完成"
        assert entry["status"] == "已完成"


# --------------------------------------------------------------------------
# 并发：解析后暂停，另一请求改写重跑记录，同时让血缘动作正常完成
# --------------------------------------------------------------------------
class TestConcurrentInterleaving:
    def test_paused_reprocess_commit_loses_while_lineage_completes(self):
        d = ActionDispatcher()
        paused = d.resolve(
            ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑")
        # 解析已给出动作，但尚未写入阶段。
        assert store.find("reprocess_task", 2)["status"] == "待排期"

        # 另一请求修改这条重跑记录（走同一个分派入口，正常完成）。
        other = d.dispatch(
            ResourceContext.of("reprocess_task", "重跑任务", 2), "标记失败")
        assert other.ok
        assert store.find("reprocess_task", 2)["status"] == "重跑失败"

        # 同号的血缘动作正常完成，不受重跑记录被改写影响。
        lineage = d.dispatch(
            ResourceContext.of("lineage_edge", "血缘边", 2), "确认核对")
        assert lineage.ok
        assert store.find("lineage_edge", 2)["status"] == "已核对"
        assert lineage.token != paused.claim.token

        # 暂停的那次提交重新核对写入资格，发现记录已变，拒绝写入。
        with pytest.raises(DispatchError) as caught:
            d.execute(paused)
        assert "写入资格已过期" in caught.value.message
        assert store.find("reprocess_task", 2)["status"] == "重跑失败"
        assert store.find("lineage_edge", 2)["status"] == "已核对"

    def test_same_record_different_actions_do_not_share_token(self):
        """同一记录上的不同动作各有自己的认领凭据，互不顶掉。"""
        d = ActionDispatcher()
        schedule = d.resolve(
            ResourceContext.of("reprocess_task", "重跑任务", 2), "排期重跑")
        fail = d.resolve(
            ResourceContext.of("reprocess_task", "重跑任务", 2), "标记失败")
        assert schedule.claim.token != fail.claim.token
        assert d.plan_for_token(schedule.claim.token) is schedule
        assert d.plan_for_token(fail.claim.token) is fail


# --------------------------------------------------------------------------
# HTTP 层：原调用方不改、扩展字段按需出现
# --------------------------------------------------------------------------
@pytest.fixture()
def client():
    try:
        from fastapi.testclient import TestClient
    except Exception as error:  # 缺少 httpx2 时跳过 HTTP 层用例
        pytest.skip(f"HTTP 测试客户端不可用：{error}")

    from app.main import app

    return TestClient(app)


class TestHttpContract:
    def test_legacy_payload_returns_legacy_shape(self, client):
        response = client.post("/api/reprocess_task/2/actions", json={"action": "排期重跑"})
        body = response.json()
        assert set(body) == {"ok", "message", "entry"}
        assert body["ok"] is True

    def test_legacy_unknown_action_shape(self, client):
        response = client.post("/api/reprocess_task/2/actions", json={"action": "未知动作"})
        assert response.json() == {
            "ok": False,
            "message": "动作「未知动作」不属于reprocess_task可执行范围",
            "entry": None,
        }

    def test_legacy_missing_record_shape(self, client):
        response = client.post("/api/reprocess_task/99/actions", json={"action": "排期重跑"})
        assert response.json() == {
            "ok": False,
            "message": "重跑任务 99 不存在或已归档",
            "entry": None,
        }

    def test_resolve_commit_two_phase(self, client):
        first = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "排期重跑", "phase": "resolve", "origin_module": "reprocess_task"},
        ).json()
        assert first["ok"]
        assert first["module"] == "reprocess_task"
        assert client.get("/api/reprocess_task/2").json()["status"] == "待排期"

        second = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "排期重跑", "phase": "commit", "token": first["token"]},
        ).json()
        assert second["ok"]
        assert second["module"] == "reprocess_task"
        assert second["action"] == "排期重跑"
        assert client.get("/api/reprocess_task/2").json()["status"] == "重跑中"

    def test_commit_with_foreign_token_rejected(self, client):
        first = client.post(
            "/api/lineage_edge/2/actions",
            json={"action": "标记失效", "phase": "resolve"},
        ).json()
        # 用血缘的 token 去提交重跑模块的动作，必须被当成过期资格。
        second = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "排期重跑", "phase": "commit", "token": first["token"]},
        ).json()
        assert second["ok"] is False
        assert "在途认领凭据已过期" in second["message"]
        assert client.get("/api/reprocess_task/2").json()["status"] == "待排期"
        assert client.get("/api/lineage_edge/2").json()["status"] == "已核对"

    def test_commit_with_other_action_token_rejected(self, client):
        """拿「标记失败」的认领去提交「排期重跑」，即使同号同模块也拒绝。"""
        claim = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "标记失败", "phase": "resolve"},
        ).json()
        body = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "排期重跑", "phase": "commit", "token": claim["token"]},
        ).json()
        assert body["ok"] is False
        assert "越权分派" in body["message"]
        assert client.get("/api/reprocess_task/2").json()["status"] == "待排期"

    def test_commit_after_record_rewritten_reports_stale_claim(self, client):
        claim = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "排期重跑", "phase": "resolve"},
        ).json()
        client.post("/api/reprocess_task/2/actions", json={"action": "标记失败"})
        client.post("/api/lineage_edge/2/actions", json={"action": "确认核对"})

        body = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "排期重跑", "phase": "commit", "token": claim["token"]},
        ).json()
        assert body["ok"] is False
        assert "写入资格已过期" in body["message"]
        assert client.get("/api/reprocess_task/2").json()["status"] == "重跑失败"
        assert client.get("/api/lineage_edge/2").json()["status"] == "已核对"

    def test_cross_route_target_module_rejected(self, client):
        body = client.post(
            "/api/lineage_edge/2/actions",
            json={"action": "标记失效", "target_module": "reprocess_task"},
        ).json()
        assert body["ok"] is False
        assert "越权分派" in body["message"]
        assert client.get("/api/lineage_edge/2").json()["status"] == "已核对"
        assert client.get("/api/reprocess_task/2").json()["status"] == "待排期"

    def test_impact_preview_separates_accepted_and_current(self, client):
        client.post("/api/reprocess_task/2/actions", json={"action": "排期重跑"})
        client.post("/api/lineage_edge/4/actions", json={"action": "标记失效"})

        body = client.get("/api/reprocess_task/2/impact").json()
        assert body["accepted"]["direct_impact"] == ["summary_layer", "quality_gate"]
        assert body["current"]["direct_impact"] == ["quality_gate"]

        lineage = client.get("/api/lineage_edge/4/impact").json()
        assert lineage["module"] == "lineage_edge"
        assert all(hop["downstream"] != "summary_layer" for hop in lineage["impact"])

    def test_two_pages_same_id_then_cross_route_message(self, client):
        """两页各一条同号记录：各自动作成功，越权回包保留原有文案。"""
        # 重跑页 id=2 排期；血缘页 id=2 确认核对。
        schedule = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "排期重跑", "origin_module": "reprocess_task",
                  "return_meta": True},
        ).json()
        verification = client.post(
            "/api/lineage_edge/2/actions",
            json={"action": "确认核对", "origin_module": "lineage_edge",
                  "return_meta": True},
        ).json()

        assert schedule["ok"] and schedule["module"] == "reprocess_task"
        assert schedule["message"] == "重跑任务已排期重跑"
        assert verification["ok"] and verification["module"] == "lineage_edge"
        assert verification["message"] == "血缘边已确认核对"
        # 两条回包的 token 各属自己的模块，谁也关不掉谁的提交。
        assert schedule["token"].startswith("reprocess_task:2:")
        assert verification["token"].startswith("lineage_edge:2:")
        assert schedule["token"] != verification["token"]

        # 同号错路由：血缘动作打到重跑记录上，界面收到的是原有未知动作文案。
        crossed = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "确认核对", "origin_module": "lineage_edge"},
        ).json()
        assert crossed == {
            "ok": False,
            "message": "动作「确认核对」不属于reprocess_task可执行范围",
            "entry": None,
        }
        # 两类记录后续各自仍处于可执行状态，越权请求没写入任何一条。
        reprocess = client.get("/api/reprocess_task/2").json()
        lineage = client.get("/api/lineage_edge/2").json()
        assert reprocess["status"] == "重跑中"
        assert lineage["status"] == "已核对"

    def test_origin_module_mismatch_on_http_rejected(self, client):
        body = client.post(
            "/api/reprocess_task/2/actions",
            json={"action": "标记失败", "origin_module": "lineage_edge"},
        ).json()
        assert body["ok"] is False
        assert "越权分派" in body["message"]
        assert client.get("/api/reprocess_task/2").json()["status"] == "待排期"
        assert client.get("/api/lineage_edge/2").json()["status"] == "已核对"


# --------------------------------------------------------------------------
# 资源上下文：跨模块取回时的归属凭据
# --------------------------------------------------------------------------
class TestResourceContext:
    def test_module_ref_distinguishes_same_id(self):
        reprocess = ResourceContext.of("reprocess_task", "重跑任务", 2)
        lineage = ResourceContext.of("lineage_edge", "血缘边", 2)
        assert not reprocess.same_target(lineage)
        assert reprocess.belongs_to("reprocess_task")
        assert not lineage.belongs_to("reprocess_task")

    def test_origin_defaults_to_target_module(self):
        context = ResourceContext.of("lineage_edge", "血缘边", 2)
        assert context.origin is None
        d = ActionDispatcher()
        result = d.dispatch(context, "确认核对")
        assert result.origin == "lineage_edge"

    def test_origin_is_carried_back(self):
        context = ResourceContext.of(
            "lineage_edge", "血缘边", 2, origin_key="lineage_edge")
        result = ActionDispatcher().dispatch(context, "确认核对")
        assert result.origin == "lineage_edge"
        assert result.to_dict()["origin"] == "lineage_edge"
