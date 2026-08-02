# Receipt aggregation contract — `boundary.receipt/v1` as a public interface

**Date:** 2026-08-01
**Audited against:** boundary v0.13.0 (`bf97990`), omnigent `d880afec` (v0.4.0.dev, cloned locally as the reference consumer)
**Status:** design — all changes additive within `boundary.receipt/v1`; no v2 required

## Why this spec exists

The receipt was built to answer *"was this run legitimate?"* — it binds the
policy that ran (`spec_dict` + `spec_hash`) to the grade it earned
(`boundary.third-umpire/v1`), and `verify_receipt` makes the claim checkable.
That per-run story is done (v3 Items 1–2).

The next consumer is not a verifier but an **aggregator**: a fleet- or
org-level governance layer that folds receipts into "spend by task class,
with verdicts, this month." Boundary does not build that layer; it emits the
artifact that layer ingests. That makes the receipt schema a public
interface, and this spec audits it against the aggregation contract while
the schema is at v1 and cheap to change.

The reference consumer is omnigent, whose governance model aggregates by
**user + UTC day + session labels** (`ActorContext.run_as`,
`UserDailyCostContext`, the reserved `cost_control.*` label namespace on
sessions). Where this spec says "the consumer needs X," omnigent is the
concrete X-needer; the fields stay consumer-neutral.

## The aggregation contract

A receipt store, with no access to the emitting machine, must answer:

- **Q1 — spend by dimension over time.** Dollars by tenant / persona / task
  class / model / day.
- **Q2 — spend vs. policy.** Headroom against `max_dollars`; cap-hit rate.
- **Q3 — cost per verified outcome.** Spend joined to PASS/WARN/FAIL and to
  individual check results (floor met, taint clean).
- **Q4 — tier legibility.** Which model did the work; whether degradation
  fired; whether the dollar figure is a real price or a conservative bound.
- **Q5 — identity and merge.** Dedupe and join receipts from many machines
  without collisions or trust in file paths.

## Audit of v1 against the contract

### Already answered

- **Q2** works today: `verdict.summary.estimated_dollars` and
  `spec.max_dollars` are both in the receipt.
- **Q3** works per run: `verdict.verdict` plus stable check names.
- `spec_hash` is a free, high-value aggregation key — "all runs under this
  exact policy" — and the versioning posture is right (schema id,
  `spec_version` inside the spec, additive `from_dict`, signing deferred
  without schema impact).

### Gaps, ranked by cost-to-retrofit

1. **Emission coverage: interactive runs never get receipts.** Only
   `run_headless` builds one (pipelines inherit it — steps route through
   `run_headless`). `boundary run` records an adhoc history row with
   `third_umpire_verdict=None` and no receipt (`cli._record_adhoc_run`);
   the multirun path likewise. Interactive spend is exactly the regime
   where fleet cost-legibility dies first, and every unreceipted run is
   data lost permanently.
2. **The aggregation dimensions live in the wrong store.** `history.db`
   carries `persona`, `workspace`, and free-form `attribution` tags — and
   already supports tag-scoped budget sums. The receipt drops all three.
   The portable artifact cannot be grouped by tenant/team/task class; its
   only classifier, `schedule_name`, is `None` for adhoc runs. (Q1 fails.)
