"""A small, dependency-free MCP server (Streamable HTTP, tools only).

Why hand-rolled? The UNO Q's App Lab container already ships FastAPI through
the web_ui brick, so serving MCP from that same web server means no extra
packages, no extra port, and nothing new to install on the board.

Protocol support
----------------
* 2026-07-28 (stateless): per-request `_meta` protocol version, `server/discover`,
  `resultType`, `Mcp-Method` / `Mcp-Name` / `MCP-Protocol-Version` header checks.
* 2025-03-26 … 2025-11-25 (handshake era): `initialize`, `notifications/initialized`,
  `ping`, JSON-RPC batches (2025-03-26). No sessions are minted — every
  request is self-contained, which those revisions allow.

Responses are always plain JSON (never SSE): every tool here answers in
milliseconds, so there is nothing to stream.

This module is framework-agnostic: `McpEndpoint.handle_http()` takes the HTTP
method, headers, and body and returns (status, headers, body). main.py adapts
it to FastAPI; the tests drive it directly and through a real HTTP server.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LEGACY_DEFAULT_WITHOUT_HEADER = "2025-03-26"   # per spec, for clients that send no version header

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

MAX_BODY_BYTES = 1_000_000


class ToolError(Exception):
    """Raise from a tool handler to return a tool-level error the model can read and act on."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, "CallContext"], Any]
    title: str | None = None
    annotations: dict | None = None

    def definition(self) -> dict:
        d: dict[str, Any] = {"name": self.name, "description": self.description,
                             "inputSchema": self.input_schema}
        if self.title:
            d["title"] = self.title
        if self.annotations:
            d["annotations"] = self.annotations
        return d


@dataclass
class CallContext:
    protocol_version: str
    client_name: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class _Reply:
    status: int
    body: Any = None                       # JSON-serialisable, or None for an empty body
    headers: dict = field(default_factory=dict)


