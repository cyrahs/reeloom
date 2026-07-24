from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from reeloom.runtime.events import RuntimeEvent
from reeloom.runtime.reducer import reduce_event
from reeloom.runtime.state import RunState


@dataclass(frozen=True, slots=True)
class StoredEvent:
    sequence: int
    event: RuntimeEvent


class EventStore(Protocol):
    """One run's append-only, replayable domain event boundary."""

    @property
    def state(self) -> RunState | None: ...

    @property
    def events(self) -> tuple[StoredEvent, ...]: ...

    def append(self, event: RuntimeEvent) -> RunState: ...

    def replay(self) -> RunState | None: ...


class InMemoryEventStore:
    """Append-only event storage for one run."""

    def __init__(self) -> None:
        self._events: list[StoredEvent] = []
        self._state: RunState | None = None

    @property
    def state(self) -> RunState | None:
        return self._state

    @property
    def events(self) -> tuple[StoredEvent, ...]:
        return tuple(self._events)

    def append(self, event: RuntimeEvent) -> RunState:
        next_state = reduce_event(self._state, event)
        stored = StoredEvent(sequence=len(self._events) + 1, event=event)
        self._events.append(stored)
        self._state = next_state
        return next_state

    def replay(self) -> RunState | None:
        state: RunState | None = None
        for stored in self._events:
            state = reduce_event(state, stored.event)
        return state
