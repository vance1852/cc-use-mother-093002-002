import unittest

from digital_trade_foundation.governance.api import make_router, route_governance
from digital_trade_foundation.governance import GovernanceService
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database


class GovernanceApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.domain = DomainService(self.database)
        self.gov = GovernanceService(self.database, self.domain)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="org-office", name="办公室")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap",
                                   new_actor_id="off-1", display_name="管理员",
                                   role="admin", organization_id="org-office")
        self.domain.register_site(request_id="site", actor_id="off-1", site_id="site-1",
                                  organization_id="org-office", name="节点",
                                  timezone_name="Africa/Accra")

    def tearDown(self):
        self.database.close()

    def test_non_governance_path_returns_none(self):
        result = route_governance(self.gov, "GET", "/health", None)
        self.assertIsNone(result)

    def test_unknown_governance_route_returns_404(self):
        status, payload = route_governance(self.gov, "GET", "/governance/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_create_agreement_over_http_shape(self):
        status, payload = route_governance(
            self.gov, "POST", "/governance/agreements",
            {"request_id": "agr-1", "site_id": "site-1", "agreement_id": "agr-1",
             "title": "联合方案"},
            {"X-Actor-Id": "off-1"})
        self.assertEqual(201, status)
        self.assertEqual("agr-1", payload["resource_id"])
        replay, _ = route_governance(
            self.gov, "POST", "/governance/agreements",
            {"request_id": "agr-1", "site_id": "site-1", "agreement_id": "agr-1",
             "title": "联合方案"},
            {"X-Actor-Id": "off-1"})
        self.assertEqual(200, replay)

    def test_missing_field_returns_400(self):
        status, payload = route_governance(
            self.gov, "POST", "/governance/agreements", {"request_id": "x"},
            {"X-Actor-Id": "off-1"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_reconciliation_requires_agreement_id(self):
        status, payload = route_governance(self.gov, "GET", "/governance/reconciliation",
                                           None, {"X-Actor-Id": "off-1"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_combined_router_falls_back_to_foundation(self):
        router = make_router(self.database)
        status, payload = router("GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        status, payload = router("GET", "/governance/agreement?agreement_id=missing", None,
                                 {"X-Actor-Id": "off-1"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
