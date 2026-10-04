"""责任不可抹除、迟到证据、局部违约、范围缩减、合作方替换与历史还原测试。"""

import unittest
from datetime import datetime, timezone

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.errors import UnprocessableState
from tests.fixtures import build_service
from tests.test_commitments import condition_id, stage_id


class LateEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def _commitment_with_overdue_condition(self):
        self.service.draft_commitment(
            request_id="d1", actor_id="tech", project_id="p1", commitment_id="c1",
            commitment_type="localization_target", provider_organization_id="o-tech",
            title="属地能力", terms={"kpi": 1})
        self.service.seal_commitment(request_id="s1", actor_id="tech", commitment_id="c1")
        self.service.add_stage(request_id="st1", actor_id="office", commitment_id="c1",
                               name="阶段", sequence=1, due_at="2026-09-30T00:00:00Z")
        stage = stage_id(self.service, "c1", 1)
        self.service.add_condition(request_id="cd1", actor_id="office", stage_id=stage,
                                   label="能力验收", sequence=1, due_at="2026-09-20T00:00:00Z")
        return stage, condition_id(self.service, stage, 1)

    def test_late_evidence_recorded_but_responsibility_and_review_continue(self):
        stage, condition = self._commitment_with_overdue_condition()
        receipt = self.service.submit_evidence(
            request_id="ev1", actor_id="tech", condition_id=condition, reference="late-doc",
            payload={"report": "ok"})
        view = self.service.get_condition(actor_id="office", condition_id=condition)
        self.assertTrue(view.evidences[0]["late"])
        # 迟到不抹掉责任：仍可独立复核通过并使阶段生效
        self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                   evidence_id=receipt.resource_id, approved=True)
        self.service.effect_stage(request_id="eff1", actor_id="office", stage_id=stage)
        stage_row = self.service.database.connection.execute(
            "SELECT status FROM commitment_stages WHERE stage_id=?", (stage,)).fetchone()
        self.assertEqual("effective", stage_row["status"])
        responsibility = self.service.responsibility(actor_id="auditor", commitment_id="c1")
        self.assertTrue(any(a["kind"] == "late_evidence" for a in responsibility.adjustments))
        self.assertEqual("o-tech", responsibility.provider_organization_id)


class ResponsibilityAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.service.draft_commitment(
            request_id="d1", actor_id="investor", project_id="p1", commitment_id="cf",
            commitment_type="escrow_funding", provider_organization_id="o-investor",
            title="资金承诺", terms={"t": 1}, amount_minor=100000, currency="USD")
        self.service.seal_commitment(request_id="s1", actor_id="investor", commitment_id="cf")

    def tearDown(self):
        self.service.database.close()

    def _disburse(self, sequence, amount):
        self.service.add_stage(request_id=f"st{sequence}", actor_id="office",
                               commitment_id="cf", name=f"阶段{sequence}", sequence=sequence,
                               disburse_amount_minor=amount)
        stage = stage_id(self.service, "cf", sequence)
        self.service.add_condition(request_id=f"cd{sequence}", actor_id="office",
                                   stage_id=stage, label=f"条件{sequence}", sequence=1)
        condition = condition_id(self.service, stage, 1)
        ev = self.service.submit_evidence(request_id=f"ev{sequence}", actor_id="local",
                                          condition_id=condition, reference="r",
                                          payload={"ok": True})
        self.service.decide_review(request_id=f"rv{sequence}", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        self.service.effect_stage(request_id=f"eff{sequence}", actor_id="office", stage_id=stage)
        self.service.deposit_escrow(request_id=f"dep{sequence}", actor_id="investor",
                                    project_id="p1", amount_minor=amount, currency="USD")
        self.service.disburse_stage(request_id=f"pay{sequence}", actor_id="office",
                                    stage_id=stage)

    def test_partial_breach_marks_breached_but_keeps_original_amount(self):
        self.service.record_partial_breach(request_id="br1", actor_id="office",
                                           commitment_id="cf", detail={"reason": "培训缺口"})
        view = self.service.responsibility(actor_id="auditor", commitment_id="cf")
        self.assertEqual("breached", view.status)
        self.assertEqual(100000, view.original_amount_minor)
        self.assertEqual(100000, view.outstanding_minor)
        self.assertTrue(any(a["kind"] == "partial_breach" for a in view.adjustments))

    def test_scope_reduction_changes_only_unfulfilled_part(self):
        self._disburse(1, 40000)
        # 已拨 40000，缩减 30000：未兑现 30000；已拨金额不受影响
        self.service.reduce_scope(request_id="rs1", actor_id="office", commitment_id="cf",
                                  amount_delta_minor=-30000, detail={"reason": "缩范围"})
        view = self.service.responsibility(actor_id="auditor", commitment_id="cf")
        self.assertEqual(40000, view.disbursed_minor)
        self.assertEqual(30000, view.outstanding_minor)

    def test_scope_reduction_cannot_roll_back_already_fulfilled_amount(self):
        self._disburse(1, 40000)
        with self.assertRaises(UnprocessableState):
            self.service.reduce_scope(request_id="rs-bad", actor_id="office",
                                      commitment_id="cf", amount_delta_minor=-70000,
                                      detail={"reason": "试图回滚已拨"})
        view = self.service.responsibility(actor_id="auditor", commitment_id="cf")
        self.assertEqual(60000, view.outstanding_minor)

    def test_positive_scope_reduction_rejected(self):
        from digital_trade_foundation.errors import ValidationError
        with self.assertRaises(ValidationError):
            self.service.reduce_scope(request_id="rs-pos", actor_id="office",
                                      commitment_id="cf", amount_delta_minor=1000)


class PartyReplacementTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def _tech_commitment(self, commitment_id):
        self.service.draft_commitment(
            request_id=f"d-{commitment_id}", actor_id="tech", project_id="p1",
            commitment_id=commitment_id, commitment_type="party_input",
            provider_organization_id="o-tech", title=commitment_id, terms={"v": 1})
        self.service.seal_commitment(request_id=f"s-{commitment_id}", actor_id="tech",
                                     commitment_id=commitment_id)

    def _register_new_tech(self):
        self.service.register_organization(request_id="org-newtech", actor_id="admin",
                                           organization_id="o-newtech", name="新技术方")

    def test_open_commitments_transfer_but_original_party_history_remains(self):
        self._tech_commitment("c-open")
        self._register_new_tech()
        replacement = self.service.replace_project_party(
            request_id="rep1", actor_id="office", project_id="p1",
            old_organization_id="o-tech", new_organization_id="o-newtech", note="轮换")
        self.assertEqual("party_replacement", replacement.resource_type)
        transferred = self._transferred()
        # 有一条尚未兑现的承诺被转移
        self.assertTrue(any(t["commitment_id"] == "c-open" for t in transferred))
        current = self.service.responsibility(actor_id="auditor", commitment_id="c-open")
        self.assertEqual("o-tech", current.provider_organization_id)  # 原始责任方保留
        self.assertEqual("o-newtech", current.replaced_by)             # 未兑现部分归新方
        # 替换发生之前的历史时点，责任仍属于原技术方
        before = self.service.responsibility(actor_id="auditor", commitment_id="c-open",
                                             at="2026-09-30T00:00:00Z")
        self.assertIsNone(before.replaced_by)

    def _transferred(self):
        return [
            {"commitment_id": r["commitment_id"], "portion_minor": r["portion_minor"]}
            for r in self.service.database.connection.execute(
                "SELECT c.commitment_id AS commitment_id, a.portion_minor AS portion_minor "
                "FROM responsibility_adjustments a JOIN commitments c "
                "ON c.commitment_id=a.commitment_id WHERE a.kind='party_replacement'").fetchall()
        ]

    def test_fulfilled_commitment_is_not_transferred(self):
        self._tech_commitment("c-done")
        # 将承诺直接置为 fulfilled（终态），替换时不应转移
        self.service.database.connection.execute(
            "UPDATE commitments SET status='fulfilled' WHERE commitment_id='c-done'")
        self._register_new_tech()
        self.service.replace_project_party(
            request_id="rep2", actor_id="office", project_id="p1",
            old_organization_id="o-tech", new_organization_id="o-newtech")
        adjustments = self.service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM responsibility_adjustments WHERE kind='party_replacement' "
            "AND commitment_id='c-done'").fetchone()["c"]
        self.assertEqual(0, adjustments)
        done = self.service.responsibility(actor_id="auditor", commitment_id="c-done")
        self.assertIsNone(done.replaced_by)

    def test_clock_advances_snapshots_point_in_time(self):
        self._tech_commitment("c-open")
        self._register_new_tech()
        self.service.clock = FixedClock(datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc))
        self.service.replace_project_party(
            request_id="rep3", actor_id="office", project_id="p1",
            old_organization_id="o-tech", new_organization_id="o-newtech")
        # 替换时刻之前取历史还原，应仍是原技术方负责
        view = self.service.responsibility(actor_id="auditor", commitment_id="c-open",
                                           at="2026-10-15T00:00:00Z")
        self.assertIsNone(view.replaced_by)
        after = self.service.responsibility(actor_id="auditor", commitment_id="c-open",
                                            at="2026-11-02T00:00:00Z")
        self.assertEqual("o-newtech", after.replaced_by)


if __name__ == "__main__":
    unittest.main()
