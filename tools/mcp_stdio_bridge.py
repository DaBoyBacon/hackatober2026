#!/usr/bin/env python3
"""stdio ⇄ HTTP bridge for the Drink Refill Monitor's MCP endpoint.

Some agent apps can only launch MCP servers as local processes speaking JSON-RPC
over stdin/stdout. This script is that process: it forwards each message to the
board over HTTP and prints the reply. Standard library only — copy it anywhere.

    python3 mcp_stdio_bridge.py http://192.168.1.50:7000/mcp

The token comes from --token or the DRINK_MCP_TOKEN environment variable
(copy it from the dashboard's "Connect an AI agent" card).

Example config for apps that use the common "mcpServers" JSON format:

    {
      "mcpServers": {
        "drink-coaster": {
          "command": "python3",
          "args": ["/path/to/mcp_stdio_bridge.py", "http://192.168.1.50:7000/mcp"],
          "env": {"DRINK_MCP_TOKEN": "paste-token-here"}
        }
      }
    }
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

META_VERSION = "io.modelcontextprotocol/protocolVersion"
NAMED_METHODS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}


def log(msg: str) -> None:
    print(f"[drink-coaster bridge] {msg}", file=sys.stderr, flush=True)


def header_safe(value: str) -> str:
    """Per MCP Streamable HTTP: non-ASCII / padded / sentinel-looking values go base64."""
    plain = (value and value == value.strip() and all(0x20 <= ord(c) <= 0x7E for c in value)
             and not (value.startswith("=?base64?") and value.endswith("?=")))
    if plain:
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


class Bridge:
    def __init__(self, url: str, token: str | None, timeout: float):
        self.url, self.token, self.timeout = url, token, timeout
        self.legacy_version: str | None = None   # set after an `initialize` handshake

    def headers_for(self, msg: dict) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        if META_VERSION in meta:                              # 2026-07-28+ request
            h["MCP-Protocol-Version"] = str(meta[META_VERSION])
            if method:
                h["Mcp-Method"] = method
            field = NAMED_METHODS.get(method)
            if field and isinstance(params.get(field), str):
                h["Mcp-Name"] = header_safe(params[field])
        elif self.legacy_version:
            h["MCP-Protocol-Version"] = self.legacy_version
        return h

    def forward(self, msg) -> list[dict]:
        """Send one message (or batch); return the JSON-RPC messages to print."""
        single = msg if isinstance(msg, dict) else {}
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode("utf-8"),
                                     headers=self.headers_for(single), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, ctype, body = resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            status, ctype, body = e.code, e.headers.get("Content-Type", ""), e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return self._synth_error(msg, f"Can't reach the coaster at {self.url}: {e}")

        if status == 202 or not body:
            return [] if status < 400 else self._synth_error(msg, f"HTTP {status} from the coaster")

        replies = self._parse(body, ctype)
        if not replies:
            return self._synth_error(msg, f"HTTP {status}: unexpected response from the coaster")
        if "id" in single:
            # Transport-level rejections (401 bad token, 403 origin, …) carry no id;
            # give them the request's id so the client can match them up instead of hanging.
            for r in replies:
                if isinstance(r, dict) and "error" in r and r.get("id") is None:
                    r["id"] = single["id"]
                    if status == 401:
                        log("The coaster rejected the token. Copy a fresh one from the dashboard.")
        for r in replies:
            res = r.get("result") if isinstance(r, dict) else None
            if single.get("method") == "initialize" and isinstance(res, dict):
                self.legacy_version = res.get("protocolVersion")
        return replies

    @staticmethod
    def _parse(body: bytes, ctype: str) -> list:
        text = body.decode("utf-8", "replace")
        if "text/event-stream" in ctype:
            out, data = [], []
            for line in text.splitlines() + [""]:
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "" and data:
                    try:
                        out.append(json.loads("\n".join(data)))
                    except json.JSONDecodeError:
                        pass
                    data = []
            return out
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else [parsed]

    @staticmethod
    def _synth_error(msg, text: str) -> list[dict]:
        """Turn transport failures into JSON-RPC errors so the client doesn't hang."""
        log(text)
        msgs = msg if isinstance(msg, list) else [msg]
        return [{"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32000, "message": text}}
                for m in msgs if isinstance(m, dict) and "id" in m and "method" in m]


def main() -> None:
    ap = argparse.ArgumentParser(description="stdio bridge to the Drink Refill Monitor MCP endpoint")
    ap.add_argument("url", help="e.g. http://192.168.1.50:7000/mcp")
    ap.add_argument("--token", default=os.environ.get("DRINK_MCP_TOKEN"),
                    help="bearer token (default: $DRINK_MCP_TOKEN)")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()
    if not args.token:
        log("No token given (--token or DRINK_MCP_TOKEN); requests will be rejected if the board requires one.")

    bridge = Bridge(args.url, args.token, args.timeout)
    log(f"Forwarding stdio to {args.url}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": f"Parse error: {e}"}}), flush=True)
            continue
        for reply in bridge.forward(msg):
            sys.stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
