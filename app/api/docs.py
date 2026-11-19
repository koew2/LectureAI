"""API documentation restricted to administrators."""

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.deps import require_docs_admin

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_docs_admin)])
DOCS_HEADERS = {"Cache-Control": "no-store"}
SCHEMA_PATH = "/openapi.json"
OAUTH_REDIRECT_PATH = "/docs/oauth2-redirect"


@router.get("/docs", response_class=HTMLResponse)
async def swagger_ui(request: Request) -> HTMLResponse:
    root = request.scope.get("root_path", "").rstrip("/")
    response = get_swagger_ui_html(
        openapi_url=f"{root}{SCHEMA_PATH}",
        title=f"{request.app.title} – Swagger UI",
        oauth2_redirect_url=f"{root}{OAUTH_REDIRECT_PATH}",
        swagger_ui_parameters={"validatorUrl": None},
    )
    response.headers.update(DOCS_HEADERS)
    return response


@router.get(SCHEMA_PATH)
async def openapi_schema(request: Request) -> JSONResponse:
    return JSONResponse(request.app.openapi(), headers=DOCS_HEADERS)


@router.get("/redoc", response_class=HTMLResponse)
async def redoc(request: Request) -> HTMLResponse:
    root = request.scope.get("root_path", "").rstrip("/")
    response = get_redoc_html(
        openapi_url=f"{root}{SCHEMA_PATH}",
        title=f"{request.app.title} – ReDoc",
    )
    response.headers.update(DOCS_HEADERS)
    return response


@router.get(OAUTH_REDIRECT_PATH, response_class=HTMLResponse)
async def swagger_oauth_redirect() -> HTMLResponse:
    response = get_swagger_ui_oauth2_redirect_html()
    response.headers.update(DOCS_HEADERS)
    return response
