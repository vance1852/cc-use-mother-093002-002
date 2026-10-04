"""承诺治理平台的核心领域服务。

在基础服务（组织、操作者、场所、资料登记、幂等、审计链）之上，管理跨境合作
"从谈判到退出"的全过程：七类承诺分列、谈判稿/正式承诺/履约证据/争议结论
分离、条件组成组核验与独立复核、资金托管与拨付、责任调整、成果唯一认领、
角色与可见范围、历史时点还原。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from .audit import append_event, canonical_json, digest
from .commitment_domain import (
    ADJUSTMENT_KINDS,
    PARTY_ROLES,
    is_commitment_type,
)
from .commitment_models import (
    Commitment,
    ConditionView,
    DisputeView,
    EscrowView,
    Project,
    ResponsibilityView,
)
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    PreconditionFailed,
    UnprocessableState,
    ValidationError,
)
from .models import Actor
from .service import DomainService

# 独立复核期限与整改期限。期限以绝对时间戳入库，服务中断或重启后仍然按原定期限
# 和原顺序继续，不会因为停机而重置。
REVIEW_WINDOW = timedelta(days=3)
REMEDIATION_WINDOW = timedelta(days=14)

# 承诺到达这些状态后，原始责任已闭环，不再参与合作方替换时的责任转移。
TERMINAL_STATUSES = frozenset({"fulfilled", "closed"})


class CommitmentService(DomainService):
    """协调承诺、条件组、复核、托管拨付与责任追溯。"""

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _amount(self, value: Any, field: str, *, allow_none: bool = False) -> int | None:
        if value is None:
            if allow_none:
                return None
            raise ValidationError(f"{field} 不能为空")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是以最小货币单位表示的非负整数")
        if value < 0:
            raise ValidationError(f"{field} 不能为负数")
        return value

    def _load(self, connection, table: str, key_field: str, key_value: str, message: str):
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {key_field}=?", (key_value,)
        ).fetchone()
        if row is None:
            raise NotFoundError(message)
        return row

    def _project_row(self, connection, project_id: str):
        return self._load(connection, "projects", "project_id", project_id, "合作项目不存在")

    def _commitment_row(self, connection, commitment_id: str):
        return self._load(connection, "commitments", "commitment_id", commitment_id, "承诺不存在")

    def _party_row(self, connection, project_id: str, organization_id: str):
        return connection.execute(
            "SELECT * FROM project_parties WHERE project_id=? AND organization_id=?",
            (project_id, organization_id),
        ).fetchone()

    def _assert_party(self, connection, project_id: str, organization_id: str):
        if self._party_row(connection, project_id, organization_id) is None:
            raise PermissionDenied("该组织不是本项目参与方")

    def _assert_project_visible(self, connection, actor: Actor, project_id: str) -> None:
        """监督人员与管理员可看全部；参与方（含已退出/被替换方）只看本项目。

        已退出或被替换的参与方保留可见性，因为其历史责任仍可被追溯。
        """

        if actor.role in ("admin", "auditor"):
            return
        if self._party_row(connection, project_id, actor.organization_id) is None:
            raise PermissionDenied("不能查看未参与的项目")

    def _assert_project_writer(self, connection, actor: Actor, project_id: str) -> None:
        if actor.role == "admin":
            return
        if actor.role != "operator":
            raise PermissionDenied("当前角色不能执行该动作")
        self._assert_party(connection, project_id, actor.organization_id)

    def _is_office(self, connection, actor: Actor, project_id: str) -> bool:
        if actor.role == "admin":
            return True
        row = self._party_row(connection, project_id, actor.organization_id)
        return row is not None and row["party_role"] == "office"

    def _snapshot(self, connection, entity_type: str, entity_id: str,
                  state: dict[str, Any], at: str) -> None:
        """把实体状态追加到只增的快照表，供历史时点还原。"""

        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence "
            "FROM state_snapshots WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        ).fetchone()["next_sequence"]
        connection.execute(
            "INSERT INTO state_snapshots(entity_type,entity_id,sequence,valid_from,state_json) "
            "VALUES(?,?,?,?,?)",
            (entity_type, entity_id, sequence, at, canonical_json(state)),
        )

    def _snapshot_commitment(self, connection, row, at: str) -> None:
        self._snapshot(connection, "commitment", row["commitment_id"], {
            "commitment_id": row["commitment_id"],
            "project_id": row["project_id"],
            "commitment_type": row["commitment_type"],
            "provider_organization_id": row["provider_organization_id"],
            "status": row["status"],
            "version": row["version"],
            "original_amount_minor": row["original_amount_minor"],
        }, at)

    def _adjustments(self, connection, commitment_id: str, at: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM responsibility_adjustments WHERE commitment_id=?"
        parameters: list[Any] = [commitment_id]
        if at is not None:
            query += " AND created_at<=?"
            parameters.append(at)
        query += " ORDER BY created_at, adjustment_id"
        items = []
        for row in connection.execute(query, parameters):
            items.append({
                "adjustment_id": row["adjustment_id"],
                "kind": row["kind"],
                "amount_delta_minor": row["amount_delta_minor"],
                "old_organization_id": row["old_organization_id"],
                "new_organization_id": row["new_organization_id"],
                "portion_minor": row["portion_minor"],
                "detail": json.loads(row["detail_json"]),
                "dispute_id": row["dispute_id"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            })
        return items

    def _current_provider(self, connection, commitment_row, at: str | None = None) -> str:
        """解析承诺在某一时点的实际责任方。

        原始责任方永不被抹掉；合作方替换只把尚未兑现的部分转移到新方，因此
        历史时点以前的责任仍归原方或当时的承接方。
        """

        query = ("SELECT new_organization_id FROM responsibility_adjustments "
                 "WHERE commitment_id=? AND kind='party_replacement'")
        parameters: list[Any] = [commitment_row["commitment_id"]]
        if at is not None:
            query += " AND created_at<=?"
            parameters.append(at)
        query += " ORDER BY created_at DESC, adjustment_id DESC LIMIT 1"
        row = connection.execute(query, parameters).fetchone()
        return row["new_organization_id"] if row else commitment_row["provider_organization_id"]

    def _outstanding(self, connection, commitment_row, at: str | None = None) -> int | None:
        """计算尚未兑现金额：原始责任只受调整影响，不因违约/替换而消失。"""

        if commitment_row["original_amount_minor"] is None:
            return None
        amount = commitment_row["original_amount_minor"]
        for adjustment in self._adjustments(connection, commitment_row["commitment_id"], at):
            amount += adjustment["amount_delta_minor"]
        query = "SELECT COALESCE(SUM(amount_minor),0) AS total FROM disbursements WHERE commitment_id=? AND status='paid'"
        parameters: list[Any] = [commitment_row["commitment_id"]]
        if at is not None:
            query += " AND paid_at<=?"
            parameters.append(at)
        disbursed = connection.execute(query, parameters).fetchone()["total"]
        return max(amount - disbursed, 0)

    def _disbursed(self, connection, commitment_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS total FROM disbursements "
            "WHERE commitment_id=? AND status='paid'",
            (commitment_id,),
        ).fetchone()["total"]

    def _replayed_receipt(self, connection, request_id: str, action: str,
                          payload: dict[str, Any]):
        """若 request_id 已有回执则幂等回放；内容不一致则冲突；无记录返回 None。"""

        from .audit import digest
        from .models import WriteReceipt

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    # ------------------------------------------------------------------
    # 项目与参与方
    # ------------------------------------------------------------------

    def create_project(self, *, request_id: str, actor_id: str,
                       project_id: str, name: str):
        payload = {"actor_id": actor_id, "project_id": project_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            project_id = self._identifier(project_id, "project_id")
            name = self._text(name, "name")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO projects(project_id,name,status,created_by,created_at) VALUES(?,?, 'active',?,?)",
                        (project_id, name, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("项目编号已经存在") from exc
                # 创建者所在组织作为合作项目办公室入项。
                connection.execute(
                    "INSERT INTO project_parties(project_id,organization_id,party_role,status,joined_at) "
                    "VALUES(?,?, 'office', 'active',?)",
                    (project_id, actor.organization_id, now),
                )
                self._snapshot(connection, "project", project_id,
                               {"project_id": project_id, "status": "active",
                                "parties": [actor.organization_id]}, now)
                append_event(connection, actor_id=actor_id, action="project.created",
                             resource_type="project", resource_id=project_id,
                             detail={"name": name, "office_organization_id": actor.organization_id},
                             occurred_at=now)
                return "project", project_id, {"project_id": project_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_project", payload=payload, create=create)

    def add_project_party(self, *, request_id: str, actor_id: str, project_id: str,
                          organization_id: str, party_role: str):
        payload = {"actor_id": actor_id, "project_id": project_id,
                   "organization_id": organization_id, "party_role": party_role}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._project_row(connection, project_id)
            if not self._is_office(connection, actor, project_id):
                raise PermissionDenied("只有合作项目办公室可以登记参与方")
            if party_role not in PARTY_ROLES:
                raise ValidationError("party_role 不在允许范围内")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            if self._party_row(connection, project_id, organization_id) is not None:
                raise ConflictError("该组织已经是项目参与方")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO project_parties(project_id,organization_id,party_role,status,joined_at) "
                    "VALUES(?,?,?, 'active',?)",
                    (project_id, organization_id, party_role, now),
                )
                append_event(connection, actor_id=actor_id, action="project_party.added",
                             resource_type="project", resource_id=project_id,
                             detail={"organization_id": organization_id, "party_role": party_role},
                             occurred_at=now)
                return ("project_party", f"{project_id}:{organization_id}",
                        {"project_id": project_id, "organization_id": organization_id,
                         "party_role": party_role})

            return self._idempotent(connection, request_id=request_id,
                                    action="add_project_party", payload=payload, create=create)

    def replace_project_party(self, *, request_id: str, actor_id: str, project_id: str,
                              old_organization_id: str, new_organization_id: str,
                              note: str = ""):
        """合作方替换：老方退出，尚未兑现的责任转移给新方，原责任记录保留。"""

        payload = {"actor_id": actor_id, "project_id": project_id,
                   "old_organization_id": old_organization_id,
                   "new_organization_id": new_organization_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._project_row(connection, project_id)
            if not self._is_office(connection, actor, project_id):
                raise PermissionDenied("只有合作项目办公室可以替换参与方")
            old_party = self._party_row(connection, project_id, old_organization_id)
            if old_party is None:
                raise NotFoundError("被替换的组织不是项目参与方")
            if old_party["status"] != "active":
                raise UnprocessableState("该参与方已不在合作状态")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (new_organization_id,)).fetchone() is None:
                raise NotFoundError("接替组织不存在")
            if new_organization_id == old_organization_id:
                raise ValidationError("接替组织不能与被替换组织相同")
            new_party = self._party_row(connection, project_id, new_organization_id)
            if new_party is not None and new_party["status"] == "active":
                raise UnprocessableState("接替组织已经是在册活跃参与方，不能重复入项")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                replacement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO party_replacements(replacement_id,project_id,old_organization_id,"
                    "new_organization_id,effective_at,created_by,note) VALUES(?,?,?,?,?,?,?)",
                    (replacement_id, project_id, old_organization_id, new_organization_id,
                     now, actor_id, note),
                )
                connection.execute(
                    "UPDATE project_parties SET status='replaced', left_at=? "
                    "WHERE project_id=? AND organization_id=?",
                    (now, project_id, old_organization_id),
                )
                if new_party is None:
                    connection.execute(
                        "INSERT INTO project_parties(project_id,organization_id,party_role,status,joined_at) "
                        "VALUES(?,?,?, 'active',?)",
                        (project_id, new_organization_id, old_party["party_role"], now),
                    )
                else:
                    # 曾经退出/被替换的组织重新承接：恢复为活跃并继承原角色。
                    connection.execute(
                        "UPDATE project_parties SET status='active', party_role=?, left_at=NULL "
                        "WHERE project_id=? AND organization_id=?",
                        (old_party["party_role"], project_id, new_organization_id),
                    )
                # 仅把老方尚未兑现的承诺责任转移给新方；已终结承诺不动，历史不抹掉。
                open_rows = connection.execute(
                    "SELECT * FROM commitments WHERE project_id=? AND provider_organization_id=? "
                    "AND status NOT IN ('fulfilled','closed')",
                    (project_id, old_organization_id),
                ).fetchall()
                transferred = []
                for commitment_row in open_rows:
                    outstanding = self._outstanding(connection, commitment_row)
                    adjustment_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO responsibility_adjustments(adjustment_id,commitment_id,kind,"
                        "amount_delta_minor,old_organization_id,new_organization_id,portion_minor,"
                        "detail_json,created_by,created_at) "
                        "VALUES(?,?,'party_replacement',0,?,?,?,?,?,?)",
                        (adjustment_id, commitment_row["commitment_id"], old_organization_id,
                         new_organization_id, outstanding or 0,
                         canonical_json({"note": note, "replacement_id": replacement_id}),
                         actor_id, now),
                    )
                    self._snapshot_commitment(connection, commitment_row, now)
                    transferred.append({"commitment_id": commitment_row["commitment_id"],
                                        "outstanding_minor": outstanding})
                append_event(connection, actor_id=actor_id, action="project_party.replaced",
                             resource_type="project", resource_id=project_id,
                             detail={"old_organization_id": old_organization_id,
                                     "new_organization_id": new_organization_id,
                                     "transferred": transferred}, occurred_at=now)
                return "party_replacement", replacement_id, {
                    "replacement_id": replacement_id,
                    "transferred_commitments": transferred,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="replace_project_party", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 承诺：谈判稿 → 正式承诺
    # ------------------------------------------------------------------

    def draft_commitment(self, *, request_id: str, actor_id: str, project_id: str,
                         commitment_id: str, commitment_type: str,
                         provider_organization_id: str, title: str, terms: dict[str, Any],
                         amount_minor: int | None = None, currency: str | None = None):
        if not isinstance(terms, dict) or not terms:
            raise ValidationError("terms 必须是非空对象")
        payload = {"actor_id": actor_id, "project_id": project_id, "commitment_id": commitment_id,
                   "commitment_type": commitment_type,
                   "provider_organization_id": provider_organization_id, "title": title,
                   "terms": terms, "amount_minor": amount_minor, "currency": currency}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._project_row(connection, project_id)
            self._assert_project_writer(connection, actor, project_id)
            if actor.role == "operator" and actor.organization_id != provider_organization_id \
                    and not self._is_office(connection, actor, project_id):
                raise PermissionDenied("不能代其他参与方立承诺")
            self._assert_party(connection, project_id, provider_organization_id)
            if not is_commitment_type(commitment_type):
                raise ValidationError("commitment_type 不属于七类承诺")
            commitment_id = self._identifier(commitment_id, "commitment_id")
            title = self._text(title, "title")
            amount = self._amount(amount_minor, "amount_minor", allow_none=True)
            if amount is not None:
                currency = self._text(currency or "", "currency", 8)
            else:
                currency = None
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO commitments(commitment_id,project_id,commitment_type,"
                        "provider_organization_id,title,terms_json,original_amount_minor,currency,"
                        "status,created_by,created_at,version) VALUES(?,?,?,?,?,?,?,?,'draft',?,?,1)",
                        (commitment_id, project_id, commitment_type, provider_organization_id,
                         title, canonical_json(terms), amount, currency, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("承诺编号已经存在") from exc
                # 初始谈判稿，与正式承诺分表保存。
                connection.execute(
                    "INSERT INTO commitment_documents(document_id,commitment_id,kind,version,"
                    "terms_json,payload_hash,created_by,created_at) VALUES(?,?,'negotiation_draft',1,?,?,?,?)",
                    (uuid.uuid4().hex, commitment_id, canonical_json(terms),
                     digest(terms),
                     actor_id, now),
                )
                self._snapshot_commitment(
                    connection,
                    connection.execute("SELECT * FROM commitments WHERE commitment_id=?",
                                       (commitment_id,)).fetchone(), now)
                append_event(connection, actor_id=actor_id, action="commitment.drafted",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"project_id": project_id, "commitment_type": commitment_type,
                                     "provider_organization_id": provider_organization_id},
                             occurred_at=now)
                return "commitment", commitment_id, {
                    "commitment_id": commitment_id, "status": "draft", "draft_version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="draft_commitment", payload=payload, create=create)

    def revise_negotiation_draft(self, *, request_id: str, actor_id: str,
                                 commitment_id: str, terms: dict[str, Any]):
        if not isinstance(terms, dict) or not terms:
            raise ValidationError("terms 必须是非空对象")
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "terms": terms}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = self._commitment_row(connection, commitment_id)
            self._assert_project_writer(connection, actor, row["project_id"])
            if row["status"] != "draft":
                raise UnprocessableState("只有谈判中的承诺可以修订谈判稿")
            if actor.role == "operator" and actor.organization_id != row["provider_organization_id"] \
                    and not self._is_office(connection, actor, row["project_id"]):
                raise PermissionDenied("不能代其他参与方修改承诺")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                version = connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 AS v FROM commitment_documents "
                    "WHERE commitment_id=? AND kind='negotiation_draft'",
                    (commitment_id,),
                ).fetchone()["v"]
                document_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO commitment_documents(document_id,commitment_id,kind,version,"
                    "terms_json,payload_hash,created_by,created_at) VALUES(?,?,'negotiation_draft',?,?,?,?,?)",
                    (document_id, commitment_id, version, canonical_json(terms),
                     digest(terms),
                     actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="commitment.draft_revised",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"version": version}, occurred_at=now)
                return ("negotiation_draft", document_id,
                        {"commitment_id": commitment_id, "draft_version": version})

            return self._idempotent(connection, request_id=request_id,
                                    action="revise_negotiation_draft", payload=payload,
                                    create=create)

    def seal_commitment(self, *, request_id: str, actor_id: str, commitment_id: str):
        """把谈判稿封存为正式承诺。封存后条款不可改，只能通过责任调整改变未兑现部分。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = self._commitment_row(connection, commitment_id)
            self._assert_project_writer(connection, actor, row["project_id"])
            if actor.role == "operator" and actor.organization_id != row["provider_organization_id"] \
                    and not self._is_office(connection, actor, row["project_id"]):
                raise PermissionDenied("不能代其他参与方封存承诺")
            if row["status"] != "draft":
                raise UnprocessableState("只有谈判中的承诺可以封存")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = connection.execute(
                    "SELECT * FROM commitment_documents WHERE commitment_id=? AND kind='negotiation_draft' "
                    "ORDER BY version DESC LIMIT 1",
                    (commitment_id,),
                ).fetchone()
                connection.execute(
                    "INSERT INTO commitment_documents(document_id,commitment_id,kind,version,"
                    "terms_json,payload_hash,created_by,created_at) VALUES(?,?,'sealed_terms',1,?,?,?,?)",
                    (uuid.uuid4().hex, commitment_id, latest["terms_json"], latest["payload_hash"],
                     actor_id, now),
                )
                connection.execute(
                    "UPDATE commitments SET status='committed', sealed_at=?, version=version+1 "
                    "WHERE commitment_id=?",
                    (now, commitment_id),
                )
                updated = self._commitment_row(connection, commitment_id)
                self._snapshot_commitment(connection, updated, now)
                append_event(connection, actor_id=actor_id, action="commitment.sealed",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"terms_hash": latest["payload_hash"]}, occurred_at=now)
                return ("commitment", commitment_id,
                        {"commitment_id": commitment_id, "status": "committed",
                         "terms_hash": latest["payload_hash"]})

            return self._idempotent(connection, request_id=request_id,
                                    action="seal_commitment", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 阶段与相互依赖的条件组
    # ------------------------------------------------------------------

    def add_stage(self, *, request_id: str, actor_id: str, commitment_id: str,
                  name: str, sequence: int, due_at: str | None = None,
                  disburse_amount_minor: int = 0):
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "name": name,
                   "sequence": sequence, "due_at": due_at,
                   "disburse_amount_minor": disburse_amount_minor}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = self._commitment_row(connection, commitment_id)
            if not self._is_office(connection, actor, row["project_id"]):
                if not (actor.role == "operator" and actor.organization_id == row["provider_organization_id"]):
                    raise PermissionDenied("只有办公室或承诺方可以编排阶段")
            if row["status"] not in ("draft", "committed"):
                raise UnprocessableState("承诺进入履约后不能再新增阶段")
            name = self._text(name, "name")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise ValidationError("sequence 必须是不小于 1 的整数")
            amount = self._amount(disburse_amount_minor, "disburse_amount_minor")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                stage_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO commitment_stages(stage_id,commitment_id,sequence,name,due_at,"
                        "disburse_amount_minor,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
                        (stage_id, commitment_id, sequence, name, due_at, amount, now),
                    )
                except Exception as exc:
                    raise ConflictError("阶段序号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="stage.added",
                             resource_type="stage", resource_id=stage_id,
                             detail={"commitment_id": commitment_id, "sequence": sequence,
                                     "disburse_amount_minor": amount}, occurred_at=now)
                return "stage", stage_id, {"stage_id": stage_id, "sequence": sequence}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_stage", payload=payload, create=create)

    def add_condition(self, *, request_id: str, actor_id: str, stage_id: str,
                      label: str, sequence: int, due_at: str | None = None):
        payload = {"actor_id": actor_id, "stage_id": stage_id, "label": label,
                   "sequence": sequence, "due_at": due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            stage = self._load(connection, "commitment_stages", "stage_id", stage_id, "阶段不存在")
            row = self._commitment_row(connection, stage["commitment_id"])
            if not self._is_office(connection, actor, row["project_id"]):
                if not (actor.role == "operator" and actor.organization_id == row["provider_organization_id"]):
                    raise PermissionDenied("只有办公室或承诺方可以登记核验条件")
            if stage["status"] != "pending":
                raise UnprocessableState("阶段生效后不能再新增条件")
            label = self._text(label, "label")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise ValidationError("sequence 必须是不小于 1 的整数")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                condition_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO stage_conditions(condition_id,stage_id,sequence,label,due_at,"
                        "status,updated_at) VALUES(?,?,?,?,?, 'awaiting_evidence',?)",
                        (condition_id, stage_id, sequence, label, due_at, now),
                    )
                except Exception as exc:
                    raise ConflictError("条件序号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="condition.added",
                             resource_type="condition", resource_id=condition_id,
                             detail={"stage_id": stage_id, "sequence": sequence, "label": label},
                             occurred_at=now)
                return "condition", condition_id, {"condition_id": condition_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_condition", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 履约证据与独立复核
    # ------------------------------------------------------------------

    def submit_evidence(self, *, request_id: str, actor_id: str, condition_id: str,
                        reference: str, payload: dict[str, Any]):
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        body = {"actor_id": actor_id, "condition_id": condition_id,
                "reference": reference, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            condition = self._load(connection, "stage_conditions", "condition_id",
                                   condition_id, "核验条件不存在")
            stage = self._load(connection, "commitment_stages", "stage_id",
                               condition["stage_id"], "阶段不存在")
            commitment = self._commitment_row(connection, stage["commitment_id"])
            self._assert_project_writer(connection, actor, commitment["project_id"])
            if condition["status"] == "accepted":
                replay = self._replayed_receipt(connection, request_id, "submit_evidence", body)
                if replay is not None:
                    return replay
                raise UnprocessableState("该条件已经通过独立复核，不能重复提交")
            reference = self._text(reference, "reference")
            now_text = self._now()
            now = self.clock.now()
            late = bool(condition["due_at"]) and now_text > condition["due_at"]
            review_deadline = (now + REVIEW_WINDOW)
            review_deadline_text = review_deadline.isoformat().replace("+00:00", "Z")

            def create() -> tuple[str, str, dict[str, Any]]:
                evidence_id = uuid.uuid4().hex
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO evidences(evidence_id,condition_id,submitter_actor_id,"
                    "submitter_organization_id,reference,payload_json,payload_hash,late,status,"
                    "review_deadline_at,submitted_at) VALUES(?,?,?,?,?,?,?,?,'submitted',?,?)",
                    (evidence_id, condition_id, actor_id, actor.organization_id, reference,
                     canonical_json(payload),
                     digest(payload),
                     1 if late else 0, review_deadline_text, now_text),
                )
                connection.execute(
                    "INSERT INTO reviews(review_id,evidence_id,status,due_at,created_at) "
                    "VALUES(?,?,'pending',?,?)",
                    (review_id, evidence_id, review_deadline_text, now_text),
                )
                connection.execute(
                    "UPDATE stage_conditions SET status='in_review', updated_at=? WHERE condition_id=?",
                    (now_text, condition_id),
                )
                # 证据迟到不抹掉原责任：记录调整事实，责任与未兑现金额仍按原承诺保留，
                # 证据照常进入独立复核。
                if late:
                    connection.execute(
                        "INSERT INTO responsibility_adjustments(adjustment_id,commitment_id,kind,"
                        "amount_delta_minor,detail_json,created_by,created_at) "
                        "VALUES(?,?,'late_evidence',0,?,?,?)",
                        (uuid.uuid4().hex, commitment["commitment_id"],
                         canonical_json({"condition_id": condition_id, "due_at": condition["due_at"],
                                         "submitted_at": now_text}), actor_id, now_text),
                    )
                self._snapshot(connection, "condition", condition_id,
                               {"condition_id": condition_id, "stage_id": condition["stage_id"],
                                "status": "in_review", "latest_evidence_id": evidence_id,
                                "late": late}, now_text)
                append_event(connection, actor_id=actor_id, action="evidence.submitted",
                             resource_type="evidence", resource_id=evidence_id,
                             detail={"condition_id": condition_id, "late": late,
                                     "review_due_at": review_deadline_text}, occurred_at=now_text)
                return ("evidence", evidence_id,
                        {"evidence_id": evidence_id, "review_id": review_id,
                         "late": late, "review_due_at": review_deadline_text})

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_evidence", payload=body, create=create)

    def decide_review(self, *, request_id: str, actor_id: str, evidence_id: str,
                      approved: bool, note: str = ""):
        """独立复核结论。只有独立复核方的 reviewer 可以裁定，办公室与管理员不得代裁。"""

        payload = {"actor_id": actor_id, "evidence_id": evidence_id,
                   "approved": approved, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role != "reviewer":
                raise PermissionDenied("独立复核只能由 reviewer 执行")
            evidence = self._load(connection, "evidences", "evidence_id", evidence_id, "证据不存在")
            review = connection.execute(
                "SELECT * FROM reviews WHERE evidence_id=? ORDER BY created_at DESC, review_id DESC LIMIT 1",
                (evidence_id,),
            ).fetchone()
            if review["status"] != "pending":
                replay = self._replayed_receipt(connection, request_id, "decide_review", payload)
                if replay is not None:
                    return replay
                raise UnprocessableState("该复核已经作出结论")
            condition = self._load(connection, "stage_conditions", "condition_id",
                                   evidence["condition_id"], "核验条件不存在")
            stage = self._load(connection, "commitment_stages", "stage_id",
                               condition["stage_id"], "阶段不存在")
            commitment = self._commitment_row(connection, stage["commitment_id"])
            party = self._party_row(connection, commitment["project_id"], actor.organization_id)
            if party is None or party["party_role"] != "independent" or party["status"] != "active":
                raise PermissionDenied("复核方必须是本项目在册的独立机构")
            note = str(note or "")[:1000]
            now_text = self._now()
            now = self.clock.now()

            def create() -> tuple[str, str, dict[str, Any]]:
                remediation_deadline = None
                if approved:
                    evidence_status = "accepted"
                    condition_status = "accepted"
                else:
                    evidence_status = "rejected"
                    condition_status = "rejected"
                    # 整改期限为绝对时间戳，按条件原顺序排队；停机不影响期限。
                    remediation_deadline = (now + REMEDIATION_WINDOW)
                    remediation_deadline = remediation_deadline.isoformat().replace("+00:00", "Z")
                connection.execute(
                    "UPDATE reviews SET reviewer_actor_id=?, reviewer_organization_id=?, status=?,"
                    "decision_note=?, remediation_deadline_at=?, decided_at=? WHERE review_id=?",
                    (actor_id, actor.organization_id, "approved" if approved else "rejected",
                     note, remediation_deadline, now_text, review["review_id"]),
                )
                connection.execute(
                    "UPDATE evidences SET status=? WHERE evidence_id=?",
                    (evidence_status, evidence_id),
                )
                connection.execute(
                    "UPDATE stage_conditions SET status=?, accepted_evidence_id=?, updated_at=? "
                    "WHERE condition_id=?",
                    (condition_status, evidence_id if approved else None,
                     now_text, condition["condition_id"]),
                )
                self._snapshot(connection, "condition", condition["condition_id"],
                               {"condition_id": condition["condition_id"],
                                "stage_id": condition["stage_id"], "status": condition_status,
                                "accepted_evidence_id": evidence_id if approved else None,
                                "review_id": review["review_id"]}, now_text)
                append_event(connection, actor_id=actor_id,
                             action="review.approved" if approved else "review.rejected",
                             resource_type="review", resource_id=review["review_id"],
                             detail={"evidence_id": evidence_id, "condition_id": condition["condition_id"],
                                     "remediation_deadline_at": remediation_deadline},
                             occurred_at=now_text)
                return "review", review["review_id"], {
                    "review_id": review["review_id"],
                    "status": "approved" if approved else "rejected",
                    "remediation_deadline_at": remediation_deadline,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_review", payload=payload, create=create)

    def effect_stage(self, *, request_id: str, actor_id: str, stage_id: str):
        """把相互依赖的条件作为一组核验：全部有经独立复核接受的证据，阶段才生效。"""

        payload = {"actor_id": actor_id, "stage_id": stage_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            stage = self._load(connection, "commitment_stages", "stage_id", stage_id, "阶段不存在")
            commitment = self._commitment_row(connection, stage["commitment_id"])
            if not self._is_office(connection, actor, commitment["project_id"]):
                raise PermissionDenied("阶段生效由合作项目办公室核验发起")
            if stage["status"] != "pending":
                replay = self._replayed_receipt(connection, request_id, "effect_stage", payload)
                if replay is not None:
                    return replay
                raise UnprocessableState("阶段已经生效或拨付")
            conditions = connection.execute(
                "SELECT * FROM stage_conditions WHERE stage_id=? ORDER BY sequence", (stage_id,)
            ).fetchall()
            if not conditions:
                raise PreconditionFailed("阶段没有登记任何核验条件，不能生效")
            missing = [c["label"] for c in conditions if c["status"] != "accepted"]
            if missing:
                raise PreconditionFailed("条件组尚未全部通过独立复核",
                                         payload={"pending_conditions": missing})
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE commitment_stages SET status='effective', effective_at=? WHERE stage_id=?",
                    (now, stage_id),
                )
                if commitment["status"] == "committed":
                    connection.execute(
                        "UPDATE commitments SET status='effective', version=version+1 WHERE commitment_id=?",
                        (commitment["commitment_id"],),
                    )
                updated_stage = self._load(connection, "commitment_stages", "stage_id",
                                           stage_id, "阶段不存在")
                updated_commitment = self._commitment_row(connection, commitment["commitment_id"])
                self._snapshot(connection, "stage", stage_id,
                               {"stage_id": stage_id, "status": "effective",
                                "commitment_id": commitment["commitment_id"],
                                "sequence": stage["sequence"],
                                "disburse_amount_minor": stage["disburse_amount_minor"]}, now)
                self._snapshot_commitment(connection, updated_commitment, now)
                append_event(connection, actor_id=actor_id, action="stage.effective",
                             resource_type="stage", resource_id=stage_id,
                             detail={"commitment_id": commitment["commitment_id"],
                                     "disburse_amount_minor": stage["disburse_amount_minor"]},
                             occurred_at=now)
                return "stage", stage_id, {"stage_id": stage_id, "status": "effective"}

            return self._idempotent(connection, request_id=request_id,
                                    action="effect_stage", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 资金托管与拨付
    # ------------------------------------------------------------------

    def deposit_escrow(self, *, request_id: str, actor_id: str, project_id: str,
                       amount_minor: int, currency: str, reference: str = ""):
        """投资方向托管账户入金。入金不等于拨付：属地能力未形成前不能释放资金。"""

        payload = {"actor_id": actor_id, "project_id": project_id,
                   "amount_minor": amount_minor, "currency": currency, "reference": reference}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._project_row(connection, project_id)
            self._assert_project_writer(connection, actor, project_id)
            amount = self._amount(amount_minor, "amount_minor")
            if amount == 0:
                raise ValidationError("入金金额必须大于零")
            currency = self._text(currency, "currency", 8)
            reference = str(reference or "")[:200]
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                account = connection.execute(
                    "SELECT * FROM escrow_accounts WHERE project_id=?", (project_id,)
                ).fetchone()
                if account is None:
                    connection.execute(
                        "INSERT INTO escrow_accounts(project_id,currency,deposited_total,"
                        "disbursed_total,balance,updated_at) VALUES(?,?,?,0,?,?)",
                        (project_id, currency, amount, amount, now),
                    )
                else:
                    if account["currency"] != currency:
                        raise ValidationError("同一项目托管账户币种必须一致")
                    connection.execute(
                        "UPDATE escrow_accounts SET deposited_total=deposited_total+?, "
                        "balance=balance+?, updated_at=? WHERE project_id=?",
                        (amount, amount, now, project_id),
                    )
                deposit_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO escrow_deposits(deposit_id,project_id,amount_minor,reference,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (deposit_id, project_id, amount, reference, now),
                )
                account = connection.execute(
                    "SELECT * FROM escrow_accounts WHERE project_id=?", (project_id,)
                ).fetchone()
                self._snapshot(connection, "escrow", project_id,
                               {"deposited_total": account["deposited_total"],
                                "disbursed_total": account["disbursed_total"],
                                "balance": account["balance"]}, now)
                append_event(connection, actor_id=actor_id, action="escrow.deposited",
                             resource_type="escrow", resource_id=project_id,
                             detail={"amount_minor": amount, "currency": currency,
                                     "balance": account["balance"]}, occurred_at=now)
                return ("escrow_deposit", deposit_id,
                        {"deposit_id": deposit_id, "balance": account["balance"]})

            return self._idempotent(connection, request_id=request_id,
                                    action="deposit_escrow", payload=payload, create=create)

    def disburse_stage(self, *, request_id: str, actor_id: str, stage_id: str):
        """阶段生效后触发拨付；重复回调不能再次改变余额。"""

        payload = {"actor_id": actor_id, "stage_id": stage_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            stage = self._load(connection, "commitment_stages", "stage_id", stage_id, "阶段不存在")
            commitment = self._commitment_row(connection, stage["commitment_id"])
            if not self._is_office(connection, actor, commitment["project_id"]):
                raise PermissionDenied("拨付由合作项目办公室发起")
            now = self._now()
            # 支付通道重复回调（阶段已拨付）：幂等回放原拨付，绝不再次改变余额。
            existing = connection.execute(
                "SELECT * FROM disbursements WHERE stage_id=?", (stage_id,)
            ).fetchone()
            if existing is not None:
                return self._replay_existing(
                    connection, request_id=request_id, action="disburse_stage", payload=payload,
                    resource_type="disbursement", resource_id=existing["disbursement_id"],
                    response={"disbursement_id": existing["disbursement_id"],
                              "payment_reference": existing["payment_reference"],
                              "amount_minor": existing["amount_minor"],
                              "replayed_existing": True}, now=now)
            if stage["status"] != "effective":
                raise PreconditionFailed("阶段尚未生效，不能拨付（属地能力未形成前不得释放资金）")
            amount = stage["disburse_amount_minor"]
            if amount <= 0:
                raise UnprocessableState("该阶段没有配置拨付金额")
            account = connection.execute(
                "SELECT * FROM escrow_accounts WHERE project_id=?",
                (commitment["project_id"],),
            ).fetchone()
            if account is None or account["balance"] < amount:
                raise PreconditionFailed("托管余额不足，不能拨付")
            outstanding = self._outstanding(connection, commitment)
            if outstanding is not None and amount > outstanding:
                raise PreconditionFailed(
                    "拨付金额超过该承诺调整后的尚未兑现额",
                    payload={"outstanding_minor": outstanding, "requested_minor": amount})

            def create() -> tuple[str, str, dict[str, Any]]:
                disbursement_id = uuid.uuid4().hex
                payment_reference = f"pay-{uuid.uuid4().hex[:16]}"
                connection.execute(
                    "INSERT INTO disbursements(disbursement_id,project_id,commitment_id,stage_id,"
                    "amount_minor,status,payment_reference,created_at,paid_at) "
                    "VALUES(?,?,?,?,?, 'paid',?,?,?)",
                    (disbursement_id, commitment["project_id"], commitment["commitment_id"],
                     stage_id, amount, payment_reference, now, now),
                )
                connection.execute(
                    "UPDATE escrow_accounts SET disbursed_total=disbursed_total+?, "
                    "balance=balance-?, updated_at=? WHERE project_id=?",
                    (amount, amount, now, commitment["project_id"]),
                )
                connection.execute(
                    "UPDATE commitment_stages SET status='paid' WHERE stage_id=?", (stage_id,)
                )
                # 全部阶段付讫且调整后金额已兑现：承诺履约完成。
                # 已被标记局部违约的承诺不自动转为 fulfilled，违约事实需保留至退出处置。
                updated_commitment = self._commitment_row(connection, commitment["commitment_id"])
                remaining_outstanding = self._outstanding(connection, updated_commitment)
                unpaid_stage = connection.execute(
                    "SELECT 1 FROM commitment_stages WHERE commitment_id=? AND status!='paid' LIMIT 1",
                    (commitment["commitment_id"],),
                ).fetchone()
                if (updated_commitment["status"] != "breached"
                        and remaining_outstanding in (None, 0) and unpaid_stage is None):
                    connection.execute(
                        "UPDATE commitments SET status='fulfilled' WHERE commitment_id=?",
                        (commitment["commitment_id"],),
                    )
                    updated_commitment = self._commitment_row(connection, commitment["commitment_id"])
                self._snapshot_commitment(connection, updated_commitment, now)
                updated_account = connection.execute(
                    "SELECT * FROM escrow_accounts WHERE project_id=?",
                    (commitment["project_id"],),
                ).fetchone()
                self._snapshot(connection, "stage", stage_id,
                               {"stage_id": stage_id, "status": "paid",
                                "commitment_id": commitment["commitment_id"],
                                "disbursement_id": disbursement_id,
                                "amount_minor": amount}, now)
                self._snapshot(connection, "escrow", commitment["project_id"],
                               {"deposited_total": updated_account["deposited_total"],
                                "disbursed_total": updated_account["disbursed_total"],
                                "balance": updated_account["balance"]}, now)
                append_event(connection, actor_id=actor_id, action="escrow.disbursed",
                             resource_type="disbursement", resource_id=disbursement_id,
                             detail={"stage_id": stage_id, "amount_minor": amount,
                                     "payment_reference": payment_reference,
                                     "balance": updated_account["balance"]}, occurred_at=now)
                return ("disbursement", disbursement_id,
                        {"disbursement_id": disbursement_id, "payment_reference": payment_reference,
                         "amount_minor": amount, "balance": updated_account["balance"]})

            return self._idempotent(connection, request_id=request_id,
                                    action="disburse_stage", payload=payload, create=create)

    def _replay_existing(self, connection, *, request_id: str, action: str,
                         payload: dict[str, Any], resource_type: str, resource_id: str,
                         response: dict[str, Any], now: str):
        """为重复回调返回（必要时登记）一条指向既有资源的幂等回执，不改变状态或余额。"""

        from .models import WriteReceipt

        existing = self._replayed_receipt(connection, request_id, action, payload)
        if existing is not None:
            return existing
        request_id = self._identifier(request_id, "request_id")
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload),
             resource_type, resource_id, canonical_json(response), now),
        )
        return WriteReceipt(request_id, resource_type, resource_id, True)

    # ------------------------------------------------------------------
    # 责任调整：局部违约、范围缩减、争议减免
    # ------------------------------------------------------------------

    def _record_adjustment(self, *, request_id: str, actor_id: str, commitment_id: str,
                           kind: str, amount_delta_minor: int, detail: dict[str, Any],
                           action: str, dispute_id: str | None = None):
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "kind": kind,
                   "amount_delta_minor": amount_delta_minor, "detail": detail,
                   "dispute_id": dispute_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = self._commitment_row(connection, commitment_id)
            if not self._is_office(connection, actor, row["project_id"]):
                raise PermissionDenied("责任调整由合作项目办公室登记")
            if kind not in ADJUSTMENT_KINDS:
                raise ValidationError("调整类别不被允许")
            if row["status"] in TERMINAL_STATUSES:
                raise UnprocessableState("承诺已终结，不能再调整")
            if isinstance(amount_delta_minor, bool) or not isinstance(amount_delta_minor, int):
                raise ValidationError("amount_delta_minor 必须是整数")
            if not isinstance(detail, dict):
                raise ValidationError("detail 必须是对象")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                # 调整只能改变尚未兑现的部分：不允许把未兑现额调成负数，也不回滚已拨金额。
                current_outstanding = self._outstanding(connection, row)
                if current_outstanding is not None and current_outstanding + amount_delta_minor < 0:
                    raise UnprocessableState("调整不能使尚未兑现金额为负，已履行部分不得回滚")
                adjustment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO responsibility_adjustments(adjustment_id,commitment_id,kind,"
                    "amount_delta_minor,detail_json,dispute_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (adjustment_id, commitment_id, kind, amount_delta_minor,
                     canonical_json(detail), dispute_id, actor_id, now),
                )
                if kind == "partial_breach" and row["status"] in ("committed", "effective"):
                    connection.execute(
                        "UPDATE commitments SET status='breached' WHERE commitment_id=?",
                        (commitment_id,),
                    )
                updated = self._commitment_row(connection, commitment_id)
                self._snapshot_commitment(connection, updated, now)
                append_event(connection, actor_id=actor_id, action=action,
                             resource_type="responsibility_adjustment",
                             resource_id=adjustment_id,
                             detail={"commitment_id": commitment_id, "kind": kind,
                                     "amount_delta_minor": amount_delta_minor}, occurred_at=now)
                return "responsibility_adjustment", adjustment_id, {
                    "adjustment_id": adjustment_id,
                    "outstanding_minor": self._outstanding(connection, updated),
                }

            return self._idempotent(connection, request_id=request_id, action=action,
                                    payload=payload, create=create)

    def record_partial_breach(self, *, request_id: str, actor_id: str, commitment_id: str,
                              amount_delta_minor: int = 0, detail: dict[str, Any] | None = None):
        """局部违约：记录违约事实，可核减未兑现部分；原始责任与已履行部分保持不变。"""

        return self._record_adjustment(request_id=request_id, actor_id=actor_id,
                                       commitment_id=commitment_id, kind="partial_breach",
                                       amount_delta_minor=amount_delta_minor, detail=detail or {},
                                       action="commitment.partial_breach")

    def reduce_scope(self, *, request_id: str, actor_id: str, commitment_id: str,
                     amount_delta_minor: int, detail: dict[str, Any] | None = None):
        """范围缩减：只能核减尚未兑现部分，并须说明缩范围原因。"""

        if isinstance(amount_delta_minor, bool) or not isinstance(amount_delta_minor, int) \
                or amount_delta_minor >= 0:
            raise ValidationError("范围缩减必须以负整数表示核减额")
        return self._record_adjustment(request_id=request_id, actor_id=actor_id,
                                       commitment_id=commitment_id, kind="scope_reduction",
                                       amount_delta_minor=amount_delta_minor,
                                       detail=detail or {}, action="commitment.scope_reduced")

    # ------------------------------------------------------------------
    # 争议与争议结论
    # ------------------------------------------------------------------

    def open_dispute(self, *, request_id: str, actor_id: str, project_id: str,
                     commitment_id: str | None, title: str, detail: dict[str, Any]):
        if not isinstance(detail, dict):
            raise ValidationError("detail 必须是对象")
        payload = {"actor_id": actor_id, "project_id": project_id, "commitment_id": commitment_id,
                   "title": title, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._project_row(connection, project_id)
            self._assert_project_writer(connection, actor, project_id)
            if commitment_id is not None:
                row = self._commitment_row(connection, commitment_id)
                if row["project_id"] != project_id:
                    raise ValidationError("承诺不属于该项目")
            title = self._text(title, "title")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                dispute_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO disputes(dispute_id,project_id,commitment_id,status,title,"
                    "detail_json,opened_by,opened_at) VALUES(?,?,?, 'open',?,?,?,?)",
                    (dispute_id, project_id, commitment_id, title,
                     canonical_json(detail), actor_id, now),
                )
                self._snapshot(connection, "dispute", dispute_id,
                               {"dispute_id": dispute_id, "status": "open",
                                "project_id": project_id, "commitment_id": commitment_id}, now)
                append_event(connection, actor_id=actor_id, action="dispute.opened",
                             resource_type="dispute", resource_id=dispute_id,
                             detail={"project_id": project_id, "commitment_id": commitment_id},
                             occurred_at=now)
                return "dispute", dispute_id, {"dispute_id": dispute_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="open_dispute", payload=payload, create=create)

    def conclude_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                         conclusion: dict[str, Any], relief_delta_minor: int = 0):
        """登记争议结论。结论独立成档，可附一次性责任减免（只影响未兑现部分）。"""

        if not isinstance(conclusion, dict) or not conclusion:
            raise ValidationError("conclusion 必须是非空对象")
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "conclusion": conclusion,
                   "relief_delta_minor": relief_delta_minor}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            dispute = self._load(connection, "disputes", "dispute_id", dispute_id, "争议不存在")
            if not self._is_office(connection, actor, dispute["project_id"]):
                raise PermissionDenied("争议结论由合作项目办公室登记")
            if dispute["status"] != "open":
                raise UnprocessableState("争议已经结论")
            if isinstance(relief_delta_minor, bool) or not isinstance(relief_delta_minor, int) \
                    or relief_delta_minor > 0:
                raise ValidationError("争议减免必须以零或负整数表示")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE disputes SET status='concluded', conclusion_json=?, concluded_by=?, "
                    "concluded_at=? WHERE dispute_id=?",
                    (canonical_json(conclusion), actor_id, now, dispute_id),
                )
                adjustment_id = None
                if dispute["commitment_id"] is not None and relief_delta_minor < 0:
                    commitment_row = self._commitment_row(connection, dispute["commitment_id"])
                    current_outstanding = self._outstanding(connection, commitment_row)
                    if current_outstanding is not None \
                            and current_outstanding + relief_delta_minor < 0:
                        raise UnprocessableState("争议减免不能超过尚未兑现金额")
                    adjustment_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO responsibility_adjustments(adjustment_id,commitment_id,kind,"
                        "amount_delta_minor,detail_json,dispute_id,created_by,created_at) "
                        "VALUES(?,?,'dispute_relief',?,?,?,?,?)",
                        (adjustment_id, dispute["commitment_id"], relief_delta_minor,
                         canonical_json({"dispute_id": dispute_id}), dispute_id, actor_id, now),
                    )
                    self._snapshot_commitment(connection, commitment_row, now)
                self._snapshot(connection, "dispute", dispute_id,
                               {"dispute_id": dispute_id, "status": "concluded",
                                "adjustment_id": adjustment_id}, now)
                append_event(connection, actor_id=actor_id, action="dispute.concluded",
                             resource_type="dispute", resource_id=dispute_id,
                             detail={"adjustment_id": adjustment_id,
                                     "relief_delta_minor": relief_delta_minor}, occurred_at=now)
                return "dispute", dispute_id, {"dispute_id": dispute_id, "status": "concluded",
                                               "adjustment_id": adjustment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="conclude_dispute", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 共享成果与唯一认领
    # ------------------------------------------------------------------

    def register_outcome(self, *, request_id: str, actor_id: str, title: str,
                         measure_unit: str | None = None, measure_value: int | None = None,
                         producer_project_id: str | None = None):
        payload = {"actor_id": actor_id, "title": title, "measure_unit": measure_unit,
                   "measure_value": measure_value, "producer_project_id": producer_project_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            if producer_project_id is not None:
                self._project_row(connection, producer_project_id)
                self._assert_project_visible(connection, actor, producer_project_id)
            title = self._text(title, "title")
            if measure_value is not None and (isinstance(measure_value, bool)
                                              or not isinstance(measure_value, int)
                                              or measure_value < 0):
                raise ValidationError("measure_value 必须是非负整数")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                outcome_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO outcomes(outcome_id,title,producer_project_id,measure_unit,"
                    "measure_value,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (outcome_id, title, producer_project_id, measure_unit, measure_value,
                     actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="outcome.registered",
                             resource_type="outcome", resource_id=outcome_id,
                             detail={"title": title,
                                     "producer_project_id": producer_project_id}, occurred_at=now)
                return "outcome", outcome_id, {"outcome_id": outcome_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_outcome", payload=payload, create=create)

    def claim_outcome(self, *, request_id: str, actor_id: str, outcome_id: str,
                      project_id: str, commitment_id: str | None = None,
                      claimed_value: int | None = None):
        """把一项共享成果认领到唯一项目。重复回调幂等，跨项目重复认领直接拒绝。"""

        payload = {"actor_id": actor_id, "outcome_id": outcome_id, "project_id": project_id,
                   "commitment_id": commitment_id, "claimed_value": claimed_value}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._project_row(connection, project_id)
            self._assert_project_writer(connection, actor, project_id)
            if connection.execute("SELECT 1 FROM outcomes WHERE outcome_id=?",
                                  (outcome_id,)).fetchone() is None:
                raise NotFoundError("成果不存在")
            if commitment_id is not None:
                row = self._commitment_row(connection, commitment_id)
                if row["project_id"] != project_id:
                    raise ValidationError("承诺不属于该项目")
            if claimed_value is not None and (isinstance(claimed_value, bool)
                                              or not isinstance(claimed_value, int)
                                              or claimed_value < 0):
                raise ValidationError("claimed_value 必须是非负整数")
            now = self._now()

            existing = connection.execute(
                "SELECT * FROM outcome_claims WHERE outcome_id=?", (outcome_id,)
            ).fetchone()
            if existing is not None and existing["project_id"] != project_id:
                raise ConflictError("该共享成果已经被其他项目认领，不能重复计入")

            def create() -> tuple[str, str, dict[str, Any]]:
                claim_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO outcome_claims(claim_id,outcome_id,project_id,commitment_id,"
                    "claimed_value,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (claim_id, outcome_id, project_id, commitment_id, claimed_value,
                     actor_id, now),
                )
                self._snapshot(connection, "outcome_claim", outcome_id,
                               {"outcome_id": outcome_id, "project_id": project_id,
                                "commitment_id": commitment_id,
                                "claimed_value": claimed_value}, now)
                append_event(connection, actor_id=actor_id, action="outcome.claimed",
                             resource_type="outcome_claim", resource_id=claim_id,
                             detail={"outcome_id": outcome_id, "project_id": project_id},
                             occurred_at=now)
                return "outcome_claim", claim_id, {"claim_id": claim_id, "outcome_id": outcome_id}

            if existing is not None:
                # 同一项目的重复认领回调：幂等回放，不新增记录、不改变归属。
                return self._replay_existing(
                    connection, request_id=request_id, action="claim_outcome", payload=payload,
                    resource_type="outcome_claim", resource_id=existing["claim_id"],
                    response={"claim_id": existing["claim_id"], "outcome_id": existing["outcome_id"],
                              "replayed_existing": True}, now=now)
            return self._idempotent(connection, request_id=request_id,
                                    action="claim_outcome", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 项目关闭（退出）
    # ------------------------------------------------------------------

    def close_project(self, *, request_id: str, actor_id: str, project_id: str):
        payload = {"actor_id": actor_id, "project_id": project_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._project_row(connection, project_id)
            if not self._is_office(connection, actor, project_id):
                raise PermissionDenied("只有合作项目办公室可以关闭项目")
            project = self._project_row(connection, project_id)
            if project["status"] != "active":
                raise UnprocessableState("项目已经关闭")
            open_commitments = connection.execute(
                "SELECT * FROM commitments WHERE project_id=? AND status NOT IN ('fulfilled','closed')",
                (project_id,),
            ).fetchall()
            pending = []
            for row in open_commitments:
                outstanding = self._outstanding(connection, row)
                # 未兑现的资金承诺必须先了结；退出责任类承诺在项目关闭后继续有效，不阻断退出。
                if outstanding and outstanding > 0:
                    pending.append({"commitment_id": row["commitment_id"],
                                    "outstanding_minor": outstanding})
            if pending:
                raise PreconditionFailed("仍有未履行的资金承诺，不能退出",
                                         payload={"pending_commitments": pending})
            open_disputes = connection.execute(
                "SELECT COUNT(*) AS c FROM disputes WHERE project_id=? AND status='open'",
                (project_id,),
            ).fetchone()["c"]
            if open_disputes:
                raise PreconditionFailed("仍有未结论的争议，不能退出")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE projects SET status='closed' WHERE project_id=?", (project_id,)
                )
                # 退出责任类承诺在项目关闭后仍然有效并可追溯，其余未结承诺随项目关闭。
                connection.execute(
                    "UPDATE commitments SET status='closed' WHERE project_id=? "
                    "AND status NOT IN ('fulfilled','closed') AND commitment_type!='exit_responsibility'",
                    (project_id,),
                )
                surviving = connection.execute(
                    "SELECT commitment_id FROM commitments WHERE project_id=? "
                    "AND commitment_type='exit_responsibility' AND status NOT IN ('fulfilled','closed')",
                    (project_id,),
                ).fetchall()
                for surviving_row in surviving:
                    self._snapshot_commitment(
                        connection, self._commitment_row(connection, surviving_row["commitment_id"]), now)
                self._snapshot(connection, "project", project_id,
                               {"project_id": project_id, "status": "closed",
                                "surviving_exit_commitments": [r["commitment_id"] for r in surviving]}, now)
                append_event(connection, actor_id=actor_id, action="project.closed",
                             resource_type="project", resource_id=project_id,
                             detail={"surviving_exit_commitments": [r["commitment_id"] for r in surviving]},
                             occurred_at=now)
                return "project", project_id, {"project_id": project_id, "status": "closed",
                                               "surviving_exit_commitments":
                                                   [r["commitment_id"] for r in surviving]}

            return self._idempotent(connection, request_id=request_id,
                                    action="close_project", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 读取侧：可见范围、对账与历史时点还原
    # ------------------------------------------------------------------

    def get_dispute(self, *, actor_id: str, dispute_id: str) -> DisputeView:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            row = self._load(connection, "disputes", "dispute_id", dispute_id, "争议不存在")
            self._assert_project_visible(connection, actor, row["project_id"])
            conclusion = json.loads(row["conclusion_json"]) if row["conclusion_json"] else None
            return DisputeView(row["dispute_id"], row["project_id"], row["commitment_id"],
                               row["status"], row["title"], json.loads(row["detail_json"]),
                               row["opened_by"], row["opened_at"], conclusion,
                               row["concluded_by"], row["concluded_at"])

    def get_project(self, *, actor_id: str, project_id: str) -> Project:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._assert_project_visible(connection, actor, project_id)
            row = self._project_row(connection, project_id)
            parties = []
            for party in connection.execute(
                "SELECT * FROM project_parties WHERE project_id=? ORDER BY joined_at, organization_id",
                (project_id,),
            ):
                parties.append({"organization_id": party["organization_id"],
                                "party_role": party["party_role"], "status": party["status"],
                                "joined_at": party["joined_at"], "left_at": party["left_at"]})
            return Project(row["project_id"], row["name"], row["status"],
                           row["created_by"], row["created_at"], tuple(parties))

    def list_commitments(self, *, actor_id: str, project_id: str,
                         commitment_type: str | None = None) -> list[Commitment]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._assert_project_visible(connection, actor, project_id)
            query = "SELECT * FROM commitments WHERE project_id=?"
            parameters: list[Any] = [project_id]
            if commitment_type:
                query += " AND commitment_type=?"
                parameters.append(commitment_type)
            query += " ORDER BY created_at, commitment_id"
            result = []
            for row in connection.execute(query, parameters):
                stages = []
                for stage in connection.execute(
                    "SELECT * FROM commitment_stages WHERE commitment_id=? ORDER BY sequence",
                    (row["commitment_id"],),
                ):
                    stages.append({"stage_id": stage["stage_id"], "sequence": stage["sequence"],
                                   "name": stage["name"], "due_at": stage["due_at"],
                                   "disburse_amount_minor": stage["disburse_amount_minor"],
                                   "status": stage["status"], "effective_at": stage["effective_at"]})
                result.append(Commitment(
                    row["commitment_id"], row["project_id"], row["commitment_type"],
                    row["provider_organization_id"], row["title"],
                    json.loads(row["terms_json"]),
                    row["original_amount_minor"], row["currency"], row["status"],
                    row["sealed_at"], row["created_by"], row["created_at"], row["version"],
                    tuple(stages),
                ))
            return result

    def get_condition(self, *, actor_id: str, condition_id: str) -> ConditionView:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            condition = self._load(connection, "stage_conditions", "condition_id",
                                   condition_id, "核验条件不存在")
            stage = self._load(connection, "commitment_stages", "stage_id",
                               condition["stage_id"], "阶段不存在")
            commitment = self._commitment_row(connection, stage["commitment_id"])
            self._assert_project_visible(connection, actor, commitment["project_id"])
            evidences = []
            for evidence in connection.execute(
                "SELECT * FROM evidences WHERE condition_id=? ORDER BY submitted_at, evidence_id",
                (condition_id,),
            ):
                review = connection.execute(
                    "SELECT * FROM reviews WHERE evidence_id=? ORDER BY created_at DESC LIMIT 1",
                    (evidence["evidence_id"],),
                ).fetchone()
                evidences.append({"evidence_id": evidence["evidence_id"],
                                  "status": evidence["status"], "late": bool(evidence["late"]),
                                  "submitted_at": evidence["submitted_at"],
                                  "submitter_organization_id": evidence["submitter_organization_id"],
                                  "review_status": review["status"],
                                  "review_due_at": review["due_at"],
                                  "reviewer_organization_id": review["reviewer_organization_id"],
                                  "remediation_deadline_at": review["remediation_deadline_at"]})
            return ConditionView(condition["condition_id"], condition["stage_id"],
                                 condition["sequence"], condition["label"], condition["due_at"],
                                 condition["status"], condition["accepted_evidence_id"],
                                 tuple(evidences))

    def responsibility(self, *, actor_id: str, commitment_id: str,
                       at: str | None = None) -> ResponsibilityView:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            row = self._commitment_row(connection, commitment_id)
            self._assert_project_visible(connection, actor, row["project_id"])
            adjustments = tuple(self._adjustments(connection, commitment_id, at))
            disbursed_query = ("SELECT COALESCE(SUM(amount_minor),0) AS t FROM disbursements "
                               "WHERE commitment_id=? AND status='paid'")
            parameters: list[Any] = [commitment_id]
            if at is not None:
                disbursed_query += " AND paid_at<=?"
                parameters.append(at)
            disbursed = connection.execute(disbursed_query, parameters).fetchone()["t"]
            current = self._current_provider(connection, row, at)
            return ResponsibilityView(
                row["commitment_id"], row["project_id"], row["commitment_type"],
                row["provider_organization_id"], row["status"],
                row["original_amount_minor"], row["currency"], adjustments,
                disbursed, self._outstanding(connection, row, at),
                None if current == row["provider_organization_id"] else current,
            )

    def escrow(self, *, actor_id: str, project_id: str) -> EscrowView:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._assert_project_visible(connection, actor, project_id)
            row = connection.execute(
                "SELECT * FROM escrow_accounts WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is None:
                return EscrowView(project_id, "", 0, 0, 0, self._now())
            return EscrowView(row["project_id"], row["currency"], row["deposited_total"],
                              row["disbursed_total"], row["balance"], row["updated_at"])

    def reconcile(self, *, actor_id: str, project_id: str,
                  at: str | None = None) -> dict[str, Any]:
        """监督对账：上存余额、已拨金额与未履行承诺三方对齐。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._assert_project_visible(connection, actor, project_id)
            account = connection.execute(
                "SELECT * FROM escrow_accounts WHERE project_id=?", (project_id,)
            ).fetchone()
            paid_query = ("SELECT COALESCE(SUM(amount_minor),0) AS t FROM disbursements "
                          "WHERE project_id=? AND status='paid'")
            parameters: list[Any] = [project_id]
            deposit_query = ("SELECT COALESCE(SUM(amount_minor),0) AS t FROM escrow_deposits "
                             "WHERE project_id=?")
            deposit_parameters: list[Any] = [project_id]
            if at is not None:
                paid_query += " AND paid_at<=?"
                parameters.append(at)
                deposit_query += " AND created_at<=?"
                deposit_parameters.append(at)
            deposited_ledger = connection.execute(deposit_query, deposit_parameters).fetchone()["t"]
            disbursed_ledger = connection.execute(paid_query, parameters).fetchone()["t"]
            if at is None and account is not None:
                ledger_balanced = (account["deposited_total"] == deposited_ledger
                                   and account["disbursed_total"] == disbursed_ledger
                                   and account["balance"] == deposited_ledger - disbursed_ledger)
            else:
                ledger_balanced = True
            commitments = []
            total_outstanding = 0
            commitment_query = "SELECT * FROM commitments WHERE project_id=?"
            commitment_parameters: list[Any] = [project_id]
            if at is not None:
                commitment_query += " AND created_at<=?"
                commitment_parameters.append(at)
            for row in connection.execute(commitment_query, commitment_parameters):
                outstanding = self._outstanding(connection, row, at)
                if outstanding:
                    total_outstanding += outstanding
                commitments.append({
                    "commitment_id": row["commitment_id"],
                    "commitment_type": row["commitment_type"],
                    "original_provider_organization_id": row["provider_organization_id"],
                    "responsible_organization_id": self._current_provider(connection, row, at),
                    "status": row["status"],
                    "outstanding_minor": outstanding,
                })
            balance = (account["balance"] if at is None and account is not None
                       else max(deposited_ledger - disbursed_ledger, 0))
            return {
                "project_id": project_id,
                "currency": account["currency"] if account is not None else None,
                "at": at,
                "deposited_total": deposited_ledger,
                "disbursed_total": disbursed_ledger,
                "balance": balance,
                "outstanding_commitments_total": total_outstanding,
                "ledger_balanced": ledger_balanced,
                "commitments": commitments,
            }

    def outcome_assignments(self, *, actor_id: str, project_id: str,
                            at: str | None = None) -> list[dict[str, Any]]:
        """还原某时点每项成果归谁、对应承诺当前由哪方负责。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._assert_project_visible(connection, actor, project_id)
            query = ("SELECT o.outcome_id, o.title, o.measure_unit, o.measure_value, "
                     "c.claim_id, c.project_id, c.commitment_id, c.claimed_value, c.created_at "
                     "FROM outcome_claims c JOIN outcomes o ON o.outcome_id=c.outcome_id "
                     "WHERE c.project_id=?")
            parameters: list[Any] = [project_id]
            if at is not None:
                query += " AND c.created_at<=?"
                parameters.append(at)
            query += " ORDER BY c.created_at, c.claim_id"
            items = []
            for row in connection.execute(query, parameters):
                responsible = None
                status = None
                if row["commitment_id"]:
                    commitment = connection.execute(
                        "SELECT * FROM commitments WHERE commitment_id=?",
                        (row["commitment_id"],),
                    ).fetchone()
                    if commitment is not None:
                        responsible = self._current_provider(connection, commitment, at)
                        status = commitment["status"]
                items.append({"outcome_id": row["outcome_id"], "title": row["title"],
                              "claimed_by_project_id": row["project_id"],
                              "commitment_id": row["commitment_id"],
                              "responsible_organization_id": responsible,
                              "commitment_status": status,
                              "claimed_value": row["claimed_value"],
                              "claimed_at": row["created_at"]})
            return items

    def history_at(self, *, actor_id: str, entity_type: str, entity_id: str,
                   at: str) -> dict[str, Any]:
        """读取某一历史时点的实体状态（只增快照还原）。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            allowed = {"commitment", "stage", "condition", "escrow", "project",
                       "dispute", "outcome_claim"}
            if entity_type not in allowed:
                raise ValidationError("entity_type 不支持历史还原")
            row = connection.execute(
                "SELECT * FROM state_snapshots WHERE entity_type=? AND entity_id=? AND valid_from<=? "
                "ORDER BY valid_from DESC, sequence DESC LIMIT 1",
                (entity_type, entity_id, at),
            ).fetchone()
            if row is None:
                raise NotFoundError("该时点之前没有可还原的状态")
            return {"entity_type": entity_type, "entity_id": entity_id,
                    "valid_from": row["valid_from"], "state": json.loads(row["state_json"])}

    def pending_reviews(self, *, actor_id: str, project_id: str) -> list[dict[str, Any]]:
        """按原顺序列出未决复核与整改期限。

        期限是绝对时间戳，服务中断恢复后顺序和到期时间都不变；逾期项以 overdue 标出。
        """

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._assert_project_visible(connection, actor, project_id)
            now_text = self._now()
            rows = connection.execute(
                "SELECT r.*, e.condition_id, cs.sequence AS condition_sequence, "
                "cst.sequence AS stage_sequence, cs.label AS condition_label "
                "FROM reviews r "
                "JOIN evidences e ON e.evidence_id=r.evidence_id "
                "JOIN stage_conditions cs ON cs.condition_id=e.condition_id "
                "JOIN commitment_stages cst ON cst.stage_id=cs.stage_id "
                "JOIN commitments cm ON cm.commitment_id=cst.commitment_id "
                "WHERE cm.project_id=? AND r.status='pending' "
                "ORDER BY r.due_at, cst.sequence, cs.sequence, r.created_at",
                (project_id,),
            ).fetchall()
            items = [{"kind": "review", "review_id": row["review_id"],
                      "evidence_id": row["evidence_id"], "condition_id": row["condition_id"],
                      "label": row["condition_label"], "due_at": row["due_at"],
                      "overdue": now_text > row["due_at"]} for row in rows]
            # 整改中的条件：取最近一次驳回复核的整改期限，按期限与条件原顺序继续排队。
            remediation_rows = connection.execute(
                "SELECT r.*, e.condition_id, cs.sequence AS condition_sequence, "
                "cst.sequence AS stage_sequence, cs.label AS condition_label "
                "FROM reviews r "
                "JOIN evidences e ON e.evidence_id=r.evidence_id "
                "JOIN stage_conditions cs ON cs.condition_id=e.condition_id "
                "JOIN commitment_stages cst ON cst.stage_id=cs.stage_id "
                "JOIN commitments cm ON cm.commitment_id=cst.commitment_id "
                "WHERE cm.project_id=? AND r.status='rejected' AND cs.status='rejected' "
                "AND r.remediation_deadline_at IS NOT NULL "
                "ORDER BY r.remediation_deadline_at, cst.sequence, cs.sequence",
                (project_id,),
            ).fetchall()
            for row in remediation_rows:
                items.append({"kind": "remediation", "review_id": row["review_id"],
                              "evidence_id": row["evidence_id"], "condition_id": row["condition_id"],
                              "label": row["condition_label"],
                              "due_at": row["remediation_deadline_at"],
                              "overdue": now_text > (row["remediation_deadline_at"] or "")})
            items.sort(key=lambda item: (item["due_at"] or ""))
            return items
