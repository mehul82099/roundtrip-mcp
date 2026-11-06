# roundtrip-mcp

A zero-dependency, single-file Python MCP server (stdio) implementing
the [SEP-2322](https://modelcontextprotocol.io/seps/2322-multi-round-trip-requests)
multi round-trip request pattern on the 2026-07-28 stateless core:
server-initiated **form-mode elicitation** carried by
`InputRequiredResult` envelopes, with a fully stateless server and a
signed `requestState` blob doing the work sessions used to do.

The demo tool is `plan_team_lunch`: call it and it asks (through the
client's elicitation UI) for cuisine, headcount and dietary
restrictions, then asks you to confirm the plan, then returns it. Two
round-trips, zero server-side state.

## The flow (per SEP-2322's ephemeral-tool workflow)

```
client -> tools/call(id=1)
server -> result { resultType: "input_required",
                   inputRequests: { "meal": elicitation/create... },
                   requestState: "<opaque signed blob>" }   # request ends
client -> tools/call(id=2, inputResponses={...}, requestState)
server -> result { resultType: "input_required",
                   inputRequests: { "confirm": elicitation/create... },
                   requestState: "..." }
client -> tools/call(id=3, inputResponses={...}, requestState)
server -> final CallToolResult with the plan
```

Each round is a brand-new JSON-RPC request (the spec requires the retry
to use a different id), so any server process holding the signing key
can finish any round: no SSE streams, no sticky load balancing, no
shared storage. That is the operational problem SEP-2322 exists to
solve.

## What it demonstrates, mapped to the specs

- **`InputRequiredResult` envelope (SEP-2322):** incomplete answers are
  returned as `resultType: "input_required"` with an `inputRequests`
  map (server-assigned keys) plus an opaque `requestState`; the client
  retries with matching `inputResponses` keys and the echoed state.
- **Stateless reconstitution:** `requestState` is base64url JSON signed
  with HMAC-SHA256 (per-process random key). The server verifies the
  signature before trusting anything inside; tampered or foreign state
  fails with `-32602`. The client treats the blob as opaque, as the
  spec requires.
- **Capability gating (2026-07-28 elicitation spec):** the server reads
  `_meta.io.modelcontextprotocol/clientCapabilities.elicitation` on
  every `tools/call` and never sends a mode the client did not declare.
  No form capability -> a plain-text fallback result, no elicitation.
  An empty `elicitation: {}` means form support, per the spec's
  backwards-compatibility rule.
- **Restricted form schemas:** flat objects, primitive fields only;
  enum options with human titles (`oneOf` const/title,
  [SEP-1330](https://modelcontextprotocol.io/seps/1330-elicitation-enum-schema-improvements-and-standards-compliance));
  defaults on every field
  ([SEP-1034](https://modelcontextprotocol.io/seps/1034-support-default-values-for-all-primitive-types-in-elicitation-schemas))
  so clients can pre-populate forms.
- **Security rules honored:** form mode never requests secrets
  (passwords, tokens, payment data) - that is what URL mode is for.
  Enforced by a test that scans every emitted elicitation.
- **Scope limits (SEP-2322 + SEP-2260):** `InputRequiredResult` is only
  ever sent for `tools/call` - never on list methods or `initialize`.
- **Error conventions:** bad arguments are tool-level `isError` results
  ([SEP-1303](https://modelcontextprotocol.io/seps/1303-input-validation-errors-as-tool-execution-errors)),
  protocol problems are JSON-RPC errors, tool names follow
  [SEP-986](https://modelcontextprotocol.io/seps/986-specify-format-for-tool-names).
- **Legacy + modern handshake (SEP-2575):** answers stateless
  `server/discover` and pre-2026-07-28 `initialize`.

## Run it

```sh
python3 roundtrip_mcp.py
```

Client configuration (stdio):

```json
{
  "mcpServers": {
    "roundtrip": {
      "command": "python3",
      "args": ["/path/to/roundtrip_mcp.py"]
    }
  }
}
```

The client must declare form-mode elicitation support
(`_meta.io.modelcontextprotocol/clientCapabilities.elicitation.form`)
on requests for the interactive flow; otherwise the server answers with
a plain-text explanation instead of eliciting.

## Tests

21 stdlib `unittest` tests drive the real server over stdio as an MCP
client: both handshakes, the `InputRequiredResult` envelope shape,
schema-subset compliance (primitives, enum titles, defaults), the full
two-round happy path, state carry-across rounds, HMAC tamper and
cross-process forgery rejection, decline/cancel/unknown-action paths,
wrong response keys, invalid round-1 answers, unconfirmed plans,
capability gating (missing/empty/url-only), list-method scope, the
no-secrets rule, and SEP-986 names. CI runs them on Ubuntu 24.04 and
Windows, Python 3.9 and 3.13.

```sh
python3 -m unittest -v
```

Honest limits: the demo covers **form mode only** (URL mode exists for
out-of-band flows like OAuth and is documented in the same spec);
requestState is signed but not encrypted (fine for a lunch plan - do
not put secrets in it); the sampling and roots round-trip request types
from SEP-2322 are not exercised, only elicitation; no interop test
against a hosted MCP client.

## Sources

- [SEP-2322: Multi Round-Trip Requests](https://modelcontextprotocol.io/seps/2322-multi-round-trip-requests) (InputRequiredResult/inputResponses/requestState shapes and the ephemeral workflow)
- [MCP specification 2026-07-28 - Elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation) (modes, capability declaration, restricted schema, security rules)
- [SEP-2260: Server requests associated with a client request](https://modelcontextprotocol.io/seps/2260-require-server-requests-to-be-associated-with-a-client-request)
- [SEP-2575: Stateless MCP](https://modelcontextprotocol.io/seps/2575-stateless-mcp), [SEP-1330](https://modelcontextprotocol.io/seps/1330-elicitation-enum-schema-improvements-and-standards-compliance), [SEP-1034](https://modelcontextprotocol.io/seps/1034-support-default-values-for-all-primitive-types-in-elicitation-schemas), [SEP-1303](https://modelcontextprotocol.io/seps/1303-input-validation-errors-as-tool-execution-errors), [SEP-986](https://modelcontextprotocol.io/seps/986-specify-format-for-tool-names)
- All fetched 2026-11-06.

## License

MIT
