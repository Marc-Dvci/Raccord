"""Authenticated HTTPS edge for the official Grafana MCP Cloud Run sidecar.

The open-source ``grafana/mcp-grafana`` process owns the Grafana credential and
listens only inside the Cloud Run instance. This proxy is the ingress container:
it authenticates callers with a separate high-entropy bearer token, strips that
credential, and forwards MCP's streaming HTTP exchange to the sidecar.

Cloud Run terminates TLS. The hop from this proxy to 127.0.0.1 never leaves the
instance, so the Grafana service-account token is neither exposed to clients nor
confused with the OAuth token expected by Grafana's hosted MCP endpoint.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import get_settings

app = FastAPI(title="Raccord Grafana MCP gateway", docs_url=None, redoc_url=None)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _authorized(header: str | None, expected: str) -> bool:
    if not expected or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header.removeprefix("Bearer ").strip(), expected)


def _request_headers(request: Request) -> dict[str, str]:
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP | {"host", "content-length", "authorization"}
    }


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _HOP_BY_HOP | {"content-length", "content-encoding"}
    }


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    settings = get_settings()
    if not settings.mcp_gateway_token:
        return JSONResponse({"status": "misconfigured", "detail": "gateway token missing"}, 503)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(settings.mcp_upstream_health_url)
    except httpx.HTTPError as exc:
        return JSONResponse({"status": "unavailable", "detail": type(exc).__name__}, 503)
    if response.status_code >= 500:
        return JSONResponse({"status": "unavailable", "upstream": response.status_code}, 503)
    return JSONResponse({"status": "ready", "upstream": response.status_code})


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def proxy_mcp(request: Request):
    settings = get_settings()
    if not _authorized(request.headers.get("authorization"), settings.mcp_gateway_token):
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)
    )
    upstream_request = client.build_request(
        request.method,
        settings.mcp_upstream_url,
        headers=_request_headers(request),
        content=await request.body(),
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse(
            {"error": "mcp_upstream_unavailable", "detail": type(exc).__name__},
            status_code=502,
        )

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )
