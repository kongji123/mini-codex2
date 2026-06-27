from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from minicodex2.agent.logging import RuntimeLogger


@dataclass(slots=True)
class AgentEvent:
    type: str
    session_id: str
    turn_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"event_{uuid4().hex}")
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentEventBus:
    def __init__(self, session_id: str, logger: RuntimeLogger | None = None) -> None:
        self.session_id = session_id
        self._events: list[AgentEvent] = []
        self.logger = logger

    def emit(
        self, event_type: str, payload: dict[str, Any] | None = None, turn_id: str | None = None
    ) -> AgentEvent:
        event = AgentEvent(
            type=event_type,
            session_id=self.session_id,
            turn_id=turn_id,
            payload=payload or {},
        )
        self._events.append(event)
        if self.logger:
            self.logger.event(event_type, event.payload)
        return event

    def events(self) -> list[AgentEvent]:
        return list(self._events)
