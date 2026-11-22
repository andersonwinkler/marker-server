#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Marker API: upload → job → poll progress → fetch result.

LLM endpoint / key / model come only from server environment.
Clients may toggle use_llm / llm_scope but never supply the endpoint.
Conversions are queued and run one at a time.
'''

import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Optional

import click
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from config import (
    apply_runtime_env, chunking_defaults_from_env, config_path,
    llm_defaults_from_env, marker_home, marker_python, runtime_settings,
    upload_dir)
from convert import pkg_version
from jobs import JobManager

# Apply CUDA / Surya / cache paths from config before Marker imports models.
apply_runtime_env()

app_data = {}
UPLOAD_DIRECTORY = upload_dir()
os.makedirs(UPLOAD_DIRECTORY, exist_ok=True)


def _form_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _safe_upload_basename(name):
    '''Basename only; strip path segments and odd characters.'''
    base = os.path.basename(name or '') or 'upload.bin'
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._') or 'upload.bin'
    return base[:180]


@asynccontextmanager
async def lifespan(app: FastAPI):
    from marker.models import create_model_dict, shutdown_models
    app_data['models'] = create_model_dict()
    app_data['jobs'] = JobManager(lambda: app_data['models'])
    yield
    if 'models' in app_data:
        shutdown_models(app_data['models'])
        del app_data['models']


app = FastAPI(
    title='Marker API',
    description='PDF/document conversion with job progress and page chunking.',
    lifespan=lifespan,
    # Custom /docs and /redoc below use relative openapi.json (proxy-friendly).
    docs_url=None,
    redoc_url=None)


@app.get('/')
async def root():
    # Relative hrefs so links work behind a reverse-proxy prefix (e.g. /marker-api/).
    return HTMLResponse(
        '''
<h1>Marker API</h1>
<ul>
  <li><a href="docs">API Documentation</a></li>
  <li><a href="redoc">ReDoc</a></li>
  <li><a href="version">Package / LLM provenance</a></li>
  <li><code>POST /marker/upload</code> → <code>{job_id}</code></li>
  <li><code>GET /marker/jobs/{job_id}</code> → progress <code>done</code>/<code>total</code></li>
  <li><code>GET /marker/jobs/{job_id}/result</code> → final payload</li>
