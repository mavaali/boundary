from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class Transcript:
    def __init__(self, path: str | Path | None = None, agent_name: str = "agent"):
        if path is None:
            base = Path.home() / ".boundary" / "transcripts"
            base.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            path = base / f"{ts}-{agent_name}-{os.getpid()}.jsonl"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def log(self, event_type: str, **data: Any) -> None:
        rec = {"ts": time.time(), "type": event_type, **data}
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
