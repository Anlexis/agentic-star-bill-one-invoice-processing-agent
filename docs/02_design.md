# Template Design Specification — CMN-C2-275 Bill One Invoice Agent

## Position in AgentCore Architecture

- **Agent Class**: `BillOneInvoiceAgent` (`src/graph/graph.py`)
- **L1 Base** (framework base class): `AgentBaseGraph` — direct framework inheritance
- **Category**: Cat 2 (multi-step domain workflow, ToolCallingAgent). Outer
  `AgentBaseGraph` 5-node backbone; the domain pipeline is encapsulated in a
  `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
  (`src/graph/domain_workflow_graph.py`).
- **Agent type**: ToolCallingAgent — classify intent -> extract invoice
  fields -> build a Bill One (Sansan invoice SaaS) REST API request -> call the
  tool -> format the confirmation. No RAG retrieval, no autonomous ReAct loop.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: framework inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | refuse injection attempts; bound every caller field; sanitize and serialize the request into `validated_input` (JSON) | user_input, input_context | validated_input, invoice_hint, issuer_ref, listing_scope, max_records | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner Bill One workflow subgraph | validated_input | result, intent, invoice_id, record_id, record_ref, issuer_name, invoice_status, invoice_summary, confirmation, billone_payload | GraphNode (caller ctx forwarded unchanged) | BillOneWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; module-level `_security_gate_output()` scan, clearing output on violation | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

Inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline
steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------------|
| validate_input | 1 ValidateInput | empty/non-request guard; injection refusal at the inner entry; deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, invoice_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> lookup_invoice / register_invoice / summarize_invoices; low-confidence -> lookup_invoice (read-only default — never a write) | intent | ANONYMOUS |
| infer_billone_fields | 3 InferBillOneFields | extract invoice number / issuer name / "Key: value" invoice fields; assemble the Bill One REST API request body per intent; an unresolved invoice number is left empty (never invented) | issuer_name, invoice_id, billone_payload | ANONYMOUS |
| call_billone_api | 4 CallBillOneApi | GET /invoices (lookup) / POST /invoices (register) / GET /invoices list (summarize) via `BillOneClient`; token via ctx.secrets; 4xx/5xx -> status=error | record_id, record_ref, invoice_id, issuer_name, invoice_status, invoice_summary | ANONYMOUS |
| confirm | 5 Confirm | format intent + record id + reference (+ status / summary count) into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / BillOneWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_billone_fields
              -> call_billone_api -> confirm -> END
```

Structured parameters travel as a JSON string: `pre_process` serializes
`{"text", "invoice_hint"}` into `validated_input`,
`BillOneWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and
the first inner node (`validate_input`) parses it back. Inner nodes read
`state.get("validated_input") or state.get("user_input", "")`.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (state contract —
fields are absent until their producer node writes them). Dict/list payloads
are stored as JSON strings (`Optional[str]`) via the module helpers
`to_json` / `from_json`, used by every producer and consumer.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| invoice_hint | NotRequired[str] | caller-supplied invoice number/ID hint, validated to a record-id shape; never inferred | pre_process / validate_input |
| issuer_ref | NotRequired[str] | caller-supplied issuer reference, locked to an inert identifier because it is rendered back into the response | pre_process / validate_input |
| listing_scope | NotRequired[str] | caller-supplied listing filter for the summarize intent; one of a closed set | pre_process / validate_input |
| max_records | NotRequired[int] | caller cap on the summarized listing; parsed finite and in range 1-500 | pre_process / validate_input |
| invoice_id | NotRequired[str] | resolved Bill One invoice number (v1: pass-through when the hint/text already carries a number) | infer_billone_fields |
| redaction_flags | NotRequired[Optional[str]] | JSON list of pattern classes redacted before logging | validate_input |
| issuer_name | NotRequired[str] | invoice issuer (vendor/supplier) display name / record label | infer_billone_fields / call_billone_api |
| billone_payload | NotRequired[Optional[str]] | JSON — assembled Bill One REST API request body (msgpack-safe: stored as a JSON string via `to_json`/`from_json`) | infer_billone_fields |
| billone_config | NotRequired[Optional[str]] | JSON — manifest `billone:` section forwarded by `_parent_config()` and injected via the inner graph's `_extra_initial_state()` | inner graph |
| record_id | NotRequired[str] | invoice number / record id returned by Bill One | call_billone_api |
| record_ref | NotRequired[str] | human-readable record reference (`billone://invoices/<number>`) | call_billone_api |
| invoice_status | NotRequired[str] | processing status returned for a looked-up invoice (received / approved / paid) | call_billone_api |
| invoice_summary | NotRequired[Optional[str]] | JSON — record count + per-status tally for the summarize intent (stored as a JSON string via `to_json`/`from_json`) | call_billone_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from
`AgentState` and are **not** re-declared.

