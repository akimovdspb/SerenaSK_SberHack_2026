# ADR 0001: Strict provider schemas for the two factory MCP tools

- Status: Accepted and implemented — deterministic requalification green; live requalification pending
- Decision ID: `CF-RP-001`
- Date: 2026-07-11
- Status: accepted for the release baseline
- Runtime baseline: Ouroboros `v6.61.4` / `a00d51dd414f794d830cacf7da760061e442fa88`
- Application route: explicitly selected provider profile inside Ouroboros

## Context

The pinned runtime converts MCP discovery records to Chat Completions function definitions in
`ouroboros/tools/registry.py`, but it copies only `name`, `description`, and `parameters`. It does
not set `function.strict`. Four preserved B04 attempts show that the resulting best-effort calls
can omit nested required fields even when the instruction and backend schema require them.

OpenAI's official function-calling contract states that `strict: true` makes calls reliably adhere
to the function schema and recommends enabling it. Strict schemas require every object to set
`additionalProperties: false` and every property to be required; nullable unions represent
optional values. The Structured Outputs subset supports nested `anyOf` and definitions, but not a
root union or the current `oneOf`/discriminator projection.

- Function calling: <https://developers.openai.com/api/docs/guides/function-calling#strict-mode>
- Supported Structured Outputs schemas:
  <https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas>

### Preserved evidence

| Attempt | Physical saves | Result | Tokens | Cost USD |
| --- | ---: | --- | ---: | ---: |
| `gate2-b04-pilot-20260711-01` | 3 | deadline; one schema-valid bundle rejected by QA | 142,575 | 0.051286 |
| `gate2-b04-pilot-20260711-02` | 2 | second bundle coherent but provenance-invalid | 135,480 | 0.069476 |
| `gate2-b04-pilot-20260711-03` | 2 | both saves schema-invalid; bounded release/fallback worked | 136,856 | 0.048805 |
| `gate2-b04-pilot-20260711-04` | 1 | one-pass contract followed; seven nested required fields omitted | 103,821 | 0.031463 |

Attempt `-04` used the current reviewed prompt and exact two-tool lock. It made one context call and
one save call, produced coherent SMS/e-mail copy and nine evidence declarations, completed in
26.258 seconds, and released the worker. Pydantic rejected the only save before the service because
the model omitted `sms.cta_url`, `sms.fact_refs`, `sms.personalization_refs`, and four required
fields in `email.sections[0]`. No agent draft was persisted; the QA-green package in the report is
the explicitly labelled deterministic fallback and is not live evidence.

The four attempts consumed 518,732 project-observed tokens and USD 0.201030. Another paid prompt
retry is not justified.

### Deterministic compatibility assessment

Before approval, `make runtime-patch-assessment` was a no-provider check that reported 11 issues:

- both provider functions omit `strict: true`;
- `cf_context_get.context_version` is nullable but not required;
- four nested draft object definitions contain properties that are not required;
- the draft union uses `oneOf` plus `discriminator` instead of supported nested `anyOf`;
- `ClaimEvidence.normalized_value` is unconstrained JSON Schema even though the P0 fact catalog
  uses a finite scalar-or-measure representation.

All current object schemas already set `additionalProperties: false` through the strict Pydantic
base model.

The approved implementation closes the normalized-value union in the application contract, so the
raw post-application/pre-adapter projection now has ten transport issues. The adapter resolves all
ten and the current assessment reports `strict_compatible=true` with zero issues. The first real
production no-forward run additionally found that FastMCP removes root
`additionalProperties:false` while serializing its tool signature. It failed before the provider
seam; the generic object normalizer now restores that strict invariant without mutating the source
MCP record. The repeated production probe is green.

The first paid post-patch Gate 0 probe then exposed a second general provider-subset rule: OpenAI
rejects a `$ref` with sibling keywords. Pydantic had emitted `description` beside the `Channel`
reference. Attempt `gate0-live-probe-20260712-05` is preserved failed with no tool calls or draft.
The adapter now preserves annotation semantics through a nested single-branch `anyOf`, requires a
bare `$ref` inside that branch and rejects all non-annotation siblings before forwarding.

## Decision

Implement one project-scoped pinned-runtime adapter, identified as `CF-RP-001`, with this exact
scope:

1. Add a small, tracked `strict_tool_adapter` to the dedicated Ouroboros image. Install it in the
   normal launcher and the no-forward contract probe; do not modify provider clients or add calls.
2. Apply it only to `mcp_factory__cf_context_get` and `mcp_factory__cf_draft_save` after the normal
   Ouroboros denylist. Every other built-in, extension, and MCP function remains byte-for-byte
   unchanged.
3. Deep-copy the two parameter schemas, make every property required, remove schema defaults,
   convert the nested Pydantic `oneOf` to semantically equivalent `anyOf`, remove its discriminator
   annotation, and set `function.strict=true`.
4. Replace the unconstrained application `normalized_value` provider type with a closed P0 union:
   scalar/null or a strict `{value, unit}` measure object. Backend equality against the selected
   FactLedger value remains authoritative.
5. Fail before a provider call if either target tool is absent, any target schema remains outside
   the supported subset, or a non-target tool changes. Do not fall back to non-strict mode.
6. Hash and disclose the adapter, normalized provider schemas, rebuilt image, and new runtime lock.

This is a transport/schema guarantee, not a repair pass. It neither creates nor changes copy,
fills missing fields, retries a failed save, nor weakens backend validation or QA.

## Alternatives rejected

- More prompt/schema prose: attempts `-01` through `-04` already cover that path; `-04` followed
  the one-save instruction and still omitted required nested keys.
- Optional/default artifact fields: would weaken the persisted DraftEnvelope and provenance
  invariants.
- Deterministic filling or content post-processing: would silently repair model output before
  persistence and obscure authorship.
- Another save round, longer timeout, stronger model, or alternate provider: adds cost/latency or
  changes routing without eliminating the schema-adherence class.
- Direct backend LLM or wrapper JSON-string tool arguments: violates the two-tool typed MCP
  contract and architecture boundary.
- Global strictification of all Ouroboros tools: broader than the product need and risks unrelated
  unsupported schemas.

## Owner decision record

- The owner explicitly replied `APPROVE CF-RP-001` on 2026-07-12.
- Adapter activation does not itself authorize an unguarded provider request.

Approval authorizes only the deterministic implementation and verification described here. Every
subsequent provider run still requires its own existing unique ID, linkage, profile, token cap,
dollar cap, empty evidence directory, and project-headroom preflight.
