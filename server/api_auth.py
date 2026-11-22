#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''HTTP Bearer / Basic API-key checks for marker-server.'''

import base64
import hmac
import re
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from config import api_key, ui_password

# Docs stay reachable so operators can open Swagger; OpenAPI calls still need
# Authorize once a key is configured. Conversion and /version always require it.
_PUBLIC_PATHS = frozenset({
    '/',
    '/docs',
    '/redoc',
    '/openapi.json'})

_BEARER_RE = re.compile(r'^Bearer\s+(\S+)\s*$', re.IGNORECASE)
_BASIC_RE = re.compile(r'^Basic\s+(\S+)\s*$', re.IGNORECASE)


def path_is_public(path: str) -> bool:
    cleaned = path.rstrip('/') or '/'
    return cleaned in _PUBLIC_PATHS


def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    match = _BEARER_RE.match(authorization.strip())
    return match.group(1) if match else None


def extract_basic_password(authorization: Optional[str]) -> Optional[str]:
    '''Return the password from Authorization: Basic … (username ignored).'''
    if not authorization:
        return None
    match = _BASIC_RE.match(authorization.strip())
    if not match:
        return None
    try:
        raw = base64.b64decode(match.group(1), validate=True).decode('utf-8')
    except (ValueError, UnicodeDecodeError):
        return None
    _user, sep, password = raw.partition(':')
    if not sep:
        return None
    return password


def key_matches(provided: Optional[str], expected: str) -> bool:
    '''Constant-time compare; encode to bytes so non-ASCII never raises.'''
    if provided is None:
        return False
    return hmac.compare_digest(
        provided.encode('utf-8'), expected.encode('utf-8'))


def request_has_secret(
        request: Request, expected: str, *, allow_basic: bool = True) -> bool:
    '''Accept Bearer <secret>, and optionally HTTP Basic password == secret.'''
    header = request.headers.get('authorization')
    if key_matches(extract_bearer(header), expected):
        return True
    if allow_basic and key_matches(extract_basic_password(header), expected):
        return True
    return False


def unauthorized(
        detail: str = 'Invalid or missing API key',
        *,
        www_authenticate: str = 'Bearer') -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={'detail': detail},
        headers={'WWW-Authenticate': www_authenticate})


async def enforce_api_key(request: Request, call_next):
    expected = api_key()
    if not expected:
        return await call_next(request)
    if path_is_public(request.url.path):
        return await call_next(request)
    # API: Bearer preferred; Basic with password == api_key also accepted.
    if not request_has_secret(request, expected, allow_basic=True):
        return unauthorized()
    return await call_next(request)


async def enforce_ui_api_key(request: Request, call_next):
    '''
    Protect the browser UI when auth is configured.

    Password is auth.ui_password if set, else auth.api_key. Without this check
    the UI would proxy with the server API key for anyone who can reach :8002.
    Accept Bearer or HTTP Basic (password = that secret) so browsers prompt.
    '''
    expected = ui_password()
    if not expected:
        return await call_next(request)
    if request_has_secret(request, expected, allow_basic=True):
        return await call_next(request)
    return Response(
        status_code=401,
        content='Authentication required',
        media_type='text/plain',
        headers={
            'WWW-Authenticate': 'Basic realm="marker-server-ui", charset="UTF-8"'})


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
