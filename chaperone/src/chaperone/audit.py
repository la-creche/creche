"""Append-only JSONL audit, one record per call, daily files.

This log is the observability half of "control" and the reconstruction tool
after an injection incident, so a record is written for every decision —
allows and denials alike. `probe_writable` lets the app layer check the
directory *before* an allowed call executes, so a broken audit refuses the
call up front instead of letting the effect happen unrecorded. Once
execution has started, a write failure still means the effect happened but
could not be recorded (`AuditError` → 500)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("chaperone.audit")

#: 180 d retention. A sweep (app.py lifespan) deletes daily files
#: older than this; nothing else in the repo ever did.
AUDIT_RETENTION_DAYS = 180


class AuditError(RuntimeError):
    pass


class AuditLog:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, now: datetime) -> Path:
        return self._dir / f"{now.strftime('%Y-%m-%d')}.jsonl"

    def probe_writable(self) -> bool:
        """Open-then-close today's file without writing a line — a cheap
        pre-flight so the app layer can refuse an allowed call instead of
        running its effect against a directory that cannot record it."""
        try:
            fd = os.open(
                self.path_for(datetime.now(UTC)), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640
            )
            os.close(fd)
            return True
        except OSError:
            return False

    def sweep_retention(self, max_age_days: int = AUDIT_RETENTION_DAYS) -> None:
        """Delete daily files older than `max_age_days`. Only files named
        exactly `YYYY-MM-DD.jsonl` are touched — anything else is left alone
        rather than guessed at."""
        cutoff = datetime.now(UTC).date() - timedelta(days=max_age_days)
        for path in self._dir.glob("*.jsonl"):
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)

    def write(self, record: dict[str, object]) -> None:
        now = datetime.now(UTC)
        record = {"ts": now.isoformat(timespec="milliseconds"), **record}
        line = json.dumps(record, ensure_ascii=False, default=repr)
        path = self.path_for(now)
        try:
            # 0640 regardless of umask: full args live here; the `agents`
            # group (the operator) reads, nobody else.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            raise AuditError(f"audit write failed: {exc}") from exc
