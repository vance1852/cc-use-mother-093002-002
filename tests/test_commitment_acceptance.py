import unittest

from digital_trade_foundation.commitment_acceptance import run


class CommitmentAcceptanceTest(unittest.TestCase):
    def test_commitment_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["early_release_blocked"])
        self.assertTrue(result["duplicate_payment_prevented"])
        self.assertTrue(result["late_evidence_flagged"])
        self.assertTrue(result["duplicate_claim_blocked"])
        self.assertTrue(result["ledger_balanced"])
        self.assertEqual(60000, result["escrow_balance"])
        self.assertEqual(40000, result["escrow_disbursed"])
        self.assertEqual(100000, result["escrow_balance_at_history"])
        self.assertEqual("o-newtech", result["tech_provider_after_replace"])
        self.assertIsNone(result["tech_provider_before_replace"])
        self.assertEqual("o-newtech", result["outcome_responsible_party"])
        self.assertFalse(result["first_payment_replayed"])
        self.assertEqual(45000, result["cf1_outstanding"])


if __name__ == "__main__":
    unittest.main()
