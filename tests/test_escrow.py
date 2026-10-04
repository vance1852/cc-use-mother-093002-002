"""资金托管、分期拨付与重复回调幂等测试。"""

import unittest

from digital_trade_foundation.errors import PreconditionFailed, UnprocessableState
from tests.fixtures import build_service
from tests.test_commitments import condition_id, stage_id


def prepare_funded_stage(service, *, amount=100000, tranche=40000, deposit=True):
    """封存分期资金承诺、编排首期阶段并入金，返回 (commitment, stage, condition)。"""

    service.draft_commitment(
        request_id="cf-draft", actor_id="investor", project_id="p1", commitment_id="cf",
        commitment_type="escrow_funding", provider_organization_id="o-investor",
        title="分期资金", terms={"tranches": 2}, amount_minor=amount, currency="USD")
    service.seal_commitment(request_id="cf-seal", actor_id="investor", commitment_id="cf")
    service.add_stage(request_id="st1", actor_id="office", commitment_id="cf", name="首期",
                      sequence=1, disburse_amount_minor=tranche)
    stage = stage_id(service, "cf", 1)
    service.add_condition(request_id="cd1", actor_id="office", stage_id=stage,
                          label="属地能力形成", sequence=1)
    condition = condition_id(service, stage, 1)
    if deposit:
        service.deposit_escrow(request_id="dep1", actor_id="investor", project_id="p1",
                               amount_minor=amount, currency="USD", reference="wire-1")
    return "cf", stage, condition


class EscrowTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_deposit_then_disburse_only_after_effective_stage(self):
        _, stage, condition = prepare_funded_stage(self.service)
        # 阶段未生效不能拨付（属地能力未形成前不得释放资金）
        with self.assertRaises(PreconditionFailed):
            self.service.disburse_stage(request_id="pay-early", actor_id="office", stage_id=stage)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="local",
                                          condition_id=condition, reference="r1",
                                          payload={"team": 30})
        self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id="eff1", actor_id="office", stage_id=stage)
        paid = self.service.disburse_stage(request_id="pay1", actor_id="office", stage_id=stage)
        self.assertFalse(paid.replayed)
        escrow = self.service.escrow(actor_id="auditor", project_id="p1")
        self.assertEqual(100000, escrow.deposited_total)
        self.assertEqual(40000, escrow.disbursed_total)
        self.assertEqual(60000, escrow.balance)

    def test_duplicate_disburse_callback_does_not_change_balance(self):
        _, stage, condition = prepare_funded_stage(self.service)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="local",
                                          condition_id=condition, reference="r1",
                                          payload={"team": 30})
        self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id="eff1", actor_id="office", stage_id=stage)
        first = self.service.disburse_stage(request_id="pay1", actor_id="office", stage_id=stage)
        # 完全相同的 request_id：幂等回放
        replay_same = self.service.disburse_stage(request_id="pay1", actor_id="office",
                                                  stage_id=stage)
        # 支付通道以新 request_id 重复回调：同样不得再次扣款
        replay_new = self.service.disburse_stage(request_id="pay1-dup", actor_id="office",
                                                 stage_id=stage)
        self.assertTrue(replay_same.replayed)
        self.assertTrue(replay_new.replayed)
        self.assertEqual(first.resource_id, replay_new.resource_id)
        escrow = self.service.escrow(actor_id="auditor", project_id="p1")
        self.assertEqual(40000, escrow.disbursed_total)
        self.assertEqual(60000, escrow.balance)
        payments = self.service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM disbursements WHERE stage_id=?", (stage,)).fetchone()["c"]
        self.assertEqual(1, payments)

    def test_insufficient_escrow_balance_blocks_disbursement(self):
        _, stage, condition = prepare_funded_stage(self.service, amount=30000, deposit=True)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="local",
                                          condition_id=condition, reference="r1",
                                          payload={"team": 30})
        self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id="eff1", actor_id="office", stage_id=stage)
        with self.assertRaises(PreconditionFailed):
            self.service.disburse_stage(request_id="pay-broke", actor_id="office", stage_id=stage)
        escrow = self.service.escrow(actor_id="auditor", project_id="p1")
        self.assertEqual(0, escrow.disbursed_total)
        self.assertEqual(30000, escrow.balance)

    def test_disbursement_cannot_exceed_adjusted_outstanding(self):
        _, stage, condition = prepare_funded_stage(self.service, amount=100000, tranche=40000)
        # 先核减未兑现部分到 30000，再尝试拨付 40000 应被阻止
        self.service.reduce_scope(request_id="rs1", actor_id="office", commitment_id="cf",
                                  amount_delta_minor=-70000, detail={"reason": "大幅缩范围"})
        ev = self.service.submit_evidence(request_id="ev1", actor_id="local",
                                          condition_id=condition, reference="r1",
                                          payload={"team": 30})
        self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id="eff1", actor_id="office", stage_id=stage)
        with self.assertRaises(PreconditionFailed):
            self.service.disburse_stage(request_id="pay-over", actor_id="office", stage_id=stage)

    def test_cannot_disburse_before_any_deposit(self):
        _, stage, condition = prepare_funded_stage(self.service, deposit=False)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="local",
                                          condition_id=condition, reference="r1",
                                          payload={"team": 30})
        self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id="eff1", actor_id="office", stage_id=stage)
        with self.assertRaises(PreconditionFailed):
            self.service.disburse_stage(request_id="pay-nofund", actor_id="office",
                                        stage_id=stage)

    def test_office_only_can_disburse(self):
        _, stage, condition = prepare_funded_stage(self.service)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="local",
                                          condition_id=condition, reference="r1",
                                          payload={"team": 30})
        self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id="eff1", actor_id="office", stage_id=stage)
        from digital_trade_foundation.errors import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.disburse_stage(request_id="pay-investor", actor_id="investor",
                                        stage_id=stage)

    def test_zero_amount_stage_cannot_disburse(self):
        self.service.draft_commitment(
            request_id="ct-d", actor_id="tech", project_id="p1", commitment_id="ct",
            commitment_type="party_input", provider_organization_id="o-tech",
            title="技术投入", terms={"v": 1})
        self.service.seal_commitment(request_id="ct-s", actor_id="tech", commitment_id="ct")
        self.service.add_stage(request_id="st", actor_id="office", commitment_id="ct",
                               name="验收", sequence=1)
        stage = stage_id(self.service, "ct", 1)
        self.service.add_condition(request_id="cd", actor_id="office", stage_id=stage,
                                   label="交付物", sequence=1)
        condition = condition_id(self.service, stage, 1)
        ev = self.service.submit_evidence(request_id="ev", actor_id="tech",
                                          condition_id=condition, reference="r",
                                          payload={"ok": True})
        self.service.decide_review(request_id="rv", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id="eff", actor_id="office", stage_id=stage)
        with self.assertRaises(UnprocessableState):
            self.service.disburse_stage(request_id="pay-zero", actor_id="office", stage_id=stage)


if __name__ == "__main__":
    unittest.main()
