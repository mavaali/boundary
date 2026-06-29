"""fabricspecs_questions — a Discover source scoped to one PM's specs.

Scans a FabricSpecs-style repo for specs that are (a) frontmatter-marked
`discoverable: true` and (b) owned by a given `owner:`, then extracts the
*unanswered* rows from each spec's `## Open Questions` section and emits one
DiscoveredTask per open question (bounded per spec).

This is what scopes the loop to MY specs rather than the whole repo:
ownership + discoverability live in frontmatter (see the SpecWriter Phase-0
change), and generated dirs are hard-excluded so the loop never mines its own
wiki/synthesis output.

Registered into discover.SOURCES as "fabricspecs_questions".
"""
from __future__ import annotations

import re
from pathlib import Path

from boundary.discover import DiscoveredTask, SOURCES

# Hard excludes: generated/agent output and non-spec scaffolding.
_EXCL = re.compile(
    r"/(wiki|syntheses|entities|causal|tools|blogs|_template|archive|"
    r"\.squad|\.github|\.maintenance|skills|logs|eval|node_modules|"
    r"react-prototype|figma-prompts)/",
    re.I,
)
# Section header: "## Open Questions" allowing a leading number ("## 18. Open Questions").
_OQ_HEADER = re.compile(r"^#{2,4}\s*(?:\d+\.\s*)?.*open questions\b.*$", re.I)
_NEXT_HEADER = re.compile(r"^#{1,4}\s+\S")
_RESOLVED_HDR = re.compile(r"resolved", re.I)


def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            out[m.group(1).lower()] = m.group(2).strip()
    return out


def _open_questions_block(text: str) -> str:
    """Return the lines under the first `## Open Questions` header, stopping at
    the next header (including a `### Resolved...` subsection)."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if _OQ_HEADER.match(ln):
            start = i + 1
            break
    if start is None:
        return ""
    out = []
    for ln in lines[start:]:
        if _NEXT_HEADER.match(ln):
            if _RESOLVED_HDR.search(ln):  # stop at "Resolved questions" subsection
                break
            if not _OQ_HEADER.match(ln):
                break
        out.append(ln)
    return "\n".join(out)


def _extract_open(block: str, *, max_per_spec: int) -> list[str]:
    """Pull unanswered questions from an Open Questions block.

    Supports two shapes:
      - markdown table with a Status column (row counts only if Status == Open)
      - bullet list (each non-empty bullet counts, minus '_none yet_' stubs)
    """
    questions: list[str] = []
    rows = [r for r in block.splitlines() if r.strip().startswith("|")]
    if len(rows) >= 2:
        header = [c.strip().lower() for c in rows[0].strip("|").split("|")]
        # locate question + status columns
        q_idx = next((i for i, c in enumerate(header) if "question" in c), 1)
        s_idx = next((i for i, c in enumerate(header) if "status" in c), None)
        for r in rows[2:]:  # skip header + separator
            cols = [c.strip() for c in r.strip("|").split("|")]
            if len(cols) <= q_idx:
                continue
            q = cols[q_idx]
            if not q or q in {"#", "-"}:
                continue
            if s_idx is not None and s_idx < len(cols):
                if "open" not in cols[s_idx].lower():
                    continue
            questions.append(q)
            if len(questions) >= max_per_spec:
                break
        return questions
    # bullet fallback
    for ln in block.splitlines():
        s = ln.strip()
        if s.startswith(("- ", "* ")) and "_none yet_" not in s.lower():
            questions.append(s[2:].strip())
            if len(questions) >= max_per_spec:
                break
    return questions


def scan_fabricspecs_questions(
    workspace,
    *,
    owner: str = "mihirwagle",
    max_tasks: int = 25,
    max_per_spec: int = 5,
    require_discoverable: bool = True,
) -> list[DiscoveredTask]:
    ws = Path(workspace).expanduser()
    out: list[DiscoveredTask] = []
    for f in ws.rglob("*.md"):
        # Normalize to POSIX separators so the exclusion regex matches on Windows
        # (where rglob yields backslash paths) as well as POSIX systems.
        if _EXCL.search("/" + f.as_posix()):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        if not fm:
            continue
        if owner and fm.get("owner", "").lower() != owner.lower():
            continue
        if require_discoverable and fm.get("discoverable", "").lower() != "true":
            continue
        block = _open_questions_block(text)
        if not block:
            continue
        rel = f.relative_to(ws).as_posix()
        for q in _extract_open(block, max_per_spec=max_per_spec):
            out.append(DiscoveredTask(
                source="fabricspecs_questions",
                title=f"[{f.stem}] {q[:90]}",
                detail=f"Open question in spec `{rel}` (owner: {owner}):\n\n> {q}\n\n"
                       f"Investigate and propose a resolution. Cite sources.",
                origin=rel,
            ))
            if len(out) >= max_tasks:
                return out
    return out


SOURCES["fabricspecs_questions"] = scan_fabricspecs_questions
