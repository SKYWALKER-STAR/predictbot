from __future__ import annotations

import json
from typing import Any

from database.connection import get_session
from database.models import AuditLog


class AuditLogger:
    def record(self, action: str, result: str, details: dict[str, Any] | None = None, error: str | None = None) -> None:
        payload = details or {}
        with get_session() as session:
            session.add(
                AuditLog(
                    action=action,
                    result=result,
                    details=json.dumps(payload, ensure_ascii=True),
                    error=error,
                )
            )
