"""无第三方依赖的轻微事故快速结算 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import SettlementError, ValidationFailed
from .service import SettlementService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: SettlementService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            actor = self._actor(normalized)
            if method == "POST" and path == "/cases":
                return Response(201, self.service.register_case(actor, payload["case_id"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "liability":
                return Response(201, self.service.issue_liability(
                    actor, parts[1], payload["shares"], payload["basis"], payload.get("idempotency_key")))
            if method == "GET" and len(parts) == 2 and parts[0] == "cases":
                return Response(200, self.service.case_detail(actor, parts[1]))
            if method == "POST" and path == "/settlements":
                return Response(201, self.service.create_sheet(actor, payload["settlement_id"], payload["case_id"]))
            if method == "GET" and len(parts) == 2 and parts[0] == "settlements":
                return Response(200, self.service.get_sheet(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "settlements" and parts[2] == "quotes":
                return Response(201, self.service.add_quote_version(
                    actor, parts[1], payload["garage_id"], payload["items"], payload.get("note", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "settlements" and parts[2] == "confirmations":
                return Response(201, self.service.record_confirmation(
                    actor, parts[1], payload["party_id"], payload["decision"], payload.get("note", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "settlements" and parts[2] == "payments":
                return Response(201, self.service.record_payment(
                    actor, parts[1], payload["party_id"], payload["amount_cny"], payload["receipt_no"],
                    payload["channel"], payload.get("received_at"), payload.get("idempotency_key")))
            if method == "POST" and len(parts) == 3 and parts[0] == "settlements" and parts[2] == "refunds":
                return Response(201, self.service.record_refund(
                    actor, parts[1], payload["party_id"], payload["amount_cny"], payload["receipt_no"],
                    payload["channel"], payload.get("original_receipt_no"), payload.get("received_at"),
                    payload.get("idempotency_key")))
            if method == "POST" and len(parts) == 3 and parts[0] == "settlements" and parts[2] == "abandon":
                return Response(200, self.service.abandon_sheet(actor, parts[1], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "cases" and parts[2] == "close":
                return Response(200, self.service.close_case(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "history":
                return Response(200, self.service.history(actor, parts[1], parts[2]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except SettlementError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RapidSettlement/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动轻微事故快速结算服务")
    parser.add_argument("--database", type=Path, default=Path("settlement.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(SettlementService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
