# CMN-C2-275 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); this backbone formatter declares ANONYMOUS -> the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value. The domain output gate is the
# MODULE-LEVEL _security_gate_output() helper (fleet-green pattern - the
# framework gate methods are @final and the real runtime auto-wraps _extra_ hooks),
# so the helper is also unit-tested directly as a plain function.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "INV-3041",
        "record_ref": "billone://invoices/INV-3041",
        "issuer_name": "Acme",
        "invoice_status": "received",
        "intent": "lookup_invoice",
        "confirmation": "Retrieved invoice record 'Acme' - status=received - ref=billone://invoices/INV-3041 - id=INV-3041",
        "billone_payload": to_json({"invoice_number": "INV-3041"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the enum .value STRING, never
        # the bare AgentStatus enum member.
        # `.__class__ is str` rather than isinstance(): a str-subclassing enum
        # member would satisfy isinstance and defeat the point of the guard.
        assert result["status"].__class__ is str
        out = result["formatted_output"]
        assert out["record_id"] == "INV-3041"
        assert out["record_ref"] == "billone://invoices/INV-3041"
        assert out["issuer_name"] == "Acme"
        assert out["invoice_status"] == "received"
        assert out["intent"] == "lookup_invoice"
        assert out["confirmation"].startswith("Retrieved invoice record")
        # JSON round-trip: the JSON billone_payload string surfaces parsed.
        assert out["billone_payload"] == {"invoice_number": "INV-3041"}
        assert out["invoice_summary"] == {}

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallBillOneApiNode: Bill One API error 403: forbidden"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "Bill One API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_output_gate_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "INV-3041", "record_ref": "billone://invoices/INV-3041", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"record_id": "INV-3041", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []


class TestOutputGateWalksNestedStructures:
    """A scan limited to top-level strings reports zero findings on a nested leak.

    Both directions are probed on purpose: the nested case alone cannot tell
    "the gate is blind" apart from "the probe is wrong", so the top-level
    control proves the verifier itself fires, and the clean case proves it is
    not simply always firing.
    """

    CREDENTIAL = "Bearer abcdefghijklmnop0123456789"

    def test_a_credential_nested_in_the_payload_is_found(self):
        violations = _security_gate_output(
            {
                "record_id": "INV-3041",
                "billone_payload": {
                    "invoice_data": [{"custom_fields": [{"name": "note", "values": [self.CREDENTIAL]}]}]
                },
            },
            is_success=True,
        )
        assert violations
        assert "billone_payload" in violations[0]

    def test_the_top_level_control_proves_the_scan_fires(self):
        violations = _security_gate_output({"record_id": "INV-3041", "confirmation": self.CREDENTIAL}, is_success=True)
        assert violations

    def test_a_clean_response_produces_no_findings(self):
        violations = _security_gate_output(
            {
                "record_id": "INV-3041",
                "billone_payload": {"invoice_data": [{"invoice_number": "INV-3041"}]},
                "invoice_summary": {"count": 3, "by_status": {"received": 3}},
            },
            is_success=True,
        )
        assert violations == []

    def test_a_violation_names_the_path_and_never_the_value(self):
        violations = _security_gate_output({"record_id": "INV-3041", "confirmation": self.CREDENTIAL}, is_success=True)
        assert self.CREDENTIAL not in " ".join(violations)


class TestOutputGateContainment:
    """Returning an error is not containment; clearing the output is.

    The framework's output builder falls back to state["result"] even when the
    status is ERROR, so a gate that only flips the status still ships the
    un-gated inner answer inside the error envelope.
    """

    CREDENTIAL = "Bearer abcdefghijklmnop0123456789"

    def _violating_state(self):
        return _state(
            confirmation=f"Retrieved invoice record 'Acme' - token {self.CREDENTIAL}",
            result={"record_id": "INV-3041", "confirmation": f"token {self.CREDENTIAL}"},
        )

    def test_every_output_bearing_field_is_cleared(self):
        result = PostProcessNode().execute(self._violating_state())
        assert result["status"] == AgentStatus.ERROR.value
        retained = [
            field
            for field in (
                "result",
                "confirmation",
                "billone_payload",
                "invoice_summary",
                "record_id",
                "record_ref",
                "invoice_id",
                "issuer_name",
                "invoice_status",
                "intent",
            )
            if field not in result or result[field]
        ]
        assert not retained, f"output-bearing state not cleared on the gate path: {retained}"
        # The replacement envelope is the record-free withheld notice - TRUTHY
        # on purpose, so the `formatted_output or result` projection stops here
        # rather than falling back onto whatever survived in state.
        assert result["formatted_output"]
        # ...and it is the constant reason code alone - no released text, no
        # gate message, nothing read from error_log.
        assert result["formatted_output"] == {"reason": "output_withheld_by_gate"}

    def test_the_error_envelope_carries_no_released_text(self):
        """The property that matters, expressed the way the caller sees it."""
        from framework.graph.agent_base_graph import AgentBaseGraph

        state = self._violating_state()
        merged = {**state, **PostProcessNode().execute(state)}
        envelope = AgentBaseGraph.get_output(None, merged)
        # The withheld notice ships, not the blocked answer.
        assert isinstance(envelope["output"], dict), envelope["output"]
        assert envelope["output"].get("reason") == "output_withheld_by_gate"
        assert "record_id" not in envelope["output"]
        assert self.CREDENTIAL not in json.dumps(envelope, default=str)

    def test_the_error_envelope_carries_no_traceback_or_source_paths(self):
        state = self._violating_state()
        rendered = json.dumps(PostProcessNode().execute(state), default=str)
        assert "Traceback" not in rendered
        assert "/src/" not in rendered
        assert '.py"' not in rendered


class TestExistingErrorPathContainment:
    """The pre-existing-ERROR branch must contain, not re-publish.

    molt source review (wave-8 batch, 2026-09-04): "the success-gate violation
    path has containment, but the independent existing-ERROR branch rebuilds a
    truthy response with record_id / record_ref and does not clear the merged
    output-bearing state."

    ``record_id`` / ``record_ref`` are the Bill One WRITE EVIDENCE - the success
    branch of this node's own gate REFUSES a SUCCESS that lacks them. Returning
    them in an envelope whose status is ERROR tells a caller being informed of
    failure that an invoice record was nonetheless touched, and which one.

    Three separate properties are asserted, because fixing one leaves the others
    open:
      1. the shipped envelope carries no record evidence, AND stays TRUTHY - a
         falsy formatted_output re-opens the framework's
         `formatted_output or result` projection (get_output(), no status check)
         onto whatever survived in state;
      2. the returned delta CLEARS the output-bearing state fields, so a
         checkpoint or a downstream reader cannot pick them up either;
      3. the error REASONS stay internal - error_log is never projected into
         the envelope, so a reason that interpolates the invoice number, or
         echoes an upstream response body, cannot put back under another key
         exactly what the envelope omits. The envelope is the constant reason
         code and nothing else.

    Reachability, stated honestly: in the compiled graph AgentBaseGraph.route()
    sends an errored state to `finalize` (bypassing post_process) and
    BaseNode.__call__ short-circuits on an incoming errored state before
    execute() runs, so this branch is source-level defence in depth, reachable
    by a direct execute(). It is not a live end-to-end leak - and it is still
    the shape a future route change or a direct caller would ship.
    """

    _RECORD_ID = "INV-3041"
    _RECORD_REF = "billone://invoices/INV-3041"
    _ISSUER_NAME = "Acme Trading K.K."
    _AP_EMAIL = "ap@acme.example"

    # Every output-bearing State field: the envelope composes them, and
    # downstream formatting / the checkpoint read them.
    _OUTPUT_BEARING = (
        "result",
        "confirmation",
        "billone_payload",
        "invoice_summary",
        "record_id",
        "record_ref",
        "invoice_id",
        "issuer_name",
        "invoice_status",
        "intent",
        "redaction_flags",
    )

    def _errored_state(self, error_log=None) -> dict:
        """An error raised AFTER the Bill One call resolved a record - the
        realistic shape (call succeeded, a later step failed), and the only
        shape in which record evidence is present on an error at all."""
        return _state(
            status=AgentStatus.ERROR.value,
            invoice_id=self._RECORD_ID,
            issuer_name=self._ISSUER_NAME,
            issuer_ref="iss88",
            invoice_summary=to_json({"count": 1, "by_status": {"received": 1}}),
            redaction_flags=to_json(["email"]),
            result={
                "record_id": self._RECORD_ID,
                "record_ref": self._RECORD_REF,
                "issuer_name": self._ISSUER_NAME,
            },
            error_log=error_log or ["ConfirmNode: downstream failure after the Bill One call"],
        )

    def test_error_envelope_is_present_and_truthy(self):
        """Containment must not be achieved by emptying the envelope: the
        framework projects `formatted_output or result` with NO status check,
        so a falsy value hands the caller `result` instead."""
        result = PostProcessNode().execute(self._errored_state())
        assert "formatted_output" in result, "error path must ship an envelope"
        assert result["formatted_output"], (
            "error envelope must be TRUTHY - a falsy one re-opens the "
            "`formatted_output or result` fallback in AgentBaseGraph.get_output()"
        )

    def test_error_envelope_carries_no_record_evidence(self):
        """The leak itself: no record identifier or issuer field may ride the
        failure envelope back to the caller."""
        shipped = json.dumps(
            PostProcessNode().execute(self._errored_state())["formatted_output"],
            default=str,
            ensure_ascii=False,
        )
        leaked = [
            field
            for field, value in (
                ("record_id", self._RECORD_ID),
                ("record_ref", self._RECORD_REF),
                ("issuer_name", self._ISSUER_NAME),
            )
            if value in shipped
        ]
        assert not leaked, f"error envelope leaked Bill One record evidence: {leaked}"

    def test_error_return_clears_output_bearing_state(self):
        """"Does not clear the merged output-bearing state": omitting a field
        from ONE envelope is not clearing it. The delta must blank every
        output-bearing field so no checkpoint or downstream reader recovers it."""
        result = PostProcessNode().execute(self._errored_state())
        retained = [field for field in self._OUTPUT_BEARING if field not in result or result[field]]
        assert not retained, f"output-bearing state not cleared on the error path: {retained}"

    def test_error_log_is_never_projected_into_the_caller_facing_envelope(self):
        """The second channel, closed: `error_log` stays internal.

        A reason that interpolates the invoice number, or echoes an upstream
        response body, would put back under another key exactly what the
        envelope omits - so the envelope reads nothing out of error_log at all:
        it is the constant reason code and nothing else. The entries are not
        re-emitted either; the state reducer appends, and they are already there
        for the audit trail.
        """
        reasons = [
            f"CallBillOneApiNode: no invoice record found for number {self._RECORD_ID}",
            f"CallBillOneApiNode: Bill One API error 403: denied for {self._ISSUER_NAME} <{self._AP_EMAIL}>",
        ]
        result = PostProcessNode().execute(self._errored_state(error_log=reasons))
        envelope = result["formatted_output"]
        assert envelope == {"reason": "billone_workflow_failed"}
        assert "error_log" not in result
        shipped = json.dumps(envelope, default=str, ensure_ascii=False)
        for fragment in (self._RECORD_ID, self._ISSUER_NAME, self._AP_EMAIL, "no invoice record", "API error"):
            assert fragment not in shipped, f"error_log text reached the caller-facing envelope: {fragment!r}"

    def test_error_status_is_still_reported(self):
        """Containment must not mask the failure."""
        assert PostProcessNode().execute(self._errored_state())["status"] == AgentStatus.ERROR.value

    def test_clean_path_control_still_returns_the_answer(self):
        """CONTROL. Without it every containment assertion above passes
        vacuously - a node that returned an empty envelope would satisfy them
        all. The success path must still carry the record evidence."""
        result = PostProcessNode().execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "INV-3041"
        assert out["record_ref"] == "billone://invoices/INV-3041"
        assert out["issuer_name"] == "Acme"
        assert out["intent"] == "lookup_invoice"
        assert out["confirmation"].startswith("Retrieved invoice record")
        assert out["billone_payload"] == {"invoice_number": "INV-3041"}