**State Constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the Bill One token is accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration Forwarding (nested Cat-2)

Two files, two jobs. `config/agent.yaml` is the **static manifest** the registry
reads to find and register the agent; it holds no runtime values.
`config/config.yaml` holds the **runtime parameters** and is the mapping passed
to the graph constructor:

```yaml
max_retry: 3          # read by the framework's retry routing
timeout_s: 30         # per-call Bill One request budget
billone:
  base_url: "https://api.billone.jp/v1"
```

`load_runtime_config()` (`src/graph/graph.py`) reads that file, and
`BillOneInvoiceAgent.__init__` defaults to it — so a directly constructed agent
and a platform-constructed one behave identically, and `src/api/server.py`
constructs the agent the same way. A config file that only takes effect on one
entry path is a setting that silently does nothing on the other.

Nodes take **no constructor arguments** (SDK v1 nodes are no-arg; configuration
never rides on node instances), so `BillOneWorkflowGraphNode._parent_config()`
forwards the `billone:` section and `timeout_s` from the graph's own config to
the inner graph under `config["configurable"]` — never `{}`. The inner graph's
`_extra_initial_state()` injects those settings into State as a JSON string
(`billone_config`), where `CallBillOneApiNode.execute(state, config=None)` reads
them (an explicit `config["configurable"]["billone"]` override is also honoured
for direct/unit invocation). A boundary test drives a declared `base_url` and
`timeout_s` from the file through to the client to prove the chain is live.

### Caller-data contract (`input_context`)

`/invoke` accepts structured caller data alongside the request text. The entry
point caps the channel at 256 KB and passes it through; `PreProcessNode` is the
only place it is interpreted, and every consumed field is bounded there:

| Field | Rule | Effect |
|---|---|---|
| `invoice_id` / `invoice_hint` / `invoice_number` | `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$` | target record; first present name wins |
| `issuer_ref` | inert identifier `^[a-z0-9_]{1,32}$` | rendered into the confirmation |
| `scope` | one of `received`, `approved`, `paid`, `all` | listing filter for the summary |
| `max_records` | finite number, 1–500 | caps the summarized listing |

