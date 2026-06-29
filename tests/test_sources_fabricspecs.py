from pathlib import Path

from boundary.discover import discover
from boundary.sources_fabricspecs import (
    scan_fabricspecs_questions, _parse_frontmatter, _open_questions_block, _extract_open,
)

SPEC = """---
title: PQ Actions spec
owner: mihirwagle
discoverable: true
created: 2026-06-18
---

# PQ Actions

## 18. Open Questions

| # | Question | Owner | Status | Resolution |
|---|----------|-------|--------|------------|
| 1 | What is the task cap per workspace? | Mihir | Open | candidates 10/25 |
| 2 | Already-decided thing? | Mihir | Resolved | done |
| 3 | Should cadence be tier-gated? | _TODO_ | Open | |

### Resolved questions (now Decisions)
| 9 | this is resolved | x | Resolved | y |
"""

NOTE = """---
owner: mihirwagle
discoverable: false
---
## Open Questions
| # | Question | Status |
|---|---|---|
| 1 | should not surface | Open |
"""

OTHER = """---
owner: miescobar
discoverable: true
---
## Open Questions
- someone else's question
"""


def _mk(tmp_path):
    (tmp_path / "spec.md").write_text(SPEC)
    (tmp_path / "note.md").write_text(NOTE)
    (tmp_path / "other.md").write_text(OTHER)
    wiki = tmp_path / "wiki"; wiki.mkdir()
    (wiki / "gen.md").write_text(SPEC)  # generated dir -> excluded
    return tmp_path


def test_parse_frontmatter():
    fm = _parse_frontmatter(SPEC)
    assert fm["owner"] == "mihirwagle" and fm["discoverable"] == "true"


def test_extract_open_only_open_rows():
    block = _open_questions_block(SPEC)
    qs = _extract_open(block, max_per_spec=10)
    assert any("task cap" in q for q in qs)
    assert any("cadence" in q for q in qs)
    assert not any("Already-decided" in q for q in qs)  # Resolved excluded
    assert not any("resolved" in q.lower() for q in qs)  # Resolved subsection excluded


def test_scope_owner_and_discoverable(tmp_path):
    tasks = scan_fabricspecs_questions(_mk(tmp_path), owner="mihirwagle")
    origins = {t.origin for t in tasks}
    assert "spec.md" in origins          # mine + discoverable
    assert "note.md" not in origins      # mine but discoverable:false
    assert "other.md" not in origins     # discoverable but not mine
    assert all("wiki" not in o for o in origins)  # generated dir excluded


def test_two_open_questions_from_spec(tmp_path):
    tasks = scan_fabricspecs_questions(_mk(tmp_path), owner="mihirwagle")
    assert len(tasks) == 2  # only the 2 Open rows, not Resolved


def test_registered_in_sources(tmp_path):
    tasks = discover(_mk(tmp_path), source="fabricspecs_questions", owner="mihirwagle")
    assert len(tasks) == 2


def test_max_per_spec_cap(tmp_path):
    tasks = scan_fabricspecs_questions(_mk(tmp_path), owner="mihirwagle", max_per_spec=1)
    assert len([t for t in tasks if t.origin == "spec.md"]) == 1
