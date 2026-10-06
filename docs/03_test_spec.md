# Test Specification - CMN-C2-275 Bill One Invoice Agent

## Test Strategy
- Test types: Unit (per node + service + config + inner graph + the caller
  contract) / Proof-of-Boundary (full outer-graph invoke, the real HTTP entry
  point, import isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end coverage lives in the boundary tests, which drive the
  real compiled outer graph and the real ASGI application).
- The Bill One call is exercised through the deterministic, network-free stub
  transport (default) and through injected fake clients; no live Bill One call.
- **Trust-gate routing canon**: per-node unit tests invoke the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust
  gate -> PII mask -> `execute()` -> credential scan) - rather than bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external
  gate) and `TrustLevel.ANONYMOUS.value` for every other node. The trust
  rejection test asserts on the RETURNED error dict (`status ==
  AgentStatus.ERROR.value`, "trust gate denied" in `error_log`, execute-only keys
  absent) - `__call__` never raises for a trust denial.
- **Two documented exceptions to that canon, both deliberate.**
  `CallBillOneApiNode.execute(state, config=...)` takes a second argument
  `__call__` cannot forward. And the caller-contract tests
  (`test_input_screening.py`) call `execute()` DIRECTLY, because a refusal test
  that passes only with the framework's own input gate in front proves nothing
  about this template's guarantee - the template has to own it.
- **Assertions are behavioural.** Refusal is asserted as an error status with
  nothing carried forward, never as a particular gate's wording; wording is a
  framework detail that changes between releases.
- **Screens are probed in both directions.** Every injection matrix is paired
  with real invoice prose that must NOT be refused - a screen that refuses
  everything has been shown to be broken in the expensive direction, not working.
