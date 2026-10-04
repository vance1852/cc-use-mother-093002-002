import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digital_trade_foundation.errors import ConflictError, PermissionDenied, ValidationError
from digital_trade_foundation.governance import GovernanceService
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database


class MutableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **kwargs):
        self.value = self.value + timedelta(**kwargs)


BASE = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


class GovernanceTestCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(BASE)
        self.domain = DomainService(self.database, self.clock)
        self.gov = GovernanceService(self.database, self.domain, self.clock)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        self.domain.register_organization(request_id="org-office", actor_id="bootstrap",
                                          organization_id="org-office", name="合作项目办公室")
        self.domain.register_actor(request_id="actor-admin", actor_id="bootstrap",
                                   new_actor_id="off-1", display_name="办公室管理员",
                                   role="admin", organization_id="org-office")
        for org_id, name in (("org-tech", "技术方"), ("org-invest", "投资方"),
                             ("org-local", "当地机构"), ("org-out", "外部机构")):
            self.domain.register_organization(request_id=f"reg-{org_id}", actor_id="off-1",
                                              organization_id=org_id, name=name)
        actors = [
            ("tech-op", "技术方代表", "operator", "org-tech"),
            ("tech-rev", "技术方复核", "reviewer", "org-tech"),
            ("inv-op", "投资方代表", "operator", "org-invest"),
            ("loc-op", "当地机构代表", "operator", "org-local"),
            ("out-op", "外部人员", "operator", "org-out"),
            ("rev-1", "独立复核员", "reviewer", "org-office"),
            ("aud-1", "监督审计员", "auditor", "org-office"),
        ]
        for actor_id, name, role, org_id in actors:
            self.domain.register_actor(request_id=f"actor-{actor_id}", actor_id="off-1",
                                       new_actor_id=actor_id, display_name=name,
                                       role=role, organization_id=org_id)
        self.domain.register_site(request_id="site-1", actor_id="off-1", site_id="site-1",
                                  organization_id="org-office", name="合作节点",
                                  timezone_name="Africa/Accra")

    def _agreement(self):
        self.gov.create_agreement(request_id="agr-1", actor_id="off-1", site_id="site-1",
                                  agreement_id="agr-1", title="联合方案", currency="USD")
        self.gov.add_party(request_id="party-tech", actor_id="off-1", agreement_id="agr-1",
                           party_id="p-tech", organization_id="org-tech",
                           party_role="tech_provider")
        self.gov.add_party(request_id="party-inv", actor_id="off-1", agreement_id="agr-1",
                           party_id="p-inv", organization_id="org-invest", party_role="investor")
        self.gov.add_party(request_id="party-loc", actor_id="off-1", agreement_id="agr-1",
                           party_id="p-loc", organization_id="org-local",
                           party_role="local_institution")

    def _commitments(self):
        self.gov.submit_document(request_id="doc-formal", actor_id="off-1",
                                 agreement_id="agr-1", document_id="formal-1",
                                 kind="formal_commitment", title="正式承诺书",
                                 content={"signed": True})
        self.gov.create_commitment(request_id="c-tech", actor_id="off-1", agreement_id="agr-1",
                                   commitment_id="c-tech", category="contribution",
                                   title="平台建设", stage=1, responsible_party_id="p-tech",
                                   target_amount=1000, terms={"item": "platform"})
        self.gov.create_commitment(request_id="c-loc", actor_id="off-1", agreement_id="agr-1",
                                   commitment_id="c-loc", category="localization",
                                   title="属地培训", stage=1, responsible_party_id="p-loc",
                                   target_amount=100, terms={"item": "training"})
        self.gov.formalize_commitments(request_id="formalize", actor_id="off-1",
                                       agreement_id="agr-1", document_id="formal-1",
                                       commitment_ids=["c-tech", "c-loc"])

    def _activate_stage_one(self):
        self.gov.create_condition_group(
            request_id="group-1", actor_id="off-1", agreement_id="agr-1", group_id="g-1",
            title="阶段一条件", effect_type="activate_stage", effect_target="1",
            conditions=[{"condition_id": "cond-1", "commitment_id": "c-tech",
                         "description": "平台上线", "evidence_due_at": "2026-10-10T00:00:00Z"}])
        self.gov.submit_evidence(request_id="ev-1", actor_id="tech-op", condition_id="cond-1",
                                 evidence_id="ev-1", title="上线报告", content={"ok": True})
        self.gov.review_evidence(request_id="ev-1-review", actor_id="rev-1",
                                 evidence_id="ev-1", decision="accept")

    def _commitment(self, commitment_id):
        items = self.gov.list_commitments(actor_id="aud-1", agreement_id="agr-1")
        return {item["commitment_id"]: item for item in items}[commitment_id]