class McpEndpoint:
    def __init__(
        self,
        name: str,
        version: str,
        tools: list[Tool],
        *,
        title: str | None = None,
        instructions: str = "",
        token_provider: Callable[[], str | None] = lambda: None,
        on_call: Callable[[str, dict, CallContext, bool], None] | None = None,
        list_ttl_ms: int = 300_000,
    ):
        self.server_info = {"name": name, "version": version}
        if title:
            self.server_info["title"] = title
        self.tools = {t.name: t for t in tools}
        self._tool_order = [t.name for t in tools]   # deterministic tools/list order
        self.instructions = instructions
        self.token_provider = token_provider
        self.on_call = on_call
        self.list_ttl_ms = list_ttl_ms

    # ================================================================ HTTP ===
    def handle_http(self, method: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict, bytes]:
        h = {k.lower(): v for k, v in headers.items()}
        reply = self._handle(method.upper(), h, body)
        out_headers = dict(reply.headers)
        if reply.body is None:
            return reply.status, out_headers, b""
        out_headers["Content-Type"] = "application/json"
        return reply.status, out_headers, json.dumps(reply.body, separators=(",", ":")).encode("utf-8")

    def _handle(self, method: str, h: dict, body: bytes) -> _Reply:
        if method != "POST":
            # No standalone SSE stream (GET) and no sessions to delete (DELETE).
            return _Reply(405, _err(None, INVALID_REQUEST, "Use POST for MCP requests."), {"Allow": "POST"})

        if not self._origin_ok(h.get("origin"), h.get("host")):
            return _Reply(403, _err(None, INVALID_REQUEST, "Origin not allowed."))

        token = self.token_provider()
        if token:
            auth = h.get("authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
                return _Reply(401, _err(None, INVALID_REQUEST,
                                        "Missing or wrong bearer token. Copy it from the dashboard's "
                                        "'Connect an AI agent' card."),
                              {"WWW-Authenticate": 'Bearer realm="drink-refill-monitor"'})

        if len(body) > MAX_BODY_BYTES:
            return _Reply(413, _err(None, INVALID_REQUEST, "Request too large."))
        try:
            msg = json.loads(body.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _Reply(400, _err(None, PARSE_ERROR, f"Parse error: {exc}"))

        if isinstance(msg, list):                       # JSON-RPC batch (2025-03-26 only)
            if not msg:
                return _Reply(400, _err(None, INVALID_REQUEST, "Empty batch."))
            replies = [self._handle_message(m, h, in_batch=True) for m in msg]
            bodies = [r.body for r in replies if r.body is not None]
            return _Reply(200, bodies) if bodies else _Reply(202)
        return self._handle_message(msg, h)

    # ============================================================ JSON-RPC ===
    def _handle_message(self, msg: Any, h: dict, in_batch: bool = False) -> _Reply:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _Reply(400, _err(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message."))
        rpc_method = msg.get("method")
        if rpc_method is None:
            return _Reply(202)                          # a response to us; we never ask, so ignore
        if not isinstance(rpc_method, str):
            return _Reply(400, _err(msg.get("id"), INVALID_REQUEST, "method must be a string."))

        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _Reply(400, _err(msg.get("id"), INVALID_PARAMS, "params must be an object."))
        if "id" not in msg:
            return _Reply(202)                          # notifications: initialized, cancelled, …

        req_id = msg["id"]
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        modern_version = meta.get(META_VERSION)

        if modern_version is not None and not in_batch:
            return self._handle_modern(req_id, rpc_method, params, meta, modern_version, h)
        return self._handle_legacy(req_id, rpc_method, params, h)

    # ---------------------------------------------------------- 2026-07-28 ---
    def _handle_modern(self, req_id, rpc_method, params, meta, version, h) -> _Reply:
        if version not in MODERN_VERSIONS:
            return _Reply(400, _err(req_id, UNSUPPORTED_PROTOCOL_VERSION,
                                    f"Unsupported protocol version: {version}",
                                    {"supported": list(MODERN_VERSIONS), "requested": version}))

        mismatch = self._check_headers(h, version, rpc_method, params)
        if mismatch:
            return _Reply(400, _err(req_id, HEADER_MISMATCH, f"Header mismatch: {mismatch}"))

        client = meta.get(META_CLIENT_INFO) if isinstance(meta.get(META_CLIENT_INFO), dict) else {}
        ctx = CallContext(version, client.get("name"))

        if rpc_method == "server/discover":
            result = {
                "supportedVersions": list(MODERN_VERSIONS),
                "capabilities": {"tools": {}},
                "ttlMs": self.list_ttl_ms,
                "cacheScope": "private",
            }
            if self.instructions:
                result["instructions"] = self.instructions
        elif rpc_method == "tools/list":
            result = {"tools": self._tool_list(), "ttlMs": self.list_ttl_ms, "cacheScope": "private"}
        elif rpc_method == "tools/call":
            result = self._call_tool(params, ctx)
            if isinstance(result, _RpcError):
                return _Reply(200, _err(req_id, result.code, result.message))
        else:
            return _Reply(404, _err(req_id, METHOD_NOT_FOUND, f"Method not found: {rpc_method}"))

        result["resultType"] = "complete"
        result.setdefault("_meta", {})[META_SERVER_INFO] = self.server_info
        return _Reply(200, _ok(req_id, result))

    @staticmethod
    def _check_headers(h: dict, version: str, rpc_method: str, params: dict) -> str | None:
        hv = h.get("mcp-protocol-version")
        if hv is None:
            return "MCP-Protocol-Version header is required"
        if hv != version:
            return f"MCP-Protocol-Version '{hv}' does not match body '{version}'"
        hm = h.get("mcp-method")
        if hm is None:
            return "Mcp-Method header is required"
        if hm != rpc_method:
            return f"Mcp-Method '{hm}' does not match body '{rpc_method}'"
        if rpc_method in ("tools/call", "prompts/get", "resources/read"):
            body_name = params.get("uri") if rpc_method == "resources/read" else params.get("name")
            hn = h.get("mcp-name")
            if hn is None:
                return "Mcp-Name header is required"
            decoded = _decode_header_value(hn)
            if decoded is None:
                return "Mcp-Name header is malformed"
            if decoded != body_name:
                return f"Mcp-Name '{decoded}' does not match body '{body_name}'"
        return None

    # ---------------------------------------------- 2025-03-26 … 2025-11-25 ---
    def _handle_legacy(self, req_id, rpc_method, params, h) -> _Reply:
        if rpc_method == "initialize":
            requested = params.get("protocolVersion")
            negotiated = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
            result = {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": self.server_info,
            }
            if self.instructions:
                result["instructions"] = self.instructions
            return _Reply(200, _ok(req_id, result))

        version = h.get("mcp-protocol-version", LEGACY_DEFAULT_WITHOUT_HEADER)
        if version not in LEGACY_VERSIONS:
            if version in MODERN_VERSIONS:
                msg = ("Requests for protocol 2026-07-28 must carry "
                       f"params._meta['{META_VERSION}'].")
            else:
                msg = f"Unsupported MCP-Protocol-Version: {version}"
            return _Reply(400, _err(req_id, INVALID_REQUEST, msg))

        if rpc_method == "ping":
            return _Reply(200, _ok(req_id, {}))
        if rpc_method == "tools/list":
            return _Reply(200, _ok(req_id, {"tools": self._tool_list()}))
        if rpc_method == "tools/call":
            result = self._call_tool(params, CallContext(version))
            if isinstance(result, _RpcError):
                return _Reply(200, _err(req_id, result.code, result.message))
            return _Reply(200, _ok(req_id, result))
        return _Reply(200, _err(req_id, METHOD_NOT_FOUND, f"Method not found: {rpc_method}"))

    # =============================================================== tools ===
    def _tool_list(self) -> list[dict]:
        return [self.tools[n].definition() for n in self._tool_order]

    def _call_tool(self, params: dict, ctx: CallContext) -> "dict | _RpcError":
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _tool_error("arguments must be an object.")

        problems = validate(args, tool.input_schema)
        if problems:
            # Input problems are reported as tool errors so the model can fix and retry.
            self._notify(tool.name, args, ctx, False)
            return _tool_error("Invalid arguments: " + "; ".join(problems))

        try:
            data = tool.handler(args, ctx)
        except ToolError as exc:
            self._notify(tool.name, args, ctx, False)
            return _tool_error(str(exc))
        except Exception as exc:  # never leak a stack trace to the model, but log it
            traceback.print_exc()
            self._notify(tool.name, args, ctx, False)
            return _tool_error(f"Internal error while running {tool.name}: {exc.__class__.__name__}")

        self._notify(tool.name, args, ctx, True)
        result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                                  "isError": False}
        if isinstance(data, dict):
            result["structuredContent"] = data
        return result

    def _notify(self, name, args, ctx, ok) -> None:
        if self.on_call:
            try:
                self.on_call(name, args, ctx, ok)
            except Exception:
                traceback.print_exc()

    # ============================================================ security ===
    @staticmethod
    def _origin_ok(origin: str | None, host: str | None) -> bool:
        """Browsers send Origin; agents usually don't. A page on some other site must not
        be able to drive this endpoint (DNS-rebinding), so Origin, when present, has to be
        a LAN-style host *and* the same host the request was sent to."""
        if not origin:
            return True
        if origin == "null":
            return False
        o = urlsplit(origin)
        if not o.hostname:
            return False
        if not _is_lan_hostname(o.hostname):
            return False
        req_host = urlsplit(f"//{host}").hostname if host else None
        return req_host is not None and req_host.lower() == o.hostname.lower()


class _RpcError:
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def _ok(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message, data=None) -> dict:
    e: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    out: dict[str, Any] = {"jsonrpc": "2.0", "error": e}
    if req_id is not None:
        out["id"] = req_id
    return out


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _decode_header_value(v: str) -> str | None:
    if v.startswith("=?base64?") and v.endswith("?="):
        try:
            return base64.b64decode(v[9:-2], validate=True).decode("utf-8")
        except Exception:
            return None
    return v


def _is_lan_hostname(hostname: str) -> bool:
    name = hostname.lower().strip("[]")
    if name in ("localhost",) or name.endswith(".local") or name.endswith(".localhost") or "." not in name:
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


# ====================================================== tiny JSON Schema ===
def validate(value: Any, schema: dict, path: str = "") -> list[str]:
    """Enough JSON Schema for tool inputs: type, enum, min/max, required, additionalProperties."""
    where = path or "arguments"
    problems: list[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{where} must be an object"]
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                problems.append(f"'{req}' is required")
        for k, v in value.items():
            if k in props:
                problems += validate(v, props[k], k)
            elif schema.get("additionalProperties") is False:
                problems.append(f"unknown argument '{k}' (allowed: {', '.join(props) or 'none'})")
        return problems
    if t == "integer":
        if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, float) and value.is_integer())):
            return [f"{where} must be an integer"]
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{where} must be a number"]
    elif t == "string":
        if not isinstance(value, str):
            return [f"{where} must be a string"]
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{where} must be at most {schema['maxLength']} characters")
    elif t == "boolean":
        if not isinstance(value, bool):
            return [f"{where} must be true or false"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where} must be one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{where} must be ≥ {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{where} must be ≤ {schema['maximum']}")
    return problems
