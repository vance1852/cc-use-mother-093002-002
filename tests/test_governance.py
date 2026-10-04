"""共享成果唯一认领、争议结论、可见范围与退出闸门测试。"""

import unittest

from digital_trade_foundation.errors import (
    ConflictError,
    PermissionDenied,
    PreconditionFailed,
    UnprocessableState,
)
from tests.fixtures import build_service
from tests.test_commitments import condition_id, stage_id
from tests.test_escrow import prepare_funded_stage


class OutcomeClaimTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def _register_outcome(self):
        return self.service.register_outcome(
            request_id="out1", actor_id="reviewer", title="共享培训数据集",
            measure_unit="people", measure_value=200)

    def test_outcome_can_be_claimed_once_and_replay_is_idempotent(self):
        outcome = self._register_outcome()
        first = self.service.claim_outcome(request_id="clm1", actor_id="tech",
                                           outcome_id=outcome.resource_id, project_id="p1")
        self.assertFalse(first.replayed)
        replay = self.service.claim_outcome(request_id="clm1", actor_id="tech",
                                            outcome_id=outcome.resource_id, project_id="p1")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        claims = self.service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM outcome_claims").fetchone()["c"]
        self.assertEqual(1, claims)

    def test_same_outcome_cannot_be_claimed_by_another_project(self):
        outcome = self._register_outcome()
        self.service.claim_outcome(request_id="clm1", actor_id="tech",
                                   outcome_id=outcome.resource_id, project_id="p1")
        # 第二个项目（由管理员创建并把办公室加入）
        self.service.create_project(request_id="p2", actor_id="office",
                                    project_id="p2", name="其他项目")
        with self.assertRaises(ConflictError):
            self.service.claim_outcome(request_id="clm2", actor_id="office",
                                       outcome_id=outcome.resource_id, project_id="p2")
        # 归属没有改变
        owner = self.service.database.connection.execute(
            "SELECT project_id FROM outcome_claims WHERE outcome_id=?",
            (outcome.resource_id,)).fetchone()["project_id"]
        self.assertEqual("p1", owner)


class DisputeTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.service.draft_commitment(
            request_id="d1", actor_id="investor", project_id="p1", commitment_id="cf",
            commitment_type="escrow_funding", provider_organization_id="o-investor",
            title="资金", terms={"t": 1}, amount_minor=100000, currency="USD")
        self.service.seal_commitment(request_id="s1", actor_id="investor", commitment_id="cf")

    def tearDown(self):
        self.service.database.close()

    def test_conclusion_is_separate_and_relief_only_touches_outstanding(self):
        dispute = self.service.open_dispute(
            request_id="dsp1", actor_id="tech", project_id="p1", commitment_id="cf",
            title="金额争议", detail={"point": "金额"})
        self.service.conclude_dispute(
            request_id="dsc1", actor_id="office", dispute_id=dispute.resource_id,
            conclusion={"ruling": "减免1万"}, relief_delta_minor=-10000)
        view = self.service.get_dispute(actor_id="auditor", dispute_id=dispute.resource_id)
        self.assertEqual("concluded", view.status)
        self.assertEqual("减免1万", view.conclusion["ruling"])
        responsibility = self.service.responsibility(actor_id="auditor", commitment_id="cf")
        self.assertEqual(90000, responsibility.outstanding_minor)
        self.assertEqual(100000, responsibility.original_amount_minor)

    def test_cannot_conclude_twice(self):
        dispute = self.service.open_dispute(
            request_id="dsp2", actor_id="tech", project_id="p1", commitment_id="cf",
            title="争议2", detail={})
        self.service.conclude_dispute(request_id="dsc2", actor_id="office",
                                      dispute_id=dispute.resource_id, conclusion={"r": "x"})
        with self.assertRaises(UnprocessableState):
            self.service.conclude_dispute(request_id="dsc2b", actor_id="office",
                                          dispute_id=dispute.resource_id, conclusion={"r": "y"})

    def test_relief_cannot_exceed_outstanding(self):
        with self.assertRaises(UnprocessableState):
            dispute = self.service.open_dispute(
                request_id="dsp3", actor_id="tech", project_id="p1", commitment_id="cf",
                title="争议3", detail={})
            self.service.conclude_dispute(
                request_id="dsc3", actor_id="office", dispute_id=dispute.resource_id,
                conclusion={"r": "超额减免"}, relief_delta_minor=-200000)


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_party_cannot_see_project_it_did_not_join(self):
        self.service.create_project(request_id="p2", actor_id="office",
                                    project_id="p2", name="其他项目")
        with self.assertRaises(PermissionDenied):
            self.service.get_project(actor_id="tech", project_id="p2")
        # 监督人员可见全部
        project = self.service.get_project(actor_id="auditor", project_id="p2")
        self.assertEqual("p2", project.project_id)

    def test_replaced_party_retains_read_visibility_for_history(self):
        self.service.draft_commitment(
            request_id="d1", actor_id="tech", project_id="p1", commitment_id="c1",
            commitment_type="party_input", provider_organization_id="o-tech",
            title="投入", terms={"v": 1})
        self.service.seal_commitment(request_id="s1", actor_id="tech", commitment_id="c1")
        self.service.register_organization(request_id="org-new", actor_id="admin",
                                           organization_id="o-new", name="新技术方")
        self.service.replace_project_party(request_id="rep1", actor_id="office",
                                           project_id="p1", old_organization_id="o-tech",
                                           new_organization_id="o-new")
        # 被替换方仍可读取其历史责任
        view = self.service.responsibility(actor_id="tech", commitment_id="c1")
        self.assertEqual("o-tech", view.provider_organization_id)


class IndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_office_and_provider_cannot_review_evidence(self):
        _, stage, condition = prepare_funded_stage(self.service)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="local",
                                          condition_id=condition, reference="r1",
                                          payload={"team": 30})
        with self.assertRaises(PermissionDenied):
            self.service.decide_review(request_id="rv-office", actor_id="office",
                                       evidence_id=ev.resource_id, approved=True)
        with self.assertRaises(PermissionDenied):
            self.service.decide_review(request_id="rv-local", actor_id="local",
                                       evidence_id=ev.resource_id, approved=True)

    def test_non_independent_reviewer_organization_rejected(self):
        # reviewer 角色但所属组织不是独立复核所
        self.service.register_actor(request_id="actor-badrev", actor_id="admin",
                                    new_actor_id="badrev", display_name="冒牌复核",
                                    role="reviewer", organization_id="o-tech")
        _, stage, condition = prepare_funded_stage(self.service)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="local",
                                          condition_id=condition, reference="r1",
                                          payload={"team": 30})
        with self.assertRaises(PermissionDenied):
            self.service.decide_review(request_id="rv-bad", actor_id="badrev",
                                       evidence_id=ev.resource_id, approved=True)


class ExitGateTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def _accept_and_pay(self, stage, req):
        self.service.add_condition(request_id=f"cd-{req}", actor_id="office", stage_id=stage,
                                   label="条件", sequence=1)
        condition = condition_id(self.service, stage, 1)
        ev = self.service.submit_evidence(request_id=f"ev-{req}", actor_id="local",
                                          condition_id=condition, reference="r",
                                          payload={"ok": True})
        self.service.decide_review(request_id=f"rv-{req}", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id=f"eff-{req}", actor_id="office", stage_id=stage)
        self.service.disburse_stage(request_id=f"pay-{req}", actor_id="office", stage_id=stage)

    def test_open_funding_commitment_blocks_close_then_exit_responsibility_survives(self):
        _, stage, _ = prepare_funded_stage(self.service, amount=40000, tranche=40000)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="local",
                                          condition_id=condition_id(self.service, stage, 1),
                                          reference="r1", payload={"team": 30})
        self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id="eff1", actor_id="office", stage_id=stage)
        self.service.disburse_stage(request_id="pay1", actor_id="office", stage_id=stage)

        # 退出责任承诺：项目关闭后继续有效
        self.service.draft_commitment(
            request_id="ce-d", actor_id="local", project_id="p1", commitment_id="ce",
            commitment_type="exit_responsibility", provider_organization_id="o-local",
            title="退出后两年运维", terms={"years": 2})
        self.service.seal_commitment(request_id="ce-s", actor_id="local", commitment_id="ce")

        # 资金已全额拨付、无开放争议，允许关闭
        receipt = self.service.close_project(request_id="close1", actor_id="office",
                                             project_id="p1")
        self.assertEqual("p1", receipt.resource_id)
        project = self.service.get_project(actor_id="auditor", project_id="p1")
        self.assertEqual("closed", project.status)
        exit_view = self.service.responsibility(actor_id="auditor", commitment_id="ce")
        self.assertNotEqual("closed", exit_view.status)

    def test_unfulfilled_funding_commitment_blocks_close(self):
        prepare_funded_stage(self.service, amount=100000, tranche=40000)
        with self.assertRaises(PreconditionFailed) as caught:
            self.service.close_project(request_id="close-early", actor_id="office",
                                       project_id="p1")
        pending = caught.exception.payload["pending_commitments"]
        self.assertTrue(any(p["commitment_id"] == "cf" for p in pending))

    def test_open_dispute_blocks_close(self):
        self.service.open_dispute(request_id="dsp1", actor_id="tech", project_id="p1",
                                  commitment_id=None, title="未决争议", detail={})
        with self.assertRaises(PreconditionFailed):
            self.service.close_project(request_id="close-dsp", actor_id="office",
                                       project_id="p1")


if __name__ == "__main__":
    unittest.main()