Three rules hold across the table. Every caller-controlled **number** goes
through a finite+bounded parser (`parse_finite`): booleans, non-numerics,
out-of-range magnitudes and — the one that is easy to miss — `NaN` / `±Infinity`
are all rejected. Non-finite values parse happily via `float()` and arrive
through raw JSON, and every comparison against `NaN` is False, so an unchecked
one does not raise: it silently makes a bound check pass. Every caller **string
that is rendered back into the response** is locked to an inert identifier
pattern, because free text in a rendered field is caller-controlled output
injection. And a rejected value is **never echoed** — the error names the field
only. Fields the pipeline does not consume are ignored rather than rejected (the
platform attaches its own metadata), and only the *count* of ignored fields
reaches the audit event, never their names.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the write-capable `CallBillOneApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary
  is therefore enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation).
- **Caller contract — refuse, then bound, then reduce** — `PreProcessNode` owns
  the caller contract and runs the three steps in that order:

  1. **Refusal.** `screen_text()` / `screen_payload()`
     (`src/services/security.py`) reject prompt-injection attempts: chat-template
     CONTROL TOKENS as a class (`<|…|>`, `[INST]`, `<<SYS>>`) plus anchored
     directive phrases, screened on the raw text, on the markup-stripped text,
     and depth-first over `input_context` **including its keys**. The order is
     load-bearing: the markup strip deletes `<|im_start|>` outright, so a
     sanitize-then-screen pipeline would forward the surviving directive as
     ordinary prose — a sanitizer is a reduction, never a refusal. Input is
     NFKC-normalized first so a full-width payload cannot evade the screen.
     Directive patterns are anchored on verb+object pairs so real invoice prose
     ("Transact as a settlement agent", "Insert Into Trust Holdings") is not
     refused. `ValidateInputNode` runs the same screen at the inner graph's own
     entry, so the guarantee does not depend on an upstream gate being active.
  2. **Bounds.** Every consumed caller field is validated and the node fails
     CLOSED naming the field but never echoing the rejected value — see
     *Caller-data contract* below.
  3. **Reduction.** `sanitize_query()` strips markup and caps length, then the
     request is serialized for the inner workflow graph.

  `ValidateInputNode.execute()` additionally runs a deterministic (regex, not a
  model) scan for email addresses and access-token-like strings (`eyJ...`,
  `secret_...`, `sk-...`) and redacts them before any logging. An invoice
  request legitimately names a vendor and an invoice number, so that half is
  flag-and-redact for safe logging, not a reject; the framework's own PII mask in
  `BaseNode.__call__` additionally masks emails/phones/names in
  `user_input`/`validated_input`. The only other deterministic auto-reject is the
  empty/non-request guard.
- **Credentials** — the integration token is read via
  `ctx.secrets.get("BILLONE_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. It is read with `.get()`, not
  `.require()`, and is therefore **not** declared under
  `config/agent.yaml requires.secrets`: `requires.secrets` is a compile-time
  gate, and declaring a secret the deployment does not provision refuses to
  compile the agent at all. A missing token is tolerated **only** while the
  deterministic network-free stub transport is active (no live call is made);
  with a live transport injected, a missing token is a hard `status=error` —
  a real API is never called unauthenticated.
- **Output gate** — the domain output gate is the **module-level**
  `_security_gate_output()` in `src/nodes/post_process_node.py`, called from
  `PostProcessNode.execute()`. It blocks any SUCCESS response that lacks record
  evidence (record_id/record_ref) and any credential-shaped string in the
  caller-facing output. Two properties are deliberate:

  * **It walks NESTED structures.** The two richest response fields
    (`billone_payload`, `invoice_summary`) are mappings, so a scan limited to
    top-level strings would report zero findings on a payload carrying a
    credential one level down.
  * **On EVERY error return it CLEARS the output-bearing state** (`result`,
    `formatted_output`, `confirmation`, `billone_payload`, `invoice_summary`,
    `record_id`, `record_ref`, `invoice_id`, `issuer_name`, `invoice_status`,
    `intent`, `redaction_flags`) rather than only flipping the status. The
    framework's output builder falls back to `state["result"]` even on the
    error path, so a gate that merely returns an error still ships the un-gated
    inner answer — credentials included — inside the error envelope. Clearing is
    what contains it, and a boundary test asserts the error envelope carries no
    released text.

  No node defines `_extra_security_gate_input/_output` instance methods
  (framework hooks are @final / auto-wrapped — domain checks live inline or in
  module-level helpers).

  **Numeric precision grid: not applicable.** Some templates in this family
  publish a "all monetary values rounded to the nearest 1,000" invariant and
  need an output gate that enforces it. This template renders **no monetary
  aggregate**: the summary is a record count and a per-status tally, and the
  confirmation carries identifiers, a status label and that count. There is
  therefore no rounding invariant to enforce and — equally important — no
  numeric rewriting step that could mangle an identifier or a decimal on its way
  out. The invariants this template *does* state (record evidence, no
  credential-shaped value anywhere in the response, containment on violation)
  are enforced for every representation instead.
- **Error-path containment (every error return, not just a gate violation)** —
  both error returns of `PostProcessNode.execute()` — a gate violation and a
  pre-existing inner-workflow error — go through the module-level `_contain()`:
  it spreads the same cleared set and replaces `formatted_output` with the
  envelope built by `error_envelope()` — a constant reason code
  (`billone_workflow_failed` / `output_withheld_by_gate`) and **nothing else**.
  `record_id`/`record_ref` are this agent's **write evidence** — the gate
  refuses a SUCCESS that lacks them — so returning them under an ERROR status
  would tell a caller being informed of failure that an invoice record was
  nonetheless touched, and which one. Omitting a field from one envelope is not
  clearing it: the clearing is what stops a checkpoint or a downstream reader
  recovering it. The envelope is deliberately **truthy** — the response builder
  reads `formatted_output` falling back to `result` with no status check, so an
  empty/falsy replacement would activate that same fallback.
- **The caller-visible error carries closed-set labels only** — the envelope
  publishes nothing read from `error_log`, from the gate's own violation
  messages, or from any other node-authored string: an internal entry can carry
  an upstream exception message or a third-party response body, and truncation,
  path stripping or credential-only redaction of such text is not a closed set.
  `error_log` itself is untouched — it is the internal channel the state reducer
  appends to and the audit trail reads; the gate's violations go there, never
  into the envelope. The invoke envelope closes the same channel at its last
  hop: `BillOneInvoiceAgent.get_output()` withholds `output` (`null`) on every
  non-success outcome — including the inner-workflow error the backbone routes
  straight to `finalize`, where post_process never runs — and reports
  `error: {"reason": <code>}` with no `error_log` key. The success envelope is
  the base one, unchanged.
- **Error reasons stay closed-set anyway** — every reason a node writes into
  `error_log` carries closed-set labels only — the record type, the HTTP
  status, the exception class, a field name — and never an interpolated invoice
  number, issuer name, exception message, rejected caller value or upstream
  response body. The log is internal, but a log entry must not be the place
  third-party text is stored.
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
  SUCCESS path (intent / presence signals only — never request text, invoice
  content, or credentials). `__call__()` is never overridden; `_invoke_impl`
  is never defined on any node. Event names (documented for operations):

  | Node | Audit event |
  |------|-----------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_billone_fields | `infer_billone_fields_complete` |
  | call_billone_api | `call_billone_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` |
  | post_process (contained error return) | `post_process_error_contained` — closed-set reason code + error COUNT only, never record content |

## Implementation note — deterministic pipeline, optional LLM gap-fill

The pipeline's primary mode is deterministic: intent classification
(`ClassifyIntentNode`) uses a keyword heuristic and field inference
(`InferBillOneFieldsNode`) uses regex and line-structure extraction, so the
template runs and tests without a model — `generation_mode` stays
`deterministic`, and every original test in
`tests/unit/test_infer_billone_fields_node.py` passes unchanged whether or not
an LLM is configured.

`InferBillOneFieldsNode` additionally makes a best-effort LLM gap-fill attempt
(`AzureOpenAIClient`, built fresh per invocation from three `ctx.secrets`) when
the regex extraction leaves `invoice_number` and/or `issuer_name` empty. It
only ever fills an empty field — never overrides a regex-confirmed match,
preserving the node's "never invented" invariant — and any failure (no secret
provisioned, API error, malformed/wrong-shape response) degrades silently back
to the deterministic result. `requires.extras` is therefore `["openai"]` (the
client's module import needs `langchain_openai`); `requires.secrets` stays
`[]` deliberately — see `config/agent.yaml`'s comment and
`tests/unit/test_config.py::test_manifest_declares_no_unprovisioned_compile_gates`.
Full detail: `docs/07_operation_guide.md`, "Optional LLM Enhancement".
`ClassifyIntentNode`'s 3-way keyword classification is unchanged — its
heuristic is low-ambiguity enough that an LLM enhancement was not pursued in
this pass.

## Limitation — the default Bill One transport is a stub (documented)

`src/services/billone_client.py` is an ordinary injectable-transport service
(per-call token, `BillOneApiError`, no framework imports) but ships a
**deterministic, network-free stub** as its default transport: it returns the
documented Bill One invoice response shapes (an `invoices` list for lookups and
listings; a task/receipt shape with a synthetic invoice-number echo for
register, derived from the request) so the pipeline is runnable and testable
without a live Bill One tenant or the `requests` package. It does **not**
perform a live Bill One call — the honest position is to document the
limitation rather than fake the call.

The stub still honours the request: the listing respects the caller's `scope`
and record cap, so a validated caller field produces a genuinely different
aggregate rather than one fixed answer. To go live, inject real `post`/`get`
transports at construction; the method contracts and payload shapes follow the
Bill One invoice API conventions, so no business-logic change is required. Every
call runs under the configured `timeout_s` budget, and a transport that overruns
it is reported as a failure rather than accepted silently. (The stub also runs
without a live credential — see *Credentials* above; a live transport requires
`BILLONE_TOKEN`.)

## Framework Utilization

### Shared Components Used
- [x] InvocationContext — read in `CallBillOneApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallBillOneApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("BILLONE_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] `emit_trace_event()` — one positional call per node on the SUCCESS path; framework lifecycle events (node_start/node_complete/node_error/trust-denial) NOT re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `BillOneWorkflowGraph` (`BaseGraph`) via `BillOneWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `BillOneWorkflowGraphNode._parent_config()` forwards
  `{billone, timeout_s}` from the graph's own runtime config (loaded from
  `config/config.yaml`) under `config["configurable"]` to the subgraph.
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as
  `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures
  (no silent pass).

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no Level-0 SDK import anywhere
- [x] `src/services/billone_client.py` and `src/services/security.py` have no
      framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | a Cat 2 template must not be flat; the 5 domain steps live in the inner graph |
| Model dependency | deterministic-only | deterministic primary + optional LLM gap-fill (graceful degrade) | deterministic primary + optional gap-fill | template runs and tests without a model; `InferBillOneFieldsNode`'s LLM call only fills a field the regex left empty and any failure degrades silently — see "Implementation note — deterministic pipeline, optional LLM gap-fill" |
| Bill One client | live `requests` call | injectable transport + documented stub default | injectable + stub default | no live network in the published template; document the limitation; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + runtime-config forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | SDK v1 nodes are no-arg (ctor args TypeError at graph build); `config/config.yaml` stays the single runtime-config source |
| Write target | infer the target invoice from NL freely | caller-supplied/explicit invoice number only; unresolved left empty | explicit only | never act on the wrong invoice record; an unresolved number on lookup -> status=error, not invented |
| Default intent | register_invoice | lookup_invoice | lookup_invoice | low-confidence classification must never default to a write |
