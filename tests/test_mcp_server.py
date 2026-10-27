"""Protocol tests for the MCP endpoint and the drink tools. Standard library only.

    python3 -m unittest discover -s tests -v
"""

import base64
import json
import sys
import time
import unittest
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from agent_tools import build_tools  # noqa: E402
from drink_monitor import DrinkMonitor, Settings  # noqa: E402
from intake import IntakeLog  # noqa: E402
from mcp_server import McpEndpoint, Tool, ToolError  # noqa: E402

MODERN = "2026-07-28"
TOKEN = "s3cret"


def echo_tool():
    def handler(args, ctx):
        if args.get("fail"):
            raise ToolError("asked to fail")
        if args.get("crash"):
            raise RuntimeError("boom")
        return {"echo": args.get("text", ""), "version": ctx.protocol_version, "client": ctx.client_name}
    return Tool("echo", "Echo text back.", {
        "type": "object", "additionalProperties": False,
        "properties": {"text": {"type": "string", "maxLength": 10},
                       "fail": {"type": "boolean"}, "crash": {"type": "boolean"},
                       "n": {"type": "integer", "minimum": 1, "maximum": 3}},
        "required": ["text"]}, handler, annotations={"readOnlyHint": True})


class Http:
    def __init__(self, endpoint):
        self.ep = endpoint

    def post(self, body, headers=None, token=TOKEN, raw=False):
        h = {"Content-Type": "application/json", "Host": "192.168.1.50:7000"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        data = body if raw else json.dumps(body).encode()
        status, hdrs, out = self.ep.handle_http("POST", h, data)
        return status, hdrs, (json.loads(out) if out else None)

    def modern(self, method, params=None, req_id=1, headers=None, version=MODERN, **kw):
        params = dict(params or {})
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": version,
                           "io.modelcontextprotocol/clientInfo": {"name": "test-agent", "version": "1"}}
        h = {"MCP-Protocol-Version": version, "Mcp-Method": method}
        if method == "tools/call":
            h["Mcp-Name"] = params.get("name", "")
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        return self.post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}, h, **kw)

    def legacy(self, method, params=None, req_id=1, version="2025-06-18"):
        h = {"MCP-Protocol-Version": version} if version else {}
        return self.post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}, h)


def make_endpoint(**kw):
    calls = []
    ep = McpEndpoint("test-server", "9.9", [echo_tool()], instructions="Be nice.",
                     token_provider=lambda: TOKEN, on_call=lambda *a: calls.append(a), **kw)
    return Http(ep), calls


