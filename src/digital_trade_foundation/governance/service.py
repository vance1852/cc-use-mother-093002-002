"""跨境合作承诺治理服务：从谈判、正式承诺、履约核验到退出的全周期规则。

本模块复用基础服务的操作者、权限、幂等回执、事务和哈希链审计能力，
在此之上分别管理各方投入、受益群体、属地化目标、知识与数据权属、
资金托管、风险保障和退出责任七类承诺，并区分谈判稿、正式承诺、
履约证据与争议结论四类文书。所有状态变化同时写入治理事件日志，
监督人员可据此还原任一历史时点的成果归属与责任方。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..audit import append_event, canonical_json, digest
from ..clock import Clock, SystemClock
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..models import Actor, WriteReceipt
from ..service import DomainService
from ..storage import Database
from .schema import ensure_schema


PARTY_ROLES = frozenset({"tech_provider", "investor", "local_institution", "implementing_agency"})
COMMITMENT_CATEGORIES = frozenset({
    "contribution", "beneficiary", "localization", "ip_data",
    "escrow", "risk_guarantee", "exit_duty",
})
DOCUMENT_KINDS = frozenset({
    "negotiation_draft", "formal_commitment", "performance_evidence", "dispute_ruling",
})
SUPERVISOR_ROLES = ("admin", "reviewer", "auditor")
FULFILLABLE_STATUSES = ("active", "breached", "rectifying")
OPEN_COMMITMENT_STATUSES = ("draft", "committed", "active", "breached", "rectifying")


class GovernanceService:
    """协调承诺治理的权限、幂等、事务、事件日志与审计规则。"""

    def __init__(self, database: Database, domain: DomainService | None = None,
                 clock: Clock | None = None) -> None:
        self.database = database
        if clock is None:
            clock = domain.clock if domain is not None else SystemClock()
        self.clock = clock
        self.domain = domain or DomainService(database, clock)
        ensure_schema(database.connection)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        return self.domain._identifier(value, field)

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        return self.domain._text(value, field, limit)

    def _actor(self, connection, actor_id: str) -> Actor:
        return self.domain._actor(connection, actor_id)

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _amount(self, value: Any, field: str, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数（最小货币单位）")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValidationError(f"{field} 必须为正数")
        return value

    def _timestamp(self, value: str, field: str) -> str:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是带时区的 ISO 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds") \
            .replace("+00:00", "Z")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create) -> WriteReceipt:
        return self.domain._idempotent(connection, request_id=request_id,
                                       action=action, payload=payload, create=create)

    def _emit(self, connection, *, agreement_id: str, event_type: str,
              payload: dict[str, Any], actor_id: str) -> None:
        """同时写治理事件日志（供历史还原）和哈希链审计（供防篡改）。"""

        occurred_at = self._now()
        connection.execute(
            "INSERT INTO governance_events(agreement_id,event_type,payload_json,actor_id,occurred_at) "
            "VALUES(?,?,?,?,?)",
            (agreement_id, event_type, canonical_json(payload), actor_id, occurred_at),
        )
        append_event(connection, actor_id=actor_id, action=f"governance.{event_type}",
                     resource_type="agreement", resource_id=agreement_id,
                     detail=payload, occurred_at=occurred_at)

    # ------------------------------------------------------------------
    # 行读取与权限
    # ------------------------------------------------------------------

    def _agreement(self, connection, agreement_id: str):
        row = connection.execute(
            "SELECT * FROM governance_agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("合作协议不存在")
        return row

    def _ensure_not_closed(self, agreement) -> None:
        if agreement["status"] in ("exited", "terminated"):
            raise ConflictError("协议已退出，禁止再变更")

    def _ensure_structural_open(self, agreement) -> None:
        if agreement["status"] in ("exiting", "exited", "terminated"):
            raise ConflictError("协议已进入退出流程，禁止新增结构性内容")

    def _party(self, connection, agreement_id: str, party_id: str):
        row = connection.execute(
            "SELECT * FROM governance_parties WHERE agreement_id=? AND party_id=?",
            (agreement_id, party_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("参与方不存在")
        return row

    def _commitment(self, connection, commitment_id: str):
        row = connection.execute(
            "SELECT * FROM governance_commitments WHERE commitment_id=?", (commitment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("承诺不存在")
        return row

    def _active_party_of_org(self, connection, agreement_id: str, organization_id: str):
        return connection.execute(
            "SELECT * FROM governance_parties WHERE agreement_id=? AND organization_id=? AND status='active'",
            (agreement_id, organization_id),
        ).fetchone()

    def _require_party_operator(self, connection, actor: Actor, agreement_id: str):
        """要求操作者是协议某一在册参与方的 operator。"""

        if actor.role != "operator":
            raise PermissionDenied("只有参与方操作员可以执行该动作")
        party = self._active_party_of_org(connection, agreement_id, actor.organization_id)
        if party is None:
            raise PermissionDenied("操作者不属于该协议的参与方")
        return party

    def _require_responsible_operator(self, connection, actor: Actor, commitment) -> None:
        """要求操作者恰好是承诺当前责任方组织的 operator。"""

        if actor.role != "operator":
            raise PermissionDenied("只有责任方操作员可以执行该动作")
        party = connection.execute(
            "SELECT * FROM governance_parties WHERE party_id=? AND status='active'",
            (commitment["responsible_party_id"],),
        ).fetchone()
        if party is None or party["organization_id"] != actor.organization_id:
            raise PermissionDenied("操作者不是该承诺的责任方")

    def _require_view(self, connection, actor: Actor, agreement_id: str) -> None:
        """监督角色可见全部协议；参与方仅可见本组织参与的协议。"""

        if actor.role in SUPERVISOR_ROLES:
            return
        if actor.role == "operator" and \
                self._active_party_of_org(connection, agreement_id, actor.organization_id) is not None:
            return
        raise PermissionDenied("当前角色不能查看该合作协议")

    def _require_supervisor(self, actor: Actor) -> None:
        self._require(actor, *SUPERVISOR_ROLES)

    # ------------------------------------------------------------------
    # 协议与参与方
    # ------------------------------------------------------------------

    def create_agreement(self, *, request_id: str, actor_id: str, site_id: str,
                         agreement_id: str, title: str, currency: str = "USD") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "agreement_id": agreement_id,
                   "title": title, "currency": currency}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            agreement_id = self._identifier(agreement_id, "agreement_id")
            title = self._text(title, "title")
            currency = self._text(currency, "currency", 12)

            def create() -> tuple[str, str, dict[str, Any]]:
                site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
                if site is None:
                    raise NotFoundError("场所不存在")
                if actor.role != "admin" and actor.organization_id != site["organization_id"]:
                    raise PermissionDenied("不能为其他组织的节点建立合作协议")
                try:
                    connection.execute(
                        "INSERT INTO governance_agreements(agreement_id,site_id,title,status,currency,"
                        "created_by,created_at) VALUES(?,?,?,'negotiating',?,?,?)",
                        (agreement_id, site_id, title, currency, actor_id, self._now()),
                    )
                    connection.execute(
                        "INSERT INTO governance_escrow_accounts(account_id,agreement_id,currency,opened_at) "
                        "VALUES(?,?,?,?)",
                        (f"esc-{agreement_id}", agreement_id, currency, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("协议编号已经存在") from exc
                self._emit(connection, agreement_id=agreement_id, event_type="agreement_created",
                           payload={"agreement_id": agreement_id, "site_id": site_id,
                                    "title": title, "currency": currency}, actor_id=actor_id)
                return "agreement", agreement_id, {"agreement_id": agreement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.create_agreement", payload=payload, create=create)

    def add_party(self, *, request_id: str, actor_id: str, agreement_id: str, party_id: str,
                  organization_id: str, party_role: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "party_id": party_id,
                   "organization_id": organization_id, "party_role": party_role}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            party_id = self._identifier(party_id, "party_id")
            if party_role not in PARTY_ROLES:
                raise ValidationError("party_role 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                self._ensure_structural_open(agreement)
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (organization_id,)).fetchone() is None:
                    raise NotFoundError("组织不存在")
                try:
                    connection.execute(
                        "INSERT INTO governance_parties(party_id,agreement_id,organization_id,party_role,"
                        "status,created_at) VALUES(?,?,?,?,'active',?)",
                        (party_id, agreement_id, organization_id, party_role, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("参与方编号已经存在") from exc
                self._emit(connection, agreement_id=agreement_id, event_type="party_added",
                           payload={"party_id": party_id, "organization_id": organization_id,
                                    "party_role": party_role}, actor_id=actor_id)
                return "party", party_id, {"party_id": party_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.add_party", payload=payload, create=create)

    def replace_party(self, *, request_id: str, actor_id: str, agreement_id: str,
                      outgoing_party_id: str, incoming_party_id: str,
                      incoming_organization_id: str, reason: str) -> WriteReceipt:
        """替换合作方：仅转移尚未兑现的责任，已兑现部分与历史记录保持原样。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id,
                   "outgoing_party_id": outgoing_party_id, "incoming_party_id": incoming_party_id,
                   "incoming_organization_id": incoming_organization_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            incoming_party_id = self._identifier(incoming_party_id, "incoming_party_id")
            reason = self._text(reason, "reason", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                self._ensure_not_closed(agreement)
                outgoing = self._party(connection, agreement_id, outgoing_party_id)
                if outgoing["status"] != "active":
                    raise ConflictError("被替换的参与方不在有效状态")
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (incoming_organization_id,)).fetchone() is None:
                    raise NotFoundError("组织不存在")
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO governance_parties(party_id,agreement_id,organization_id,party_role,"
                        "status,created_at) VALUES(?,?,?,?,'active',?)",
                        (incoming_party_id, agreement_id, incoming_organization_id,
                         outgoing["party_role"], now),
                    )
                except Exception as exc:
                    raise ConflictError("新参与方编号已经存在") from exc
                connection.execute(
                    "UPDATE governance_parties SET status='replaced', replaced_by=? WHERE party_id=?",
                    (incoming_party_id, outgoing_party_id),
                )
                transferred = []
                rows = connection.execute(
                    "SELECT * FROM governance_commitments WHERE responsible_party_id=? "
                    "AND status IN ('draft','committed','active','breached','rectifying')",
                    (outgoing_party_id,),
                ).fetchall()
                for row in rows:
                    remaining = row["target_amount"] - row["fulfilled_amount"]
                    connection.execute(
                        "UPDATE governance_commitments SET responsible_party_id=? WHERE commitment_id=?",
                        (incoming_party_id, row["commitment_id"]),
                    )
                    connection.execute(
                        "INSERT INTO governance_amendments(amendment_id,commitment_id,kind,before_json,"
                        "after_json,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, row["commitment_id"], "party_replacement",
                         canonical_json({"responsible_party_id": outgoing_party_id,
                                         "remaining_amount": remaining}),
                         canonical_json({"responsible_party_id": incoming_party_id,
                                         "remaining_amount": remaining}),
                         reason, actor_id, now),
                    )
                    self._emit(connection, agreement_id=agreement_id,
                               event_type="responsibility_transferred",
                               payload={"commitment_id": row["commitment_id"],
                                        "from_party_id": outgoing_party_id,
                                        "to_party_id": incoming_party_id,
                                        "remaining_amount": remaining},
                               actor_id=actor_id)
                    transferred.append(row["commitment_id"])
                self._emit(connection, agreement_id=agreement_id, event_type="party_replaced",
                           payload={"outgoing_party_id": outgoing_party_id,
                                    "incoming_party_id": incoming_party_id,
                                    "incoming_organization_id": incoming_organization_id,
                                    "party_role": outgoing["party_role"], "reason": reason},
                           actor_id=actor_id)
                return "party", incoming_party_id, {"party_id": incoming_party_id,
                                                    "transferred_commitments": transferred}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.replace_party", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 文书：谈判稿、正式承诺、履约证据、争议结论
    # ------------------------------------------------------------------

    def submit_document(self, *, request_id: str, actor_id: str, agreement_id: str,
                        document_id: str, kind: str, title: str, content: dict[str, Any],
                        supersedes: str | None = None) -> WriteReceipt:
        """登记谈判稿或正式承诺文本；履约证据与争议结论走各自专用流程。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "document_id": document_id,
                   "kind": kind, "title": title, "content": content, "supersedes": supersedes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if kind not in ("negotiation_draft", "formal_commitment"):
                raise ValidationError("该接口仅登记谈判稿或正式承诺文本")
            document_id = self._identifier(document_id, "document_id")
            title = self._text(title, "title")
            if not isinstance(content, dict) or not content:
                raise ValidationError("content 必须是非空对象")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                self._ensure_not_closed(agreement)
                if kind == "negotiation_draft":
                    self._require_party_operator(connection, actor, agreement_id)
                else:
                    self._require(actor, "admin")
                if supersedes is not None:
                    previous = connection.execute(
                        "SELECT * FROM governance_documents WHERE document_id=? AND agreement_id=?",
                        (supersedes, agreement_id),
                    ).fetchone()
                    if previous is None:
                        raise NotFoundError("被替代的文书不存在")
                    if previous["kind"] != kind:
                        raise ValidationError("只能替代同类文书")
                    if previous["status"] in ("superseded",):
                        raise ConflictError("被替代的文书已失效")
                    connection.execute(
                        "UPDATE governance_documents SET status='superseded' WHERE document_id=?",
                        (supersedes,),
                    )
                status = "proposed" if kind == "negotiation_draft" else "registered"
                try:
                    connection.execute(
                        "INSERT INTO governance_documents(document_id,agreement_id,kind,title,content_json,"
                        "content_hash,supersedes,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (document_id, agreement_id, kind, title, canonical_json(content),
                         digest(content), supersedes, status, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("文书编号已经存在") from exc
                self._emit(connection, agreement_id=agreement_id, event_type="document_registered",
                           payload={"document_id": document_id, "kind": kind, "title": title,
                                    "supersedes": supersedes}, actor_id=actor_id)
                return "document", document_id, {"document_id": document_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.submit_document", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 承诺
    # ------------------------------------------------------------------

    def create_commitment(self, *, request_id: str, actor_id: str, agreement_id: str,
                          commitment_id: str, category: str, title: str, stage: int,
                          responsible_party_id: str, target_amount: int,
                          terms: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "commitment_id": commitment_id,
                   "category": category, "title": title, "stage": stage,
                   "responsible_party_id": responsible_party_id, "target_amount": target_amount,
                   "terms": terms}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if category not in COMMITMENT_CATEGORIES:
                raise ValidationError("承诺类别不在允许范围内")
            commitment_id = self._identifier(commitment_id, "commitment_id")
            title = self._text(title, "title")
            if isinstance(stage, bool) or not isinstance(stage, int) or stage < 1:
                raise ValidationError("stage 必须是不小于 1 的整数")
            target_amount = self._amount(target_amount, "target_amount", allow_zero=True)
            if not isinstance(terms, dict):
                raise ValidationError("terms 必须是对象")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                self._ensure_structural_open(agreement)
                if actor.role == "operator":
                    self._require_party_operator(connection, actor, agreement_id)
                else:
                    self._require(actor, "admin")
                party = self._party(connection, agreement_id, responsible_party_id)
                if party["status"] != "active":
                    raise ConflictError("责任方不在有效状态")
                try:
                    connection.execute(
                        "INSERT INTO governance_commitments(commitment_id,agreement_id,category,title,"
                        "stage,responsible_party_id,target_amount,fulfilled_amount,terms_json,"
                        "original_terms_json,status,created_at) VALUES(?,?,?,?,?,?,?,0,?,?, 'draft',?)",
                        (commitment_id, agreement_id, category, title, stage, responsible_party_id,
                         target_amount, canonical_json(terms), canonical_json(terms), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("承诺编号已经存在") from exc
                self._emit(connection, agreement_id=agreement_id, event_type="commitment_created",
                           payload={"commitment_id": commitment_id, "category": category,
                                    "title": title, "stage": stage,
                                    "responsible_party_id": responsible_party_id,
                                    "target_amount": target_amount, "terms": terms},
                           actor_id=actor_id)
                return "commitment", commitment_id, {"commitment_id": commitment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.create_commitment", payload=payload, create=create)

    def formalize_commitments(self, *, request_id: str, actor_id: str, agreement_id: str,
                              document_id: str, commitment_ids: list[str]) -> WriteReceipt:
        """把谈判期承诺转为正式承诺：引用一份正式承诺文书，承诺进入已承诺状态。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "document_id": document_id,
                   "commitment_ids": commitment_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            if not isinstance(commitment_ids, list) or not commitment_ids:
                raise ValidationError("commitment_ids 必须是非空列表")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                self._ensure_not_closed(agreement)
                document = connection.execute(
                    "SELECT * FROM governance_documents WHERE document_id=? AND agreement_id=?",
                    (document_id, agreement_id),
                ).fetchone()
                if document is None:
                    raise NotFoundError("正式承诺文书不存在")
                if document["kind"] != "formal_commitment":
                    raise ValidationError("只能引用正式承诺文书")
                now = self._now()
                for commitment_id in commitment_ids:
                    row = connection.execute(
                        "SELECT * FROM governance_commitments WHERE commitment_id=? AND agreement_id=?",
                        (commitment_id, agreement_id),
                    ).fetchone()
                    if row is None:
                        raise NotFoundError(f"承诺 {commitment_id} 不存在")
                    if row["status"] != "draft":
                        raise ConflictError(f"承诺 {commitment_id} 不在谈判稿状态")
                    connection.execute(
                        "UPDATE governance_commitments SET status='committed', source_document_id=? "
                        "WHERE commitment_id=?",
                        (document_id, commitment_id),
                    )
                if agreement["status"] == "negotiating":
                    connection.execute(
                        "UPDATE governance_agreements SET status='committed' WHERE agreement_id=?",
                        (agreement_id,),
                    )
                self._emit(connection, agreement_id=agreement_id, event_type="commitments_formalized",
                           payload={"document_id": document_id, "commitment_ids": list(commitment_ids)},
                           actor_id=actor_id)
                return "agreement", agreement_id, {"agreement_id": agreement_id,
                                                   "formalized": list(commitment_ids)}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.formalize_commitments",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 条件组与履约证据
    # ------------------------------------------------------------------

    def create_condition_group(self, *, request_id: str, actor_id: str, agreement_id: str,
                               group_id: str, title: str, effect_type: str, effect_target: str,
                               conditions: list[dict[str, Any]]) -> WriteReceipt:
        """把互相依赖的条件登记为一组：整组核验通过后才触发阶段生效或拨付。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "group_id": group_id,
                   "title": title, "effect_type": effect_type, "effect_target": effect_target,
                   "conditions": conditions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            group_id = self._identifier(group_id, "group_id")
            title = self._text(title, "title")
            if effect_type not in ("activate_stage", "release_tranche"):
                raise ValidationError("effect_type 不在允许范围内")
            effect_target = str(effect_target).strip()
            if effect_type == "activate_stage":
                try:
                    if int(effect_target) < 1:
                        raise ValueError
                except ValueError as exc:
                    raise ValidationError("activate_stage 的目标必须是不小于 1 的阶段号") from exc
            else:
                effect_target = self._identifier(effect_target, "effect_target")
            if not isinstance(conditions, list) or not conditions:
                raise ValidationError("conditions 必须是非空列表")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                self._ensure_structural_open(agreement)
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO governance_condition_groups(group_id,agreement_id,title,effect_type,"
                        "effect_target,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                        (group_id, agreement_id, title, effect_type, effect_target, now),
                    )
                except Exception as exc:
                    raise ConflictError("条件组编号已经存在") from exc
                seen = set()
                for spec in conditions:
                    condition_id = self._identifier(str(spec.get("condition_id", "")), "condition_id")
                    if condition_id in seen:
                        raise ValidationError("条件编号在组内重复")
                    seen.add(condition_id)
                    commitment = connection.execute(
                        "SELECT * FROM governance_commitments WHERE commitment_id=? AND agreement_id=?",
                        (str(spec.get("commitment_id", "")), agreement_id),
                    ).fetchone()
                    if commitment is None:
                        raise NotFoundError("条件引用的承诺不存在")
                    description = self._text(str(spec.get("description", "")), "description", 400)
                    due_at = spec.get("evidence_due_at")
                    if due_at is not None:
                        due_at = self._timestamp(due_at, "evidence_due_at")
                    connection.execute(
                        "INSERT INTO governance_conditions(condition_id,group_id,commitment_id,"
                        "description,evidence_due_at,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                        (condition_id, group_id, commitment["commitment_id"], description, due_at, now),
                    )
                self._emit(connection, agreement_id=agreement_id, event_type="condition_group_created",
                           payload={"group_id": group_id, "effect_type": effect_type,
                                    "effect_target": effect_target,
                                    "condition_ids": sorted(seen)},
                           actor_id=actor_id)
                return "condition_group", group_id, {"group_id": group_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.create_condition_group",
                                    payload=payload, create=create)

    def submit_evidence(self, *, request_id: str, actor_id: str, condition_id: str,
                        evidence_id: str, title: str, content: dict[str, Any]) -> WriteReceipt:
        """责任方提交履约证据；迟到的证据被标记但不抹掉对应责任。"""

        payload = {"actor_id": actor_id, "condition_id": condition_id, "evidence_id": evidence_id,
                   "title": title, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            evidence_id = self._identifier(evidence_id, "evidence_id")
            title = self._text(title, "title")
            if not isinstance(content, dict) or not content:
                raise ValidationError("content 必须是非空对象")

            def create() -> tuple[str, str, dict[str, Any]]:
                condition = connection.execute(
                    "SELECT c.*, g.agreement_id AS agreement_id, g.group_id AS group_id "
                    "FROM governance_conditions c "
                    "JOIN governance_condition_groups g ON g.group_id=c.group_id "
                    "WHERE c.condition_id=?",
                    (condition_id,),
                ).fetchone()
                if condition is None:
                    raise NotFoundError("核验条件不存在")
                agreement = self._agreement(connection, condition["agreement_id"])
                self._ensure_not_closed(agreement)
                commitment = self._commitment(connection, condition["commitment_id"])
                self._require_responsible_operator(connection, actor, commitment)
                if condition["status"] != "pending":
                    raise ConflictError("条件已核验，不再接受新证据")
                now = self._now()
                late = 1 if condition["evidence_due_at"] and now > condition["evidence_due_at"] else 0
                document_id = f"doc-{evidence_id}"
                try:
                    connection.execute(
                        "INSERT INTO governance_documents(document_id,agreement_id,kind,title,"
                        "content_json,content_hash,supersedes,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,NULL,'submitted',?,?)",
                        (document_id, condition["agreement_id"], "performance_evidence", title,
                         canonical_json(content), digest(content), actor_id, now),
                    )
                    connection.execute(
                        "INSERT INTO governance_evidence(evidence_id,condition_id,document_id,"
                        "submitted_by,submitter_org,late,status,created_at) VALUES(?,?,?,?,?,?,"
                        "'submitted',?)",
                        (evidence_id, condition_id, document_id, actor_id,
                         actor.organization_id, late, now),
                    )
                except Exception as exc:
                    raise ConflictError("证据编号已经存在") from exc
                self._emit(connection, agreement_id=condition["agreement_id"],
                           event_type="evidence_submitted",
                           payload={"evidence_id": evidence_id, "condition_id": condition_id,
                                    "commitment_id": commitment["commitment_id"], "late": late},
                           actor_id=actor_id)
                return "evidence", evidence_id, {"evidence_id": evidence_id, "late": late}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.submit_evidence", payload=payload, create=create)

    def review_evidence(self, *, request_id: str, actor_id: str, evidence_id: str,
                        decision: str, note: str = "") -> WriteReceipt:
        """独立复核证据；条件全部满足时整组核验并触发阶段生效或拨付。"""

        payload = {"actor_id": actor_id, "evidence_id": evidence_id, "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            if decision not in ("accept", "reject"):
                raise ValidationError("decision 必须是 accept 或 reject")

            def create() -> tuple[str, str, dict[str, Any]]:
                evidence = connection.execute(
                    "SELECT e.*, c.group_id AS group_id, c.commitment_id AS commitment_id, "
                    "g.agreement_id AS agreement_id "
                    "FROM governance_evidence e "
                    "JOIN governance_conditions c ON c.condition_id=e.condition_id "
                    "JOIN governance_condition_groups g ON g.group_id=c.group_id "
                    "WHERE e.evidence_id=?",
                    (evidence_id,),
                ).fetchone()
                if evidence is None:
                    raise NotFoundError("证据不存在")
                if evidence["status"] != "submitted":
                    raise ConflictError("证据已经复核，不能重复处理")
                if actor.organization_id == evidence["submitter_org"]:
                    raise PermissionDenied("复核人必须独立于证据提交方")
                now = self._now()
                status = "accepted" if decision == "accept" else "rejected"
                connection.execute(
                    "UPDATE governance_evidence SET status=?, reviewed_by=?, reviewed_at=? "
                    "WHERE evidence_id=?",
                    (status, actor_id, now, evidence_id),
                )
                self._emit(connection, agreement_id=evidence["agreement_id"],
                           event_type="evidence_reviewed",
                           payload={"evidence_id": evidence_id, "condition_id": evidence["condition_id"],
                                    "decision": decision, "note": note},
                           actor_id=actor_id)
                if decision == "accept":
                    connection.execute(
                        "UPDATE governance_conditions SET status='verified', verified_at=? "
                        "WHERE condition_id=?",
                        (now, evidence["condition_id"]),
                    )
                    self._maybe_verify_group(connection, evidence["group_id"],
                                             evidence["agreement_id"], actor_id)
                return "evidence", evidence_id, {"evidence_id": evidence_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.review_evidence", payload=payload, create=create)

    def _maybe_verify_group(self, connection, group_id: str, agreement_id: str, actor_id: str) -> None:
        """组内条件全部核验后，整组生效并在同一事务内应用效果。"""

        group = connection.execute(
            "SELECT * FROM governance_condition_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        if group is None or group["status"] != "pending":
            return
        pending = connection.execute(
            "SELECT COUNT(*) AS count FROM governance_conditions WHERE group_id=? AND status='pending'",
            (group_id,),
        ).fetchone()["count"]
        if pending:
            return
        now = self._now()
        connection.execute(
            "UPDATE governance_condition_groups SET status='verified', verified_at=? WHERE group_id=?",
            (now, group_id),
        )
        self._emit(connection, agreement_id=agreement_id, event_type="group_verified",
                   payload={"group_id": group_id, "effect_type": group["effect_type"],
                            "effect_target": group["effect_target"]},
                   actor_id=actor_id)
        if group["effect_type"] == "activate_stage":
            stage = int(group["effect_target"])
            rows = connection.execute(
                "SELECT commitment_id FROM governance_commitments WHERE agreement_id=? AND stage=? "
                "AND status='committed'",
                (agreement_id, stage),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE governance_commitments SET status='active' WHERE commitment_id=?",
                    (row["commitment_id"],),
                )
                self._emit(connection, agreement_id=agreement_id, event_type="commitment_activated",
                           payload={"commitment_id": row["commitment_id"], "stage": stage},
                           actor_id=actor_id)
            agreement = self._agreement(connection, agreement_id)
            if agreement["status"] == "committed":
                connection.execute(
                    "UPDATE governance_agreements SET status='active' WHERE agreement_id=?",
                    (agreement_id,),
                )
        else:
            tranche = connection.execute(
                "SELECT * FROM governance_tranches WHERE tranche_id=?",
                (group["effect_target"],),
            ).fetchone()
            if tranche is None:
                return
            if tranche["status"] == "held":
                connection.execute(
                    "UPDATE governance_tranches SET status='released', release_pending=0 "
                    "WHERE tranche_id=?",
                    (tranche["tranche_id"],),
                )
                self._emit(connection, agreement_id=agreement_id, event_type="tranche_released",
                           payload={"tranche_id": tranche["tranche_id"], "amount": tranche["amount"]},
                           actor_id=actor_id)
            elif tranche["status"] == "scheduled":
                connection.execute(
                    "UPDATE governance_tranches SET release_pending=1 WHERE tranche_id=?",
                    (tranche["tranche_id"],),
                )

    # ------------------------------------------------------------------
    # 履约登记与独立复核
    # ------------------------------------------------------------------

    def record_fulfillment(self, *, request_id: str, actor_id: str, commitment_id: str,
                           fulfillment_id: str, amount: int, note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "fulfillment_id": fulfillment_id, "amount": amount, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            fulfillment_id = self._identifier(fulfillment_id, "fulfillment_id")
            amount = self._amount(amount, "amount")
            note = self._text(note, "note", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                commitment = self._commitment(connection, commitment_id)
                agreement = self._agreement(connection, commitment["agreement_id"])
                self._ensure_not_closed(agreement)
                self._require_responsible_operator(connection, actor, commitment)
                if commitment["status"] not in FULFILLABLE_STATUSES:
                    raise ConflictError("承诺尚未生效，不能登记履约")
                pending = connection.execute(
                    "SELECT COALESCE(SUM(amount),0) AS total FROM governance_fulfillments "
                    "WHERE commitment_id=? AND status='pending_review'",
                    (commitment_id,),
                ).fetchone()["total"]
                remaining = commitment["target_amount"] - commitment["fulfilled_amount"] - pending
                if amount > remaining:
                    raise ConflictError("履约数量超过剩余义务")
                try:
                    connection.execute(
                        "INSERT INTO governance_fulfillments(fulfillment_id,commitment_id,amount,note,"
                        "submitted_by,submitter_org,status,created_at) VALUES(?,?,?,?,?,?,"
                        "'pending_review',?)",
                        (fulfillment_id, commitment_id, amount, note, actor_id,
                         actor.organization_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("履约记录编号已经存在") from exc
                self._emit(connection, agreement_id=commitment["agreement_id"],
                           event_type="fulfillment_submitted",
                           payload={"fulfillment_id": fulfillment_id, "commitment_id": commitment_id,
                                    "amount": amount},
                           actor_id=actor_id)
                return "fulfillment", fulfillment_id, {"fulfillment_id": fulfillment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.record_fulfillment",
                                    payload=payload, create=create)

    def review_fulfillment(self, *, request_id: str, actor_id: str, fulfillment_id: str,
                           decision: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "fulfillment_id": fulfillment_id, "decision": decision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            if decision not in ("approve", "reject"):
                raise ValidationError("decision 必须是 approve 或 reject")

            def create() -> tuple[str, str, dict[str, Any]]:
                fulfillment = connection.execute(
                    "SELECT * FROM governance_fulfillments WHERE fulfillment_id=?",
                    (fulfillment_id,),
                ).fetchone()
                if fulfillment is None:
                    raise NotFoundError("履约记录不存在")
                if fulfillment["status"] != "pending_review":
                    raise ConflictError("履约记录已经复核，不能重复处理")
                if actor.organization_id == fulfillment["submitter_org"]:
                    raise PermissionDenied("复核人必须独立于履约提交方")
                commitment = self._commitment(connection, fulfillment["commitment_id"])
                now = self._now()
                if decision == "reject":
                    connection.execute(
                        "UPDATE governance_fulfillments SET status='rejected', reviewed_by=?, "
                        "reviewed_at=? WHERE fulfillment_id=?",
                        (actor_id, now, fulfillment_id),
                    )
                    self._emit(connection, agreement_id=commitment["agreement_id"],
                               event_type="fulfillment_rejected",
                               payload={"fulfillment_id": fulfillment_id,
                                        "commitment_id": commitment["commitment_id"]},
                               actor_id=actor_id)
                    return "fulfillment", fulfillment_id, {"fulfillment_id": fulfillment_id,
                                                           "decision": decision}
                remaining = commitment["target_amount"] - commitment["fulfilled_amount"]
                if fulfillment["amount"] > remaining:
                    raise ConflictError("履约数量超过剩余义务")
                fulfilled = commitment["fulfilled_amount"] + fulfillment["amount"]
                status = "fulfilled" if fulfilled >= commitment["target_amount"] else commitment["status"]
                connection.execute(
                    "UPDATE governance_fulfillments SET status='approved', reviewed_by=?, reviewed_at=? "
                    "WHERE fulfillment_id=?",
                    (actor_id, now, fulfillment_id),
                )
                connection.execute(
                    "UPDATE governance_commitments SET fulfilled_amount=?, status=? WHERE commitment_id=?",
                    (fulfilled, status, commitment["commitment_id"]),
                )
                self._emit(connection, agreement_id=commitment["agreement_id"],
                           event_type="fulfillment_approved",
                           payload={"fulfillment_id": fulfillment_id,
                                    "commitment_id": commitment["commitment_id"],
                                    "amount": fulfillment["amount"],
                                    "fulfilled_amount": fulfilled, "status": status},
                           actor_id=actor_id)
                return "fulfillment", fulfillment_id, {"fulfillment_id": fulfillment_id,
                                                       "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.review_fulfillment",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 资金托管与分期拨付
    # ------------------------------------------------------------------

    def _escrow_balance(self, connection, account_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN entry_type='deposit' THEN amount ELSE -amount END),0) "
            "AS balance FROM governance_escrow_ledger WHERE account_id=?",
            (account_id,),
        ).fetchone()
        return row["balance"]

    def _ledger_entry(self, connection, *, account_id: str, entry_type: str, amount: int,
                      tranche_id: str | None, reference: str) -> int:
        balance = self._escrow_balance(connection, account_id)
        balance = balance + amount if entry_type == "deposit" else balance - amount
        if balance < 0:
            raise ConflictError("托管余额不足")
        connection.execute(
            "INSERT INTO governance_escrow_ledger(entry_id,account_id,entry_type,amount,tranche_id,"
            "reference,balance_after,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, account_id, entry_type, amount, tranche_id, reference,
             balance, self._now()),
        )
        return balance

    def schedule_tranche(self, *, request_id: str, actor_id: str, agreement_id: str,
                         tranche_id: str, sequence_no: int, amount: int,
                         condition_group_id: str) -> WriteReceipt:
        """登记分期资金计划；每期必须挂在核验其拨付条件的条件组上。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "tranche_id": tranche_id,
                   "sequence_no": sequence_no, "amount": amount,
                   "condition_group_id": condition_group_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            tranche_id = self._identifier(tranche_id, "tranche_id")
            if isinstance(sequence_no, bool) or not isinstance(sequence_no, int) or sequence_no < 1:
                raise ValidationError("sequence_no 必须是不小于 1 的整数")
            amount = self._amount(amount, "amount")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                self._ensure_structural_open(agreement)
                group = connection.execute(
                    "SELECT * FROM governance_condition_groups WHERE group_id=? AND agreement_id=?",
                    (condition_group_id, agreement_id),
                ).fetchone()
                if group is None:
                    raise NotFoundError("条件组不存在")
                if group["effect_type"] != "release_tranche" or group["effect_target"] != tranche_id:
                    raise ValidationError("条件组必须声明释放该分期资金")
                account = connection.execute(
                    "SELECT * FROM governance_escrow_accounts WHERE agreement_id=?",
                    (agreement_id,),
                ).fetchone()
                release_pending = 1 if group["status"] == "verified" else 0
                try:
                    connection.execute(
                        "INSERT INTO governance_tranches(tranche_id,account_id,sequence_no,amount,"
                        "condition_group_id,status,release_pending,created_at) "
                        "VALUES(?,?,?,?,?,'scheduled',?,?)",
                        (tranche_id, account["account_id"], sequence_no, amount,
                         condition_group_id, release_pending, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("分期编号或期次已经存在") from exc
                self._emit(connection, agreement_id=agreement_id, event_type="tranche_scheduled",
                           payload={"tranche_id": tranche_id, "sequence_no": sequence_no,
                                    "amount": amount, "condition_group_id": condition_group_id},
                           actor_id=actor_id)
                return "tranche", tranche_id, {"tranche_id": tranche_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.schedule_tranche", payload=payload, create=create)

    def deposit_escrow(self, *, request_id: str, actor_id: str, tranche_id: str,
                       amount: int) -> WriteReceipt:
        """投资方把一期资金存入托管；若条件组已核验则同时解除托管。"""

        payload = {"actor_id": actor_id, "tranche_id": tranche_id, "amount": amount}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            amount = self._amount(amount, "amount")

            def create() -> tuple[str, str, dict[str, Any]]:
                tranche = connection.execute(
                    "SELECT t.*, a.agreement_id AS agreement_id FROM governance_tranches t "
                    "JOIN governance_escrow_accounts a ON a.account_id=t.account_id "
                    "WHERE t.tranche_id=?",
                    (tranche_id,),
                ).fetchone()
                if tranche is None:
                    raise NotFoundError("分期资金不存在")
                agreement = self._agreement(connection, tranche["agreement_id"])
                self._ensure_not_closed(agreement)
                if actor.role != "operator":
                    raise PermissionDenied("只有投资方操作员可以注资")
                investor = connection.execute(
                    "SELECT * FROM governance_parties WHERE agreement_id=? AND organization_id=? "
                    "AND party_role='investor' AND status='active'",
                    (tranche["agreement_id"], actor.organization_id),
                ).fetchone()
                if investor is None:
                    raise PermissionDenied("只有协议投资方的操作员可以注资")
                if tranche["status"] != "scheduled":
                    raise ConflictError("该分期已注资或已结束")
                if amount != tranche["amount"]:
                    raise ValidationError("注资额必须等于分期计划金额")
                self._ledger_entry(connection, account_id=tranche["account_id"],
                                   entry_type="deposit", amount=amount,
                                   tranche_id=tranche_id, reference=request_id)
                connection.execute(
                    "UPDATE governance_tranches SET status='held' WHERE tranche_id=?",
                    (tranche_id,),
                )
                self._emit(connection, agreement_id=tranche["agreement_id"],
                           event_type="escrow_deposited",
                           payload={"tranche_id": tranche_id, "amount": amount,
                                    "account_id": tranche["account_id"]},
                           actor_id=actor_id)
                if tranche["release_pending"]:
                    connection.execute(
                        "UPDATE governance_tranches SET status='released', release_pending=0 "
                        "WHERE tranche_id=?",
                        (tranche_id,),
                    )
                    self._emit(connection, agreement_id=tranche["agreement_id"],
                               event_type="tranche_released",
                               payload={"tranche_id": tranche_id, "amount": amount},
                               actor_id=actor_id)
                return "tranche", tranche_id, {"tranche_id": tranche_id, "status": "held"}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.deposit_escrow", payload=payload, create=create)

    def confirm_disbursement(self, *, request_id: str, actor_id: str, tranche_id: str,
                             callback_reference: str) -> WriteReceipt:
        """拨付回调：重复回调不会再次改变承诺状态或托管余额。"""

        payload = {"actor_id": actor_id, "tranche_id": tranche_id,
                   "callback_reference": callback_reference}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            callback_reference = self._text(callback_reference, "callback_reference", 120)

            def create() -> tuple[str, str, dict[str, Any]]:
                tranche = connection.execute(
                    "SELECT t.*, a.agreement_id AS agreement_id FROM governance_tranches t "
                    "JOIN governance_escrow_accounts a ON a.account_id=t.account_id "
                    "WHERE t.tranche_id=?",
                    (tranche_id,),
                ).fetchone()
                if tranche is None:
                    raise NotFoundError("分期资金不存在")
                if tranche["status"] == "disbursed":
                    return "tranche", tranche_id, {"tranche_id": tranche_id,
                                                   "status": "disbursed", "duplicate": True}
                if tranche["status"] != "released":
                    raise ConflictError("条件组未核验或资金未托管，不能拨付")
                self._ledger_entry(connection, account_id=tranche["account_id"],
                                   entry_type="disburse", amount=tranche["amount"],
                                   tranche_id=tranche_id, reference=callback_reference)
                connection.execute(
                    "UPDATE governance_tranches SET status='disbursed' WHERE tranche_id=?",
                    (tranche_id,),
                )
                self._emit(connection, agreement_id=tranche["agreement_id"],
                           event_type="tranche_disbursed",
                           payload={"tranche_id": tranche_id, "amount": tranche["amount"],
                                    "callback_reference": callback_reference},
                           actor_id=actor_id)
                return "tranche", tranche_id, {"tranche_id": tranche_id, "status": "disbursed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.confirm_disbursement",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 违约与整改
    # ------------------------------------------------------------------

    def report_breach(self, *, request_id: str, actor_id: str, commitment_id: str,
                      breach_id: str, severity: str, description: str) -> WriteReceipt:
        """登记违约：只针对尚未兑现的部分，已兑现成果与历史责任保持不变。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "breach_id": breach_id,
                   "severity": severity, "description": description}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_supervisor(actor)
            breach_id = self._identifier(breach_id, "breach_id")
            if severity not in ("partial", "full"):
                raise ValidationError("severity 必须是 partial 或 full")
            description = self._text(description, "description", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                commitment = self._commitment(connection, commitment_id)
                agreement = self._agreement(connection, commitment["agreement_id"])
                self._ensure_not_closed(agreement)
                if commitment["status"] not in ("committed", "active"):
                    raise ConflictError("当前状态不能登记新的违约")
                unfulfilled = commitment["target_amount"] - commitment["fulfilled_amount"]
                try:
                    connection.execute(
                        "INSERT INTO governance_breaches(breach_id,commitment_id,severity,description,"
                        "unfulfilled_amount,previous_status,status,reported_by,created_at) "
                        "VALUES(?,?,?,?,?,?, 'open',?,?)",
                        (breach_id, commitment_id, severity, description, unfulfilled,
                         commitment["status"], actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("违约记录编号已经存在") from exc
                connection.execute(
                    "UPDATE governance_commitments SET status='breached' WHERE commitment_id=?",
                    (commitment_id,),
                )
                self._emit(connection, agreement_id=commitment["agreement_id"],
                           event_type="breach_reported",
                           payload={"breach_id": breach_id, "commitment_id": commitment_id,
                                    "severity": severity, "unfulfilled_amount": unfulfilled},
                           actor_id=actor_id)
                return "breach", breach_id, {"breach_id": breach_id,
                                             "unfulfilled_amount": unfulfilled}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.report_breach", payload=payload, create=create)

    def create_rectification(self, *, request_id: str, actor_id: str, breach_id: str,
                             rectification_id: str, requirement: str, due_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "breach_id": breach_id,
                   "rectification_id": rectification_id, "requirement": requirement, "due_at": due_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            rectification_id = self._identifier(rectification_id, "rectification_id")
            requirement = self._text(requirement, "requirement", 400)
            due_at = self._timestamp(due_at, "due_at")

            def create() -> tuple[str, str, dict[str, Any]]:
                breach = connection.execute(
                    "SELECT * FROM governance_breaches WHERE breach_id=?", (breach_id,)
                ).fetchone()
                if breach is None:
                    raise NotFoundError("违约记录不存在")
                if breach["status"] not in ("open", "rectifying"):
                    raise ConflictError("违约已结案，不能追加整改")
                commitment = self._commitment(connection, breach["commitment_id"])
                agreement = self._agreement(connection, commitment["agreement_id"])
                self._ensure_not_closed(agreement)
                order_seq = connection.execute(
                    "SELECT COALESCE(MAX(r.order_seq),0)+1 AS next_seq FROM governance_rectifications r "
                    "JOIN governance_breaches b ON b.breach_id=r.breach_id "
                    "JOIN governance_commitments c ON c.commitment_id=b.commitment_id "
                    "WHERE c.agreement_id=?",
                    (commitment["agreement_id"],),
                ).fetchone()["next_seq"]
                try:
                    connection.execute(
                        "INSERT INTO governance_rectifications(rectification_id,breach_id,requirement,"
                        "due_at,order_seq,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                        (rectification_id, breach_id, requirement, due_at, order_seq, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("整改编号已经存在") from exc
                connection.execute(
                    "UPDATE governance_breaches SET status='rectifying' WHERE breach_id=?",
                    (breach_id,),
                )
                connection.execute(
                    "UPDATE governance_commitments SET status='rectifying' WHERE commitment_id=?",
                    (commitment["commitment_id"],),
                )
                self._emit(connection, agreement_id=commitment["agreement_id"],
                           event_type="rectification_created",
                           payload={"rectification_id": rectification_id, "breach_id": breach_id,
                                    "commitment_id": commitment["commitment_id"],
                                    "due_at": due_at, "order_seq": order_seq},
                           actor_id=actor_id)
                return "rectification", rectification_id, {"rectification_id": rectification_id,
                                                           "order_seq": order_seq}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.create_rectification",
                                    payload=payload, create=create)

    def submit_rectification(self, *, request_id: str, actor_id: str,
                             rectification_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "rectification_id": rectification_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                rectification = connection.execute(
                    "SELECT r.*, b.commitment_id AS commitment_id FROM governance_rectifications r "
                    "JOIN governance_breaches b ON b.breach_id=r.breach_id "
                    "WHERE r.rectification_id=?",
                    (rectification_id,),
                ).fetchone()
                if rectification is None:
                    raise NotFoundError("整改要求不存在")
                commitment = self._commitment(connection, rectification["commitment_id"])
                agreement = self._agreement(connection, commitment["agreement_id"])
                self._ensure_not_closed(agreement)
                self._require_responsible_operator(connection, actor, commitment)
                if rectification["status"] == "overdue":
                    raise ConflictError("整改已逾期，需由监督方重新下达")
                if rectification["status"] != "pending":
                    raise ConflictError("整改已提交或已复核")
                connection.execute(
                    "UPDATE governance_rectifications SET status='submitted', submitted_at=? "
                    "WHERE rectification_id=?",
                    (self._now(), rectification_id),
                )
                self._emit(connection, agreement_id=commitment["agreement_id"],
                           event_type="rectification_submitted",
                           payload={"rectification_id": rectification_id,
                                    "breach_id": rectification["breach_id"]},
                           actor_id=actor_id)
                return "rectification", rectification_id, {"rectification_id": rectification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.submit_rectification",
                                    payload=payload, create=create)

    def review_rectification(self, *, request_id: str, actor_id: str, rectification_id: str,
                             decision: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "rectification_id": rectification_id, "decision": decision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            if decision not in ("approve", "reject"):
                raise ValidationError("decision 必须是 approve 或 reject")

            def create() -> tuple[str, str, dict[str, Any]]:
                rectification = connection.execute(
                    "SELECT r.*, b.commitment_id AS commitment_id, b.breach_id AS breach_id "
                    "FROM governance_rectifications r "
                    "JOIN governance_breaches b ON b.breach_id=r.breach_id "
                    "WHERE r.rectification_id=?",
                    (rectification_id,),
                ).fetchone()
                if rectification is None:
                    raise NotFoundError("整改要求不存在")
                if rectification["status"] != "submitted":
                    raise ConflictError("整改不在待复核状态")
                commitment = self._commitment(connection, rectification["commitment_id"])
                party = connection.execute(
                    "SELECT * FROM governance_parties WHERE party_id=?",
                    (commitment["responsible_party_id"],),
                ).fetchone()
                if party is not None and actor.organization_id == party["organization_id"]:
                    raise PermissionDenied("复核人必须独立于整改提交方")
                now = self._now()
                if decision == "reject":
                    connection.execute(
                        "UPDATE governance_rectifications SET status='rejected', reviewed_by=?, "
                        "reviewed_at=? WHERE rectification_id=?",
                        (actor_id, now, rectification_id),
                    )
                    connection.execute(
                        "UPDATE governance_breaches SET status='open' WHERE breach_id=?",
                        (rectification["breach_id"],),
                    )
                    connection.execute(
                        "UPDATE governance_commitments SET status='breached' WHERE commitment_id=?",
                        (commitment["commitment_id"],),
                    )
                    self._emit(connection, agreement_id=commitment["agreement_id"],
                               event_type="rectification_reviewed",
                               payload={"rectification_id": rectification_id, "decision": decision},
                               actor_id=actor_id)
                    return "rectification", rectification_id, {"rectification_id": rectification_id,
                                                               "decision": decision}
                connection.execute(
                    "UPDATE governance_rectifications SET status='approved', reviewed_by=?, "
                    "reviewed_at=? WHERE rectification_id=?",
                    (actor_id, now, rectification_id),
                )
                self._emit(connection, agreement_id=commitment["agreement_id"],
                           event_type="rectification_reviewed",
                           payload={"rectification_id": rectification_id, "decision": decision},
                           actor_id=actor_id)
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM governance_rectifications "
                    "WHERE breach_id=? AND status IN ('pending','submitted')",
                    (rectification["breach_id"],),
                ).fetchone()["count"]
                if remaining == 0:
                    breach = connection.execute(
                        "SELECT * FROM governance_breaches WHERE breach_id=?",
                        (rectification["breach_id"],),
                    ).fetchone()
                    restored = "fulfilled" \
                        if commitment["fulfilled_amount"] >= commitment["target_amount"] \
                        else breach["previous_status"]
                    connection.execute(
                        "UPDATE governance_breaches SET status='resolved' WHERE breach_id=?",
                        (rectification["breach_id"],),
                    )
                    connection.execute(
                        "UPDATE governance_commitments SET status=? WHERE commitment_id=?",
                        (restored, commitment["commitment_id"]),
                    )
                    self._emit(connection, agreement_id=commitment["agreement_id"],
                               event_type="breach_resolved",
                               payload={"breach_id": rectification["breach_id"],
                                        "commitment_id": commitment["commitment_id"],
                                        "restored_status": restored},
                               actor_id=actor_id)
                return "rectification", rectification_id, {"rectification_id": rectification_id,
                                                           "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.review_rectification",
                                    payload=payload, create=create)

    def sweep_overdue_rectifications(self, *, actor_id: str,
                                     agreement_id: str) -> dict[str, Any]:
        """按原顺序把逾期未整改的记录标记为逾期并升级违约；重启后继续处理。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            agreement = self._agreement(connection, agreement_id)
            self._ensure_not_closed(agreement)
            now = self._now()
            rows = connection.execute(
                "SELECT r.*, b.commitment_id AS commitment_id FROM governance_rectifications r "
                "JOIN governance_breaches b ON b.breach_id=r.breach_id "
                "JOIN governance_commitments c ON c.commitment_id=b.commitment_id "
                "WHERE c.agreement_id=? AND r.status='pending' AND r.due_at<? "
                "ORDER BY r.order_seq",
                (agreement_id, now),
            ).fetchall()
            overdue = []
            for row in rows:
                connection.execute(
                    "UPDATE governance_rectifications SET status='overdue' WHERE rectification_id=?",
                    (row["rectification_id"],),
                )
                connection.execute(
                    "UPDATE governance_breaches SET status='escalated' WHERE breach_id=?",
                    (row["breach_id"],),
                )
                self._emit(connection, agreement_id=agreement_id,
                           event_type="rectification_overdue",
                           payload={"rectification_id": row["rectification_id"],
                                    "breach_id": row["breach_id"], "order_seq": row["order_seq"]},
                           actor_id=actor_id)
                self._emit(connection, agreement_id=agreement_id, event_type="breach_escalated",
                           payload={"breach_id": row["breach_id"],
                                    "commitment_id": row["commitment_id"]},
                           actor_id=actor_id)
                overdue.append(row["rectification_id"])
            return {"agreement_id": agreement_id, "overdue_rectifications": overdue}

    # ------------------------------------------------------------------
    # 范围缩减（只影响尚未兑现的部分）
    # ------------------------------------------------------------------

    def reduce_scope(self, *, request_id: str, actor_id: str, commitment_id: str,
                     new_target_amount: int, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "new_target_amount": new_target_amount, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            new_target_amount = self._amount(new_target_amount, "new_target_amount", allow_zero=True)
            reason = self._text(reason, "reason", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                commitment = self._commitment(connection, commitment_id)
                agreement = self._agreement(connection, commitment["agreement_id"])
                self._ensure_not_closed(agreement)
                if commitment["status"] not in ("draft", "committed", "active",
                                                "breached", "rectifying"):
                    raise ConflictError("当前状态不能缩减范围")
                if new_target_amount < commitment["fulfilled_amount"]:
                    raise ValidationError("缩减后的目标不能低于已兑现数量")
                if new_target_amount >= commitment["target_amount"]:
                    raise ValidationError("新目标必须低于原目标")
                status = commitment["status"]
                if commitment["fulfilled_amount"] >= new_target_amount:
                    status = "fulfilled"
                connection.execute(
                    "INSERT INTO governance_amendments(amendment_id,commitment_id,kind,before_json,"
                    "after_json,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, commitment_id, "scope_reduction",
                     canonical_json({"target_amount": commitment["target_amount"],
                                     "remaining_amount": commitment["target_amount"]
                                     - commitment["fulfilled_amount"]}),
                     canonical_json({"target_amount": new_target_amount,
                                     "remaining_amount": new_target_amount
                                     - commitment["fulfilled_amount"]}),
                     reason, actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE governance_commitments SET target_amount=?, status=? WHERE commitment_id=?",
                    (new_target_amount, status, commitment_id),
                )
                self._emit(connection, agreement_id=commitment["agreement_id"],
                           event_type="scope_reduced",
                           payload={"commitment_id": commitment_id,
                                    "previous_target": commitment["target_amount"],
                                    "new_target": new_target_amount,
                                    "fulfilled_amount": commitment["fulfilled_amount"],
                                    "status": status, "reason": reason},
                           actor_id=actor_id)
                return "commitment", commitment_id, {"commitment_id": commitment_id,
                                                     "target_amount": new_target_amount}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.reduce_scope", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 成果登记与认领（共享成果不能被重复认领）
    # ------------------------------------------------------------------

    def register_outcome(self, *, request_id: str, actor_id: str, agreement_id: str,
                         outcome_id: str, outcome_key: str, title: str,
                         beneficiary_group: str, shared: bool) -> WriteReceipt:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "outcome_id": outcome_id,
                   "outcome_key": outcome_key, "title": title,
                   "beneficiary_group": beneficiary_group, "shared": shared}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            outcome_id = self._identifier(outcome_id, "outcome_id")
            outcome_key = self._identifier(outcome_key, "outcome_key")
            title = self._text(title, "title")
            beneficiary_group = self._text(beneficiary_group, "beneficiary_group")
            if not isinstance(shared, bool):
                raise ValidationError("shared 必须是布尔值")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                self._ensure_not_closed(agreement)
                if actor.role == "operator":
                    self._require_party_operator(connection, actor, agreement_id)
                else:
                    self._require(actor, "admin")
                try:
                    connection.execute(
                        "INSERT INTO governance_outcomes(outcome_id,agreement_id,outcome_key,title,"
                        "beneficiary_group,shared,created_at) VALUES(?,?,?,?,?,?,?)",
                        (outcome_id, agreement_id, outcome_key, title, beneficiary_group,
                         1 if shared else 0, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("成果编号或成果键已经存在") from exc
                self._emit(connection, agreement_id=agreement_id, event_type="outcome_registered",
                           payload={"outcome_id": outcome_id, "outcome_key": outcome_key,
                                    "title": title, "beneficiary_group": beneficiary_group,
                                    "shared": shared},
                           actor_id=actor_id)
                return "outcome", outcome_id, {"outcome_id": outcome_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.register_outcome", payload=payload, create=create)

    def claim_outcome(self, *, request_id: str, actor_id: str, outcome_id: str,
                      project_key: str) -> WriteReceipt:
        """认领成果：同一成果只能归属一个项目，重复认领被拒绝。"""

        payload = {"actor_id": actor_id, "outcome_id": outcome_id, "project_key": project_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            project_key = self._identifier(project_key, "project_key")

            def create() -> tuple[str, str, dict[str, Any]]:
                outcome = connection.execute(
                    "SELECT * FROM governance_outcomes WHERE outcome_id=?", (outcome_id,)
                ).fetchone()
                if outcome is None:
                    raise NotFoundError("成果不存在")
                agreement = self._agreement(connection, outcome["agreement_id"])
                self._ensure_not_closed(agreement)
                if actor.role == "operator":
                    self._require_party_operator(connection, actor, outcome["agreement_id"])
                else:
                    self._require(actor, "admin")
                if outcome["claimed_by_project"] is not None:
                    if outcome["claimed_by_project"] == project_key:
                        return "outcome", outcome_id, {"outcome_id": outcome_id,
                                                       "claimed_by_project": project_key,
                                                       "duplicate": True}
                    raise ConflictError("成果已被其他项目认领，不能重复计入")
                connection.execute(
                    "UPDATE governance_outcomes SET claimed_by_project=?, claimed_at=? "
                    "WHERE outcome_id=?",
                    (project_key, self._now(), outcome_id),
                )
                self._emit(connection, agreement_id=outcome["agreement_id"],
                           event_type="outcome_claimed",
                           payload={"outcome_id": outcome_id,
                                    "outcome_key": outcome["outcome_key"],
                                    "project_key": project_key},
                           actor_id=actor_id)
                return "outcome", outcome_id, {"outcome_id": outcome_id,
                                               "claimed_by_project": project_key}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.claim_outcome", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 争议与结论
    # ------------------------------------------------------------------

    def file_dispute(self, *, request_id: str, actor_id: str, agreement_id: str,
                     dispute_id: str, description: str,
                     commitment_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "dispute_id": dispute_id,
                   "description": description, "commitment_id": commitment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            dispute_id = self._identifier(dispute_id, "dispute_id")
            description = self._text(description, "description", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                self._ensure_not_closed(agreement)
                self._require_party_operator(connection, actor, agreement_id)
                if commitment_id is not None:
                    commitment = self._commitment(connection, commitment_id)
                    if commitment["agreement_id"] != agreement_id:
                        raise ValidationError("争议引用的承诺不属于该协议")
                try:
                    connection.execute(
                        "INSERT INTO governance_disputes(dispute_id,agreement_id,commitment_id,"
                        "raised_by,description,status,created_at) VALUES(?,?,?,?,?,'filed',?)",
                        (dispute_id, agreement_id, commitment_id, actor_id, description, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("争议编号已经存在") from exc
                self._emit(connection, agreement_id=agreement_id, event_type="dispute_filed",
                           payload={"dispute_id": dispute_id, "commitment_id": commitment_id},
                           actor_id=actor_id)
                return "dispute", dispute_id, {"dispute_id": dispute_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.file_dispute", payload=payload, create=create)

    def conclude_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                         document_id: str, title: str, ruling: dict[str, Any]) -> WriteReceipt:
        """登记争议结论：结论作为独立文书保存，与谈判稿、正式承诺、证据区分。"""

        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "document_id": document_id,
                   "title": title, "ruling": ruling}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            document_id = self._identifier(document_id, "document_id")
            title = self._text(title, "title")
            if not isinstance(ruling, dict) or not ruling:
                raise ValidationError("ruling 必须是非空对象")

            def create() -> tuple[str, str, dict[str, Any]]:
                dispute = connection.execute(
                    "SELECT * FROM governance_disputes WHERE dispute_id=?", (dispute_id,)
                ).fetchone()
                if dispute is None:
                    raise NotFoundError("争议不存在")
                if dispute["status"] != "filed":
                    raise ConflictError("争议已有结论")
                try:
                    connection.execute(
                        "INSERT INTO governance_documents(document_id,agreement_id,kind,title,"
                        "content_json,content_hash,supersedes,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,NULL,'concluded',?,?)",
                        (document_id, dispute["agreement_id"], "dispute_ruling", title,
                         canonical_json(ruling), digest(ruling), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("文书编号已经存在") from exc
                connection.execute(
                    "UPDATE governance_disputes SET status='concluded', ruling_document_id=?, "
                    "concluded_at=? WHERE dispute_id=?",
                    (document_id, self._now(), dispute_id),
                )
                self._emit(connection, agreement_id=dispute["agreement_id"],
                           event_type="dispute_concluded",
                           payload={"dispute_id": dispute_id, "ruling_document_id": document_id},
                           actor_id=actor_id)
                return "dispute", dispute_id, {"dispute_id": dispute_id,
                                               "ruling_document_id": document_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.conclude_dispute", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------------

    def begin_exit(self, *, request_id: str, actor_id: str, agreement_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                if agreement["status"] not in ("negotiating", "committed", "active"):
                    raise ConflictError("当前状态不能进入退出流程")
                connection.execute(
                    "UPDATE governance_agreements SET status='exiting' WHERE agreement_id=?",
                    (agreement_id,),
                )
                self._emit(connection, agreement_id=agreement_id, event_type="exit_began",
                           payload={"agreement_id": agreement_id}, actor_id=actor_id)
                return "agreement", agreement_id, {"agreement_id": agreement_id,
                                                   "status": "exiting"}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.begin_exit", payload=payload, create=create)

    def complete_exit(self, *, request_id: str, actor_id: str, agreement_id: str) -> WriteReceipt:
        """完成退出：全部承诺了结、违约与争议结案、托管余额清零后才允许退出。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement = self._agreement(connection, agreement_id)
                if agreement["status"] != "exiting":
                    raise ConflictError("协议不在退出流程中")
                blockers = []
                open_commitments = connection.execute(
                    "SELECT COUNT(*) AS count FROM governance_commitments WHERE agreement_id=? "
                    "AND status IN ('draft','committed','active','breached','rectifying')",
                    (agreement_id,),
                ).fetchone()["count"]
                if open_commitments:
                    blockers.append(f"未了结承诺 {open_commitments} 项")
                open_breaches = connection.execute(
                    "SELECT COUNT(*) AS count FROM governance_breaches b "
                    "JOIN governance_commitments c ON c.commitment_id=b.commitment_id "
                    "WHERE c.agreement_id=? AND b.status IN ('open','rectifying','escalated')",
                    (agreement_id,),
                ).fetchone()["count"]
                if open_breaches:
                    blockers.append(f"未结案违约 {open_breaches} 项")
                open_disputes = connection.execute(
                    "SELECT COUNT(*) AS count FROM governance_disputes "
                    "WHERE agreement_id=? AND status='filed'",
                    (agreement_id,),
                ).fetchone()["count"]
                if open_disputes:
                    blockers.append(f"未结案争议 {open_disputes} 项")
                account = connection.execute(
                    "SELECT * FROM governance_escrow_accounts WHERE agreement_id=?",
                    (agreement_id,),
                ).fetchone()
                balance = self._escrow_balance(connection, account["account_id"])
                if balance != 0:
                    blockers.append(f"托管余额未清零（{balance}）")
                blocked_tranches = connection.execute(
                    "SELECT COUNT(*) AS count FROM governance_tranches WHERE account_id=? "
                    "AND status IN ('held','released')",
                    (account["account_id"],),
                ).fetchone()["count"]
                if blocked_tranches:
                    blockers.append(f"托管中分期 {blocked_tranches} 期")
                if blockers:
                    raise ConflictError("退出条件未满足：" + "；".join(blockers))
                scheduled = connection.execute(
                    "SELECT tranche_id FROM governance_tranches WHERE account_id=? "
                    "AND status='scheduled'",
                    (account["account_id"],),
                ).fetchall()
                for row in scheduled:
                    connection.execute(
                        "UPDATE governance_tranches SET status='cancelled' WHERE tranche_id=?",
                        (row["tranche_id"],),
                    )
                    self._emit(connection, agreement_id=agreement_id,
                               event_type="tranche_cancelled",
                               payload={"tranche_id": row["tranche_id"]}, actor_id=actor_id)
                connection.execute(
                    "UPDATE governance_agreements SET status='exited' WHERE agreement_id=?",
                    (agreement_id,),
                )
                self._emit(connection, agreement_id=agreement_id, event_type="agreement_exited",
                           payload={"agreement_id": agreement_id}, actor_id=actor_id)
                return "agreement", agreement_id, {"agreement_id": agreement_id,
                                                   "status": "exited"}

            return self._idempotent(connection, request_id=request_id,
                                    action="governance.complete_exit", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：可见范围受限
    # ------------------------------------------------------------------

    def get_agreement_view(self, *, actor_id: str, agreement_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            agreement = self._agreement(connection, agreement_id)
            self._require_view(connection, actor, agreement_id)
            parties = connection.execute(
                "SELECT * FROM governance_parties WHERE agreement_id=? ORDER BY created_at, party_id",
                (agreement_id,),
            ).fetchall()
            counts = connection.execute(
                "SELECT status, COUNT(*) AS count FROM governance_commitments "
                "WHERE agreement_id=? GROUP BY status",
                (agreement_id,),
            ).fetchall()
            return {
                "agreement_id": agreement_id,
                "site_id": agreement["site_id"],
                "title": agreement["title"],
                "status": agreement["status"],
                "currency": agreement["currency"],
                "parties": [{"party_id": row["party_id"],
                             "organization_id": row["organization_id"],
                             "party_role": row["party_role"],
                             "status": row["status"],
                             "replaced_by": row["replaced_by"]} for row in parties],
                "commitment_counts": {row["status"]: row["count"] for row in counts},
            }

    def list_commitments(self, *, actor_id: str, agreement_id: str,
                         status: str | None = None,
                         category: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._agreement(connection, agreement_id)
            self._require_view(connection, actor, agreement_id)
            query = "SELECT * FROM governance_commitments WHERE agreement_id=?"
            parameters: list[Any] = [agreement_id]
            if status:
                query += " AND status=?"
                parameters.append(status)
            if category:
                query += " AND category=?"
                parameters.append(category)
            query += " ORDER BY created_at, commitment_id"
            rows = connection.execute(query, parameters).fetchall()
            return [self._commitment_dict(row) for row in rows]

    @staticmethod
    def _commitment_dict(row) -> dict[str, Any]:
        return {
            "commitment_id": row["commitment_id"],
            "agreement_id": row["agreement_id"],
            "category": row["category"],
            "title": row["title"],
            "stage": row["stage"],
            "responsible_party_id": row["responsible_party_id"],
            "target_amount": row["target_amount"],
            "fulfilled_amount": row["fulfilled_amount"],
            "remaining_amount": row["target_amount"] - row["fulfilled_amount"],
            "status": row["status"],
            "terms": json.loads(row["terms_json"]),
            "original_terms": json.loads(row["original_terms_json"]),
            "source_document_id": row["source_document_id"],
        }

    def list_documents(self, *, actor_id: str, agreement_id: str,
                       kind: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._agreement(connection, agreement_id)
            self._require_view(connection, actor, agreement_id)
            query = "SELECT * FROM governance_documents WHERE agreement_id=?"
            parameters: list[Any] = [agreement_id]
            if kind:
                if kind not in DOCUMENT_KINDS:
                    raise ValidationError("文书类别不在允许范围内")
                query += " AND kind=?"
                parameters.append(kind)
            query += " ORDER BY created_at, document_id"
            rows = connection.execute(query, parameters).fetchall()
            return [{"document_id": row["document_id"], "kind": row["kind"],
                     "title": row["title"], "status": row["status"],
                     "supersedes": row["supersedes"], "created_by": row["created_by"],
                     "created_at": row["created_at"],
                     "content": json.loads(row["content_json"])} for row in rows]

    def list_evidence(self, *, actor_id: str, agreement_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._agreement(connection, agreement_id)
            self._require_view(connection, actor, agreement_id)
            rows = connection.execute(
                "SELECT e.* FROM governance_evidence e "
                "JOIN governance_conditions c ON c.condition_id=e.condition_id "
                "JOIN governance_condition_groups g ON g.group_id=c.group_id "
                "WHERE g.agreement_id=? ORDER BY e.created_at, e.evidence_id",
                (agreement_id,),
            ).fetchall()
            return [{"evidence_id": row["evidence_id"], "condition_id": row["condition_id"],
                     "document_id": row["document_id"], "submitted_by": row["submitted_by"],
                     "submitter_org": row["submitter_org"], "late": bool(row["late"]),
                     "status": row["status"], "reviewed_by": row["reviewed_by"],
                     "reviewed_at": row["reviewed_at"], "created_at": row["created_at"]}
                    for row in rows]

    def list_breaches(self, *, actor_id: str, agreement_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._agreement(connection, agreement_id)
            self._require_view(connection, actor, agreement_id)
            breaches = connection.execute(
                "SELECT b.* FROM governance_breaches b "
                "JOIN governance_commitments c ON c.commitment_id=b.commitment_id "
                "WHERE c.agreement_id=? ORDER BY b.created_at, b.breach_id",
                (agreement_id,),
            ).fetchall()
            result = []
            for breach in breaches:
                rectifications = connection.execute(
                    "SELECT * FROM governance_rectifications WHERE breach_id=? ORDER BY order_seq",
                    (breach["breach_id"],),
                ).fetchall()
                result.append({
                    "breach_id": breach["breach_id"],
                    "commitment_id": breach["commitment_id"],
                    "severity": breach["severity"],
                    "description": breach["description"],
                    "unfulfilled_amount": breach["unfulfilled_amount"],
                    "status": breach["status"],
                    "rectifications": [
                        {"rectification_id": row["rectification_id"],
                         "requirement": row["requirement"], "due_at": row["due_at"],
                         "order_seq": row["order_seq"], "status": row["status"]}
                        for row in rectifications
                    ],
                })
            return result

    def pending_work(self, *, actor_id: str, agreement_id: str) -> dict[str, Any]:
        """未决复核与整改期限：按登记时的全局事件顺序排列，重启后保持不变。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._agreement(connection, agreement_id)
            self._require_supervisor(actor)
            sequence_of = self._creation_sequences(connection, agreement_id)
            items: list[dict[str, Any]] = []
            for row in connection.execute(
                    "SELECT e.evidence_id AS item_id, e.created_at AS created_at "
                    "FROM governance_evidence e "
                    "JOIN governance_conditions c ON c.condition_id=e.condition_id "
                    "JOIN governance_condition_groups g ON g.group_id=c.group_id "
                    "WHERE g.agreement_id=? AND e.status='submitted'",
                    (agreement_id,)):
                items.append({"kind": "evidence_review", "id": row["item_id"],
                              "created_at": row["created_at"], "due_at": None})
            for row in connection.execute(
                    "SELECT f.fulfillment_id AS item_id, f.created_at AS created_at "
                    "FROM governance_fulfillments f "
                    "JOIN governance_commitments c ON c.commitment_id=f.commitment_id "
                    "WHERE c.agreement_id=? AND f.status='pending_review'",
                    (agreement_id,)):
                items.append({"kind": "fulfillment_review", "id": row["item_id"],
                              "created_at": row["created_at"], "due_at": None})
            for row in connection.execute(
                    "SELECT r.rectification_id AS item_id, r.created_at AS created_at, "
                    "r.due_at AS due_at FROM governance_rectifications r "
                    "JOIN governance_breaches b ON b.breach_id=r.breach_id "
                    "JOIN governance_commitments c ON c.commitment_id=b.commitment_id "
                    "WHERE c.agreement_id=? AND r.status IN ('pending','submitted')",
                    (agreement_id,)):
                items.append({"kind": "rectification", "id": row["item_id"],
                              "created_at": row["created_at"], "due_at": row["due_at"]})
            items.sort(key=lambda item: sequence_of.get((item["kind"], item["id"]), 0))
            return {"agreement_id": agreement_id, "items": items,
                    "generated_at": self._now()}

    @staticmethod
    def _creation_sequences(connection, agreement_id: str) -> dict[tuple[str, str], int]:
        """从治理事件日志推导每类未决对象的登记顺序。"""

        mapping = {"evidence_submitted": ("evidence_review", "evidence_id"),
                   "fulfillment_submitted": ("fulfillment_review", "fulfillment_id"),
                   "rectification_created": ("rectification", "rectification_id")}
        sequences: dict[tuple[str, str], int] = {}
        rows = connection.execute(
            "SELECT sequence, event_type, payload_json FROM governance_events "
            "WHERE agreement_id=? AND event_type IN "
            "('evidence_submitted','fulfillment_submitted','rectification_created') "
            "ORDER BY sequence",
            (agreement_id,),
        ).fetchall()
        for row in rows:
            kind, key = mapping[row["event_type"]]
            payload = json.loads(row["payload_json"])
            sequences[(kind, payload[key])] = row["sequence"]
        return sequences

    # ------------------------------------------------------------------
    # 监督：对账与历史还原
    # ------------------------------------------------------------------

    def reconcile(self, *, actor_id: str, agreement_id: str) -> dict[str, Any]:
        """监督人员随时核对托管余额、已拨金额与未履行承诺。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            agreement = self._agreement(connection, agreement_id)
            self._require_supervisor(actor)
            account = connection.execute(
                "SELECT * FROM governance_escrow_accounts WHERE agreement_id=?",
                (agreement_id,),
            ).fetchone()
            totals = connection.execute(
                "SELECT entry_type, COALESCE(SUM(amount),0) AS total "
                "FROM governance_escrow_ledger WHERE account_id=? GROUP BY entry_type",
                (account["account_id"],),
            ).fetchall()
            deposited = sum(row["total"] for row in totals if row["entry_type"] == "deposit")
            disbursed = sum(row["total"] for row in totals if row["entry_type"] == "disburse")
            balance = self._escrow_balance(connection, account["account_id"])
            tranches = connection.execute(
                "SELECT * FROM governance_tranches WHERE account_id=? ORDER BY sequence_no",
                (account["account_id"],),
            ).fetchall()
            by_status: dict[str, int] = {}
            for row in tranches:
                by_status[row["status"]] = by_status.get(row["status"], 0) + row["amount"]
            commitments = connection.execute(
                "SELECT * FROM governance_commitments WHERE agreement_id=? "
                "AND status IN ('committed','active','breached','rectifying') "
                "ORDER BY created_at, commitment_id",
                (agreement_id,),
            ).fetchall()
            unfulfilled = [self._commitment_dict(row) for row in commitments]
            open_breaches = connection.execute(
                "SELECT COUNT(*) AS count FROM governance_breaches b "
                "JOIN governance_commitments c ON c.commitment_id=b.commitment_id "
                "WHERE c.agreement_id=? AND b.status IN ('open','rectifying','escalated')",
                (agreement_id,),
            ).fetchone()["count"]
            open_disputes = connection.execute(
                "SELECT COUNT(*) AS count FROM governance_disputes "
                "WHERE agreement_id=? AND status='filed'",
                (agreement_id,),
            ).fetchone()["count"]
            held = by_status.get("held", 0)
            released = by_status.get("released", 0)
            tranche_disbursed = by_status.get("disbursed", 0)
            consistent = (balance == held + released) and (disbursed == tranche_disbursed)
            return {
                "agreement_id": agreement_id,
                "status": agreement["status"],
                "currency": agreement["currency"],
                "escrow": {
                    "balance": balance,
                    "total_deposited": deposited,
                    "total_disbursed": disbursed,
                    "held_amount": held,
                    "released_amount": released,
                    "scheduled_amount": by_status.get("scheduled", 0),
                    "ledger_consistent": consistent,
                },
                "tranches": [{"tranche_id": row["tranche_id"],
                              "sequence_no": row["sequence_no"],
                              "amount": row["amount"],
                              "status": row["status"],
                              "condition_group_id": row["condition_group_id"]}
                             for row in tranches],
                "unfulfilled_commitments": unfulfilled,
                "outstanding_obligation": sum(item["remaining_amount"] for item in unfulfilled),
                "open_breaches": open_breaches,
                "open_disputes": open_disputes,
                "generated_at": self._now(),
            }

    def reconstruct(self, *, actor_id: str, agreement_id: str, at: str) -> dict[str, Any]:
        """还原某个历史时点：每项成果归谁、哪方仍负有责任。"""

        at = self._timestamp(at, "at")
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._agreement(connection, agreement_id)
            self._require_supervisor(actor)
            rows = connection.execute(
                "SELECT event_type, payload_json FROM governance_events "
                "WHERE agreement_id=? AND occurred_at<=? ORDER BY sequence",
                (agreement_id, at),
            ).fetchall()
        state: dict[str, Any] = {
            "status": "negotiating",
            "parties": {},
            "commitments": {},
            "outcomes": {},
            "escrow": {"balance": 0, "disbursed": 0},
        }
        for row in rows:
            self._fold(state, row["event_type"], json.loads(row["payload_json"]))
        return {
            "agreement_id": agreement_id,
            "at": at,
            "status": state["status"],
            "parties": list(state["parties"].values()),
            "commitments": list(state["commitments"].values()),
            "outcomes": list(state["outcomes"].values()),
            "escrow": state["escrow"],
        }

    @staticmethod
    def _fold(state: dict[str, Any], event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "party_added":
            state["parties"][payload["party_id"]] = {
                "party_id": payload["party_id"],
                "organization_id": payload["organization_id"],
                "party_role": payload["party_role"],
                "status": "active",
            }
        elif event_type == "party_replaced":
            outgoing = state["parties"].get(payload["outgoing_party_id"])
            if outgoing is not None:
                outgoing["status"] = "replaced"
            state["parties"][payload["incoming_party_id"]] = {
                "party_id": payload["incoming_party_id"],
                "organization_id": payload["incoming_organization_id"],
                "party_role": payload["party_role"],
                "status": "active",
            }
        elif event_type == "commitment_created":
            state["commitments"][payload["commitment_id"]] = {
                "commitment_id": payload["commitment_id"],
                "category": payload["category"],
                "stage": payload["stage"],
                "responsible_party_id": payload["responsible_party_id"],
                "target_amount": payload["target_amount"],
                "fulfilled_amount": 0,
                "remaining_amount": payload["target_amount"],
                "status": "draft",
            }
        elif event_type == "commitments_formalized":
            for commitment_id in payload["commitment_ids"]:
                if commitment_id in state["commitments"]:
                    state["commitments"][commitment_id]["status"] = "committed"
            if state["status"] == "negotiating":
                state["status"] = "committed"
        elif event_type == "commitment_activated":
            commitment = state["commitments"].get(payload["commitment_id"])
            if commitment is not None:
                commitment["status"] = "active"
            state["status"] = "active"
        elif event_type == "fulfillment_approved":
            commitment = state["commitments"].get(payload["commitment_id"])
            if commitment is not None:
                commitment["fulfilled_amount"] = payload["fulfilled_amount"]
                commitment["remaining_amount"] = \
                    commitment["target_amount"] - payload["fulfilled_amount"]
                commitment["status"] = payload["status"]
        elif event_type == "breach_reported":
            commitment = state["commitments"].get(payload["commitment_id"])
            if commitment is not None:
                commitment["status"] = "breached"
        elif event_type == "rectification_created":
            commitment = state["commitments"].get(payload["commitment_id"])
            if commitment is not None:
                commitment["status"] = "rectifying"
        elif event_type == "breach_resolved":
            commitment = state["commitments"].get(payload["commitment_id"])
            if commitment is not None:
                commitment["status"] = payload["restored_status"]
        elif event_type == "scope_reduced":
            commitment = state["commitments"].get(payload["commitment_id"])
            if commitment is not None:
                commitment["target_amount"] = payload["new_target"]
                commitment["remaining_amount"] = \
                    payload["new_target"] - commitment["fulfilled_amount"]
                commitment["status"] = payload["status"]
        elif event_type == "responsibility_transferred":
            commitment = state["commitments"].get(payload["commitment_id"])
            if commitment is not None:
                commitment["responsible_party_id"] = payload["to_party_id"]
        elif event_type == "outcome_registered":
            state["outcomes"][payload["outcome_id"]] = {
                "outcome_id": payload["outcome_id"],
                "outcome_key": payload["outcome_key"],
                "title": payload["title"],
                "beneficiary_group": payload["beneficiary_group"],
                "shared": payload["shared"],
                "claimed_by_project": None,
            }
        elif event_type == "outcome_claimed":
            outcome = state["outcomes"].get(payload["outcome_id"])
            if outcome is not None:
                outcome["claimed_by_project"] = payload["project_key"]
        elif event_type == "escrow_deposited":
            state["escrow"]["balance"] += payload["amount"]
        elif event_type == "tranche_disbursed":
            state["escrow"]["balance"] -= payload["amount"]
            state["escrow"]["disbursed"] += payload["amount"]
        elif event_type == "exit_began":
            state["status"] = "exiting"
        elif event_type == "agreement_exited":
            state["status"] = "exited"
