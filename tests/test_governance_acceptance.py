import unittest

from digital_trade_foundation.governance.acceptance import run


class GovernanceAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(all(result["checks"].values()))


if __name__ == "__main__":
    unittest.main()
