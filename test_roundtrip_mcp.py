import base64
import json
import os
import re
import subprocess
import sys
import unittest

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "roundtrip_mcp.py")
CAPS = {"io.modelcontextprotocol/clientCapabilities": {
    "elicitation": {"form": {}}}}


class Server:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0

    def request(self, method, params=None, meta=None):
        self.next_id += 1
        rid = self.next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method,
               "params": params or {}}
        if meta is not None:
            msg["_meta"] = meta
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline().decode())
        assert reply.get("id") == rid, f"bad id: {reply}"
        return reply

    def result(self, method, params=None, meta=None):
        msg = self.request(method, params, meta)
        assert "result" in msg, f"error from {method}: {msg}"
        return msg["result"]

    def call(self, arguments=None, meta=CAPS):
        return self.result("tools/call",
                           {"name": "plan_team_lunch",
                            "arguments": arguments or {}}, meta)

    def close(self):
        self.proc.kill()
        self.proc.wait(timeout=5)


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class HandshakeTests(unittest.TestCase):
    def test_discover(self):
        s = Server()
        try:
            d = s.result("server/discover")
            self.assertEqual(d["protocolVersion"], "2026-07-28")
            self.assertEqual(d["serverInfo"]["name"], "roundtrip-mcp")
        finally:
            s.close()

    def test_legacy_initialize(self):
        s = Server()
        try:
            r = s.result("initialize", {"protocolVersion": "2025-06-18"})
            self.assertEqual(r["protocolVersion"], "2025-06-18")
            self.assertEqual(len(s.result("tools/list")["tools"]), 1)
        finally:
            s.close()

    def test_unknown_method_and_tool(self):
        s = Server()
        try:
            self.assertEqual(s.request("nope")["error"]["code"], -32601)
            self.assertEqual(s.request("tools/call", {"name": "nope"})
                             ["error"]["code"], -32602)
        finally:
            s.close()

    def test_parse_error(self):
        s = Server()
        try:
            s.proc.stdin.write(b"{oops\n")
            s.proc.stdin.flush()
            msg = json.loads(s.proc.stdout.readline().decode())
            self.assertEqual(msg["error"]["code"], -32700)
        finally:
            s.close()


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.s = Server()

    def tearDown(self):
        self.s.close()

    def test_first_call_returns_input_required(self):
        r = self.s.call()
        self.assertEqual(r["resultType"], "input_required")
        self.assertIn("meal", r["inputRequests"])
        req = r["inputRequests"]["meal"]
        self.assertEqual(req["method"], "elicitation/create")
        self.assertEqual(req["params"]["mode"], "form")
        self.assertTrue(r["requestState"])

    def test_form_schema_restricted_subset(self):
        r = self.s.call()
        schema = r["inputRequests"]["meal"]["params"]["requestedSchema"]
        self.assertEqual(schema["type"], "object")
        types = set()
        for prop in schema["properties"].values():
            self.assertIn(prop["type"], ("string", "integer", "boolean",
                                         "array"))
            types.add(prop["type"])
        # enum titles (SEP-1330) and defaults (SEP-1034) present
        cuisine = schema["properties"]["cuisine"]
        self.assertEqual({c["const"] for c in cuisine["oneOf"]},
                         {"pizza", "sushi", "tacos", "indian", "salad"})
        self.assertTrue(all("title" in c for c in cuisine["oneOf"]))
        self.assertEqual(cuisine["default"], "pizza")
        dietary = schema["properties"]["dietary"]
        self.assertIn("anyOf", dietary["items"])
        self.assertEqual(dietary["default"], ["none"])
        self.assertEqual(schema["properties"]["headcount"]["default"], 4)

    def test_full_two_round_flow(self):
        r1 = self.s.call()
        r2 = self.s.call({
            "inputResponses": {"meal": {"action": "accept", "content": {
                "cuisine": "sushi", "headcount": 6,
                "dietary": ["vegetarian", "gluten-free"]}}},
            "requestState": r1["requestState"]})
        self.assertEqual(r2["resultType"], "input_required")
        self.assertIn("confirm", r2["inputRequests"])
        desc = r2["inputRequests"]["confirm"]["params"][
            "requestedSchema"]["properties"]["confirm"]["description"]
        self.assertIn("Sushi for 6", desc)
        r3 = self.s.call({
            "inputResponses": {"confirm": {"action": "accept", "content": {
                "confirm": True, "notes": "quiet table"}}},
            "requestState": r2["requestState"]})
        self.assertEqual(r3["resultType"], "complete")
        plan = r3["structuredContent"]["plan"]
        self.assertEqual(plan["cuisine"], "sushi")
        self.assertEqual(plan["headcount"], 6)
        self.assertEqual(plan["dietary"], ["vegetarian", "gluten-free"])
        self.assertEqual(plan["notes"], "quiet table")
        self.assertIn("Sushi for 6", r3["content"][0]["text"])

    def test_state_carries_round1_answers(self):
        # The server is stateless: round-2 requestState must carry round 1.
        r1 = self.s.call()
        blob = r1["requestState"]
        body = blob.split(".", 1)[0]
        payload = json.loads(b64d(body))
        self.assertEqual(payload, {"round": 1})
        r2 = self.s.call({
            "inputResponses": {"meal": {"action": "accept", "content": {
                "cuisine": "tacos", "headcount": 2, "dietary": []}}},
            "requestState": blob})
        body2 = r2["requestState"].split(".", 1)[0]
        payload2 = json.loads(b64d(body2))
        self.assertEqual(payload2["cuisine"], "tacos")
        self.assertEqual(payload2["round"], 2)

    def test_tampered_state_rejected(self):
        r1 = self.s.call()
        blob = r1["requestState"]
        body, _sig = blob.split(".", 1)
        payload = json.loads(b64d(body))
        payload["round"] = 2
        forged = base64.urlsafe_b64encode(json.dumps(payload).encode()
                                          ).decode().rstrip("=") + "." + "0" * 64
        msg = self.s.request("tools/call", {
            "name": "plan_team_lunch",
            "arguments": {"inputResponses": {"confirm": {
                "action": "accept", "content": {"confirm": True}}},
                "requestState": forged}}, CAPS)
        self.assertEqual(msg["error"]["code"], -32602)
        self.assertIn("requestState", msg["error"]["message"])

    def test_foreign_state_rejected(self):
        r1 = self.s.call()
        r2 = self.s.call({
            "inputResponses": {"meal": {"action": "accept", "content": {
                "cuisine": "indian", "headcount": 3, "dietary": []}}},
            "requestState": r1["requestState"]})
        other = Server()
        try:
            msg = other.request("tools/call", {
                "name": "plan_team_lunch",
                "arguments": {"inputResponses": {"confirm": {
                    "action": "accept", "content": {"confirm": True}}},
                    "requestState": r2["requestState"]}}, CAPS)
            # Signed with the first process's key: rejected here.
            self.assertEqual(msg["error"]["code"], -32602)
        finally:
            other.close()

    def test_decline_and_cancel(self):
        for action in ("decline", "cancel"):
            r1 = self.s.call()
            r2 = self.s.call({
                "inputResponses": {"meal": {"action": action}},
                "requestState": r1["requestState"]})
            self.assertTrue(r2["isError"])
            self.assertIn(action, r2["content"][0]["text"])

    def test_unknown_action(self):
        r1 = self.s.call()
        msg = self.s.request("tools/call", {
            "name": "plan_team_lunch",
            "arguments": {"inputResponses": {"meal": {"action": "yolo"}},
                          "requestState": r1["requestState"]}}, CAPS)
        self.assertEqual(msg["error"]["code"], -32602)

    def test_wrong_response_key(self):
        r1 = self.s.call()
        msg = self.s.request("tools/call", {
            "name": "plan_team_lunch",
            "arguments": {"inputResponses": {"wrong": {"action": "accept"}},
                          "requestState": r1["requestState"]}}, CAPS)
        self.assertEqual(msg["error"]["code"], -32602)

    def test_bad_round1_answers(self):
        r1 = self.s.call()
        for content in ({"cuisine": "burgers", "headcount": 3},
                        {"cuisine": "sushi", "headcount": 0},
                        {"cuisine": "sushi", "headcount": 51},
                        {"cuisine": "sushi", "headcount": "4"},
                        {"cuisine": "sushi", "headcount": 3,
                         "dietary": ["carnivore"]}):
            msg = self.s.request("tools/call", {
                "name": "plan_team_lunch",
                "arguments": {"inputResponses": {"meal": {
                    "action": "accept", "content": content}},
                    "requestState": r1["requestState"]}}, CAPS)
            self.assertIn("error", msg, f"accepted {content}")

    def test_not_confirmed(self):
        r1 = self.s.call()
        r2 = self.s.call({
            "inputResponses": {"meal": {"action": "accept", "content": {
                "cuisine": "salad", "headcount": 1, "dietary": ["vegan"]}}},
            "requestState": r1["requestState"]})
        r3 = self.s.call({
            "inputResponses": {"confirm": {"action": "accept", "content": {
                "confirm": False}}},
            "requestState": r2["requestState"]})
        self.assertTrue(r3["isError"])
        self.assertIn("not confirmed", r3["content"][0]["text"])


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.s = Server()

    def tearDown(self):
        self.s.close()

    def test_no_capability_no_elicitation(self):
        # Without a declared elicitation capability the server MUST NOT
        # send elicitation requests; it falls back to a plain result.
        for meta in (None, {}, {"io.modelcontextprotocol/"
                                "clientCapabilities": {}}):
            r = self.s.call(meta=meta)
            self.assertEqual(r["resultType"], "complete")
            self.assertNotIn("inputRequests", r)
            self.assertEqual(r["structuredContent"]["elicitation"],
                             "unsupported")

    def test_empty_elicitation_object_means_form(self):
        r = self.s.call(meta={"io.modelcontextprotocol/clientCapabilities":
                              {"elicitation": {}}})
        self.assertEqual(r["resultType"], "input_required")

    def test_url_only_capability_no_form(self):
        r = self.s.call(meta={"io.modelcontextprotocol/clientCapabilities":
                              {"elicitation": {"url": {}}}})
        self.assertEqual(r["resultType"], "complete")
        self.assertNotIn("inputRequests", r)

    def test_lists_never_input_required(self):
        # SEP-2322: InputRequiredResult only on tools/call, resources/read,
        # prompts/get, tasks/get payload - never on lists.
        r = self.s.result("tools/list")
        self.assertNotEqual(r.get("resultType"), "input_required")
        self.assertNotIn("inputRequests", r)

    def test_no_sensitive_fields(self):
        # Spec security rule: form mode must never request secrets.
        r = self.s.call()
        blob = json.dumps(r["inputRequests"]).lower()
        for word in ("password", "secret", "token", "api_key", "apikey",
                     "credit", "cvv", "ssn"):
            self.assertNotIn(word, blob)

    def test_tool_name_sep986(self):
        tools = self.s.result("tools/list")["tools"]
        for t in tools:
            self.assertRegex(t["name"], re.compile(r"^[A-Za-z0-9_./-]{1,64}$"))


if __name__ == "__main__":
    unittest.main()