class StageAndEscrowTest(GovernanceTestCase):
    def test_stage_commitments_activate_only_after_group_verified(self):
        self._agreement()
        self._commitments()
        self.assertEqual("committed", self._commitment("c-tech")["status"])
        self.gov.create_condition_group(
            request_id="group-1", actor_id="off-1", agreement_id="agr-1", group_id="g-1",
            title="阶段一条件", effect_type="activate_stage", effect_target="1",
            conditions=[{"condition_id": "cond-1", "commitment_id": "c-tech",
                         "description": "平台上线", "evidence_due_at": "2026-10-10T00:00:00Z"}])
        self.gov.submit_evidence(request_id="ev-1", actor_id="tech-op", condition_id="cond-1",
                                 evidence_id="ev-1", title="上线报告", content={"ok": True})
        self.assertEqual("committed", self._commitment("c-tech")["status"])
        self.gov.review_evidence(request_id="ev-1-review", actor_id="rev-1",
                                 evidence_id="ev-1", decision="accept")
        self.assertEqual("active", self._commitment("c-tech")["status"])
        view = self.gov.get_agreement_view(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual("active", view["status"])

    def test_rejected_evidence_blocks_group_verification(self):
        self._agreement()
        self._commitments()
        self.gov.create_condition_group(
            request_id="group-1", actor_id="off-1", agreement_id="agr-1", group_id="g-1",
            title="阶段一条件", effect_type="activate_stage", effect_target="1",
            conditions=[{"condition_id": "cond-1", "commitment_id": "c-tech",
                         "description": "平台上线", "evidence_due_at": None}])
        self.gov.submit_evidence(request_id="ev-1", actor_id="tech-op", condition_id="cond-1",
                                 evidence_id="ev-1", title="上线报告", content={"ok": True})
        self.gov.review_evidence(request_id="ev-1-review", actor_id="rev-1",
                                 evidence_id="ev-1", decision="reject")
        self.assertEqual("committed", self._commitment("c-tech")["status"])

    def test_tranche_cannot_disburse_before_conditions_verified(self):
        self._agreement()
        self._commitments()
        self.gov.create_condition_group(
            request_id="group-t", actor_id="off-1", agreement_id="agr-1", group_id="g-t",
            title="拨付条件", effect_type="release_tranche", effect_target="tr-1",
            conditions=[{"condition_id": "cond-cap", "commitment_id": "c-loc",
                         "description": "属地能力形成", "evidence_due_at": None}])
        self.gov.schedule_tranche(request_id="tr-1", actor_id="off-1", agreement_id="agr-1",
                                  tranche_id="tr-1", sequence_no=1, amount=5000,
                                  condition_group_id="g-t")
        self.gov.deposit_escrow(request_id="dep-1", actor_id="inv-op",
                                tranche_id="tr-1", amount=5000)
        with self.assertRaises(ConflictError):
            self.gov.confirm_disbursement(request_id="cb-1", actor_id="off-1",
                                          tranche_id="tr-1", callback_reference="pay-1")
        recon = self.gov.reconcile(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual(5000, recon["escrow"]["balance"])
        self.assertEqual(0, recon["escrow"]["total_disbursed"])

    def test_tranche_releases_after_group_verified_and_disburses_once(self):
        self._agreement()
        self._commitments()
        self.gov.create_condition_group(
            request_id="group-t", actor_id="off-1", agreement_id="agr-1", group_id="g-t",
            title="拨付条件", effect_type="release_tranche", effect_target="tr-1",
            conditions=[{"condition_id": "cond-cap", "commitment_id": "c-loc",
                         "description": "属地能力形成", "evidence_due_at": None}])
        self.gov.schedule_tranche(request_id="tr-1", actor_id="off-1", agreement_id="agr-1",
                                  tranche_id="tr-1", sequence_no=1, amount=5000,
                                  condition_group_id="g-t")
        self.gov.submit_evidence(request_id="ev-cap", actor_id="loc-op",
                                 condition_id="cond-cap", evidence_id="ev-cap",
                                 title="能力评估", content={"score": 90})
        self.gov.review_evidence(request_id="ev-cap-review", actor_id="rev-1",
                                 evidence_id="ev-cap", decision="accept")
        self.gov.deposit_escrow(request_id="dep-1", actor_id="inv-op",
                                tranche_id="tr-1", amount=5000)
        first = self.gov.confirm_disbursement(request_id="cb-1", actor_id="off-1",
                                              tranche_id="tr-1", callback_reference="pay-1")
        replay = self.gov.confirm_disbursement(request_id="cb-1", actor_id="off-1",
                                               tranche_id="tr-1", callback_reference="pay-1")
        duplicate = self.gov.confirm_disbursement(request_id="cb-2", actor_id="off-1",
                                                  tranche_id="tr-1", callback_reference="pay-1b")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual("tr-1", duplicate.resource_id)
        recon = self.gov.reconcile(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual(0, recon["escrow"]["balance"])
        self.assertEqual(5000, recon["escrow"]["total_disbursed"])
        self.assertTrue(recon["escrow"]["ledger_consistent"])
        disbursed = [t for t in recon["tranches"] if t["status"] == "disbursed"]
        self.assertEqual(1, len(disbursed))

    def test_tranche_scheduled_after_group_verified_still_releases(self):
        self._agreement()
        self._commitments()
        self.gov.create_condition_group(
            request_id="group-t", actor_id="off-1", agreement_id="agr-1", group_id="g-t",
            title="拨付条件", effect_type="release_tranche", effect_target="tr-1",
            conditions=[{"condition_id": "cond-cap", "commitment_id": "c-loc",
                         "description": "属地能力形成", "evidence_due_at": None}])
        self.gov.submit_evidence(request_id="ev-cap", actor_id="loc-op",
                                 condition_id="cond-cap", evidence_id="ev-cap",
                                 title="能力评估", content={"score": 90})
        self.gov.review_evidence(request_id="ev-cap-review", actor_id="rev-1",
                                 evidence_id="ev-cap", decision="accept")
        self.gov.schedule_tranche(request_id="tr-1", actor_id="off-1", agreement_id="agr-1",
                                  tranche_id="tr-1", sequence_no=1, amount=5000,
                                  condition_group_id="g-t")
        self.gov.deposit_escrow(request_id="dep-1", actor_id="inv-op",
                                tranche_id="tr-1", amount=5000)
        self.gov.confirm_disbursement(request_id="cb-1", actor_id="off-1",
                                      tranche_id="tr-1", callback_reference="pay-1")
        recon = self.gov.reconcile(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual(5000, recon["escrow"]["total_disbursed"])

    def test_deposit_requires_investor_and_exact_amount(self):
        self._agreement()
        self._commitments()
        self.gov.create_condition_group(
            request_id="group-t", actor_id="off-1", agreement_id="agr-1", group_id="g-t",
            title="拨付条件", effect_type="release_tranche", effect_target="tr-1",
            conditions=[{"condition_id": "cond-cap", "commitment_id": "c-loc",
                         "description": "属地能力形成", "evidence_due_at": None}])
        self.gov.schedule_tranche(request_id="tr-1", actor_id="off-1", agreement_id="agr-1",
                                  tranche_id="tr-1", sequence_no=1, amount=5000,
                                  condition_group_id="g-t")
        with self.assertRaises(PermissionDenied):
            self.gov.deposit_escrow(request_id="dep-x", actor_id="tech-op",
                                    tranche_id="tr-1", amount=5000)
        with self.assertRaises(ValidationError):
            self.gov.deposit_escrow(request_id="dep-y", actor_id="inv-op",
                                    tranche_id="tr-1", amount=4000)


class EvidenceAndReviewTest(GovernanceTestCase):
    def test_evidence_submission_restricted_to_responsible_party(self):
        self._agreement()
        self._commitments()
        self.gov.create_condition_group(
            request_id="group-1", actor_id="off-1", agreement_id="agr-1", group_id="g-1",
            title="阶段一条件", effect_type="activate_stage", effect_target="1",
            conditions=[{"condition_id": "cond-1", "commitment_id": "c-tech",
                         "description": "平台上线", "evidence_due_at": None}])
        with self.assertRaises(PermissionDenied):
            self.gov.submit_evidence(request_id="ev-x", actor_id="loc-op",
                                     condition_id="cond-1", evidence_id="ev-x",
                                     title="越权提交", content={"a": 1})
        with self.assertRaises(PermissionDenied):
            self.gov.submit_evidence(request_id="ev-y", actor_id="out-op",
                                     condition_id="cond-1", evidence_id="ev-y",
                                     title="外部提交", content={"a": 1})
        with self.assertRaises(PermissionDenied):
            self.gov.submit_evidence(request_id="ev-z", actor_id="aud-1",
                                     condition_id="cond-1", evidence_id="ev-z",
                                     title="监督提交", content={"a": 1})

    def test_review_requires_independent_reviewer(self):
        self._agreement()
        self._commitments()
        self.gov.create_condition_group(
            request_id="group-1", actor_id="off-1", agreement_id="agr-1", group_id="g-1",
            title="阶段一条件", effect_type="activate_stage", effect_target="1",
            conditions=[{"condition_id": "cond-1", "commitment_id": "c-tech",
                         "description": "平台上线", "evidence_due_at": None}])
        self.gov.submit_evidence(request_id="ev-1", actor_id="tech-op", condition_id="cond-1",
                                 evidence_id="ev-1", title="上线报告", content={"ok": True})
        with self.assertRaises(PermissionDenied):
            self.gov.review_evidence(request_id="rv-self", actor_id="tech-rev",
                                     evidence_id="ev-1", decision="accept")
        with self.assertRaises(PermissionDenied):
            self.gov.review_evidence(request_id="rv-op", actor_id="loc-op",
                                     evidence_id="ev-1", decision="accept")
        self.gov.review_evidence(request_id="rv-ok", actor_id="rev-1",
                                 evidence_id="ev-1", decision="accept")
        with self.assertRaises(ConflictError):
            self.gov.review_evidence(request_id="rv-again", actor_id="rev-1",
                                     evidence_id="ev-1", decision="accept")

    def test_late_evidence_flagged_but_obligation_remains(self):
        self._agreement()
        self._commitments()
        self.gov.create_condition_group(
            request_id="group-1", actor_id="off-1", agreement_id="agr-1", group_id="g-1",
            title="阶段一条件", effect_type="activate_stage", effect_target="1",
            conditions=[{"condition_id": "cond-1", "commitment_id": "c-tech",
                         "description": "平台上线",
                         "evidence_due_at": "2026-10-02T00:00:00Z"}])
        self.clock.advance(days=5)
        self.gov.submit_evidence(request_id="ev-1", actor_id="tech-op", condition_id="cond-1",
                                 evidence_id="ev-1", title="迟到报告", content={"ok": True})
        items = self.gov.list_evidence(actor_id="aud-1", agreement_id="agr-1")
        self.assertTrue(items[0]["late"])
        self.gov.review_evidence(request_id="rv-1", actor_id="rev-1",
                                 evidence_id="ev-1", decision="accept")
        commitment = self._commitment("c-tech")
        self.assertEqual("active", commitment["status"])
        self.assertEqual(1000, commitment["remaining_amount"])


class BreachAndAmendmentTest(GovernanceTestCase):
    def test_partial_breach_rectification_and_responsibility_retained(self):
        self._agreement()
        self._commitments()
        self._activate_stage_one()
        self.gov.record_fulfillment(request_id="ful-1", actor_id="tech-op",
                                    commitment_id="c-tech", fulfillment_id="ful-1",
                                    amount=400, note="首批交付")
        self.gov.review_fulfillment(request_id="ful-1-review", actor_id="rev-1",
                                    fulfillment_id="ful-1", decision="approve")
        self.gov.report_breach(request_id="br-1", actor_id="rev-1", commitment_id="c-tech",
                               breach_id="br-1", severity="partial",
                               description="第二批交付逾期")
        self.assertEqual("breached", self._commitment("c-tech")["status"])
        self.gov.create_rectification(request_id="rc-1", actor_id="rev-1", breach_id="br-1",
                                      rectification_id="rc-1", requirement="补交交付物",
                                      due_at="2026-10-20T00:00:00Z")
        self.gov.submit_rectification(request_id="rc-1-submit", actor_id="tech-op",
                                      rectification_id="rc-1")
        self.gov.review_rectification(request_id="rc-1-review", actor_id="rev-1",
                                      rectification_id="rc-1", decision="approve")
        commitment = self._commitment("c-tech")
        self.assertEqual("active", commitment["status"])
        self.assertEqual(400, commitment["fulfilled_amount"])
        self.assertEqual(600, commitment["remaining_amount"])
        breaches = self.gov.list_breaches(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual("resolved", breaches[0]["status"])
        self.assertEqual(600, breaches[0]["unfulfilled_amount"])

    def test_fulfillment_continues_after_breach(self):
        self._agreement()
        self._commitments()
        self._activate_stage_one()
        self.gov.report_breach(request_id="br-1", actor_id="aud-1", commitment_id="c-tech",
                               breach_id="br-1", severity="partial", description="进度落后")
        self.gov.record_fulfillment(request_id="ful-1", actor_id="tech-op",
                                    commitment_id="c-tech", fulfillment_id="ful-1",
                                    amount=100, note="违约期间继续履约")
        self.gov.review_fulfillment(request_id="ful-1-review", actor_id="rev-1",
                                    fulfillment_id="ful-1", decision="approve")
        self.assertEqual(100, self._commitment("c-tech")["fulfilled_amount"])

    def test_scope_reduction_only_touches_unfulfilled_part(self):
        self._agreement()
        self._commitments()
        self._activate_stage_one()
        self.gov.record_fulfillment(request_id="ful-1", actor_id="tech-op",
                                    commitment_id="c-tech", fulfillment_id="ful-1",
                                    amount=400, note="首批交付")
        self.gov.review_fulfillment(request_id="ful-1-review", actor_id="rev-1",
                                    fulfillment_id="ful-1", decision="approve")
        self.gov.reduce_scope(request_id="reduce-1", actor_id="off-1",
                              commitment_id="c-tech", new_target_amount=700,
                              reason="范围调整")
        commitment = self._commitment("c-tech")
        self.assertEqual(700, commitment["target_amount"])
        self.assertEqual(400, commitment["fulfilled_amount"])
        self.assertEqual(300, commitment["remaining_amount"])
        self.assertEqual({"item": "platform"}, commitment["original_terms"])
        with self.assertRaises(ValidationError):
            self.gov.reduce_scope(request_id="reduce-2", actor_id="off-1",
                                  commitment_id="c-tech", new_target_amount=300,
                                  reason="低于已兑现")
        with self.assertRaises(ValidationError):
            self.gov.reduce_scope(request_id="reduce-3", actor_id="off-1",
                                  commitment_id="c-tech", new_target_amount=800,
                                  reason="不是缩减")

    def test_reduction_to_fulfilled_marks_commitment_fulfilled(self):
        self._agreement()
        self._commitments()
        self._activate_stage_one()
        self.gov.record_fulfillment(request_id="ful-1", actor_id="tech-op",
                                    commitment_id="c-tech", fulfillment_id="ful-1",
                                    amount=400, note="首批交付")
        self.gov.review_fulfillment(request_id="ful-1-review", actor_id="rev-1",
                                    fulfillment_id="ful-1", decision="approve")
        self.gov.reduce_scope(request_id="reduce-1", actor_id="off-1",
                              commitment_id="c-tech", new_target_amount=400,
                              reason="剩余部分取消")
        self.assertEqual("fulfilled", self._commitment("c-tech")["status"])

    def test_party_replacement_transfers_only_unfulfilled(self):
        self._agreement()
        self._commitments()
        self._activate_stage_one()
        self.gov.record_fulfillment(request_id="ful-1", actor_id="tech-op",
                                    commitment_id="c-tech", fulfillment_id="ful-1",
                                    amount=400, note="首批交付")
        self.gov.review_fulfillment(request_id="ful-1-review", actor_id="rev-1",
                                    fulfillment_id="ful-1", decision="approve")
        self.gov.report_breach(request_id="br-1", actor_id="rev-1", commitment_id="c-tech",
                               breach_id="br-1", severity="partial", description="交付逾期")
        self.domain.register_organization(request_id="reg-org-tech2", actor_id="off-1",
                                          organization_id="org-tech2", name="接替技术方")
        self.domain.register_actor(request_id="actor-tech2-op", actor_id="off-1",
                                   new_actor_id="tech2-op", display_name="接替方代表",
                                   role="operator", organization_id="org-tech2")
        self.gov.replace_party(request_id="replace-1", actor_id="off-1", agreement_id="agr-1",
                               outgoing_party_id="p-tech", incoming_party_id="p-tech2",
                               incoming_organization_id="org-tech2", reason="原技术方退出")
        commitment = self._commitment("c-tech")
        self.assertEqual("p-tech2", commitment["responsible_party_id"])
        self.assertEqual(400, commitment["fulfilled_amount"])
        self.assertEqual(600, commitment["remaining_amount"])
        breaches = self.gov.list_breaches(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual("open", breaches[0]["status"])
        view = self.gov.get_agreement_view(actor_id="aud-1", agreement_id="agr-1")
        parties = {party["party_id"]: party["status"] for party in view["parties"]}
        self.assertEqual("replaced", parties["p-tech"])
        self.assertEqual("active", parties["p-tech2"])
        self.gov.record_fulfillment(request_id="ful-2", actor_id="tech2-op",
                                    commitment_id="c-tech", fulfillment_id="ful-2",
                                    amount=600, note="接替方完成剩余交付")
        self.gov.review_fulfillment(request_id="ful-2-review", actor_id="rev-1",
                                    fulfillment_id="ful-2", decision="approve")
        self.assertEqual("fulfilled", self._commitment("c-tech")["status"])


class OutcomeAndDisputeTest(GovernanceTestCase):
    def test_shared_outcome_cannot_be_double_claimed(self):
        self._agreement()
        self.gov.register_outcome(request_id="out-1", actor_id="loc-op", agreement_id="agr-1",
                                  outcome_id="out-1", outcome_key="joint-result",
                                  title="共享成果", beneficiary_group="中小企业",
                                  shared=True)
        self.gov.claim_outcome(request_id="claim-a", actor_id="loc-op",
                               outcome_id="out-1", project_key="proj-alpha")
        with self.assertRaises(ConflictError):
            self.gov.claim_outcome(request_id="claim-b", actor_id="tech-op",
                                   outcome_id="out-1", project_key="proj-beta")
        again = self.gov.claim_outcome(request_id="claim-a2", actor_id="loc-op",
                                       outcome_id="out-1", project_key="proj-alpha")
        self.assertEqual("out-1", again.resource_id)

    def test_document_kinds_are_separated(self):
        self._agreement()
        self.gov.submit_document(request_id="draft-1", actor_id="tech-op",
                                 agreement_id="agr-1", document_id="draft-1",
                                 kind="negotiation_draft", title="谈判稿一",
                                 content={"v": 1})
        self.gov.submit_document(request_id="draft-2", actor_id="tech-op",
                                 agreement_id="agr-1", document_id="draft-2",
                                 kind="negotiation_draft", title="谈判稿二",
                                 content={"v": 2}, supersedes="draft-1")
        with self.assertRaises(PermissionDenied):
            self.gov.submit_document(request_id="formal-x", actor_id="tech-op",
                                     agreement_id="agr-1", document_id="formal-x",
                                     kind="formal_commitment", title="越权正式承诺",
                                     content={"v": 1})
        self.gov.submit_document(request_id="formal-1", actor_id="off-1",
                                 agreement_id="agr-1", document_id="formal-1",
                                 kind="formal_commitment", title="正式承诺书",
                                 content={"signed": True})
        drafts = self.gov.list_documents(actor_id="aud-1", agreement_id="agr-1",
                                         kind="negotiation_draft")
        status = {doc["document_id"]: doc["status"] for doc in drafts}
        self.assertEqual("superseded", status["draft-1"])
        self.assertEqual("proposed", status["draft-2"])
        formal = self.gov.list_documents(actor_id="aud-1", agreement_id="agr-1",
                                         kind="formal_commitment")
        self.assertEqual(1, len(formal))

    def test_dispute_conclusion_recorded_as_ruling_document(self):
        self._agreement()
        self._commitments()
        self.gov.file_dispute(request_id="dp-1", actor_id="loc-op", agreement_id="agr-1",
                              dispute_id="dp-1", description="权属分歧",
                              commitment_id="c-tech")
        recon = self.gov.reconcile(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual(1, recon["open_disputes"])
        self.gov.conclude_dispute(request_id="dp-1-end", actor_id="rev-1", dispute_id="dp-1",
                                  document_id="ruling-1", title="争议结论",
                                  ruling={"decision": "共同所有"})
        recon = self.gov.reconcile(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual(0, recon["open_disputes"])
        rulings = self.gov.list_documents(actor_id="aud-1", agreement_id="agr-1",
                                          kind="dispute_ruling")
        self.assertEqual("争议结论", rulings[0]["title"])
        with self.assertRaises(ConflictError):
            self.gov.conclude_dispute(request_id="dp-1-again", actor_id="rev-1",
                                      dispute_id="dp-1", document_id="ruling-2",
                                      title="重复结论", ruling={"decision": "x"})


class SupervisionTest(GovernanceTestCase):
    def test_visibility_scope_restricts_outside_operators(self):
        self._agreement()
        with self.assertRaises(PermissionDenied):
            self.gov.get_agreement_view(actor_id="out-op", agreement_id="agr-1")
        with self.assertRaises(PermissionDenied):
            self.gov.list_commitments(actor_id="out-op", agreement_id="agr-1")
        view = self.gov.get_agreement_view(actor_id="tech-op", agreement_id="agr-1")
        self.assertEqual("agr-1", view["agreement_id"])
        recon = self.gov.reconcile(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual("agr-1", recon["agreement_id"])
        with self.assertRaises(PermissionDenied):
            self.gov.reconcile(actor_id="tech-op", agreement_id="agr-1")

    def test_reconstruction_at_historical_points(self):
        self._agreement()
        self._commitments()
        self._activate_stage_one()
        self.gov.register_outcome(request_id="out-1", actor_id="loc-op", agreement_id="agr-1",
                                  outcome_id="out-1", outcome_key="joint-result",
                                  title="共享成果", beneficiary_group="中小企业",
                                  shared=True)
        self.gov.claim_outcome(request_id="claim-a", actor_id="loc-op",
                               outcome_id="out-1", project_key="proj-alpha")
        midpoint = self.clock.now().isoformat().replace("+00:00", "Z")
        self.clock.advance(days=1)
        self.domain.register_organization(request_id="reg-org-tech2", actor_id="off-1",
                                          organization_id="org-tech2", name="接替技术方")
        self.gov.replace_party(request_id="replace-1", actor_id="off-1", agreement_id="agr-1",
                               outgoing_party_id="p-tech", incoming_party_id="p-tech2",
                               incoming_organization_id="org-tech2", reason="退出")
        before = self.gov.reconstruct(actor_id="aud-1", agreement_id="agr-1", at=midpoint)
        after = self.gov.reconstruct(actor_id="aud-1", agreement_id="agr-1",
                                     at=self.clock.now().isoformat().replace("+00:00", "Z"))
        before_map = {c["commitment_id"]: c for c in before["commitments"]}
        after_map = {c["commitment_id"]: c for c in after["commitments"]}
        self.assertEqual("p-tech", before_map["c-tech"]["responsible_party_id"])
        self.assertEqual("p-tech2", after_map["c-tech"]["responsible_party_id"])
        self.assertEqual("proj-alpha", before["outcomes"][0]["claimed_by_project"])
        self.assertEqual("active", before_map["c-tech"]["status"])
        self.assertEqual("active", before["status"])

    def test_reconstruction_requires_supervisor(self):
        self._agreement()
        with self.assertRaises(PermissionDenied):
            self.gov.reconstruct(actor_id="tech-op", agreement_id="agr-1",
                                 at="2026-10-01T09:00:00Z")


class IdempotencyTest(GovernanceTestCase):
    def test_write_replay_returns_original_receipt(self):
        self._agreement()
        first = self.gov.create_commitment(
            request_id="c-1", actor_id="off-1", agreement_id="agr-1", commitment_id="c-1",
            category="contribution", title="投入", stage=1, responsible_party_id="p-tech",
            target_amount=10, terms={})
        replay = self.gov.create_commitment(
            request_id="c-1", actor_id="off-1", agreement_id="agr-1", commitment_id="c-1",
            category="contribution", title="投入", stage=1, responsible_party_id="p-tech",
            target_amount=10, terms={})
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        items = self.gov.list_commitments(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual(1, len(items))

    def test_request_id_rejects_changed_payload(self):
        self._agreement()
        self.gov.create_commitment(
            request_id="c-1", actor_id="off-1", agreement_id="agr-1", commitment_id="c-1",
            category="contribution", title="投入", stage=1, responsible_party_id="p-tech",
            target_amount=10, terms={})
        with self.assertRaises(ConflictError):
            self.gov.create_commitment(
                request_id="c-1", actor_id="off-1", agreement_id="agr-1", commitment_id="c-2",
                category="contribution", title="投入", stage=1, responsible_party_id="p-tech",
                target_amount=10, terms={})


class ExitTest(GovernanceTestCase):
    def test_exit_blocked_until_obligations_closed(self):
        self._agreement()
        self._commitments()
        self._activate_stage_one()
        self.gov.begin_exit(request_id="exit-1", actor_id="off-1", agreement_id="agr-1")
        with self.assertRaises(ConflictError):
            self.gov.complete_exit(request_id="exit-2", actor_id="off-1", agreement_id="agr-1")
        self.gov.record_fulfillment(request_id="ful-1", actor_id="tech-op",
                                    commitment_id="c-tech", fulfillment_id="ful-1",
                                    amount=1000, note="全部交付")
        self.gov.review_fulfillment(request_id="ful-1-review", actor_id="rev-1",
                                    fulfillment_id="ful-1", decision="approve")
        self.gov.record_fulfillment(request_id="ful-2", actor_id="loc-op",
                                    commitment_id="c-loc", fulfillment_id="ful-2",
                                    amount=100, note="培训完成")
        self.gov.review_fulfillment(request_id="ful-2-review", actor_id="rev-1",
                                    fulfillment_id="ful-2", decision="approve")
        self.gov.complete_exit(request_id="exit-3", actor_id="off-1", agreement_id="agr-1")
        view = self.gov.get_agreement_view(actor_id="aud-1", agreement_id="agr-1")
        self.assertEqual("exited", view["status"])
        with self.assertRaises(ConflictError):
            self.gov.create_commitment(request_id="c-late", actor_id="off-1",
                                       agreement_id="agr-1", commitment_id="c-late",
                                       category="contribution", title="退出后新增",
                                       stage=1, responsible_party_id="p-tech",
                                       target_amount=1, terms={})


class RestartContinuityTest(unittest.TestCase):
    def test_restart_preserves_pending_work_order_and_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.sqlite3"
            clock = MutableClock(BASE)
            database = Database(path)
            domain = DomainService(database, clock)
            gov = GovernanceService(database, domain, clock)
            domain.register_organization(request_id="org-office", actor_id="bootstrap",
                                         organization_id="org-office", name="办公室")
            domain.register_actor(request_id="actor-admin", actor_id="bootstrap",
                                  new_actor_id="off-1", display_name="管理员",
                                  role="admin", organization_id="org-office")
            domain.register_organization(request_id="reg-org-tech", actor_id="off-1",
                                         organization_id="org-tech", name="技术方")
            domain.register_actor(request_id="actor-tech", actor_id="off-1",
                                  new_actor_id="tech-op", display_name="技术方代表",
                                  role="operator", organization_id="org-tech")
            domain.register_actor(request_id="actor-rev", actor_id="off-1",
                                  new_actor_id="rev-1", display_name="复核员",
                                  role="reviewer", organization_id="org-office")
            domain.register_actor(request_id="actor-aud", actor_id="off-1",
                                  new_actor_id="aud-1", display_name="审计员",
                                  role="auditor", organization_id="org-office")
            domain.register_site(request_id="site-1", actor_id="off-1", site_id="site-1",
                                 organization_id="org-office", name="节点",
                                 timezone_name="Africa/Accra")
            gov.create_agreement(request_id="agr-1", actor_id="off-1", site_id="site-1",
                                 agreement_id="agr-1", title="联合方案")
            gov.add_party(request_id="party-tech", actor_id="off-1", agreement_id="agr-1",
                          party_id="p-tech", organization_id="org-tech",
                          party_role="tech_provider")
            gov.submit_document(request_id="formal-1", actor_id="off-1", agreement_id="agr-1",
                                document_id="formal-1", kind="formal_commitment",
                                title="正式承诺书", content={"signed": True})
            gov.create_commitment(request_id="c-1", actor_id="off-1", agreement_id="agr-1",
                                  commitment_id="c-1", category="contribution", title="投入",
                                  stage=1, responsible_party_id="p-tech",
                                  target_amount=100, terms={})
            gov.formalize_commitments(request_id="formalize", actor_id="off-1",
                                      agreement_id="agr-1", document_id="formal-1",
                                      commitment_ids=["c-1"])
            gov.create_condition_group(
                request_id="group-1", actor_id="off-1", agreement_id="agr-1", group_id="g-1",
                title="阶段一", effect_type="activate_stage", effect_target="1",
                conditions=[{"condition_id": "cond-1", "commitment_id": "c-1",
                             "description": "上线", "evidence_due_at": None}])
            gov.submit_evidence(request_id="ev-1", actor_id="tech-op", condition_id="cond-1",
                                evidence_id="ev-1", title="报告", content={"ok": True})
            gov.review_evidence(request_id="ev-1-review", actor_id="rev-1",
                                evidence_id="ev-1", decision="accept")
            gov.report_breach(request_id="br-1", actor_id="rev-1", commitment_id="c-1",
                              breach_id="br-1", severity="partial", description="落后")
            gov.create_rectification(request_id="rc-1", actor_id="rev-1", breach_id="br-1",
                                     rectification_id="rc-1", requirement="整改一",
                                     due_at="2026-10-05T00:00:00Z")
            gov.create_rectification(request_id="rc-2", actor_id="rev-1", breach_id="br-1",
                                     rectification_id="rc-2", requirement="整改二",
                                     due_at="2026-10-06T00:00:00Z")
            before = gov.pending_work(actor_id="aud-1", agreement_id="agr-1")
            self.assertEqual(["rc-1", "rc-2"], [item["id"] for item in before["items"]])
            database.close()

            clock.advance(days=10)
            database2 = Database(path)
            domain2 = DomainService(database2, clock)
            gov2 = GovernanceService(database2, domain2, clock)
            after = gov2.pending_work(actor_id="aud-1", agreement_id="agr-1")
            self.assertEqual([item["id"] for item in before["items"]],
                             [item["id"] for item in after["items"]])
            swept = gov2.sweep_overdue_rectifications(actor_id="off-1", agreement_id="agr-1")
            self.assertEqual(["rc-1", "rc-2"], swept["overdue_rectifications"])
            breaches = gov2.list_breaches(actor_id="aud-1", agreement_id="agr-1")
            self.assertEqual("escalated", breaches[0]["status"])
            with self.assertRaises(ConflictError):
                gov2.submit_rectification(request_id="rc-1-submit", actor_id="tech-op",
                                          rectification_id="rc-1")
            valid, _ = domain2.verify_audit()
            self.assertTrue(valid)
            database2.close()


if __name__ == "__main__":
    unittest.main()
