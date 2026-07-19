# Sprint 4 Multi-Agent Framework — Architectural Notes

## Date: 2026-07-15
## Author: Multi-Agent Framework Evolution Sprint

---

## 1. Design Philosophy: Reasoning vs Templating

The multi-agent framework's value is **autonomous protocol-spec reasoning**, not
static template execution. Two complementary mechanisms exist:

| Layer | Mechanism | When it runs | When it should fire |
|-------|-----------|--------------|----------------------|
| **Primary** | Demo scaffold (`examples/demo/src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn`) declares `vc_SessionID` natively | Every install | Always (since Sprint 4) |
| **Primary** | IA HARD RULE in `prompts/infrastructure_agent_prompt.txt` tells the IA to KEEP all `vc_*` declarations when re-emitting `ComponentsAndPorts.ttcn` | Every IA re-write | Always |
| **Primary** | CA reasoning test (5-step protocol-spec reasoning, see `knowledge_base/generation_patterns.json` `Protocol_Spec_Reasoning_Test_For_New_VC_Variables`) | Every CA run | When a new `vc_*` is needed |
| **Safety net** | `_patch_vcsessionid_into_components()` post-install patch | After install | ONLY if all primary mechanisms failed (log loudly) |

The framework's success criterion: a **new test case the LLM has never seen**
must work without ANY code changes to the framework, ONLY by following the
reasoning test. If we have to add a new patch for every new test case, the
multi-agent framework has failed its purpose.

## 2. Why We Removed Post-hoc Patches

Sprints 1–3 used post-hoc patches to "fix" issues the LLM produced. By Sprint 4
this pattern was identified as anti-pattern:

- **Brittle to generalization.** A patch for `vc_SessionID` does not help with
  `vc_ContractCertChain`. Each new cross-step state would need its own patch.
- **Hides LLM misunderstanding.** If the LLM doesn't know WHY `vc_SessionID`
  needs to be declared, it can't apply the same pattern to `vc_EMAID`.
- **Wastes compute.** The patch runs on every install even when the upstream
  mechanism (demo scaffold) is correct.
- **Violates paper direction.** The paper's thesis is that the multi-agent
  framework achieves autonomous protocol-spec reasoning. Post-hoc patches
  contradict this thesis.

The new architecture has the patch kept as a safety net (with loud logging when
it fires) but the primary mechanisms — demo scaffold + IA prompt + CA reasoning
test — should ensure it never fires.

## 3. The Five-Step Reasoning Test

When the LLM encounters a need to persist a piece of cross-step state, it
applies this test (encoded in the new `Protocol_Spec_Reasoning_Test_For_New_VC_Variables`
pattern in `knowledge_base/generation_patterns.json`):

1. **What is the state?** Identify the protocol field derived from the protocol
   specification, NOT from copying a previous test case.
2. **Where does it live?** It MUST live on the tester component
   (`SECCTester` in `ComponentsAndPorts.ttcn`). TTCN-3 has no globals.
   Prefix with `vc_`. Type as `octetstring` for IDs and `charstring` for
   human-readable fields.
3. **Who declares it?** The Infrastructure Agent declares it in
   `ComponentsAndPorts.ttcn`. The Core Agent NEVER writes
   `ComponentsAndPorts.ttcn` (that is out of scope).
4. **How is it set?** Inside the PR that receives the relevant SECC response.
   Use `match()` (regex/indexof are illegal in TITAN) + `substr()` for parsing,
   then assign with `vc_X := char2oct(substr(...))` (octetstring) or
   `vc_X := substr(...)` (charstring).
5. **How is it read?** In downstream TBs: embed via `& oct2char(vc_X) &` for
   octetstring → JSON string, or direct field assignment for typed fields.