class TestModernProtocol(unittest.TestCase):
    def setUp(self):
        self.http, self.calls = make_endpoint()

    def test_discover(self):
        status, _, body = self.http.modern("server/discover")
        self.assertEqual(status, 200)
        r = body["result"]
        self.assertEqual(r["supportedVersions"], [MODERN])
        self.assertEqual(r["resultType"], "complete")
        self.assertIn("tools", r["capabilities"])
        self.assertEqual(r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "test-server")
        self.assertEqual(r["instructions"], "Be nice.")

    def test_tools_list_is_cacheable(self):
        _, _, body = self.http.modern("tools/list")
        r = body["result"]
        self.assertEqual([t["name"] for t in r["tools"]], ["echo"])
        self.assertIn("ttlMs", r)
        self.assertEqual(r["cacheScope"], "private")

    def test_tools_call(self):
        _, _, body = self.http.modern("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        r = body["result"]
        self.assertFalse(r["isError"])
        self.assertEqual(r["structuredContent"], {"echo": "hi", "version": MODERN, "client": "test-agent"})
        self.assertEqual(json.loads(r["content"][0]["text"])["echo"], "hi")
        self.assertEqual(r["resultType"], "complete")
        self.assertEqual(self.calls[-1][0], "echo")

    def test_unsupported_version(self):
        status, _, body = self.http.modern("tools/list", version="2099-01-01")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], -32022)
        self.assertEqual(body["error"]["data"]["supported"], [MODERN])

    def test_header_mismatches(self):
        cases = [
            {"MCP-Protocol-Version": None},
            {"MCP-Protocol-Version": "2025-06-18"},
            {"Mcp-Method": None},
            {"Mcp-Method": "tools/list"},
            {"Mcp-Name": None},
            {"Mcp-Name": "other"},
        ]
        for h in cases:
            with self.subTest(h=h):
                status, _, body = self.http.modern("tools/call", {"name": "echo", "arguments": {"text": "x"}},
                                                   headers=h)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], -32020)

    def test_base64_mcp_name(self):
        enc = "=?base64?" + base64.b64encode(b"echo").decode() + "?="
        status, _, body = self.http.modern("tools/call", {"name": "echo", "arguments": {"text": "x"}},
                                           headers={"Mcp-Name": enc})
        self.assertEqual(status, 200, body)

    def test_unknown_method_is_404(self):
        status, _, body = self.http.modern("prompts/list")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], -32601)

    def test_unknown_tool(self):
        _, _, body = self.http.modern("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(body["error"]["code"], -32602)


class TestToolErrors(unittest.TestCase):
    def setUp(self):
        self.http, self.calls = make_endpoint()

    def call(self, args):
        return self.http.modern("tools/call", {"name": "echo", "arguments": args})[2]["result"]

    def test_validation_errors_are_tool_errors(self):
        for args, needle in [({}, "'text' is required"),
                             ({"text": 5}, "text must be a string"),
                             ({"text": "x" * 11}, "at most 10"),
                             ({"text": "x", "n": 9}, "≤ 3"),
                             ({"text": "x", "n": 1.5}, "integer"),
                             ({"text": "x", "zzz": 1}, "unknown argument 'zzz'")]:
            with self.subTest(args=args):
                r = self.call(args)
                self.assertTrue(r["isError"])
                self.assertIn(needle, r["content"][0]["text"])

    def test_tool_error_and_crash(self):
        r = self.call({"text": "x", "fail": True})
        self.assertTrue(r["isError"])
        self.assertEqual(r["content"][0]["text"], "asked to fail")
        r = self.call({"text": "x", "crash": True})
        self.assertTrue(r["isError"])
        self.assertNotIn("boom", r["content"][0]["text"])       # no internals leaked
        self.assertFalse(self.calls[-1][3])


class TestLegacyProtocol(unittest.TestCase):
    def setUp(self):
        self.http, _ = make_endpoint()

    def test_initialize_negotiates(self):
        for asked, expect in [("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"),
                              ("2024-11-05", "2025-11-25"), (None, "2025-11-25")]:
            with self.subTest(asked=asked):
                _, _, body = self.http.legacy("initialize", {"protocolVersion": asked, "capabilities": {},
                                                             "clientInfo": {"name": "c", "version": "1"}},
                                              version=None)
                r = body["result"]
                self.assertEqual(r["protocolVersion"], expect)
                self.assertEqual(r["serverInfo"]["name"], "test-server")
                self.assertEqual(r["instructions"], "Be nice.")

    def test_notification_is_202(self):
        status, _, body = self.http.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, None))

    def test_list_call_ping(self):
        self.assertEqual(self.http.legacy("ping")[2]["result"], {})
        tools = self.http.legacy("tools/list")[2]["result"]["tools"]
        self.assertNotIn("ttlMs", self.http.legacy("tools/list")[2]["result"])
        self.assertEqual(tools[0]["annotations"], {"readOnlyHint": True})
        r = self.http.legacy("tools/call", {"name": "echo", "arguments": {"text": "yo"}})[2]["result"]
        self.assertEqual(r["structuredContent"]["echo"], "yo")
        self.assertNotIn("resultType", r)

    def test_no_version_header_means_2025_03_26(self):
        r = self.http.legacy("tools/call", {"name": "echo", "arguments": {"text": "a"}}, version=None)[2]
        self.assertEqual(r["result"]["structuredContent"]["version"], "2025-03-26")

    def test_bad_version_header(self):
        status, _, _ = self.http.legacy("tools/list", version="1999-01-01")
        self.assertEqual(status, 400)

    def test_batch(self):
        status, _, body = self.http.post([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in body], [1, 2])

    def test_unknown_legacy_method(self):
        status, _, body = self.http.legacy("resources/list")
        self.assertEqual((status, body["error"]["code"]), (200, -32601))


class TestTransportSecurity(unittest.TestCase):
    def setUp(self):
        self.http, _ = make_endpoint()

    def test_get_and_delete_are_405(self):
        for m in ("GET", "DELETE"):
            status, hdrs, _ = self.http.ep.handle_http(m, {}, b"")
            self.assertEqual(status, 405)
            self.assertEqual(hdrs["Allow"], "POST")

    def test_token_required(self):
        for tok in (None, "wrong"):
            status, hdrs, body = self.http.modern("tools/list", token=tok)
            self.assertEqual(status, 401)
            self.assertIn("Bearer", hdrs["WWW-Authenticate"])
            self.assertNotIn("id", body)

    def test_no_token_configured_means_open(self):
        ep = McpEndpoint("t", "1", [echo_tool()], token_provider=lambda: None)
        status, _, _ = Http(ep).modern("tools/list", token=None)
        self.assertEqual(status, 200)

    def test_origin_rules(self):
        good = ["http://192.168.1.50:7000"]
        bad = ["https://evil.example.com", "http://192.168.1.51:7000", "null"]
        for origin in good:
            self.assertEqual(self.http.modern("tools/list", headers={"Origin": origin})[0], 200, origin)
        for origin in bad:
            self.assertEqual(self.http.modern("tools/list", headers={"Origin": origin})[0], 403, origin)

    def test_garbage(self):
        self.assertEqual(self.http.post(b"{not json", raw=True)[2]["error"]["code"], -32700)
        self.assertEqual(self.http.post({"id": 1, "method": "ping"})[0], 400)     # missing jsonrpc
        self.assertEqual(self.http.post(b"x" * 1_000_001, raw=True)[0], 413)


