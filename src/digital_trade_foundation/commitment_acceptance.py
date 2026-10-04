"""承诺治理平台离线端到端验收。

在临时 SQLite 数据库中完整演练跨境合作"从谈判到退出"：
七类承诺分列、谈判稿与正式承诺分离、条件组独立复核、分期资金在属地能力
形成后才拨付、重复回调不二次扣款、迟到证据与局部违约不抹除责任、
合作方替换只转移未兑现部分、共享成果不能跨项目重复认领、托管对账与
历史时点还原。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .commitment_service import CommitmentService
from .errors import ConflictError, PreconditionFailed
from .storage import Database


def _stage_id(service, commitment_id: str, sequence: int) -> str:
    return service.database.connection.execute(
        "SELECT stage_id FROM commitment_stages WHERE commitment_id=? AND sequence=?",
        (commitment_id, sequence)).fetchone()["stage_id"]


def _condition_id(service, stage_id: str, sequence: int) -> str:
    return service.database.connection.execute(
        "SELECT condition_id FROM stage_conditions WHERE stage_id=? AND sequence=?",
        (stage_id, sequence)).fetchone()["condition_id"]


def _accept_condition(service, *, stage_id: str, sequence: int, submitter: str,
                      reviewer: str, req: str, payload: dict | None = None) -> None:
    condition = _condition_id(service, stage_id, sequence)
    evidence = service.submit_evidence(
        request_id=f"ev-{req}", actor_id=submitter, condition_id=condition,
        reference=f"ref-{req}", payload=payload or {"ok": True})
    service.decide_review(request_id=f"rv-{req}", actor_id=reviewer,
                          evidence_id=evidence.resource_id, approved=True)


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "commitment_acceptance.sqlite3")
        service = CommitmentService(
            database, FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))

        # 建档：办公室 + 技术方/投资方/当地机构/独立复核所
        service.register_organization(request_id="org-office", actor_id="bootstrap",
                                      organization_id="o-office", name="合作项目办公室")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                               display_name="管理员", role="admin", organization_id="o-office")
        for oid, name in [("o-tech", "技术方"), ("o-investor", "投资方"),
                          ("o-local", "当地机构"), ("o-independent", "独立复核所")]:
            service.register_organization(request_id=f"org-{oid}", actor_id="admin",
                                          organization_id=oid, name=name)
        for aid, name, role, oid in [
                ("office", "办公室专员", "operator", "o-office"),
                ("tech", "技术专员", "operator", "o-tech"),
                ("investor", "投资专员", "operator", "o-investor"),
                ("local", "本地专员", "operator", "o-local"),
                ("reviewer", "独立复核员", "reviewer", "o-independent")]:
            service.register_actor(request_id=f"actor-{aid}", actor_id="admin",
                                   new_actor_id=aid, display_name=name, role=role,
                                   organization_id=oid)

        service.create_project(request_id="project", actor_id="office",
                               project_id="p1", name="非洲数字贸易协作")
        for oid, party_role in [("o-tech", "technology_provider"),
                                ("o-investor", "investor"),
                                ("o-local", "local_agency"),
                                ("o-independent", "independent")]:
            service.add_project_party(request_id=f"party-{oid}", actor_id="office",
                                      project_id="p1", organization_id=oid,
                                      party_role=party_role)

        # 技术方投入：平台与培训
        service.draft_commitment(request_id="ct-draft", actor_id="tech", project_id="p1",
                                 commitment_id="ct1", commitment_type="party_input",
                                 provider_organization_id="o-tech", title="平台部署与培训",
                                 terms={"trainees": 200})
        service.revise_negotiation_draft(request_id="ct-rev", actor_id="tech",
                                         commitment_id="ct1", terms={"trainees": 260})
        service.seal_commitment(request_id="ct-seal", actor_id="tech", commitment_id="ct1")

        # 投资方分期资金，入金但不释放
        service.draft_commitment(request_id="cf-draft", actor_id="investor", project_id="p1",
                                 commitment_id="cf1", commitment_type="escrow_funding",
                                 provider_organization_id="o-investor", title="分期建设资金",
                                 terms={"tranches": 2}, amount_minor=100000, currency="USD")
        service.seal_commitment(request_id="cf-seal", actor_id="investor", commitment_id="cf1")
        service.add_stage(request_id="sf1", actor_id="office", commitment_id="cf1",
                          name="首期（属地团队就位）", sequence=1,
                          disburse_amount_minor=40000)
        fund_stage = _stage_id(service, "cf1", 1)
        service.add_condition(request_id="cf1c", actor_id="office", stage_id=fund_stage,
                              label="属地团队就位", sequence=1)
        service.deposit_escrow(request_id="deposit", actor_id="investor", project_id="p1",
                               amount_minor=100000, currency="USD", reference="wire-001")
        # 入金后推进时间，使入金快照与拨付快照落在不同时刻，便于历史时点还原
        service.clock = FixedClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))

        # 能力未形成前：阶段不能生效，资金不能释放
        early_effect_blocked = False
        early_disburse_blocked = False
        try:
            service.effect_stage(request_id="eff-too-early", actor_id="office",
                                 stage_id=fund_stage)
        except PreconditionFailed:
            early_effect_blocked = True
        try:
            service.disburse_stage(request_id="pay-too-early", actor_id="office",
                                   stage_id=fund_stage)
        except PreconditionFailed:
            early_disburse_blocked = True
        early_release_blocked = early_effect_blocked and early_disburse_blocked

        # 首期条件齐备、独立复核通过后生效并拨付
        _accept_condition(service, stage_id=fund_stage, sequence=1,
                          submitter="local", reviewer="reviewer", req="fund")
        service.effect_stage(request_id="eff-fund", actor_id="office", stage_id=fund_stage)
        first_pay = service.disburse_stage(request_id="pay-fund", actor_id="office",
                                           stage_id=fund_stage)
        # 支付通道重复回调：幂等，不二次扣款
        replayed_pay = service.disburse_stage(request_id="pay-fund", actor_id="office",
                                              stage_id=fund_stage)

        escrow = service.escrow(actor_id="office", project_id="p1")

        # 迟到证据：推进时钟后提交超期证据，仍留痕并继续复核
        service.add_stage(request_id="st1", actor_id="office", commitment_id="ct1",
                          name="平台上线", sequence=1, due_at="2026-11-01T00:00:00Z")
        tech_stage = _stage_id(service, "ct1", 1)
        service.add_condition(request_id="ct1c", actor_id="office", stage_id=tech_stage,
                              label="平台验收报告", sequence=1,
                              due_at="2026-10-20T00:00:00Z")
        service.clock = FixedClock(datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc))
        late_condition = _condition_id(service, tech_stage, 1)
        late_evidence = service.submit_evidence(
            request_id="ev-late", actor_id="tech", condition_id=late_condition,
            reference="late-report", payload={"report": "平台验收完成"})
        late_view = service.get_condition(actor_id="office", condition_id=late_condition)
        service.decide_review(request_id="rv-late", actor_id="reviewer",
                              evidence_id=late_evidence.resource_id, approved=True)
        service.effect_stage(request_id="eff-tech", actor_id="office", stage_id=tech_stage)

        # 局部违约 + 范围缩减：只改未兑现部分
        service.record_partial_breach(request_id="breach", actor_id="office",
                                      commitment_id="cf1", detail={"reason": "培训缺口"})
        service.reduce_scope(request_id="reduce", actor_id="office", commitment_id="cf1",
                             amount_delta_minor=-10000, detail={"reason": "缩减二期范围"})

        # 合作方替换：技术方 o-tech -> o-newtech，只转移未兑现承诺
        service.register_organization(request_id="org-new", actor_id="admin",
                                      organization_id="o-newtech", name="新技术方")
        service.replace_project_party(request_id="replace", actor_id="office", project_id="p1",
                                      old_organization_id="o-tech",
                                      new_organization_id="o-newtech", note="技术轮换")
        responsibility_now = service.responsibility(actor_id="office", commitment_id="ct1")
        responsibility_before = service.responsibility(actor_id="office", commitment_id="ct1",
                                                       at="2026-10-24T00:00:00Z")

        # 共享成果不能被多个项目重复认领
        outcome = service.register_outcome(request_id="outcome", actor_id="reviewer",
                                           title="共享培训数据集", measure_unit="people",
                                           measure_value=260)
        service.claim_outcome(request_id="claim-1", actor_id="tech",
                              outcome_id=outcome.resource_id, project_id="p1",
                              commitment_id="ct1")
        service.create_project(request_id="project-2", actor_id="office", project_id="p2",
                               name="另一个项目")
        duplicate_claim_blocked = False
        try:
            service.claim_outcome(request_id="claim-2", actor_id="office",
                                  outcome_id=outcome.resource_id, project_id="p2")
        except ConflictError:
            duplicate_claim_blocked = True

        # 争议结论
        dispute = service.open_dispute(request_id="dispute", actor_id="tech", project_id="p1",
                                       commitment_id="cf1", title="二期金额争议",
                                       detail={"point": "金额"})
        service.conclude_dispute(request_id="conclude", actor_id="office",
                                 dispute_id=dispute.resource_id,
                                 conclusion={"ruling": "减免5000"}, relief_delta_minor=-5000)

        # 监督对账与历史时点还原
        reconcile = service.reconcile(actor_id="office", project_id="p1")
        assignments = service.outcome_assignments(actor_id="office", project_id="p1")
        escrow_history = service.history_at(actor_id="office", entity_type="escrow",
                                            entity_id="p1", at="2026-10-02T00:00:00Z")
        audit_valid, audit_events = service.verify_audit()

        result = {
            "status": "ok",
            "early_release_blocked": early_release_blocked,
            "escrow_balance": escrow.balance,
            "escrow_disbursed": escrow.disbursed_total,
            "duplicate_payment_prevented": bool(replayed_pay.replayed),
            "late_evidence_flagged": bool(late_view.evidences[0]["late"]),
            "cf1_outstanding": service.responsibility(actor_id="office",
                                                      commitment_id="cf1").outstanding_minor,
            "tech_provider_after_replace": responsibility_now.replaced_by,
            "tech_provider_before_replace": responsibility_before.replaced_by,
            "duplicate_claim_blocked": duplicate_claim_blocked,
            "outcome_owner": assignments[0]["claimed_by_project_id"],
            "outcome_responsible_party": assignments[0]["responsible_organization_id"],
            "ledger_balanced": reconcile["ledger_balanced"],
            "escrow_balance_at_history": escrow_history["state"]["balance"],
            "audit_valid": audit_valid,
            "audit_events": audit_events,
            "first_payment_replayed": first_pay.replayed,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["early_release_blocked"]
          and result["duplicate_payment_prevented"]
          and result["late_evidence_flagged"]
          and result["duplicate_claim_blocked"]
          and result["ledger_balanced"]
          and result["escrow_balance"] == 60000
          and result["escrow_balance_at_history"] == 100000
          and result["tech_provider_after_replace"] == "o-newtech"
          and result["tech_provider_before_replace"] is None
          and result["outcome_responsible_party"] == "o-newtech")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
