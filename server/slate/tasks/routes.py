from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from slate.access.routes import require_owner
from slate.tasks.machine import InvalidTransition
from slate.tasks.manager import TaskManager
from slate.tasks.store import (
    ApprovalClosed,
    ApprovalNotFound,
    StaleVersion,
    TaskNotFound,
)

router = APIRouter(tags=["tasks"], dependencies=[Depends(require_owner)])


def manager(request: Request) -> TaskManager:
    return request.app.state.tasks


Tasks = Annotated[TaskManager, Depends(manager)]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "deny"]
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
        raise HTTPException(404, "This task does not exist") from None
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
    return decide_or_raise(
        tasks,
        approval_id,
        body.decision == "approve",
        actor="web",
        expected_version=body.expected_version,
    )


def decide_or_raise(
    tasks: TaskManager,
    approval_id: str,
    approve: bool,
    *,
    actor: str,
    expected_version: int | None,
) -> dict[str, Any]:
    try:
        task, approval = tasks.decide(
            approval_id,
            approve,
            actor=actor,
            expected_version=expected_version,
        )
    except ApprovalNotFound:
        raise HTTPException(404, "This approval does not exist") from None
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
async def cancel_task(task_id: str, request: Request, tasks: Tasks) -> dict[str, Any]:
    task = cancel_or_raise(tasks, task_id, actor="web")
    session = request.app.state.voice.current
    if session is not None:
        await session.task_cancelled(task_id)
    return task


def cancel_or_raise(tasks: TaskManager, task_id: str, *, actor: str) -> dict[str, Any]:
    try:
        return tasks.cancel(task_id, actor=actor).to_dict()
    except TaskNotFound:
        raise HTTPException(404, "This task does not exist") from None
    except InvalidTransition as bad:
        raise HTTPException(
            409, {"error": "invalid_transition", "state": bad.state.value}
        ) from None
