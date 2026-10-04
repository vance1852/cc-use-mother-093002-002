"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .commitment_service import CommitmentService
from .errors import DomainError, ValidationError
from .storage import Database


def route(service: CommitmentService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = dict(body or {})
    body.setdefault("actor_id", headers.get("X-Actor-Id", ""))
    actor_id = body["actor_id"] or headers.get("X-Actor-Id", "")
    parsed = urlparse(path)
    parts = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        # ------------------------------------------------------------ 基础服务
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [item.__dict__ for item in
                                   service.list_domain_data(site_id, q("category"))]}
        if method == "GET" and parsed.path == "/audit-events":
            return 200, {"items": service.audit_events(int(q("after_sequence", "0") or "0"))}

        # ------------------------------------------------------------ 项目与参与方
        if method == "POST" and parsed.path == "/projects":
            receipt = service.create_project(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and len(parts) == 2 and parts[0] == "projects":
            return 200, asdict(service.get_project(actor_id=actor_id, project_id=parts[1]))
        if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "close":
            receipt = service.close_project(project_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "parties":
            receipt = service.add_project_party(project_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if (method == "POST" and len(parts) == 4 and parts[0] == "projects"
                and parts[2] == "parties" and parts[3] == "replacements"):
            receipt = service.replace_project_party(project_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        # ------------------------------------------------------------ 承诺与阶段
        if method == "POST" and parsed.path == "/commitments":
            receipt = service.draft_commitment(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "commitments" and parts[2] == "drafts":
            receipt = service.revise_negotiation_draft(commitment_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "commitments" and parts[2] == "seal":
            receipt = service.seal_commitment(commitment_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "commitments" and parts[2] == "stages":
            receipt = service.add_stage(commitment_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "commitments":
            items = service.list_commitments(actor_id=actor_id, project_id=parts[1],
                                             commitment_type=q("commitment_type"))
            return 200, {"items": [asdict(item) for item in items]}
        if method == "GET" and len(parts) == 3 and parts[0] == "commitments" \
                and parts[2] == "responsibility":
            return 200, asdict(service.responsibility(actor_id=actor_id,
                                                      commitment_id=parts[1], at=q("at")))
        if method == "POST" and len(parts) == 3 and parts[0] == "commitments" \
                and parts[2] == "partial-breach":
            receipt = service.record_partial_breach(commitment_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "commitments" \
                and parts[2] == "scope-reduction":
            receipt = service.reduce_scope(commitment_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        # ------------------------------------------------------------ 条件、证据、复核
        if method == "POST" and len(parts) == 3 and parts[0] == "stages" and parts[2] == "conditions":
            receipt = service.add_condition(stage_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and len(parts) == 2 and parts[0] == "conditions":
            return 200, asdict(service.get_condition(actor_id=actor_id, condition_id=parts[1]))
        if method == "POST" and len(parts) == 3 and parts[0] == "conditions" and parts[2] == "evidences":
            receipt = service.submit_evidence(condition_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "evidences" and parts[2] == "review":
            receipt = service.decide_review(evidence_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "stages" and parts[2] == "effect":
            receipt = service.effect_stage(stage_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "stages" and parts[2] == "disburse":
            receipt = service.disburse_stage(stage_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        # ------------------------------------------------------------ 托管
        if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "escrow":
            return 200, asdict(service.escrow(actor_id=actor_id, project_id=parts[1]))
        if method == "POST" and len(parts) == 4 and parts[0] == "projects" \
                and parts[2] == "escrow" and parts[3] == "deposits":
            receipt = service.deposit_escrow(project_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "reconcile":
            return 200, service.reconcile(actor_id=actor_id, project_id=parts[1], at=q("at"))
        if method == "GET" and len(parts) == 3 and parts[0] == "projects" \
                and parts[2] == "pending-reviews":
            return 200, {"items": service.pending_reviews(actor_id=actor_id, project_id=parts[1])}
        if method == "GET" and len(parts) == 3 and parts[0] == "projects" \
                and parts[2] == "outcome-assignments":
            return 200, {"items": service.outcome_assignments(actor_id=actor_id,
                                                              project_id=parts[1], at=q("at"))}

        # ------------------------------------------------------------ 争议
        if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "disputes":
            receipt = service.open_dispute(project_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "conclude":
            receipt = service.conclude_dispute(dispute_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and len(parts) == 2 and parts[0] == "disputes":
            return 200, asdict(service.get_dispute(actor_id=actor_id, dispute_id=parts[1]))

        # ------------------------------------------------------------ 成果
        if method == "POST" and parsed.path == "/outcomes":
            receipt = service.register_outcome(**body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and len(parts) == 3 and parts[0] == "outcomes" and parts[2] == "claims":
            receipt = service.claim_outcome(outcome_id=parts[1], **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        # ------------------------------------------------------------ 历史还原
        if method == "GET" and parsed.path == "/history":
            entity_type = q("entity_type", "")
            entity_id = q("entity_id", "")
            at = q("at", "")
            if not entity_type or not entity_id or not at:
                raise ValidationError("entity_type、entity_id、at 均不能为空")
            return 200, service.history_at(actor_id=actor_id, entity_type=entity_type,
                                           entity_id=entity_id, at=at)

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        payload: dict[str, Any] = {"error": exc.code, "message": str(exc)}
        if exc.payload:
            payload["details"] = exc.payload
        return exc.status, payload
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: CommitmentService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动数字贸易承诺治理服务")
    parser.add_argument("--database", default="commitment_governance.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = CommitmentService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
