import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from slate.access.credentials import BadSecret, CredentialNotFound, CredentialStore
from slate.api.tasks import require_local

MAX_BODY = 32 * 1024
MAX_NAME = 120

router = APIRouter(tags=["credentials"], dependencies=[Depends(require_local)])


def store(request: Request) -> CredentialStore:
    return request.app.state.credentials


Store = Annotated[CredentialStore, Depends(store)]


async def body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "The request body is too large")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(422, "Send the body as JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(422, "Send the body as a JSON object")
    return data


Body = Annotated[dict[str, Any], Depends(body)]


def text(data: dict[str, Any], name: str, *, required: bool) -> str | None:
    value = data.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NAME:
        raise HTTPException(422, f"{name} must be 1 to {MAX_NAME} characters")
    return value


def found(credentials: CredentialStore, credential_id: str):
    try:
        return credentials.get(credential_id)
    except CredentialNotFound:
        raise HTTPException(404, "This credential does not exist") from None


@router.post("/credentials", status_code=201)
def add(data: Body, credentials: Store) -> dict[str, Any]:
    try:
        made = credentials.add(
            service=text(data, "service", required=True) or "",
            label=text(data, "label", required=True) or "",
            kind=str(data.get("kind")),
            username=text(data, "username", required=False),
            secret=data.get("secret"),
        )
    except BadSecret as bad:
        raise HTTPException(422, str(bad)) from None
    return made.to_dict()


@router.get("/credentials")
def list_credentials(credentials: Store) -> list[dict[str, Any]]:
    return [item.to_dict() for item in credentials.all()]


@router.get("/credentials/{credential_id}")
def detail(
    credential_id: str,
    credentials: Store,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    item = found(credentials, credential_id)
    return {
        "credential": item.to_dict(),
        "uses": [use.to_dict() for use in credentials.uses(credential_id, limit)],
    }


@router.patch("/credentials/{credential_id}")
def rename(credential_id: str, data: Body, credentials: Store) -> dict[str, Any]:
    found(credentials, credential_id)
    return credentials.rename(
        credential_id,
        label=text(data, "label", required=False),
        username=text(data, "username", required=False),
    ).to_dict()


@router.put("/credentials/{credential_id}/secret")
def replace(credential_id: str, data: Body, credentials: Store) -> dict[str, Any]:
    found(credentials, credential_id)
    try:
        return credentials.replace_secret(credential_id, data.get("secret")).to_dict()
    except BadSecret as bad:
        raise HTTPException(422, str(bad)) from None


@router.delete("/credentials/{credential_id}", status_code=204)
def delete(credential_id: str, credentials: Store) -> None:
    found(credentials, credential_id)
    credentials.delete(credential_id)
