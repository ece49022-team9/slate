"""Task lifecycle as a finite state machine.

The whole lifecycle is the TRANSITIONS table: (state, event) -> next state.
Any pair that is not in the table is an illegal transition and is rejected.
Terminal states have no outgoing edges, so once a task is finished it stays
finished.
"""

from enum import StrEnum


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class Event(StrEnum):
    START = "start"
    REQUEST_APPROVAL = "request_approval"
    APPROVE = "approve"
    DENY = "deny"
    SUCCEED = "succeed"
    FAIL = "fail"
    CANCEL = "cancel"
    TIMEOUT = "timeout"


S, E = State, Event

TRANSITIONS: dict[tuple[State, Event], State] = {
    (S.QUEUED, E.START): S.RUNNING,
    (S.QUEUED, E.CANCEL): S.CANCELLED,
    (S.RUNNING, E.REQUEST_APPROVAL): S.AWAITING_APPROVAL,
    (S.RUNNING, E.SUCCEED): S.SUCCEEDED,
    (S.RUNNING, E.FAIL): S.FAILED,
    (S.RUNNING, E.CANCEL): S.CANCELLED,
    (S.RUNNING, E.TIMEOUT): S.TIMED_OUT,
    (S.AWAITING_APPROVAL, E.APPROVE): S.RUNNING,
    (S.AWAITING_APPROVAL, E.DENY): S.CANCELLED,
    (S.AWAITING_APPROVAL, E.CANCEL): S.CANCELLED,
    (S.AWAITING_APPROVAL, E.TIMEOUT): S.TIMED_OUT,
}

TERMINAL = frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED, S.TIMED_OUT})
ACTIVE = frozenset(State) - TERMINAL


class InvalidTransition(Exception):
    def __init__(self, state: State, event: Event) -> None:
        super().__init__(f"cannot {event.value} a task that is {state.value}")
        self.state = state
        self.event = event


def next_state(state: State, event: Event) -> State:
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition(state, event) from None
