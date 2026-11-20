"""Files in DATA_DIR: seen.json (processed jobs), jobs.jsonl (every evaluation), labels.jsonl (👍/👎),
state.json (source fetch times) and watch.log."""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seen_path, self.jobs_path = self.dir / "seen.json", self.dir / "jobs.jsonl"
        self.labels_path, self.state_path = self.dir / "labels.jsonl", self.dir / "state.json"
        self.log_path = self.dir / "watch.log"

    def load_seen(self) -> set[str]:
        return set(json.loads(self.seen_path.read_text())) if self.seen_path.exists() else set()

    def save_seen(self, seen: set[str]) -> None:
        self.seen_path.write_text(json.dumps(sorted(seen)))

    def load_state(self) -> dict:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def save_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state))

    def add_job(self, record: dict) -> None:
        with self.jobs_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%F %T"), **record}, ensure_ascii=False) + "\n")

    def jobs(self) -> list[dict]:
        return _rows(self.jobs_path)

    def add_label(self, job_id: str, good: bool) -> None:
        with self.labels_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": job_id, "good": good, "ts": time.strftime("%F %T")}) + "\n")

    def labels(self) -> dict[str, bool]:
        return {r["id"]: r["good"] for r in _rows(self.labels_path)}  # last label per id wins


def setup_logging(log_file: Path | None = None) -> logging.Logger:
    log = logging.getLogger("vacancy_watch")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = False  # hiring-agent configures the root logger; avoid printing twice
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(out)
    if log_file:
        f = logging.FileHandler(log_file, encoding="utf-8")
        f.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(f)
    return log
