"""承诺治理模块的 HTTP/JSON 边界，与基础服务共用同一台服务器。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..api import route as foundation_route
from ..errors import DomainError, ValidationError
from ..service import DomainService
from ..storage import Database
from .service import GovernanceService


def _created(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def route_governance(governance: GovernanceService, method: str, path: str,
                     body: dict[str, Any] | None,
                     headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]] | None:
    """分派 /governance 前缀的请求；非治理路径返回 None 交给基础路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    if not parsed.path.startswith("/governance"):
        return None
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    try:
        if method == "POST":
            commands: dict[str, Callable[..., Any]] = {
                "/governance/agreements": governance.create_agreement,
                "/governance/parties": governance.add_party,
                "/governance/party-replacements": governance.replace_party,
                "/governance/documents": governance.submit_document,
                "/governance/commitments": governance.create_commitment,
                "/governance/commitment-formalizations": governance.formalize_commitments,
                "/governance/condition-groups": governance.create_condition_group,
                "/governance/evidence": governance.submit_evidence,
                "/governance/evidence-reviews": governance.review_evidence,
                "/governance/fulfillments": governance.record_fulfillment,
                "/governance/fulfillment-reviews": governance.review_fulfillment,
                "/governance/tranches": governance.schedule_tranche,
                "/governance/deposits": governance.deposit_escrow,
                "/governance/disbursements": governance.confirm_disbursement,
                "/governance/breaches": governance.report_breach,
                "/governance/rectifications": governance.create_rectification,
                "/governance/rectification-submissions": governance.submit_rectification,
                "/governance/rectification-reviews": governance.review_rectification,
                "/governance/scope-reductions": governance.reduce_scope,
                "/governance/outcomes": governance.register_outcome,
                "/governance/outcome-claims": governance.claim_outcome,
                "/governance/disputes": governance.file_dispute,
                "/governance/dispute-conclusions": governance.conclude_dispute,
                "/governance/exit-beginnings": governance.begin_exit,
                "/governance/exit-completions": governance.complete_exit,
            }
            handler = commands.get(parsed.path)
            if handler is not None:
                return _created(handler(actor_id=actor_id, **body))
            if parsed.path == "/governance/overdue-sweeps":
                agreement_id = str(body.get("agreement_id", ""))
                return 200, governance.sweep_overdue_rectifications(
                    actor_id=actor_id, agreement_id=agreement_id)
        if method == "GET":
            agreement_id = query.get("agreement_id", [""])[0]
            if parsed.path == "/governance/agreement":
                if not agreement_id:
                    raise ValidationError("agreement_id 不能为空")
                return 200, governance.get_agreement_view(actor_id=actor_id,
                                                          agreement_id=agreement_id)
            if parsed.path == "/governance/commitments":
                if not agreement_id:
                    raise ValidationError("agreement_id 不能为空")
                items = governance.list_commitments(
                    actor_id=actor_id, agreement_id=agreement_id,
                    status=query.get("status", [None])[0],
                    category=query.get("category", [None])[0])
                return 200, {"items": items}
            if parsed.path == "/governance/documents":
                if not agreement_id:
                    raise ValidationError("agreement_id 不能为空")
                items = governance.list_documents(actor_id=actor_id, agreement_id=agreement_id,
                                                  kind=query.get("kind", [None])[0])
                return 200, {"items": items}
            if parsed.path == "/governance/evidence":
                if not agreement_id:
                    raise ValidationError("agreement_id 不能为空")
                return 200, {"items": governance.list_evidence(actor_id=actor_id,
                                                               agreement_id=agreement_id)}
            if parsed.path == "/governance/breaches":
                if not agreement_id:
                    raise ValidationError("agreement_id 不能为空")
                return 200, {"items": governance.list_breaches(actor_id=actor_id,
                                                               agreement_id=agreement_id)}
            if parsed.path == "/governance/pending-work":
                if not agreement_id:
                    raise ValidationError("agreement_id 不能为空")
                return 200, governance.pending_work(actor_id=actor_id, agreement_id=agreement_id)
            if parsed.path == "/governance/reconciliation":
                if not agreement_id:
                    raise ValidationError("agreement_id 不能为空")
                return 200, governance.reconcile(actor_id=actor_id, agreement_id=agreement_id)
            if parsed.path == "/governance/reconstruction":
                if not agreement_id:
                    raise ValidationError("agreement_id 不能为空")
                at = query.get("at", [""])[0]
                if not at:
                    raise ValidationError("at 不能为空")
                return 200, governance.reconstruct(actor_id=actor_id, agreement_id=agreement_id,
                                                   at=at)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def make_router(database: Database) -> Callable[[str, str, dict[str, Any] | None,
                                                 dict[str, str] | None],
                                                tuple[int, dict[str, Any]]]:
    """组合治理路由与基础路由，供 HTTP 服务与测试复用。"""

    domain = DomainService(database)
    governance = GovernanceService(database, domain)

    def combined(method: str, path: str, body: dict[str, Any] | None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        result = route_governance(governance, method, path, body, headers)
        if result is not None:
            return result
        return foundation_route(domain, method, path, body, headers)

    return combined


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为组合路由调用。"""

    router: Callable[[str, str, dict[str, Any] | None, dict[str, str] | None],
                     tuple[int, dict[str, Any]]]

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = self.router(self.command, self.path, body,
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
    """启动同时承载基础服务与承诺治理模块的 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动跨境合作承诺治理服务")
    parser.add_argument("--database", default="governance.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.router = staticmethod(make_router(database))
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
