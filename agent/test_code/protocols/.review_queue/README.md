# Review Queue — LLM-Proposed Protocol Additions

This directory holds **proposals** from the LLM agents for additions to a
main protocol descriptor (e.g. `../iso15118_2.json`). They are NOT
applied automatically. A human reviews and merges.

## Why a queue, not auto-merge?

The Core/Orchestrator LLMs are creative and will sometimes discover a
new message or union-field mapping that wasn't in the descriptor. We
want to:

- **Capture** the discovery so future runs benefit from it.
- **Validate** the discovery against the protocol spec / demo code
  before it goes into production.
- **Audit** who/what/when added each entry.
- **Reject** confidently when the LLM hallucinated.

## File format

Each file is a standalone JSON object that can be `jq`-merged into a
protocol descriptor. Example:

```json
{
  "target_protocol": "iso15118_2",
  "proposed_at": "2026-06-16T03:42:00+08:00",
  "proposed_by": "Core Agent (run #abc123)",
  "proposed_during_tc": "TC_SECC_CMN_VTB_WeldingDetection_002",
  "rationale": "Discovered 'weldingDetectionReq' is the correct union field when reading the message name 'WeldingDetection' (vs. the naive 'weldingdetectionReq' camelCase).",
  "merge_path": {
    "messages": [
      {
        "name": "WeldingDetection",
        "category": "CMN",
        "protocol_layer": "V2G_Application",
        "transport": "TCP_TLS",
        "uses_raw_octetstring": false,
        "covered_by_precondition": null,
        "request_type": "WeldingDetectionReq",
        "response_type": "WeldingDetectionRes",
        "union_field": "weldingDetectionReq",
        "default_precondition_chain": [
          "f_{SUT}_CMN_PR_InitConfiguration_001",
          "f_{SUT}_CMN_PR_SupportedAppProtocol_001"
        ]
      }
    ]
  }
}
```

The `merge_path` is structured so that
`jq -s '.[0] * .[1].merge_path' ../iso15118_2.json ./proposal.json`
produces a merged descriptor. The reviewer should dry-run this and
visually inspect before committing.

## Review workflow

1. **Read** the proposal. Pay attention to:
   - Is the `union_field` value one that exists in the protocol's union type?
     Cross-check with `examples/{protocol}/src/ttcn3/1_Data_Definitions/Types/`.
   - Does the `request_type` / `response_type` match a real TTCN-3 type?
   - Are the `default_precondition_chain` entries consistent with the protocol's
     pre-condition naming conventions?

2. **Verify** the proposal by:
   - Reading the linked spec section.
   - Running `tests/test_protocol_registry.py::test_resolve_<proposed_msg>` to
     confirm the registry can resolve it.
   - Building the affected TC and confirming it compiles.

3. **Decide**:
   - **Approve**: Move the file to `protocols/.review_queue/.approved/`,
     then merge it into the main descriptor with `jq` or your editor.
     Finally delete the file from `.review_queue/`.
   - **Reject**: Delete the file. Add a one-line entry to the commit
     message or PR description: `Rejected {file}: {reason}`.
   - **Defer**: Leave the file in place; the proposal will be considered
     again in the next review pass.

## What does NOT go in the queue

- **Bug fixes** to existing entries: edit the main descriptor directly.
- **Schema additions** (new fields): coordinate via PR review.
- **Cosmetic changes** (typos, reformatting): edit the main descriptor directly.

The queue is for **new knowledge** the LLM discovered, not for
maintenance of existing knowledge.