### Worked Examples Already in the Knowledge Base
- `vc_SessionID` (ISO 15118-2 § 8 SessionSetupRes → every subsequent V2G_Message)
- `vc_SAScheduleTupleID` (ISO 15118-2 § 9.4 ChargeParameterDiscoveryRes → § 9.6 PowerDeliveryReq)
- `vc_EMAID` + `vc_ContractCertChain` (ISO 15118-20 § 7.4 PaymentDetailsRes → § 7.5 AuthorizationReq)

When ISO 15118-20 test cases are added (or any future protocol extension), the
LLM applies the same 5 steps — **no new framework code required**.

## 4. Role Split: CA vs IA

### Core Agent (CA) — Code Generator
**Responsibilities:**
- Generate `f_*_PR_*.ttcn` (precondition functions)
- Generate `f_*_TB_*.ttcn` (test behavior functions)
- Generate `TC_*.ttcn` (test case orchestrators)
- Generate `f_*_PO_*.ttcn` (postcondition functions)
- Reference existing `vc_*` component variables in JSON envelopes
- Signal in Summary if a new `vc_*` is needed

**Out of scope:**
- Writing `ComponentsAndPorts.ttcn`
- Writing `*.cfg` files (those go to a dedicated generator)
- Writing `*.cc` / `*.hh` files (C++ adapter)

### Infrastructure Agent (IA) — Infrastructure Maintainer
**Responsibilities:**
- Maintain `ComponentsAndPorts.ttcn` (component types, ports)
- Maintain shared types (`SECC_Types.ttcn`)
- Maintain templates (`SECC_CmnTemplates.ttcn`)
- Maintain configuration files (`*.cfg`)
- **KEEP existing `vc_*` declarations when re-emitting** (HARD RULE)
- Declare new `vc_*` variables when signaled by the CA (HARD RULE)
- Ensure all hard rules from the CA's prompt are respected in any file edits

**Out of scope:**
- Generating test behavior logic (that's CA's job)
- Modifying the protocol specification

## 5. Failure Recovery Architecture

### P0: Re-sanitize Solver Output
Any file written by the IA or CA to disk MUST pass through
`_apply_sanitizers_to_text()` first. This prevents reintroduction of
invalid placeholders like `PLATFORM1234556`.

### P0: Integrate exi_microservice.log into FC Context
The Fault Corrector receives a tail of `exi_microservice.log` as
supplemental evidence. Without this, FC was misdiagnosing decode errors
as protocol failures.

### P0: Scope IA's Edits
The IA's `files_modified` dict is filtered to only the files that have
compile errors in `build_log`. Unrelated modifications are discarded.

### P1: Differentiate WatchdogKilled from Pipeline Bugs
A `_WatchdogKilled` exception is propagated without aborting the pipeline.
The repair loop continues, using any partially-generated output.

### P1: Long-term Memory via FC Repair Promotion
Successful FC `specific_change` entries are dynamically promoted into
`core_agent_prompt.txt`'s "Known Bugs & Their Fixes" section. This is the
agent's long-term memory — it persists across test cases.

## 6. Future Work (Sprint 5 candidates)

- **Re-sanitize at the source, not just on disk** — currently sanitizers run
  after the LLM writes, but the LLM is still producing dirty text. A
  `sanitize-before-send` hook would reduce wasted compute.
- **Generalize the IA `vc_*` declaration to ALL known `vc_*` types** —
  currently the demo scaffold declares `vc_SessionID`; future sprints
  should declare all `vc_*` variables from a single canonical list.
- **Add automated test of "new test case works without framework change"**
  — every sprint pick a previously-unsupported test case and verify it
  passes WITHOUT any framework code change. This is the paper's validation
  criterion.
- **Add an "explain your reasoning" step to the CA prompt** — currently the
  CA generates code; have it first explain which cross-step state it
  identified and why, then generate. This makes the reasoning explicit.

---

For questions or amendments, contact the multi-agent framework maintainers.
---

## 7. Sprint 4 Architectural Pivot (2026-07-15) — Plan-as-Contract

