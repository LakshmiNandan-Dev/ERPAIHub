from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ValidationResult:
    name: str
    ok: Optional[bool]                 # True/False, or None if skipped
    issues: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.ok is None:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"
