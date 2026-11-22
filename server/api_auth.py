#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''HTTP Bearer API-key checks for marker-server.'''

import hmac
import re
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from config import api_key

# Docs stay reachable so operators can open Swagger; OpenAPI calls still need
# Authorize once a key is configured. Conversion and /version always require it.
_PUBLIC_PATHS = frozenset({
    '/',
    '/docs',
    '/redoc',
    '/openapi.json'})

_BEARER_RE = re.compile(r'^Bearer\s+(\S+)\s*$', re.IGNORECASE)


def path_is_public(path: str) -> bool:
    cleaned = path.rstrip('/') or '/'
    return cleaned in _PUBLIC_PATHS


def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    match = _BEARER_RE.match(authorization.strip())
    return match.group(1) if match else None


def key_matches(provided: Optional[str], expected: str) -> bool:
    '''Constant-time compare; encode to bytes so non-ASCII never raises.'''
    if provided is None:
        return False
    return hmac.compare_digest(
        provided.encode('utf-8'), expected.encode('utf-8'))


def unauthorized(detail: str = 'Invalid or missing API key') -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={'detail': detail},
        headers={'WWW-Authenticate': 'Bearer'})


async def enforce_api_key(request: Request, call_next):
    expected = api_key()
    if not expected:
        return await call_next(request)
    if path_is_public(request.url.path):
        return await call_next(request)
    provided = extract_bearer(request.headers.get('authorization'))
    if not key_matches(provided, expected):
        return unauthorized()
    return await call_next(request)


def apply_openapi_security(schema: dict) -> dict:
    '''Mark all operations as Bearer-protected in the OpenAPI document.'''
    components = schema.setdefault('components', {})
    schemes = components.setdefault('securitySchemes', {})
    schemes['HTTPBearer'] = {
        'type': 'http',
        'scheme': 'bearer',
        'bearerFormat': 'API key',
        'description': (
            'Set auth.api_key in config.json (or MARKER_SERVER_API_KEY). '
            'Send Authorization: Bearer <key>.')}
    schema['security'] = [{'HTTPBearer': []}]
    return schema
