"""The kind of a family (contract 02 §4.2, contract 05 §2.1)."""

from __future__ import annotations

from enum import StrEnum


class SessionKind(StrEnum):
    """The family's kind. A door token reaches one kind (contract 02 §3.1)."""

    ATTENDED = "attended"
    THIN = "thin"
    AUTONOMOUS = "autonomous"
