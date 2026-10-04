"""服务中断恢复：未决复核与整改期限按原定期限和顺序继续，状态跨重启持久化。"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.commitment_service import CommitmentService
from digital_trade_foundation.storage import Database
from tests.test_commitments import condition_id, stage_id
from tests.test_escrow import prepare_funded_stage


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "recovery.sqlite3"

    def tearDown(self):
        self.directory.cleanup()

    def _service(self, when):
        database = Database(self.path)
        return database, CommitmentService(database, FixedClock(when))

    def test_deadlines_and_order_survive_restart(self):
        start = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        database, service = self._service(start)
        # fixtures 默认使用内存库；这里直接在文件库上搭建最小环境
        service.register_organization(request_id="o1", actor_id="bootstrap",
                                      organization_id="o-office", name="办公室")
        service.register_actor(request_id="a1", actor_id="bootstrap", new_actor_id="admin",
                               display_name="管理员", role="admin", organization_id="o-office")
        for oid, name in [("o-tech", "技术方"), ("o-independent", "独立复核所"),
                          ("o-local", "当地机构"), ("o-investor", "投资方")]:
            service.register_organization(request_id=f"o-{oid}", actor_id="admin",
                                          organization_id=oid, name=name)
        for aid, name, role, oid in [
                ("office", "办公室", "operator", "o-office"),
                ("tech", "技术", "operator", "o-tech"),
                ("local", "本地", "operator", "o-local"),
                ("reviewer", "复核", "reviewer", "o-independent")]:
            service.register_actor(request_id=f"a-{aid}", actor_id="admin", new_actor_id=aid,
                                   display_name=name, role=role, organization_id=oid)
        service.create_project(request_id="p1", actor_id="office", project_id="p1", name="项目")
        service.add_project_party(request_id="pa-tech", actor_id="office", project_id="p1",
                                  organization_id="o-tech", party_role="technology_provider")
        service.add_project_party(request_id="pa-ind", actor_id="office", project_id="p1",
                                  organization_id="o-independent", party_role="independent")
        service.add_project_party(request_id="pa-local", actor_id="office", project_id="p1",
                                  organization_id="o-local", party_role="local_agency")

        service.draft_commitment(
            request_id="d1", actor_id="tech", project_id="p1", commitment_id="c1",
            commitment_type="localization_target", provider_organization_id="o-tech",
            title="属地能力", terms={"kpi": 1})
        service.seal_commitment(request_id="s1", actor_id="tech", commitment_id="c1")
        service.add_stage(request_id="st1", actor_id="office", commitment_id="c1",
                          name="阶段", sequence=1)
        stage = stage_id(service, "c1", 1)
        service.add_condition(request_id="cd1", actor_id="office", stage_id=stage,
                              label="能力验收", sequence=1)
        condition = condition_id(service, stage, 1)
        evidence = service.submit_evidence(request_id="ev1", actor_id="tech",
                                           condition_id=condition, reference="r1",
                                           payload={"ok": 1})
        review_due_before = (start + timedelta(days=3)).isoformat().replace("+00:00", "Z")

        # 模拟服务中断：关闭数据库，很久以后以更晚时钟重开
        database.close()
        later = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)
        database2, service2 = self._service(later)

        pending = service2.pending_reviews(actor_id="office", project_id="p1")
        self.assertEqual(1, len(pending))
        # 复核期限仍是入证据时定下的绝对时间，没有因重启而顺延，且已逾期
        self.assertEqual(review_due_before, pending[0]["due_at"])
        self.assertTrue(pending[0]["overdue"])

        # 复核在重启后仍可正常裁定，阶段可继续推进
        service2.decide_review(request_id="rv1", actor_id="reviewer",
                               evidence_id=evidence.resource_id, approved=True)
        service2.effect_stage(request_id="eff1", actor_id="office", stage_id=stage)
        stage_row = database2.connection.execute(
            "SELECT status FROM commitment_stages WHERE stage_id=?", (stage,)).fetchone()
        self.assertEqual("effective", stage_row["status"])

        valid, count = service2.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)
        database2.close()


if __name__ == "__main__":
    unittest.main()