# --------------------------------------------------------- drink tools ---
class FakeHost:
    """Runs everything inline (no loop thread) on a real DrinkMonitor."""
    OFF, CPG, CUP = 0, 100.0, 300.0

    def __init__(self):
        self.monitor = DrinkMonitor(Settings(tare_offset=self.OFF, counts_per_gram=self.CPG,
                                             cup_weight_g=self.CUP))
        self.intake = IntakeLog()
        self.events = deque()
        self.history = deque()
        self.nudges = []
        self.saved = 0
        self.t = time.time() - 600     # simulated clock, always behind the real one

    def run_on_loop(self, fn, timeout=3.0):
        return fn()

    def save_settings(self):
        self.saved += 1

    def nudge(self, beeps, message, push):
        self.nudges.append((beeps, message, push))
        return False

    def log_event(self, type_, **detail):
        self.events.append({"t": time.time(), "type": type_, **detail})

    def sensor_ok(self):
        return True

    def hold(self, grams, seconds):
        for _ in range(int(seconds * 10)):
            self.t += 0.1
            t = self.t
            for ev in self.monitor.update(self.OFF + grams * self.CPG, t):
                if ev["type"] == "drank":
                    self.intake.add(ev["grams"], t)
            self.history.append((t, None, self.monitor.state.value))


class TestDrinkTools(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        ep = McpEndpoint("drink", "1", build_tools(self.host), token_provider=lambda: None)
        self.http = Http(ep)

    def call(self, name, args=None):
        r = self.http.modern("tools/call", {"name": name, "arguments": args or {}}, token=None)[2]["result"]
        return r

    def test_status_through_a_drink(self):
        self.host.hold(300 + 400, 3)
        s = self.call("get_drink_status")["structuredContent"]
        self.assertEqual((s["state"], s["percent_left"], s["needs_refill"]), ("ok", 100, False))
        self.assertTrue(s["setup_complete"])
        self.host.hold(300 + 50, 3)
        s = self.call("get_drink_status")["structuredContent"]
        self.assertEqual(s["state"], "low")
        self.assertIsNotNone(s["drinking_rate_g_per_min"])
        self.assertIsNotNone(s["estimated_minutes_until_empty"])
        self.host.hold(300, 3)
        s = self.call("get_drink_status")["structuredContent"]
        self.assertTrue(s["needs_refill"])
        self.assertIn("needs a refill", s["summary"])

    def test_intake_summary(self):
        self.host.hold(700, 3)
        self.host.hold(600, 3)
        s = self.call("get_intake_summary", {"days": 2, "utc_offset_minutes": 0})["structuredContent"]
        self.assertEqual(len(s["daily"]), 2)
        self.assertAlmostEqual(s["today_g"], 100, delta=1)

    def test_snooze_and_settings(self):
        self.host.hold(700, 3)
        self.assertTrue(self.call("snooze_refill_reminders")["structuredContent"]["snoozed"])
        r = self.call("update_alert_settings", {"low_percent": 40, "reminder_minutes": 0})
        self.assertFalse(r["isError"])
        self.assertEqual(self.host.monitor.settings.low_percent, 40)
        self.assertEqual(self.host.saved, 1)
        self.assertTrue(self.call("update_alert_settings", {})["isError"])
        self.assertTrue(self.call("update_alert_settings", {"low_percent": 95})["isError"])

    def test_nudge_cooldown(self):
        r = self.call("nudge_drinker", {"beeps": 2, "message": "Drink up"})
        self.assertFalse(r["isError"])
        self.assertEqual(self.host.nudges, [(2, "Drink up", False)])
        r = self.call("nudge_drinker", {})
        self.assertTrue(r["isError"])
        self.assertIn("Wait", r["content"][0]["text"])

    def test_history_events(self):
        self.host.hold(700, 3)
        r = self.call("get_drink_history", {"minutes": 5})["structuredContent"]
        self.assertLessEqual(len(r["level"]), 60)
        self.assertIn("events", r)


if __name__ == "__main__":
    unittest.main()
