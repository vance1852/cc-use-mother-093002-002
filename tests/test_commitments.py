"""承诺、谈判稿/正式承诺分离、阶段与条件组核验测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from digital_trade_foundation.commitment_domain import COMMITMENT_TYPES
from digital_trade_foundation.errors import (
    PermissionDenied,
    PreconditionFailed,
    UnprocessableState,
    ValidationError,
)
from tests.fixtures import build_service


def stage_id(service, commitment_id: str, sequence: int) -> str:
    row = service.database.connection.execute(
        "SELECT stage_id FROM commitment_stages WHERE commitment_id=? AND sequence=?",
        (commitment_id, sequence)).fetchone()
    return row["stage_id"]


def condition_id(service, stage: str, sequence: int) -> str:
    row = service.database.connection.execute(
        "SELECT condition_id FROM stage_conditions WHERE stage_id=? AND sequence=?",
        (stage, sequence)).fetchone()
    return row["condition_id"]


class CommitmentLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def _sealed(self, commitment_id="c1", commitment_type="party_input",
                provider="o-tech", actor="tech", amount=None, currency=None,
                request_prefix="c1"):
        kwargs = dict(request_id=f"{request_prefix}-draft", actor_id=actor, project_id="p1",
                      commitment_id=commitment_id, commitment_type=commitment_type,
                      provider_organization_id=provider, title="一项承诺",
                      terms={"v": 1})
        if amount is not None:
            kwargs["amount_minor"] = amount
            kwargs["currency"] = currency
        self.service.draft_commitment(**kwargs)
        self.service.seal_commitment(request_id=f"{request_prefix}-seal", actor_id=actor,
                                     commitment_id=commitment_id)

    def test_all_seven_commitment_types_accepted(self):
        for index, commitment_type in enumerate(sorted(COMMITMENT_TYPES)):
            commitment_id = f"c-{commitment_type}"
            self.service.draft_commitment(
                request_id=f"d-{index}", actor_id="tech", project_id="p1",
                commitment_id=commitment_id, commitment_type=commitment_type,
                provider_organization_id="o-tech", title=commitment_type, terms={"x": index})
            self.service.seal_commitment(request_id=f"s-{index}", actor_id="tech",
                                         commitment_id=commitment_id)
        commitments = self.service.list_commitments(actor_id="auditor", project_id="p1")
        self.assertEqual(len(COMMITMENT_TYPES), len(commitments))
        self.assertTrue(all(c.status == "committed" for c in commitments))

    def test_unknown_commitment_type_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.draft_commitment(
                request_id="bad-type", actor_id="tech", project_id="p1", commitment_id="cx",
                commitment_type="ghost_benefit", provider_organization_id="o-tech",
                title="x", terms={"v": 1})

    def test_negotiation_draft_revisions_kept_separate_from_sealed_terms(self):
        self._sealed()
        drafts = self.service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM commitment_documents WHERE commitment_id='c1' "
            "AND kind='negotiation_draft'").fetchone()["c"]
        sealed = self.service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM commitment_documents WHERE commitment_id='c1' "
            "AND kind='sealed_terms'").fetchone()["c"]
        self.assertEqual(1, drafts)
        self.assertEqual(1, sealed)

    def test_sealed_commitment_cannot_be_redrafted_or_resealed(self):
        self._sealed()
        with self.assertRaises(UnprocessableState):
            self.service.revise_negotiation_draft(request_id="rev-x", actor_id="tech",
                                                  commitment_id="c1", terms={"v": 9})
        with self.assertRaises(UnprocessableState):
            self.service.seal_commitment(request_id="seal-again", actor_id="tech",
                                         commitment_id="c1")

    def test_provider_cannot_draft_for_another_party(self):
        with self.assertRaises(PermissionDenied):
            self.service.draft_commitment(
                request_id="foreign", actor_id="tech", project_id="p1", commitment_id="c9",
                commitment_type="party_input", provider_organization_id="o-local",
                title="代拟", terms={"v": 1})

    def test_non_party_organization_cannot_commit(self):
        # 先注册一个未入项的组织和操作者
        self.service.register_organization(request_id="org-outsider", actor_id="admin",
                                           organization_id="o-outside", name="外部机构")
        self.service.register_actor(request_id="actor-outsider", actor_id="admin",
                                    new_actor_id="outsider", display_name="外部人",
                                    role="operator", organization_id="o-outside")
        with self.assertRaises(PermissionDenied):
            self.service.draft_commitment(
                request_id="outside-draft", actor_id="outsider", project_id="p1",
                commitment_id="c-out", commitment_type="party_input",
                provider_organization_id="o-outside", title="外部", terms={"v": 1})

    def test_stage_requires_entire_condition_group_accepted(self):
        self._sealed(commitment_id="cf", commitment_type="escrow_funding",
                     provider="o-investor", actor="investor", amount=10000, currency="USD",
                     request_prefix="cf")
        self.service.add_stage(request_id="st1", actor_id="office", commitment_id="cf",
                               name="阶段一", sequence=1)
        stage = stage_id(self.service, "cf", 1)
        self.service.add_condition(request_id="cd1", actor_id="office", stage_id=stage,
                                   label="条件1", sequence=1)
        self.service.add_condition(request_id="cd2", actor_id="office", stage_id=stage,
                                   label="条件2", sequence=2)
        # 无证据时不能生效
        with self.assertRaises(PreconditionFailed) as caught:
            self.service.effect_stage(request_id="eff-none", actor_id="office", stage_id=stage)
        self.assertEqual({"pending_conditions": ["条件1", "条件2"]}, caught.exception.payload)
        # 仅有一个条件被接受，仍不能生效
        c1 = condition_id(self.service, stage, 1)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="tech", condition_id=c1,
                                          reference="r1", payload={"ok": True})
        self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                   evidence_id=ev.resource_id, approved=True)
        with self.assertRaises(PreconditionFailed) as caught:
            self.service.effect_stage(request_id="eff-half", actor_id="office", stage_id=stage)
        self.assertEqual(["条件2"], caught.exception.payload["pending_conditions"])

    def test_stage_without_conditions_cannot_become_effective(self):
        self._sealed(commitment_id="ce", commitment_type="risk_assurance",
                     provider="o-local", actor="local", request_prefix="ce")
        self.service.add_stage(request_id="st-empty", actor_id="office", commitment_id="ce",
                               name="空阶段", sequence=1)
        with self.assertRaises(PreconditionFailed):
            self.service.effect_stage(
                request_id="eff-empty", actor_id="office",
                stage_id=stage_id(self.service, "ce", 1))


class ConditionTimingTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_rejected_evidence_creates_remediation_deadline_and_keeps_condition_open(self):
        self.service.draft_commitment(
            request_id="d1", actor_id="tech", project_id="p1", commitment_id="c1",
            commitment_type="localization_target", provider_organization_id="o-tech",
            title="属地团队", terms={"headcount": 20})
        self.service.seal_commitment(request_id="s1", actor_id="tech", commitment_id="c1")
        self.service.add_stage(request_id="st1", actor_id="office", commitment_id="c1",
                               name="阶段", sequence=1)
        stage = stage_id(self.service, "c1", 1)
        self.service.add_condition(request_id="cd1", actor_id="office", stage_id=stage,
                                   label="团队名单", sequence=1)
        condition = condition_id(self.service, stage, 1)
        ev = self.service.submit_evidence(request_id="ev1", actor_id="tech",
                                          condition_id=condition, reference="r1",
                                          payload={"headcount": 5})
        decision = self.service.decide_review(request_id="rv1", actor_id="reviewer",
                                              evidence_id=ev.resource_id, approved=False,
                                              note="人数不足")
        self.assertTrue(decision.replayed is False)
        expected_due = (datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
                        + timedelta(days=14)).isoformat().replace("+00:00", "Z")
        view = self.service.get_condition(actor_id="office", condition_id=condition)
        self.assertEqual(expected_due, view.evidences[0]["remediation_deadline_at"])
        self.assertEqual("rejected", view.status)
        pending = self.service.pending_reviews(actor_id="office", project_id="p1")
        self.assertEqual("remediation", pending[0]["kind"])
        self.assertEqual(expected_due, pending[0]["due_at"])


if __name__ == "__main__":
    unittest.main()
