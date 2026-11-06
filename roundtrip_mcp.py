#!/usr/bin/env python3
"""roundtrip-mcp - a zero-dependency MCP server (stdio) implementing the
SEP-2322 multi round-trip request pattern on the 2026-07-28 stateless core.

A demo tool, plan_team_lunch, collects structured input from the user
through form-mode elicitation carried by InputRequiredResult envelopes:

  client  -> tools/call(id=1)
  server  -> result {resultType: "input_required",
                     inputRequests: {<key>: elicitation/create...},
                     requestState: <opaque signed blob>}   (request ends)
  client  -> tools/call(id=2, params + inputResponses + requestState)
  server  -> final CallToolResult

The server is fully stateless: everything it needs across rounds travels
in requestState, an HMAC-signed base64url JSON blob (per-process random
key), exactly the ephemeral-tool workflow SEP-2322 specifies. Tampered
state fails with -32602.

Client capability rules honored (2026-07-28 elicitation spec): the server
checks _meta.io.modelcontextprotocol/clientCapabilities.elicitation.form
on each tools/call and falls back to plain text questions when the
client does not declare form-mode support - it never sends an
undeclared mode. Form schemas stay within the restricted subset (flat
object, primitives; enum titles via SEP-1330; defaults via SEP-1034) and
never request secrets (spec security rule).

Errors: bad arguments are tool execution errors (SEP-1303); unknown
methods -32601; malformed JSON -32700.

Run: python3 roundtrip_mcp.py
"""
import base64
import hashlib
import hmac
import json
import secrets
import sys

PROTOCOL_VERSION = "2026-07-28"
LEGACY_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
SERVER_INFO = {"name": "roundtrip-mcp", "version": "1.0.0"}

ERR_INVALID_PARAMS = -32602
ERR_METHOD_NOT_FOUND = -32601
ERR_PARSE = -32700

_STATE_KEY = secrets.token_bytes(32)  # per-process; old states die on restart

CUISINES = [("pizza", "Pizza"), ("sushi", "Sushi"), ("tacos", "Tacos"),
            ("indian", "Indian"), ("salad", "Salad bar")]
DIETARY = [("none", "No restrictions"), ("vegetarian", "Vegetarian"),
           ("vegan", "Vegan"), ("gluten-free", "Gluten-free"),
           ("halal", "Halal"), ("kosher", "Kosher")]

