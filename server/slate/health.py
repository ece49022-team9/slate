from fastapi import APIRouter, Response

router = APIRouter(tags=["health"])


@router.get("/health", status_code=204, response_class=Response)
async def health() -> Response:
    return Response(status_code=204)
