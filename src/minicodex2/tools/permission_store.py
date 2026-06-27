from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class PermissionRequest:
    id: str
    action: str
    reason: str
    payload: dict[str, Any]
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PermissionStore:
    def __init__(self) -> None:
        self._requests: dict[str, PermissionRequest] = {}

    def create(self, action: str, reason: str, payload: dict[str, Any]) -> PermissionRequest:
        request = PermissionRequest(
            id=f"perm_{uuid4().hex[:12]}",
            action=action,
            reason=reason,
            payload=payload,
        )
        self._requests[request.id] = request
        return request

    def resolve(self, request_id: str, approved: bool) -> PermissionRequest:
        request = self._requests[request_id]
        request.status = "approved" if approved else "denied"
        return request

    def consume_approval(self, action: str, payload: dict[str, Any]) -> PermissionRequest | None:
        for request in self._requests.values():
            if request.status == "approved" and request.action == action and request.payload == payload:
                request.status = "consumed"
                return request
        return None

    def pending(self) -> list[PermissionRequest]:
        return [request for request in self._requests.values() if request.status == "pending"]