CAP_KEY = "io.modelcontextprotocol/clientCapabilities"


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def mint_state(payload):
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_STATE_KEY, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_state(blob):
    try:
        body, sig = blob.split(".", 1)
    except ValueError:
        raise ValueError("malformed requestState")
    expect = hmac.new(_STATE_KEY, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        raise ValueError("requestState signature mismatch")
    payload = json.loads(_b64d(body))
    if not isinstance(payload, dict):
        raise ValueError("requestState payload is not an object")
    return payload


def _one_of(pairs):
    return [{"const": value, "title": title} for value, title in pairs]


def cuisine_schema():
    # SEP-1330 enum-with-titles, SEP-1034 defaults; flat primitives only.
    return {
        "type": "object",
        "properties": {
            "cuisine": {
                "type": "string",
                "description": "What should the team eat?",
                "oneOf": _one_of(CUISINES),
                "default": "pizza",
            },
            "headcount": {
                "type": "integer",
                "description": "How many people are eating?",
                "minimum": 1, "maximum": 50, "default": 4,
            },
            "dietary": {
                "type": "array",
                "description": "Dietary restrictions to respect.",
                "items": {"anyOf": _one_of(DIETARY)},
                "default": ["none"],
            },
        },
        "required": ["cuisine", "headcount"],
    }


def confirm_schema(plan_text):
    return {
        "type": "object",
        "properties": {
            "confirm": {
                "type": "boolean",
                "description": f"Lock in this plan? {plan_text}",
                "default": True,
            },
            "notes": {
                "type": "string",
                "description": "Anything else the organizer should know "
                               "(optional).",
                "maxLength": 500,
            },
        },
        "required": ["confirm"],
    }


def _input_required(key, message, schema, state):
    return {
        "resultType": "input_required",
        "inputRequests": {key: {
            "method": "elicitation/create",
            "params": {"mode": "form", "message": message,
                       "requestedSchema": schema}}},
        "requestState": state,
    }


def _tool_complete(text, structured):
    return {"resultType": "complete",
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured}


def _tool_error(text):
    # SEP-1303: argument/domain failures are tool execution errors.
    return {"resultType": "complete",
            "content": [{"type": "text", "text": text}], "isError": True}


def _client_supports_form(meta):
    caps = (meta or {}).get(CAP_KEY, {})
    elic = caps.get("elicitation")
    if elic is None:
        return False
    if elic == {}:  # empty object == form support (backwards compat)
        return True
    return isinstance(elic, dict) and "form" in elic


def plan_team_lunch(arguments, meta):
    arguments = arguments or {}
    input_responses = arguments.get("inputResponses") or {}
    blob = arguments.get("requestState")

    state = {}
    if blob is not None:
        if not isinstance(blob, str):
            return ("error", "requestState must be a string.")
        try:
            state = read_state(blob)
        except (ValueError, json.JSONDecodeError):
            return ("error", "requestState is invalid or was not minted by "
                             "this server process. Start over with a fresh "
                             "plan_team_lunch call (no requestState).")
    round_no = state.get("round", 0)

    if not input_responses:
        # Round 1: ask for cuisine, headcount, dietary restrictions.
        if not _client_supports_form(meta):
            return ("complete", _tool_complete(
                "This client does not declare form-mode elicitation "
                "support, so plan_team_lunch cannot ask its questions "
                "interactively. Reconnect with an elicitation-capable "
                "client to use the interactive flow.",
                {"plan": None, "elicitation": "unsupported"}))
        return ("complete", _input_required(
            "meal", "Let's plan the team lunch - what should we eat, "
                    "for how many, and with which dietary restrictions?",
            cuisine_schema(), mint_state({"round": 1})))

    resp = input_responses.get("meal") or input_responses.get("confirm")
    if resp is None:
        return ("error", "inputResponses must answer the key from "
                         "inputRequests (same map key).")
    action = resp.get("action")
    if action in ("decline", "cancel"):
        return ("complete", _tool_error(
            f"Lunch planning stopped: user chose '{action}'. Call "
            "plan_team_lunch again to restart."))
    if action != "accept":
        return ("error", f"Unknown elicitation action '{action}'.")

    if round_no == 1 or "meal" in input_responses:
        content = resp.get("content") or {}
        cuisine = content.get("cuisine")
        headcount = content.get("headcount")
        valid = {c for c, _ in CUISINES}
        if cuisine not in valid:
            return ("error", f"cuisine must be one of "
                             f"{sorted(valid)}.")
        if not isinstance(headcount, int) or isinstance(headcount, bool) \
                or not 1 <= headcount <= 50:
            return ("error", "headcount must be an integer 1-50.")
        dietary = content.get("dietary", ["none"])
        if not isinstance(dietary, list) or not all(
                d in {v for v, _ in DIETARY} for d in dietary):
            return ("error", "dietary must be a list of known restriction "
                             "ids.")
        title = dict(CUISINES)[cuisine]
        plan = (f"{title} for {headcount} "
                f"({', '.join(dietary) if dietary else 'no restrictions'})")
        return ("complete", _input_required(
            "confirm", f"Confirm the plan: {plan}.",
            confirm_schema(plan),
            mint_state({"round": 2, "cuisine": cuisine,
                        "headcount": headcount, "dietary": dietary})))

    # Round 2 answered: finalize.
    if not state.get("cuisine"):
        return ("error", "requestState is missing the round-1 answers; "
                         "start over with a fresh call.")
    content = resp.get("content") or {}
    confirm = content.get("confirm")
    if confirm is not True:
        return ("complete", _tool_error(
            "Plan not confirmed. Call plan_team_lunch again to restart."))
    notes = content.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 500:
        return ("error", "notes must be a string of at most 500 characters.")
    title = dict(CUISINES)[state["cuisine"]]
    plan = {
        "cuisine": state["cuisine"], "cuisine_title": title,
        "headcount": state["headcount"], "dietary": state["dietary"],
        "notes": notes,
        "summary": (f"{title} for {state['headcount']} person(s), "
                    f"restrictions: "
                    f"{', '.join(state['dietary']) or 'none'}."),
    }
    return ("complete", _tool_complete(
        "Team lunch locked in: " + plan["summary"]
        + (f" Notes: {notes}" if notes else ""), {"plan": plan}))


TOOLS = [{
    "name": "plan_team_lunch",
    "description": "Plan a team lunch interactively. Answers come back "
                   "through form-mode elicitation (SEP-2322 multi "
                   "round-trip): first call returns an InputRequiredResult "
                   "with cuisine/headcount/dietary questions, the retry "
                   "with inputResponses confirms the plan, and the final "
                   "retry returns the locked-in plan. Stateless: state "
                   "travels in the signed requestState blob, so the retry "
                   "can land on any server process sharing the key.",
    "inputSchema": {"type": "object", "properties": {
        "inputResponses": {"type": "object"},
        "requestState": {"type": "string"}},
        "additionalProperties": True},
}]


def capabilities():
    return {"tools": {"listChanged": False}}


def handle_message(msg):
    if not isinstance(msg, dict) or "id" not in msg:
        return None
    rid = msg["id"]
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "server/discover":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO, "capabilities": capabilities()}}
    if method == "initialize":
        requested = params.get("protocolVersion", "")
        version = requested if requested in LEGACY_VERSIONS \
            or requested == PROTOCOL_VERSION else PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version,
            "serverInfo": SERVER_INFO, "capabilities": capabilities()}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        if params.get("name") != "plan_team_lunch":
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": ERR_INVALID_PARAMS,
                "message": f"Unknown tool '{params.get('name')}'."}}
        kind, payload = plan_team_lunch(params.get("arguments"),
                                        msg.get("_meta"))
        if kind == "error":
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": ERR_INVALID_PARAMS, "message": payload}}
        return {"jsonrpc": "2.0", "id": rid, "result": payload}
    return {"jsonrpc": "2.0", "id": rid, "error": {
        "code": ERR_METHOD_NOT_FOUND, "message": f"Unknown method '{method}'."}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": ERR_PARSE, "message": "Parse error"}})
                + "\n")
            sys.stdout.flush()
            continue
        response = handle_message(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
