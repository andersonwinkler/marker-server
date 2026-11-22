#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Shared upload size helpers for the API and UI.'''

import os

from fastapi import HTTPException, Request

from config import runtime_settings


def max_upload_bytes() -> int:
    env = os.getenv('MARKER_MAX_UPLOAD_BYTES')
    if env is not None and str(env).strip() != '':
        return int(env)
    try:
        return int(runtime_settings().get('max_upload_bytes') or 0)
    except Exception:
        return 512 * 1024 * 1024


def reject_oversized_content_length(request: Request):
    '''Raise 413 when Content-Length exceeds max_upload_bytes.'''
    max_bytes = max_upload_bytes()
    if max_bytes <= 0:
        return
    raw = request.headers.get('content-length')
    if raw is None or str(raw).strip() == '':
        return
    try:
        length = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail='Invalid Content-Length')
    if length > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f'Upload exceeds max_upload_bytes ({max_bytes})')