**The fundamental shift that came out of the 2026-07-15 batch run:**

Before Sprint 4, the IA and CA had to **guess** the state variables they
needed by reading demo files (ComponentsAndPorts.ttcn) and inference.
This is fragile — it works for ServiceDiscovery_001 only because the demo
happens to declare `vc_SessionID`. For `VC_SAScheduleTupleID` (needed
by PowerDelivery), the demo doesn't declare it, and the IA never knew it
should.

**New mechanism:** The Plan JSON now carries a `cross_step_state` field
computed by the rule-based Plan Builder from **pure ISO 15118-2 protocol
semantics** (no demo archaeology). The Plan becomes a **declarative
contract** between Plan Builder → IA → CA:

```
plan["cross_step_state"] = {
    "producers": [{"pr": "f_..._PR_SessionSetup_001",
                   "vc_name": "vc_SessionID",
                   "vc_type": "octetstring"}, ...],
    "consumers": [{"consumer_message": "ServiceDiscovery",
                   "vc_names": ["vc_SessionID"]}, ...],
    "summary": "Declare var octetstring vc_SessionID := '00000000...'
                in SECCTester ..."
}
```

**Why this is the right pivot:**
1. It removes the LLM "guessing" dependency. The IA receives the list of
   `vc_*` to declare; the CA receives the list of `vc_*` that are
   guaranteed to exist. No more "I forgot to declare `vc_SessionID`,
   now compile fails, spend 90 seconds rebuilding."
2. It makes the framework **look like a multi-agent system**, not a
   static-templating system. The Plan Builder reasons about cross-step
   state; the IA & CA are code generators that act on the Plan.
3. It generalizes to **any** test case, not just the 10 pilot ones. New
   test cases with new `vc_*` requirements (e.g. `vc_DummyEvID`,
   `vc_TLSRootCertHash`) will be auto-detected and auto-declared,
   without framework changes.

**Concretely implemented:**
- `_CROSS_STATE_PRODUCERS` table: maps PR function suffix → field
  produced → `vc_*` name → TTCN-3 type.
- `_CROSS_STATE_CONSUMERS` table: maps message type → list of `vc_*` it
  consumes from the precondition chain.
- `_derive_cross_step_state()`: pure function, no LLM needed.
- `_build_cross_step_state_directive()`: formats the contract into a
  short, machine-checkable block for both IA and CA prompts.
- The `{CROSS_STEP_STATE_DIRECTIVE}` placeholder appears in both
  `infrastructure_agent_prompt.txt` and `core_agent_prompt.txt`.

**Bonus discovery (also 2026-07-15):**

The `match(resp_json, "*\"ResponseCode\":\"OK*\"*")` pattern in the asset
pool files is BROKEN — it requires `OK` to be followed immediately by a
quote, but ISO 15118-2 ResponseCodes are `OK_SuccessfulNegotiation`,
`OK_NewSessionEstablished`, etc. (underscore-suffix format).

**Fix:** All match patterns updated to `"OK_*\"*"` / `"FAILED_*\"*"`:
- `agent/test_code/generated/_global_asset_pool/f_SECC_CMN_PR_SessionSetup_001.ttcn`
- `agent/test_code/generated/_global_asset_pool/f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn`
- `agent/test_code/prompts/core_agent_prompt.txt` (HARD RULE #3 + TB
  ResponseCode section)
- `agent/test_code/tb_verdict_linter.py` (regex comment + matching hint)

This was the actual cause of `TC_SECC_CMN_VTB_ServiceDiscovery_001`
failing — the SECC returned `OK_SuccessfulNegotiation` but the LLM's
TB failed the ResponseCode check and setverdict(fail), which propagated
to the TC.

The dynamic `<index>`-based message-error analysis in the runtime loop
could have caught this if the FC had been told "the SECC returned OK_*,
but your verdict is fail — investigate the ResponseCode parsing."
That FC prompt improvement is the next sprint.
