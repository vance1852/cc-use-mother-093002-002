"""承诺治理测试共享的环境构建器。"""

from __future__ import annotations

from datetime import datetime, timezone

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.commitment_service import CommitmentService
from digital_trade_foundation.storage import Database


def build_service(when: datetime | None = None) -> CommitmentService:
    """搭建含办公室、技术方、投资方、当地机构、独立复核所的项目环境。"""

    database = Database()
    service = CommitmentService(
        database, FixedClock(when or datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))

    service.register_organization(request_id="org-office", actor_id="bootstrap",
                                  organization_id="o-office", name="合作项目办公室")
    service.register_actor(request_id="actor-admin", actor_id="bootstrap", new_actor_id="admin",
                           display_name="管理员", role="admin", organization_id="o-office")

    orgs = [("o-tech", "技术方"), ("o-investor", "投资方"),
            ("o-local", "当地机构"), ("o-independent", "独立复核所")]
    for index, (org_id, name) in enumerate(orgs):
        service.register_organization(request_id=f"org-{org_id}", actor_id="admin",
                                      organization_id=org_id, name=name)

    actors = [
        ("office", "办公室专员", "operator", "o-office"),
        ("tech", "技术专员", "operator", "o-tech"),
        ("investor", "投资专员", "operator", "o-investor"),
        ("local", "本地专员", "operator", "o-local"),
        ("reviewer", "独立复核员", "reviewer", "o-independent"),
        ("auditor", "监督人员", "auditor", "o-office"),
    ]
    for actor_id, name, role, org_id in actors:
        service.register_actor(request_id=f"actor-{actor_id}", actor_id="admin",
                               new_actor_id=actor_id, display_name=name, role=role,
                               organization_id=org_id)

    service.create_project(request_id="project-p1", actor_id="office",
                           project_id="p1", name="非洲数贸协作项目")
    parties = [("o-tech", "technology_provider"), ("o-investor", "investor"),
               ("o-local", "local_agency"), ("o-independent", "independent")]
    for org_id, party_role in parties:
        service.add_project_party(request_id=f"party-{org_id}", actor_id="office",
                                  project_id="p1", organization_id=org_id, party_role=party_role)
    return service
