from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .time_utils import now_iso


@dataclass
class Checkpoint:
    run_id: str
    creator_id: str
    discovered_note_ids: list[str] = field(default_factory=list)
    completed_note_ids: list[str] = field(default_factory=list)
    failed_note_ids: list[str] = field(default_factory=list)
    current_note_id: str | None = None
    updated_at: str = field(default_factory=now_iso)
    safe_stop_reason: str | None = None

    @classmethod
    def load_latest(cls, checkpoints_dir: Path, creator_id: str) -> "Checkpoint | None":
        files = sorted(checkpoints_dir.glob(f"*_{creator_id}.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return cls(**data)
        return None

    def save(self, checkpoints_dir: Path) -> Path:
        self.updated_at = now_iso()
        checkpoints_dir.mkdir(parents=True, exist_ok=True)
        path = checkpoints_dir / f"{self.run_id}_{self.creator_id}.json"
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, ensure_ascii=False, indent=2)
        tmp.replace(path)
        return path

