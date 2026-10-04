"""承诺治理 HTTP 路由测试。"""

import unittest

from digital_trade_foundation.api import route
from tests.fixtures import build_service


def headers(actor: str) -> dict[str, str]:
    return {"X-Actor-Id": actor}


class CommitmentApiTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_full_flow_over_http(self):
        # 技术方立承诺并封存
        status, payload = route(self.service, "POST", "/commitments", {
            "request_id": "d1", "project_id": "p1", "commitment_id": "ct",
            "commitment_type": "party_input", "provider_organization_id": "o-tech",
            "title": "平台培训", "terms": {"trainees": 200}}, headers("tech"))
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/commitments/ct/seal",
                                {"request_id": "s1"}, headers("tech"))
        self.assertEqual(201, status)
        self.assertEqual("ct", payload["resource_id"])

        # 条件组提前生效返回 412 与待满足条件
        route(self.service, "POST", "/commitments/ct/stages",
              {"request_id": "st1", "name": "验收", "sequence": 1}, headers("office"))
        status, listed = route(self.service, "GET", "/projects/p1/commitments", None,
                               headers("auditor"))
        stage_id = listed["items"][0]["stages"][0]["stage_id"]
        route(self.service, "POST", f"/stages/{stage_id}/conditions",
              {"request_id": "cd1", "label": "验收报告", "sequence": 1}, headers("office"))
        condition_id = self.service.database.connection.execute(
            "SELECT condition_id FROM stage_conditions WHERE stage_id=?", (stage_id,)
        ).fetchone()["condition_id"]
        status, payload = route(self.service, "POST", f"/stages/{stage_id}/effect",
                                {"request_id": "eff-early"}, headers("office"))
        self.assertEqual(412, status)
        self.assertEqual(["验收报告"], payload["details"]["pending_conditions"])

        # 提交证据并经独立复核接受
        status, evidence = route(self.service, "POST", f"/conditions/{condition_id}/evidences",
                                 {"request_id": "ev1", "reference": "doc1",
                                  "payload": {"report": "ok"}}, headers("tech"))
        self.assertEqual(201, status)
        evidence_id = evidence["resource_id"]
        status, _ = route(self.service, "POST", f"/evidences/{evidence_id}/review",
                          {"request_id": "rv1", "approved": True}, headers("reviewer"))
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", f"/stages/{stage_id}/effect",
                                {"request_id": "eff1"}, headers("office"))
        self.assertEqual(201, status)

        # 责任与对账读取
        status, responsibility = route(self.service, "GET",
                                       "/commitments/ct/responsibility", None, headers("auditor"))
        self.assertEqual(200, status)
        self.assertEqual("effective", responsibility["status"])
        status, reconcile = route(self.service, "GET", "/projects/p1/reconcile", None,
                                  headers("auditor"))
        self.assertEqual(200, status)
        self.assertTrue(reconcile["ledger_balanced"])

    def test_visibility_enforced_over_http(self):
        status, payload = route(self.service, "POST", "/projects",
                                {"request_id": "p2", "project_id": "p2", "name": "其他"},
                                headers("office"))
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/projects/p2", None, headers("tech"))
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_outcome_double_claim_conflict_over_http(self):
        status, outcome = route(self.service, "POST", "/outcomes",
                                {"request_id": "out1", "title": "共享成果"}, headers("reviewer"))
        outcome_id = outcome["resource_id"]
        status, _ = route(self.service, "POST", f"/outcomes/{outcome_id}/claims",
                          {"request_id": "c1", "project_id": "p1"}, headers("tech"))
        self.assertEqual(201, status)
        route(self.service, "POST", "/projects",
              {"request_id": "p2", "project_id": "p2", "name": "其他"}, headers("office"))
        status, payload = route(self.service, "POST", f"/outcomes/{outcome_id}/claims",
                                {"request_id": "c2", "project_id": "p2"}, headers("office"))
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_history_endpoint_restores_point_in_time(self):
        route(self.service, "POST", "/commitments", {
            "request_id": "d1", "project_id": "p1", "commitment_id": "ct",
            "commitment_type": "party_input", "provider_organization_id": "o-tech",
            "title": "投入", "terms": {"v": 1}}, headers("tech"))
        route(self.service, "POST", "/commitments/ct/seal", {"request_id": "s1"},
              headers("tech"))
        status, payload = route(
            self.service, "GET",
            "/history?entity_type=commitment&entity_id=ct&at=2030-01-01T00:00:00Z",
            None, headers("auditor"))
        self.assertEqual(200, status)
        self.assertEqual("committed", payload["state"]["status"])


if __name__ == "__main__":
    unittest.main()
