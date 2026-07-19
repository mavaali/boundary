"""Run receipts — `boundary.receipt/v1`.

A receipt binds two artifacts Boundary already produces independently:

  - the **policy**: `Envelope.spec_dict()` + its canonical `spec_hash()`
  - the **grade**: a `ThirdUmpireReport` exported as `boundary.third-umpire/v1`

into one portable claim: *this run executed inside this exact envelope, and
here is the grade.* A verdict on its own says "the run was graded"; against
which policy is on trust. The receipt names the policy by hash and carries it,
so the claim is self-contained and checkable.

`verify_receipt` re-hashes the embedded spec (must equal `spec_hash`) and, when
the transcript is still present, re-grades it (verdict must match, and the
transcript's own recorded `spec_hash` must equal the receipt's — so a receipt
can't be re-pointed at a different run). It is self-reported, not cryptographic
provenance: the value is an audit trail and a CI/merge gate, and signing can be
added later without changing the schema.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from boundary.envelope import canonical_spec_hash
from boundary.third_umpire import ThirdUmpire, ThirdUmpireReport

SCHEMA = "boundary.receipt/v1"


@dataclass
class Receipt:
    spec_hash: str
    spec: dict
    verdict: dict  # a boundary.third-umpire/v1 document
    run_id: int | None = None
    schedule_name: str | None = None
    model: str | None = None
    estimated_dollars: float = 0.0
    transcript_path: str | None = None
    created_at: int = 0

    SCHEMA = SCHEMA

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "spec_hash": self.spec_hash,
            "spec": self.spec,
            "verdict": self.verdict,
            "run_id": self.run_id,
            "schedule_name": self.schedule_name,
            "model": self.model,
            "estimated_dollars": self.estimated_dollars,
            "transcript_path": self.transcript_path,
            "created_at": self.created_at,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> Receipt:
        return cls(
            spec_hash=d["spec_hash"],
            spec=d["spec"],
            verdict=d["verdict"],
            run_id=d.get("run_id"),
            schedule_name=d.get("schedule_name"),
            model=d.get("model"),
            estimated_dollars=d.get("estimated_dollars", 0.0),
            transcript_path=d.get("transcript_path"),
            created_at=d.get("created_at", 0),
        )

    @classmethod
    def build(
        cls,
        report: ThirdUmpireReport,
        *,
        spec: dict,
        spec_hash: str,
        run_id: int | None = None,
        schedule_name: str | None = None,
        model: str | None = None,
        estimated_dollars: float = 0.0,
        transcript_path: str | None = None,
        created_at: int | None = None,
    ) -> Receipt:
        return cls(
            spec_hash=spec_hash,
            spec=spec,
            verdict=report.as_dict(),
            run_id=run_id,
            schedule_name=schedule_name,
            model=model,
            estimated_dollars=estimated_dollars,
            transcript_path=transcript_path,
            created_at=created_at if created_at is not None else int(time.time()),
        )


@dataclass
class VerifyResult:
    ok: bool
    spec_hash_ok: bool
    verdict_ok: bool
    detail: str


def verify_receipt(receipt: Receipt, *, regrade: bool = True) -> VerifyResult:
    """Re-derive the receipt's claims and confirm they still hold.

    1. Spec integrity — the embedded spec re-hashes to the stated `spec_hash`.
       Catches tampering with the stored policy.
    2. Verdict integrity — re-grading the transcript reproduces the stored
       verdict, AND the transcript's own recorded `spec_hash` equals the
       receipt's. Catches transcript tampering and receipt-to-transcript swaps.
       Skipped (not failed) when the transcript is absent.
    """
    parts: list[str] = []

    computed = canonical_spec_hash(receipt.spec)
    spec_hash_ok = computed == receipt.spec_hash
    parts.append("spec_hash ok" if spec_hash_ok
                 else f"spec_hash MISMATCH (stored {receipt.spec_hash[:12]}…, recomputed {computed[:12]}…)")

    verdict_ok = True
    tp = receipt.transcript_path
    if not regrade:
        parts.append("re-grade skipped (regrade=False)")
    elif not tp or not Path(tp).exists():
        parts.append("re-grade skipped: transcript unavailable")
    else:
        try:
            report = ThirdUmpire.grade(tp)
        except Exception as e:  # noqa: BLE001 - grading a foreign transcript
            verdict_ok = False
            parts.append(f"re-grade errored: {type(e).__name__}: {e}")
        else:
            regraded = report.verdict
            stored = (receipt.verdict or {}).get("verdict")
            transcript_hash = _transcript_spec_hash(tp)
            verdict_match = regraded == stored
            hash_match = transcript_hash is None or transcript_hash == receipt.spec_hash
            verdict_ok = verdict_match and hash_match
            if not verdict_match:
                parts.append(f"verdict MISMATCH (stored {stored}, re-graded {regraded})")
            elif not hash_match:
                parts.append(
                    f"transcript policy MISMATCH (transcript {str(transcript_hash)[:12]}…, "
                    f"receipt {receipt.spec_hash[:12]}…)")
            else:
                parts.append(f"verdict ok ({regraded})")

    return VerifyResult(
        ok=spec_hash_ok and verdict_ok,
        spec_hash_ok=spec_hash_ok,
        verdict_ok=verdict_ok,
        detail="; ".join(parts),
    )


def _transcript_spec_hash(transcript_path: str | Path) -> str | None:
    """The spec_hash the transcript recorded at envelope_start, if any."""
    try:
        for event in ThirdUmpire._load(transcript_path):
            if event.get("type") == "envelope_start":
                return event.get("spec_hash")
    except OSError:
        return None
    return None