</ul>
''')


@app.get('/docs', include_in_schema=False)
async def swagger_ui():
    '''Swagger UI with a relative OpenAPI URL (works under /marker-api/).'''
    return get_swagger_ui_html(
        openapi_url='openapi.json',
        title=app.title + ' - Docs')


@app.get('/redoc', include_in_schema=False)
async def redoc_ui():
    return get_redoc_html(
        openapi_url='openapi.json',
        title=app.title + ' - ReDoc')


@app.get('/version')
async def version():
    '''Packages + marker.home. Never returns LLM URL/key.'''
    defaults = llm_defaults_from_env()
    chunk = chunking_defaults_from_env()
    rt = runtime_settings()
    try:
        home = marker_home()
        py = marker_python()
    except RuntimeError as exc:
        home, py = None, None
        home_error = str(exc)
    else:
        home_error = None
    return {
        'marker_server_config': config_path(),
        'marker_home': home,
        'marker_python': py,
        'marker_home_error': home_error,
        'marker_version': pkg_version('marker-pdf'),
        'surya_version': pkg_version('surya-ocr'),
        'cuda_visible_devices': rt.get('cuda_visible_devices'),
        'llm_enabled': bool(defaults.get('use_llm')),
        'llm_model': defaults.get('openai_model') if defaults.get('use_llm') else None,
        'chunking': chunk}


class CommonParams(BaseModel):
    filepath: Annotated[
        Optional[str], Field(description='Local path on the server.')] = None
    profile: Annotated[
        Optional[str],
        Field(
            description="Delivery profile: clean|paginated|rag|layout|html|dual")] = (
        'clean')
    page_range: Annotated[Optional[str], Field(description='Page range')] = None
    force_ocr: Annotated[bool, Field(description='Force OCR')] = False
    paginate_output: Annotated[
        Optional[bool], Field(description='Paginate markdown')] = None
    output_format: Annotated[
        Optional[str],
        Field(description='Override format; usually set via profile')] = None
    mode: Annotated[Optional[str], Field(description='balanced|fast')] = None
    use_llm: Annotated[
        Optional[bool],
        Field(description='Use server-configured vision LLM')] = None
    extract_images: Annotated[Optional[bool], Field(default=None)] = None
    ocr_inline_math: Annotated[Optional[bool], Field(default=None)] = None
    disable_ocr: Annotated[Optional[bool], Field(default=None)] = None
    highres_image_dpi: Annotated[Optional[int], Field(default=None)] = None
    lowres_image_dpi: Annotated[Optional[int], Field(default=None)] = None
    llm_scope: Annotated[
        Optional[str],
        Field(description='full|tables_equations|none')] = None
    chunking: Annotated[
        Optional[str], Field(description='auto|on|off')] = None
    chunk_threshold: Annotated[Optional[int], Field(default=None)] = None
    chunk_pages: Annotated[Optional[int], Field(default=None)] = None
    chunk_overlap: Annotated[Optional[int], Field(default=None)] = None
    table_aware_cuts: Annotated[Optional[bool], Field(default=None)] = None
    table_prescan: Annotated[Optional[bool], Field(default=None)] = None
    table_repair: Annotated[Optional[bool], Field(default=None)] = None
    table_llm_fallback: Annotated[Optional[bool], Field(default=None)] = None
    seam_polish: Annotated[Optional[bool], Field(default=None)] = None
    seam_chars: Annotated[Optional[int], Field(default=None)] = None
    include_markdown: Annotated[Optional[bool], Field(default=None)] = None
    include_html: Annotated[Optional[bool], Field(default=None)] = None
    include_blocks: Annotated[Optional[bool], Field(default=None)] = None
    block_text_format: Annotated[
        Optional[str], Field(description='html|plain')] = None
    image_delivery: Annotated[
        Optional[str], Field(description='inline_base64|refs_only|none')] = None
    filter_block_types: Annotated[Optional[str], Field(default=None)] = None
    filename: Annotated[Optional[str], Field(default=None)] = None


def _options_from_params(params: CommonParams) -> dict:
    options = params.model_dump()
    options.pop('filepath', None)
    chunk_defaults = chunking_defaults_from_env()
    for key, default in chunk_defaults.items():
        if options.get(key) is None:
            options[key] = default
    return {k: v for k, v in options.items() if v is not None}


def _submit(filepath: str, options: dict) -> dict:
    jobs: JobManager = app_data['jobs']
    job = jobs.submit(filepath, options)
    return jobs.status_dict(job)


@app.post('/marker')
async def convert_pdf(params: CommonParams):
    if not params.filepath:
        raise HTTPException(status_code=400, detail='filepath is required')
    if not os.path.isfile(params.filepath):
        raise HTTPException(status_code=400, detail='filepath not found')
    options = _options_from_params(params)
    options['filename'] = options.get('filename') or os.path.basename(
        params.filepath)
    return _submit(params.filepath, options)


@app.post('/marker/upload')
async def convert_pdf_upload(
    profile: Optional[str] = Form(default='clean'),
    page_range: Optional[str] = Form(default=None),
    mode: Optional[str] = Form(default=None),
    force_ocr: Optional[bool] = Form(default=False),
    paginate_output: Optional[str] = Form(default=None),
    output_format: Optional[str] = Form(default=None),
    use_llm: Optional[str] = Form(default=None),
    extract_images: Optional[str] = Form(default=None),
    ocr_inline_math: Optional[str] = Form(default=None),
    disable_ocr: Optional[str] = Form(default=None),
    highres_image_dpi: Optional[int] = Form(default=None),
    lowres_image_dpi: Optional[int] = Form(default=None),
    llm_scope: Optional[str] = Form(default=None),
    chunking: Optional[str] = Form(default=None),
    chunk_threshold: Optional[int] = Form(default=None),
    chunk_pages: Optional[int] = Form(default=None),
    chunk_overlap: Optional[int] = Form(default=None),
    table_aware_cuts: Optional[str] = Form(default=None),
    table_prescan: Optional[str] = Form(default=None),
    table_repair: Optional[str] = Form(default=None),
    table_llm_fallback: Optional[str] = Form(default=None),
    seam_polish: Optional[str] = Form(default=None),
    seam_chars: Optional[int] = Form(default=None),
    include_markdown: Optional[str] = Form(default=None),
    include_html: Optional[str] = Form(default=None),
    include_blocks: Optional[str] = Form(default=None),
    block_text_format: Optional[str] = Form(default=None),
    image_delivery: Optional[str] = Form(default=None),
    filter_block_types: Optional[str] = Form(default=None),
    file: UploadFile = File(..., description='PDF or document to convert')):
    original_name = _safe_upload_basename(file.filename)
    upload_path = os.path.join(
        UPLOAD_DIRECTORY, f'{uuid.uuid4().hex}_{original_name}')
    with open(upload_path, 'wb+') as upload_file:
        upload_file.write(await file.read())

    params = CommonParams(
        filepath=upload_path,
        profile=profile,
        page_range=page_range,
        mode=mode,
        force_ocr=force_ocr,
        paginate_output=_form_bool(paginate_output),
        output_format=output_format,
        use_llm=_form_bool(use_llm),
        extract_images=_form_bool(extract_images),
        ocr_inline_math=_form_bool(ocr_inline_math),
        disable_ocr=_form_bool(disable_ocr),
        highres_image_dpi=highres_image_dpi,
        lowres_image_dpi=lowres_image_dpi,
        llm_scope=llm_scope,
        chunking=chunking,
        chunk_threshold=chunk_threshold,
        chunk_pages=chunk_pages,
        chunk_overlap=chunk_overlap,
        table_aware_cuts=_form_bool(table_aware_cuts),
        table_prescan=_form_bool(table_prescan),
        table_repair=_form_bool(table_repair),
        table_llm_fallback=_form_bool(table_llm_fallback),
        seam_polish=_form_bool(seam_polish),
        seam_chars=seam_chars,
        include_markdown=_form_bool(include_markdown),
        include_html=_form_bool(include_html),
        include_blocks=_form_bool(include_blocks),
        block_text_format=block_text_format,
        image_delivery=image_delivery,
        filter_block_types=filter_block_types,
        filename=original_name)
    options = _options_from_params(params)
    return _submit(upload_path, options)


@app.get('/marker/jobs/{job_id}')
async def job_status(job_id: str):
    jobs: JobManager = app_data['jobs']
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Unknown job_id')
    return jobs.status_dict(job)


@app.get('/marker/jobs/{job_id}/result')
async def job_result(job_id: str):
    jobs: JobManager = app_data['jobs']
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Unknown job_id')
    if job.status == 'failed':
        return JSONResponse(
            status_code=500,
            content={
                'success': False,
                'error': job.error,
                'job_id': job_id,
                **jobs.status_dict(job)})
    if job.status == 'cancelled':
        raise HTTPException(status_code=409, detail='Job was cancelled')
    if job.status != 'done' or job.result is None:
        raise HTTPException(
            status_code=409,
            detail={
                'message': 'Job not finished',
                **jobs.status_dict(job)})
    payload = dict(job.result)
    payload['job_id'] = job_id
    return payload


@app.delete('/marker/jobs/{job_id}')
async def job_cancel(job_id: str):
    jobs: JobManager = app_data['jobs']
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Unknown job_id')
    return jobs.status_dict(job)


@click.command()
@click.option('--port', type=int, default=8001)
@click.option('--host', type=str, default='127.0.0.1')
def server_cli(port: int, host: str):
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    server_cli()