- The framework PII mask rewrites Title-Case bigrams (across newlines), emails
  and digit groups in `user_input`/`validated_input` to `[MASKED]` before
  `execute()` sees the text, so positive payloads are PII-free (invoice numbers
  like INV-3041 are short alphanumerics, never 12-digit runs) and
  intentional-PII tests assert the `[MASKED]` path.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS an error dict ("trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | serialize NL request + invoice hint into `validated_input` (JSON); markup strip; hint priority invoice_id > invoice_hint > invoice_number | hint resolved by priority; `<script>` stripped; empty/missing -> `status=error` |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`) | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short -> error; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = lookup_invoice / register_invoice / summarize_invoices (keyword; priority register > summarize > lookup, read-only default) | correct intent per keyword; write and summarize keywords win over lookup; no-signal defaults to lookup_invoice with a non-fatal note; empty -> error; audit emits the intent label only |
| U-05 | test_infer_billone_fields_node.py | invoice-number resolution (text > number-shaped hint; never invented); quoted issuer; `Key: value` custom fields; Bill One `invoice_data` payload per intent; optional LLM gap-fill (`TestInferBillOneFieldsNodeLLMGapFill`): fills invoice_number/issuer_name only when the regex left them empty, never overrides a regex-confirmed match, markdown-fenced JSON still parses, malformed/wrong-shape response and a raising LLM both fall back silently, no `llm=` injected + no secret bound falls back (the real production shape), `summarize_invoices` / a fully-resolved text / empty input never call the LLM at all | lookup `{invoice_number}`; register `invoice_data[0]` with invoice_number/issuer_name/custom_fields; summarize `{scope, limit}`; unresolved number left `""`; empty input -> error; LLM gap-fill only fills empty fields and degrades to the deterministic result on any failure |
| U-06 | test_call_billone_api_node.py | lookup/register/summarize via the network-free stub; `billone_config` state field + `execute(state, config=...)` override; the validated scope/cap reaching the listing; the `timeout_s` budget reaching the client; a non-finite budget falling back; API error / unresolved number / no matching record / unknown intent / missing payload; secret posture; **closed-set error reasons** (no-match reason omits the invoice number; API-error reason carries the HTTP status not the upstream body; transport-failure reason carries the exception type not the error string/URL; clean-call control) | record_id/record_ref (+ invoice_status on lookup, invoice_summary JSON on summarize) on success; caller scope/cap change the aggregate; 403 surfaces in error_log; live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to the client; audit emits presence signals with `stub_transport=True`; every error_log entry carries a closed-set label only (error_log is internal — never projected into the caller-visible envelope — and kept closed-set anyway) |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; status/count/ref/id formatting; issuer fallback | "Retrieved invoice record/Registered invoice/Summarized invoice records ... status=... count=... ref=... id=..."; missing evidence -> error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (JSON payload round-trip); errored state passes through `__call__` un-masked (short-circuit); the record-evidence gate; the NESTED credential scan (leak case + top-level control + clean control); CONTAINMENT of a violating response; **existing-ERROR path containment** (envelope present AND truthy; no record_id/record_ref/issuer_name in the shipped envelope; every output-bearing field cleared in the delta; error status still reported; error_log NEVER projected into the envelope and not re-emitted; clean-path control so the containment assertions cannot pass vacuously) | success shape with parsed `billone_payload`/`invoice_summary`; error status/error_log preserved, no success shape fabricated; SUCCESS without record_id/record_ref blocked; a credential nested in `billone_payload` found and its PATH (never its value) named; on EVERY error return every output-bearing field cleared and the envelope replaced by the closed-set notice — a constant `reason` code and nothing else: no error_log line, gate message, released text, traceback or source path, and no Bill One record evidence |
| U-14 | test_error_envelope_closed_set.py | the caller-visible ERROR envelope, parameterised over every error path: `PostProcessNode` (inner-workflow error; gate: no evidence / top-level credential / nested credential / both rules — direct `execute()` and through the framework pipeline) and `BillOneInvoiceAgent.get_output()` (inner error routed to finalize, timeout status, gate block, non-dict / out-of-set / non-string `reason`); a seeded sentinel (a name, an email, a token-shaped fragment) walked through every nested key and value of the returned mapping; the builder's closed set; the source half (API error -> HTTP status; transport failure -> exception class; rejected caller field -> field name + fixed reason) | every envelope value is drawn from `ERROR_REASONS` and `formatted_output` is truthy; the sentinel appears nowhere in the returned mapping; gate violations stay in `error_log`; the invoke body on non-success is `output: null` + `error: {reason}` with no `error_log` key and the base keys preserved; the success envelope is unchanged; a success without a gated dict withholds `output` |
| U-09 | test_billone_client.py | Bill One REST client: find/register/list invoices; `Authorization: Bearer` header; `BillOneApiError` on non-2xx; stub shapes and `_stub` marker; scope/limit honoured; the service-layer hard cap; the request-budget overrun; `uses_stub_transport`; params dict never mutated | correct URLs/headers/bodies; 400 raises with joined `errors`; a filtered listing returns only that status; a hostile limit is clamped to the cap; an over-budget transport raises 504; a prompt transport does not |
| U-10 | test_config.py | manifest sanity + the runtime-config chain | manifest is FLAT (no `agent:` block), id/Cat/industry/namespace/base_type/entry-point/trust correct, `requires.secrets == []` (deliberately - the Bill One token AND the LLM gap-fill's Azure OpenAI secrets are both read without gating compile), `requires.extras == ["openai"]` (fleet-wide guaranteed, safe to declare), `generation_mode: deterministic`; `config/config.yaml` carries max_retry/timeout_s/billone; a declared value reaches the agent AND the forwarded inner-graph config |
| U-11 | test_domain_workflow_graph.py | inner `BillOneWorkflowGraph`: identity, `_extra_initial_state()` billone_config JSON injection, `route()` error short-circuit, `get_output` contract, compile, direct inner invoke on the stub; routing-annotation guards | name/state_schema correct; config forwarded as a JSON string; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence; every conditional path callable is annotated with the graph's OWN State (an annotation naming the shared base state would project the domain fields away and make the branch unreachable) |
| U-12 | test_input_screening.py | the caller contract: injection refusal (control tokens `<\|…\|>` / `[INST]` / `<<SYS>>`, directive phrases, markup-spliced directives, full-width evasion, hostile field NAMES, nested values, escaped payloads) both at pre_process and independently at the inner entry; the sanitizer-is-not-refusal ordering; the finite-number matrix per field; identifier and inert-render locks; unrecognised fields ignored not echoed; scan boundedness | every attack form -> error with nothing carried forward and the payload never echoed; every real invoice sentence -> success; NaN/±Infinity/bool/non-numeric/out-of-range rejected fail-CLOSED naming the field but not the value; rendered caller strings locked to `[a-z0-9_]{1,32}`; platform metadata tolerated and its names kept out of the audit payload |
| U-13 | test_framework_compliance_tc06_tc07.py | TC-06/TC-07: overriding the default input/output gate raises at class definition | domain nodes extend the gates only through the documented hooks |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no platform-SDK imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external-trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, BillOneWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; ANONYMOUS caller denied at pre_process (error, no post_process, no output); blank input -> error, not crash |
| PB-6b | The real HTTP entry point | test_pb_invoke_asgi.py | Through the ASGI app with Bearer auth: unauthenticated callers refused with an indistinguishable 401 body; the published sign-off request returns real record evidence; caller data in `input_context` REACHES the inner graph and changes the aggregate (scope + cap), and two different caps give two different answers; each intent path reachable; injection and out-of-bounds caller fields (including bare `NaN`/`Infinity` on the wire) rejected end to end with no output; an oversized caller channel refused at the adapter naming the limit, not the payload; no credential-shaped value, traceback or source path in any response; on every non-success outcome — refusal, rejected caller field, blank input, and an inner-workflow error that bypasses post_process (asserted via `node_history`) — the body is `output: null` + `error: {reason}` with the reason drawn from `ERROR_REASONS`, no `error_log` key, and no log wording anywhere in the rendered body |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (no `hitl.enabled: true`): module-level skipif; stub bodies are real AssertionErrors so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy tests
> (validate / classify / call nodes assert on the event payload,
> `call.args[1]`), and by `scripts/check_audit_trace.py`, which fails closed if
> any boundary node's `execute()` has no reachable domain event. PB-3 (live
> external service) is not exercised here - the shipped transport is the
> documented network-free stub.

## Output-schema note

This template renders **no monetary aggregate**: the summary is a record count
and a per-status tally, and the confirmation carries identifiers, a status label
and that count. There is therefore no rounding invariant to test, and no numeric
rewriting step that could mangle an identifier or a decimal on the way out. The
invariants that ARE stated - record evidence, no credential-shaped value
anywhere in the response, containment on violation - are tested for every
representation the response can take, including nested mappings.

## Test Execution Summary
- Runner: `python -m pytest tests/ -v`, against the framework wheel CI installs.
- Total tests: 227
- Pass: 225 / Fail: 0 / Skip: 2 (PB-7 A/B - auto-waived, non-HITL)
