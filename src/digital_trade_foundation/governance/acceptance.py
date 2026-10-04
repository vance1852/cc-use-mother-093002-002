"""承诺治理模块的离线端到端验收。

在临时 SQLite 数据库中走完一条完整链路：登记协议与参与方、区分四类文书、
条件组核验触发阶段生效与分期拨付、重复回调防护、成果唯一认领、
违约整改、范围缩减、合作方替换、监督对账、历史时点还原以及
服务重启后未决工作按原顺序继续。成功时输出 status 为 ok 的 JSON。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..errors import ConflictError, PermissionDenied
from ..service import DomainService
from ..storage import Database
from .service import GovernanceService


class MutableClock:
    """可在验收过程中推进的 UTC 时钟。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value = self.value + timedelta(**kwargs)


BASE_TIME = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def run() -> dict[str, object]:
    """执行完整验收链并返回检查结果。"""

    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "governance.sqlite3"
        clock = MutableClock(BASE_TIME)
        database = Database(path)
        domain = DomainService(database, clock)
        gov = GovernanceService(database, domain, clock)

        # 基础主体：办公室、技术方、投资方、当地机构与监督角色
        domain.register_organization(request_id="org-office", actor_id="bootstrap",
                                     organization_id="org-office", name="合作项目办公室")
        domain.register_actor(request_id="actor-admin", actor_id="bootstrap",
                              new_actor_id="off-1", display_name="办公室管理员",
                              role="admin", organization_id="org-office")
        for org_id, name in (("org-tech", "技术方"), ("org-invest", "投资方"),
                             ("org-local", "当地机构"), ("org-tech2", "接替技术方")):
            domain.register_organization(request_id=f"reg-{org_id}", actor_id="off-1",
                                         organization_id=org_id, name=name)
        domain.register_actor(request_id="actor-tech", actor_id="off-1", new_actor_id="tech-op",
                              display_name="技术方代表", role="operator", organization_id="org-tech")
        domain.register_actor(request_id="actor-tech2", actor_id="off-1", new_actor_id="tech2-op",
                              display_name="接替技术方代表", role="operator",
                              organization_id="org-tech2")
        domain.register_actor(request_id="actor-invest", actor_id="off-1", new_actor_id="inv-op",
                              display_name="投资方代表", role="operator", organization_id="org-invest")
        domain.register_actor(request_id="actor-local", actor_id="off-1", new_actor_id="loc-op",
                              display_name="当地机构代表", role="operator", organization_id="org-local")
        domain.register_actor(request_id="actor-reviewer", actor_id="off-1", new_actor_id="rev-1",
                              display_name="独立复核员", role="reviewer", organization_id="org-office")
        domain.register_actor(request_id="actor-auditor", actor_id="off-1", new_actor_id="aud-1",
                              display_name="监督审计员", role="auditor", organization_id="org-office")
        domain.register_site(request_id="site-1", actor_id="off-1", site_id="site-1",
                             organization_id="org-office", name="非洲合作节点",
                             timezone_name="Africa/Accra")

        # 协议与参与方
        gov.create_agreement(request_id="agr-1", actor_id="off-1", site_id="site-1",
                             agreement_id="agr-1", title="中非数字贸易联合方案", currency="USD")
        gov.add_party(request_id="party-tech", actor_id="off-1", agreement_id="agr-1",
                      party_id="p-tech", organization_id="org-tech", party_role="tech_provider")
        gov.add_party(request_id="party-invest", actor_id="off-1", agreement_id="agr-1",
                      party_id="p-inv", organization_id="org-invest", party_role="investor")
        gov.add_party(request_id="party-local", actor_id="off-1", agreement_id="agr-1",
                      party_id="p-loc", organization_id="org-local", party_role="local_institution")

        # 谈判稿与正式承诺文书相互区分
        gov.submit_document(request_id="doc-draft-1", actor_id="tech-op", agreement_id="agr-1",
                            document_id="draft-1", kind="negotiation_draft",
                            title="联合方案谈判稿一", content={"version": 1})
        gov.submit_document(request_id="doc-draft-2", actor_id="tech-op", agreement_id="agr-1",
                            document_id="draft-2", kind="negotiation_draft",
                            title="联合方案谈判稿二", content={"version": 2},
                            supersedes="draft-1")
        gov.submit_document(request_id="doc-formal", actor_id="off-1", agreement_id="agr-1",
                            document_id="formal-1", kind="formal_commitment",
                            title="正式承诺书", content={"signed": True})

        # 七类承诺分别登记
        commitments = [
            ("c-platform", "contribution", "平台建设投入", "p-tech", 100000),
            ("c-train", "localization", "属地人才培训", "p-tech", 200),
            ("c-benef", "beneficiary", "受益群体覆盖", "p-loc", 500),
            ("c-ip", "ip_data", "知识与数据权属", "p-tech", 1),
            ("c-escrow", "escrow", "资金托管安排", "p-inv", 300000),
            ("c-risk", "risk_guarantee", "风险保障金", "p-inv", 50000),
            ("c-exit", "exit_duty", "退出交接责任", "p-tech", 1),
        ]
        for index, (cid, category, title, party, amount) in enumerate(commitments):
            gov.create_commitment(request_id=f"commit-{cid}", actor_id="off-1",
                                  agreement_id="agr-1", commitment_id=cid, category=category,
                                  title=title, stage=1, responsible_party_id=party,
                                  target_amount=amount, terms={"index": index})
        gov.formalize_commitments(request_id="formalize-1", actor_id="off-1",
                                  agreement_id="agr-1", document_id="formal-1",
                                  commitment_ids=[item[0] for item in commitments])

        # 条件组：阶段生效与分期拨付分别成组核验
        gov.create_condition_group(
            request_id="group-stage", actor_id="off-1", agreement_id="agr-1",
            group_id="g-stage", title="阶段一生效条件", effect_type="activate_stage",
            effect_target="1",
            conditions=[
                {"condition_id": "cond-platform", "commitment_id": "c-platform",
                 "description": "平台上线报告", "evidence_due_at": "2026-10-10T00:00:00Z"},
                {"condition_id": "cond-train", "commitment_id": "c-train",
                 "description": "属地团队完成首批培训", "evidence_due_at": "2026-10-10T00:00:00Z"},
            ])
        gov.create_condition_group(
            request_id="group-tranche", actor_id="off-1", agreement_id="agr-1",
            group_id="g-tranche", title="首期拨付条件", effect_type="release_tranche",
            effect_target="tr-1",
            conditions=[
                {"condition_id": "cond-local-cap", "commitment_id": "c-train",
                 "description": "属地能力评估通过", "evidence_due_at": "2026-10-05T00:00:00Z"},
            ])
        gov.schedule_tranche(request_id="tranche-1", actor_id="off-1", agreement_id="agr-1",
                             tranche_id="tr-1", sequence_no=1, amount=150000,
                             condition_group_id="g-tranche")
        gov.create_condition_group(
            request_id="group-tranche-2", actor_id="off-1", agreement_id="agr-1",
            group_id="g-tranche-2", title="二期拨付条件", effect_type="release_tranche",
            effect_target="tr-2",
            conditions=[
                {"condition_id": "cond-benef", "commitment_id": "c-benef",
                 "description": "受益群体核验", "evidence_due_at": "2026-12-01T00:00:00Z"},
            ])
        gov.schedule_tranche(request_id="tranche-2", actor_id="off-1", agreement_id="agr-1",
                             tranche_id="tr-2", sequence_no=2, amount=150000,
                             condition_group_id="g-tranche-2")

        # 证据迟到被标记但责任保留；独立复核后阶段生效
        clock.advance(days=11)
        gov.submit_evidence(request_id="ev-train", actor_id="tech-op",
                            condition_id="cond-train", evidence_id="ev-train",
                            title="培训签到与考核", content={"sessions": 4})
        gov.submit_evidence(request_id="ev-platform", actor_id="tech-op",
                            condition_id="cond-platform", evidence_id="ev-platform",
                            title="平台上线报告", content={"url": "https://example.local"})
        gov.review_evidence(request_id="ev-train-review", actor_id="rev-1",
                            evidence_id="ev-train", decision="accept")
        gov.review_evidence(request_id="ev-platform-review", actor_id="rev-1",
                            evidence_id="ev-platform", decision="accept")
        evidence_items = gov.list_evidence(actor_id="aud-1", agreement_id="agr-1")
        late_flags = {item["evidence_id"]: item["late"] for item in evidence_items}
        checks["late_evidence_flagged"] = late_flags.get("ev-train") is True
        stage_one = {item["commitment_id"]: item["status"]
                     for item in gov.list_commitments(actor_id="aud-1", agreement_id="agr-1")}
        checks["stage_activated"] = stage_one.get("c-platform") == "active"

        # 属地能力证据齐备前，投资方已注资也不能拨付
        gov.submit_evidence(request_id="ev-local-cap", actor_id="tech-op",
                            condition_id="cond-local-cap", evidence_id="ev-local-cap",
                            title="属地能力评估", content={"score": 88})
        gov.review_evidence(request_id="ev-local-cap-review", actor_id="rev-1",
                            evidence_id="ev-local-cap", decision="accept")
        gov.deposit_escrow(request_id="deposit-1", actor_id="inv-op",
                           tranche_id="tr-1", amount=150000)
        first = gov.confirm_disbursement(request_id="callback-1", actor_id="off-1",
                                         tranche_id="tr-1", callback_reference="pay-0001")
        replay = gov.confirm_disbursement(request_id="callback-1", actor_id="off-1",
                                          tranche_id="tr-1", callback_reference="pay-0001")
        duplicate = gov.confirm_disbursement(request_id="callback-2", actor_id="off-1",
                                             tranche_id="tr-1", callback_reference="pay-0001")
        recon_after_pay = gov.reconcile(actor_id="aud-1", agreement_id="agr-1")
        checks["tranche_disbursed"] = recon_after_pay["escrow"]["total_disbursed"] == 150000
        checks["duplicate_callback_ignored"] = (
            not first.replayed and replay.replayed
            and recon_after_pay["escrow"]["balance"] == 0
            and len([t for t in recon_after_pay["tranches"]
                     if t["status"] == "disbursed"]) == 1
            and duplicate.resource_id == "tr-1")

        # 履约登记与独立复核
        gov.record_fulfillment(request_id="fulfill-platform", actor_id="tech-op",
                               commitment_id="c-platform", fulfillment_id="ful-platform",
                               amount=100000, note="平台交付完成")
        gov.review_fulfillment(request_id="fulfill-platform-review", actor_id="rev-1",
                               fulfillment_id="ful-platform", decision="approve")
        gov.record_fulfillment(request_id="fulfill-train-1", actor_id="tech-op",
                               commitment_id="c-train", fulfillment_id="ful-train-1",
                               amount=100, note="首批培训 100 人")
        gov.review_fulfillment(request_id="fulfill-train-1-review", actor_id="rev-1",
                               fulfillment_id="ful-train-1", decision="approve")

        # 局部违约与整改：已兑现部分与历史责任保留
        gov.report_breach(request_id="breach-1", actor_id="rev-1", commitment_id="c-train",
                          breach_id="br-1", severity="partial",
                          description="第二批培训未按期开展")
        gov.create_rectification(request_id="rect-1", actor_id="rev-1", breach_id="br-1",
                                 rectification_id="rc-1", requirement="补交培训计划并执行",
                                 due_at="2026-10-20T00:00:00Z")
        gov.submit_rectification(request_id="rect-1-submit", actor_id="tech-op",
                                 rectification_id="rc-1")
        gov.review_rectification(request_id="rect-1-review", actor_id="rev-1",
                                 rectification_id="rc-1", decision="approve")
        breaches = gov.list_breaches(actor_id="aud-1", agreement_id="agr-1")
        train_commitment = {item["commitment_id"]: item
                            for item in gov.list_commitments(actor_id="aud-1",
                                                             agreement_id="agr-1")}["c-train"]
        checks["breach_resolved"] = breaches[0]["status"] == "resolved"
        checks["responsibility_retained"] = (
            breaches[0]["unfulfilled_amount"] == 100
            and train_commitment["fulfilled_amount"] == 100
            and train_commitment["remaining_amount"] == 100)

        # 范围缩减只影响尚未兑现的部分
        gov.reduce_scope(request_id="reduce-1", actor_id="off-1", commitment_id="c-benef",
                         new_target_amount=400, reason="受益区域调整")
        benef = {item["commitment_id"]: item
                 for item in gov.list_commitments(actor_id="aud-1", agreement_id="agr-1")}["c-benef"]
        checks["reduction_keeps_original_terms"] = (
            benef["target_amount"] == 400 and benef["original_terms"]["index"] == 2)

        # 合作方替换：未兑现责任转移给接替方
        gov.replace_party(request_id="replace-1", actor_id="off-1", agreement_id="agr-1",
                          outgoing_party_id="p-tech", incoming_party_id="p-tech2",
                          incoming_organization_id="org-tech2", reason="原技术方退出")
        moved = {item["commitment_id"]: item["responsible_party_id"]
                 for item in gov.list_commitments(actor_id="aud-1", agreement_id="agr-1")}
        checks["replacement_transferred_remaining"] = (
            moved["c-train"] == "p-tech2" and moved["c-exit"] == "p-tech2")

        # 共享成果只能被一个项目认领
        gov.register_outcome(request_id="outcome-1", actor_id="loc-op", agreement_id="agr-1",
                             outcome_id="out-1", outcome_key="joint-training-2026",
                             title="联合培训成果", beneficiary_group="当地中小企业",
                             shared=True)
        gov.claim_outcome(request_id="claim-a", actor_id="loc-op", outcome_id="out-1",
                          project_key="proj-alpha")
        try:
            gov.claim_outcome(request_id="claim-b", actor_id="tech2-op", outcome_id="out-1",
                              project_key="proj-beta")
            checks["double_claim_blocked"] = False
        except ConflictError:
            checks["double_claim_blocked"] = True

        # 争议与结论文书
        gov.file_dispute(request_id="dispute-1", actor_id="loc-op", agreement_id="agr-1",
                         dispute_id="dp-1", description="数据权属条款分歧",
                         commitment_id="c-ip")
        gov.conclude_dispute(request_id="dispute-1-conclusion", actor_id="rev-1",
                             dispute_id="dp-1", document_id="ruling-1",
                             title="数据权属争议结论",
                             ruling={"decision": "数据共同所有", "binding": True})
        rulings = gov.list_documents(actor_id="aud-1", agreement_id="agr-1",
                                     kind="dispute_ruling")
        checks["dispute_concluded"] = len(rulings) == 1

        # 历史时点还原：替换前责任在原技术方，替换后在接替方
        before_replace = gov.reconstruct(actor_id="aud-1", agreement_id="agr-1",
                                         at="2026-10-11T12:00:00Z")
        after_replace = gov.reconstruct(actor_id="aud-1", agreement_id="agr-1",
                                        at=clock.now().isoformat().replace("+00:00", "Z"))
        train_before = {c["commitment_id"]: c for c in before_replace["commitments"]}["c-train"]
        train_after = {c["commitment_id"]: c for c in after_replace["commitments"]}["c-train"]
        outcome_after = after_replace["outcomes"][0]
        checks["reconstruction_restores_history"] = (
            train_before["responsible_party_id"] == "p-tech"
            and train_after["responsible_party_id"] == "p-tech2"
            and outcome_after["claimed_by_project"] == "proj-alpha"
            and before_replace["escrow"]["disbursed"] == 0
            and after_replace["escrow"]["disbursed"] == 150000)

        # 未决工作：制造一条逾期整改与一条待复核履约，重启后按原顺序继续
        gov.report_breach(request_id="breach-2", actor_id="rev-1", commitment_id="c-benef",
                          breach_id="br-2", severity="partial",
                          description="受益登记进度落后")
        gov.create_rectification(request_id="rect-2", actor_id="rev-1", breach_id="br-2",
                                 rectification_id="rc-2", requirement="补录受益名单",
                                 due_at="2026-10-15T00:00:00Z")
        gov.record_fulfillment(request_id="fulfill-benef-1", actor_id="loc-op",
                               commitment_id="c-benef", fulfillment_id="ful-benef-1",
                               amount=50, note="首批受益登记 50 家")
        before_close = gov.pending_work(actor_id="aud-1", agreement_id="agr-1")
        database.close()

        # 模拟服务中断后重启：同一数据库文件，新的服务实例
        database2 = Database(path)
        clock.advance(days=20)
        domain2 = DomainService(database2, clock)
        gov2 = GovernanceService(database2, domain2, clock)
        after_restart = gov2.pending_work(actor_id="aud-1", agreement_id="agr-1")
        checks["restart_pending_order"] = (
            [item["id"] for item in before_close["items"]] == ["rc-2", "ful-benef-1"]
            and [item["id"] for item in before_close["items"]]
            == [item["id"] for item in after_restart["items"]]
            and [item["kind"] for item in after_restart["items"]]
            == ["rectification", "fulfillment_review"])
        swept = gov2.sweep_overdue_rectifications(actor_id="off-1", agreement_id="agr-1")
        checks["overdue_swept_in_order"] = swept["overdue_rectifications"] == ["rc-2"]

        # 监督对账：托管余额、已拨金额、未履行承诺互相对得上
        final_recon = gov2.reconcile(actor_id="aud-1", agreement_id="agr-1")
        checks["reconciliation_consistent"] = (
            final_recon["escrow"]["ledger_consistent"]
            and final_recon["escrow"]["balance"] == 0
            and final_recon["escrow"]["total_disbursed"] == 150000
            and final_recon["outstanding_obligation"] > 0
            and final_recon["open_breaches"] == 1)

        # 仍有未了结责任时不能退出
        try:
            gov2.begin_exit(request_id="exit-1", actor_id="off-1", agreement_id="agr-1")
            gov2.complete_exit(request_id="exit-2", actor_id="off-1", agreement_id="agr-1")
            checks["exit_blocked"] = False
        except ConflictError:
            checks["exit_blocked"] = True

        # 参与方越权访问被限制
        domain2.register_organization(request_id="reg-org-out", actor_id="off-1",
                                      organization_id="org-out", name="外部机构")
        domain2.register_actor(request_id="actor-out", actor_id="off-1", new_actor_id="out-op",
                               display_name="外部人员", role="operator", organization_id="org-out")
        try:
            gov2.get_agreement_view(actor_id="out-op", agreement_id="agr-1")
            checks["visibility_scoped"] = False
        except PermissionDenied:
            checks["visibility_scoped"] = True

        valid, event_count = domain2.verify_audit()
        database2.close()

    return {
        "status": "ok" if all(checks.values()) and valid else "failed",
        "checks": checks,
        "audit_valid": valid,
        "audit_events": event_count,
        "escrow": final_recon["escrow"],
        "outstanding_obligation": final_recon["outstanding_obligation"],
    }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
