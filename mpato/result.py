from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Result:
    success: bool
    data: Any = None
    status_code: Optional[int] = None
    error: Optional[str] = None
    raw: Optional[bytes] = None

    def raise_for_error(self):
        if not self.success:
            raise RuntimeError(self.error or "Call failed")
        return self

    def __repr__(self):
        if self.success:
            return f"Result(success=True, status_code={self.status_code})"
        return f"Result(success=False, error={self.error!r})"
