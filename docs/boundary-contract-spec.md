# The Boundary Contract — a portable spec (DRAFT v0.1)

**Status:** draft for direction. This extracts the contract semantics already
implemented in Boundary into a harness-independent specification, so the value is
the *vocabulary* a harness implements, not this particular binary.

**Why this exists (the kill condition).** A Claude Code plugin using hooks + `srt`
could replicate much of Boundary's enforcement. If Boundary's value is the engine,
that substitution collapses it to a config file on someone else's harness. If the
value is the *contract* — a named, implementable standard for declaring an
agent-run envelope, staging a thesis, and grading the run against the declaration —
then a hooks plugin that implements the contract is an *adopter*, not a replacement.
This spec is the move from engine to vocabulary.

The contract has three parts. All three are implemented in this repo today; this
document is their portable statement, not a proposal for new behavior.

---

## Part A — The Envelope (the declared contract)

An **Envelope** is a pre-declared boundary a run executes inside, enforced at the
tool layer (not the prompt layer) so a confused agent cannot interpolate past it.
Each field names *where* it is enforced: **tool** (the call is refused at execution
time) or **verdict** (graded post-run by Part C).

| Field | Type | Meaning | Enforced |
|---|---|---|---|
| `writable_paths` | `[glob]` | workspace-relative write allowlist (anchored, segment-aware; `..`/absolute rejected) | tool |
| `max_writes` | int | write **ceiling** (cardinality) | tool |
| `min_writes` | int | write **floor** (liveness — "enough must happen") | verdict |
| `max_appends` | int | chunked-continuation cap (separate from `max_writes`) | tool |
| `max_external` | int | external-call rate cap | tool |
| `max_input_tokens` / `max_output_tokens` | int | per-run token caps | tool (halt) |
| `max_dollars` | float | per-run spend cap | tool (halt) |
| `spend_pressure_at` | `[frac]` | soft-landing nudges before the hard spend halt | tool (nudge) |
| `degrade_to` / `degrade_at` | model id / frac | swap to a cheaper model past a spend fraction | tool |
| `on_unpriced_model` | `max_rate\|zero\|<model>` | **fail-closed** pricing policy (an unpriced model is an uncapped run) | tool |
| `require_staging` / `max_unstaged_reads` | bool / int | require a staged thesis before deep reads/writes (Part B) | tool |
| `on_commit` | `refuse\|queue\|ask\|allow` | policy for irreversible external actions | tool |
| `commit_allowlist` | `[name]` | which commit tools are allowed under `allow` | tool |
| `on_taint` | `warn\|refuse\|allow` | policy for untrusted content reaching a writable sink | tool |
| `write_profile` | `edit\|batch\|synthesis` | declares run shape so spend is graded on the right axis | verdict |
| `require_srt_for_bash` | bool | refuse bash unless egress is OS-bounded (`srt`) | tool |

**Invariant (the design rule):** every *mutating* field is enforced at the **tool
layer** except the two that are liveness/quality properties of the *whole run*
(`min_writes`, `write_profile`), which are graded at the **verdict layer**. A
conforming implementation MUST refuse, not merely warn, on the tool-layer fields.

**The load-bearing property** is write **cardinality** (`min_writes`/`max_writes`):
an exact O(1) counter over the run, not an inference over a transcript. This is the
explicit-state mechanism argued in `docs/separation-thesis.md` — the thing a
per-call guard structurally cannot supply.

---

## Part B — The staging protocol

Before deep reads sprawl or any write, the agent MUST commit a provisional thesis
via a `stage_proposal` call. Its shape:

```
stage_proposal(
  thesis: string,              # answer-first provisional conclusion
  hypotheses: [string],        # 2–3, falsifiable
  evidence_plan: [string],     # smallest reads that test/kill the thesis
  intended_write: string?,     # path/action the run is heading toward
  cost_class: string?,         # declared spend class
  kill_criteria: [string]?,    # what would abandon the thesis
)
```

**Semantics a conforming harness MUST provide:**

1. **Gate.** With `require_staging` and a non-empty `writable_paths`, a write /
   commit / bash call before the first `stage_proposal` is **refused** with an
   instruction to stage first. Orientation reads are allowed up to
   `max_unstaged_reads`, then further deep reads are refused until staging.
2. **Resume-from-stage.** When a write is later refused (e.g. path or cardinality),
   the run resumes **from the staged thesis and its reads**, not from a fresh
   research phase. This is the *staging pivot* — the primitive that distinguishes
   Boundary from policy-authz siblings, and (with the verdict format) the narrow
   defensible core the kill condition leaves standing.
3. **Provenance.** The stage records the taint set that fed the thesis, so Part C
   can see whether a conclusion rested on untrusted content.

---

## Part C — The verdict format (`boundary.third-umpire/v1`)

A run is graded by **property checks against the envelope spec**, not against the
agent's prose. The verdict is a stable, versioned, machine-readable document — the
"evidence of runtime enforcement" a CI gate or auditor consumes.

```json
{
  "schema": "boundary.third-umpire/v1",
  "verdict": "PASS | WARN | FAIL",
  "transcript_path": "…",
  "summary": { "writes_executed": 1, "estimated_dollars": 0.01, "model": "…",
               "sandbox_driver": "…", "…": "…" },
  "checks": [
    { "name": "produced_output", "passed": false, "severity": "fail",
      "detail": "1 write(s) executed but min_writes=2 required — run under-delivered against its liveness floor" }
  ]
}
```

**Rules:**

- `verdict` is `FAIL` if any check with `severity:"fail"` did not pass; else `WARN`
  if any `warn` check did not pass; else `PASS`.
- `checks[].name` values are stable identifiers (e.g. `writes_inside_allowlist`,
  `produced_output`, `spend_pacing`, `egress_uncontained`, `taint_flow`,
  `taint_egress`, `budget_halt`, `cache_utilization`, `thrashing`). New checks MAY
  be added within `v1`; removing or repurposing a name requires a version bump.
- Produced by `boundary third-umpire <transcript> --format json`.
- A conforming implementation MAY emit additional summary keys; consumers MUST
  ignore unknown keys (forward-compatibility within `v1`).

---

## Conformance

- **Level 1 — Enforced envelope.** Implements Part A tool-layer refusals over its
  own tool-call loop (the `srt`/hooks substitution target reaches here).
- **Level 2 — Staged.** Adds Part B, including resume-from-stage (mid-run state the
  agent loop must carry — where a stateless hook layer stops).
- **Level 3 — Graded.** Emits a Part C `boundary.third-umpire/v1` verdict.

The competitive point: Level 1 is commodity. **Levels 2–3 are the vocabulary worth
standardizing** — and a harness that implements them is speaking Boundary's
contract, whoever ships the engine.

---

## Status / non-goals

- **Draft.** Field names track the current `Envelope` dataclass and
  `stage_proposal` tool; a frozen `v1` of Part A/B awaits the same 1.0 milestone
  that freezes the API (the information-flow/taint leg).
- **Not a policy language.** This is a fixed typed contract (allowlist + cardinality
  + spend + commit/taint policy), deliberately narrower than a general policy engine
  (OPA/Rego, Progent DSL). Narrowness is the point — it is small enough to be a
  standard.
- **Prompt injection is out of scope** (inherent, audit F12). The contract bounds
  the *consequences* of a steered agent; it does not prevent steering.
