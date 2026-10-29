from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from slate.api.events import LOCAL_HOSTS, LOCAL_ORIGINS
from slate.tasks.machine import InvalidTransition
from slate.tasks.manager import TaskManager
from slate.tasks.store import (
    ApprovalClosed,
    ApprovalNotFound,
    StaleVersion,
    TaskNotFound,
)


def require_local(request: Request) -> None:
    """Same rule as the event socket: only the web app on this machine."""
    origin = request.headers.get("origin")
    if (
        request.client is None
        or request.client.host not in LOCAL_HOSTS
        or (origin is not None and origin not in LOCAL_ORIGINS)
    ):
        raise HTTPException(403, "only the local web app can use this")


router = APIRouter(tags=["tasks"], dependencies=[Depends(require_local)])


def manager(request: Request) -> TaskManager:
    return request.app.state.tasks


Tasks = Annotated[TaskManager, Depends(manager)]


class Decision(BaseModel):
    decision: Literal["approve", "deny"]
    source: Literal["web", "device"] = "web"
    # The task version the user was looking at. If the task changed since,
    # the decision is refused instead of applied to something they did not see.
    expected_version: int | None = None


@router.get("/tasks")
def list_tasks(
    tasks: Tasks,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    return [task.to_dict() for task in tasks.store.recent(limit)]


@router.get("/tasks/{task_id}")
def get_task(task_id: str, tasks: Tasks) -> dict[str, Any]:
    try:
        task = tasks.store.get(task_id)
    except TaskNotFound:
        raise HTTPException(404, "no such task") from None
    return {
        "task": task.to_dict(),
        "history": [step.to_dict() for step in tasks.store.history(task_id)],
        "approvals": [item.to_dict() for item in tasks.store.approvals(task_id)],
    }


@router.post("/approvals/{approval_id}")
def decide(
    approval_id: str,
    body: Decision,
    tasks: Tasks,
) -> dict[str, Any]:
    try:
        task, approval = tasks.decide(
            approval_id,
            body.decision == "approve",
            actor=body.source,
            expected_version=body.expected_version,
        )
    except ApprovalNotFound:
        raise HTTPException(404, "no such approval") from None
    except ApprovalClosed as closed:
        raise HTTPException(
            409,
            {
                "error": "approval_closed",
                "status": closed.approval.status,
                "decided_by": closed.approval.decided_by,
            },
        ) from None
    except StaleVersion as stale:
        raise HTTPException(
            409,
            {
                "error": "stale_version",
                "expected": stale.expected,
                "actual": stale.actual,
            },
        ) from None
    return {"task": task.to_dict(), "approval": approval.to_dict()}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, tasks: Tasks) -> dict[str, Any]:
    try:
        return tasks.cancel(task_id, actor="web").to_dict()
    except TaskNotFound:
        raise HTTPException(404, "no such task") from None
    except InvalidTransition as bad:
        raise HTTPException(
            409, {"error": "invalid_transition", "state": bad.state.value}
        ) from None