3. **No global identity.** `run_id` is a rowid in a machine-local SQLite
   file; two machines collide. No receipt id, no host, no
   `started_at`/`ended_at` (only `created_at`, the build time). (Q5 fails;
   Q1's time axis is the wrong timestamp.)
4. **Pricing basis is recorded nowhere.** `token_rates` is deliberately
   excluded from `spec_dict()` (correct — a rate-card update must not
   change the policy hash), but nothing else records whether
   `estimated_dollars` came from a real rate card or the fail-closed
   most-expensive-rate bound. This matters doubly because the reference
   consumer's convention is the opposite of ours: omnigent's
   `total_cost_usd` is `0.0` when pricing is unavailable, boundary books
   the conservative maximum. Mixing the two without a marker silently
   overcounts org rollups. (Q4 fails.)
5. **Two spend fields, no canonical one.** Top-level `estimated_dollars`
   (headless loop variable) and `verdict.summary.estimated_dollars`
   (envelope_end, rounded) can drift; the build site already shows the
   seam (model is read from the summary, dollars from the local).
6. **Model is single-string and lossy.** `degrade_to`/`degrade_at` means
   multi-model runs, but only the final model is recorded, with no
   per-model token/dollar split — Q4's "what did each tier cost" is
   unanswerable precisely when the tier-policy feature fires.

Minor, documentation-only: currency is implied USD; `transcript_path` and
`writable_paths` are machine-absolute evidence pointers, not join keys.

## Design

### Decision 1 — additive fields, still v1

`from_dict` reads optional fields with `.get()`; existing receipts stay
valid and verifiable. New top-level fields:

| Field | Type | Source | Contract question |
| :--- | :--- | :--- | :--- |
| `receipt_id` | str, `uuid4` | minted at build | Q5 — global identity |
| `host` | str | `platform.node()` | Q5 — merge/debug locality |
| `started_at` / `ended_at` | float, unix UTC | already threaded to `record_run` | Q1 — the real time axis |
| `persona` | str \| null | run config | Q1 — task-class dimension |
| `workspace` | str \| null | run config | Q1 — evidence + scoping |
| `attribution` | dict[str,str] | `--attribution` / schedule config | Q1 — tenant/team/label dimension |
| `pricing` | dict, below | envelope loop | Q4 — is the number real |

`pricing` (v1 shape):

```json
{"rate_source": "rate_card" | "fail_closed_max" | "unavailable",
 "currency": "USD"}
```

`rate_source` is the load-bearing member: `fail_closed_max` marks a
conservative bound, so a consumer that (like omnigent) treats unpriced as
0.0 can segregate bounded estimates instead of summing them as actuals.
The shape leaves room for a later `per_model` breakdown without renaming.

### Decision 2 — one canonical spend number

Top-level `estimated_dollars` is authoritative for aggregation;
`verdict.summary.estimated_dollars` is evidence (what the grader saw),
compared but never summed. Stated in the module docstring, and the build
site asserts they agree within rounding — drift becomes a test failure,
not a data-quality mystery downstream.

### Decision 3 — emit receipts everywhere a verdict exists

- `boundary run` (envelope mode): grade the transcript, build the receipt,
  store via `history.set_receipt`, write the `.receipt.json` sibling —
  the same best-effort contract as headless (a receipt failure never
  fails the run).
- multirun: same, per member run.
- Runs with no transcript/grade emit nothing: a receipt without a verdict
  has no claim to make. That is the one honest coverage hole, and it is a
  property of the run mode, not the schema.

### Decision 4 — per-model split is deferred, deliberately

Q4's full form (tokens/dollars per model when degradation fires) needs
loop instrumentation at every model switch, not a schema change. The
`pricing` dict reserves the seam (`per_model` key). Not in this spec's
implementation; do not block the additive fields on it.

### Non-goals

- **No reporting layer in boundary.** The fold over receipts is the
  consumer's product. Boundary's job ends at emitting a receipt that can
  be folded.
- **No signing.** Unchanged from the v1 posture: schema-compatible later.
- **No omnigent adapter.** The mapping is documented below so the schema
  is provably sufficient, but shipping an integration is a separate
  decision with its own spec.

## Omnigent mapping (proof the fields suffice)

| Omnigent concept | Receipt field |
| :--- | :--- |
| session label map (`{"team": "ml"}`) | `attribution` |
| `ActorContext.run_as` | `attribution["user"]` by convention |
| `UserDailyCostContext` (per-user, per-UTC-day) | `attribution["user"]` × `started_at` (UTC) × `estimated_dollars` |
| `total_cost_usd == 0.0` when unpriced | `pricing.rate_source` distinguishes real / bounded / unavailable |
| session / conversation id join | `receipt_id` + `host` as the foreign run identity |

## Testing

- Round-trip: `from_dict(as_dict())` with and without every new field;
  pre-this-spec receipt JSON still loads and verifies.
- `verify_receipt` is unaffected by new fields (they are claims about
  context, not about policy or verdict — integrity scope unchanged).
- Emission: interactive envelope-mode run produces a stored + sibling
  receipt whose `spec_hash` verifies; multirun produces one per member.
- Drift assertion: build-site agreement between the two dollar figures.
- Selftest: extend the receipt guarantee check to assert the new fields
  survive a build/verify cycle, so a regression fails `boundary selftest`.

## Sequencing

1. Schema additions + canonical-spend assertion (pure `receipt.py`, one PR).
2. Interactive + multirun emission (touches `cli.py`, `multirun.py`).
3. `pricing.per_model` instrumentation — separate future spec, after the
   loop grows per-model accounting.

Item 2 is the one bleeding data daily; item 1 must land first or the new
emitters write receipts missing the dimensions that motivated them.
