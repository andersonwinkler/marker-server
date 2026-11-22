#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''In-process Marker conversion helpers.'''

import base64
import importlib.metadata as md
import io
import json
import re
import subprocess
from typing import Any, Callable, Optional

from marker.config.parser import ConfigParser
from marker.converters.pdf import PdfConverter
from marker.output import text_from_rendered
from marker.settings import settings

from config import llm_defaults_from_env

# Qwen calls dropped when llm_scope=tables_equations.
_LLM_SCOPE_DROP = {
    'LLMFormProcessor',
    'LLMComplexRegionProcessor',
    'LLMImageDescriptionProcessor',
    'LLMHandwritingProcessor',
    'LLMMathBlockProcessor',
    'LLMSectionHeaderProcessor',
    'LLMPageCorrectionProcessor'}

ProgressCb = Optional[Callable[[dict], None]]


def pkg_version(dist_name):
    try:
        return md.version(dist_name)
    except md.PackageNotFoundError:
        return None


def pdf_page_count(pdffile):
    '''Return page count via pdfinfo, with pypdf fallback.'''
    try:
        out = subprocess.check_output(
            ['pdfinfo', pdffile], text=True, stderr=subprocess.STDOUT)
        match = re.search(r'^Pages:\s*(\d+)\s*$', out, re.MULTILINE)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        return len(PdfReader(pdffile).pages)
    except Exception as exc:
        raise RuntimeError(f'Could not determine page count for {pdffile}') from exc


def processor_list(llm_scope, extract_images):
    '''Comma-separated Marker processor paths, or None for defaults.'''
    from marker.util import classes_to_strings

    drop = set()
    if extract_images is False:
        drop.add('LLMImageDescriptionProcessor')
    if llm_scope == 'tables_equations':
        drop.update(_LLM_SCOPE_DROP)
    elif llm_scope == 'none':
        drop.update(
            proc.__name__ for proc in PdfConverter.default_processors
            if proc.__name__.startswith('LLM'))
    elif llm_scope not in (None, '', 'full'):
        raise ValueError(f'Unknown llm_scope {llm_scope!r}')
    if not drop:
        return None
    kept = [
        proc for proc in PdfConverter.default_processors
        if proc.__name__ not in drop]
    return ','.join(classes_to_strings(kept))


def merge_llm_options(options: dict) -> dict:
    '''Fill LLM settings from server env; never trust client endpoint fields.'''
    options = dict(options)
    # Strip any client-supplied endpoint credentials if present
    for key in (
            'openai_base_url', 'openai_api_key', 'openai_model',
            'openai_image_format', 'llm_service', 'timeout'):
        options.pop(key, None)
    defaults = llm_defaults_from_env()
    use_llm = options.get('use_llm')
    if use_llm is None:
        use_llm = defaults['use_llm']
    options['use_llm'] = bool(use_llm)
    if options['use_llm']:
        for key, default in defaults.items():
            if key == 'use_llm':
                continue
            options[key] = default
    return {k: v for k, v in options.items() if v is not None}


def provenance_from_options(options: dict) -> dict:
    use_llm = bool(options.get('use_llm'))
    return {
        'marker_version': pkg_version('marker-pdf'),
        'surya_version': pkg_version('surya-ocr'),
        'llm_model': options.get('openai_model') if use_llm else None}


def _encode_images(images: dict) -> dict:
    encoded = {}
    for k, v in images.items():
        byte_stream = io.BytesIO()
        v.save(byte_stream, format=settings.OUTPUT_IMAGE_FORMAT)
        encoded[k] = base64.b64encode(byte_stream.getvalue()).decode(
            settings.OUTPUT_ENCODING)
    return encoded


def _blocks_from_chunks_text(text: str):
    '''Parse Marker chunks/json renderer text into a Python object.'''
    if text is None:
        return None
    if isinstance(text, (dict, list)):
        return text
    text = str(text).strip()
    if not text:
        return None
    return json.loads(text)


def run_marker(
        filepath: str,
        options: dict,
        artifact_dict: dict,
        page_range: Optional[str] = None,
        force_paginate: bool = False,
        force_format: Optional[str] = None) -> dict:
    '''
    Run one PdfConverter pass. Returns raw Marker fields plus provenance.

    force_paginate / force_format override delivery for internal chunk windows.
    '''
    options = merge_llm_options(options)
    opts = dict(options)
    if page_range is not None:
        opts['page_range'] = page_range
    if force_paginate:
        opts['paginate_output'] = True
    if force_format:
        opts['output_format'] = force_format

    scope = opts.pop('llm_scope', None)
    # Delivery-only keys ConfigParser should not see
    for key in (
            'profile', 'include_markdown', 'include_html', 'include_blocks',
            'chunking', 'chunk_threshold', 'chunk_pages', 'chunk_overlap',
            'table_aware_cuts', 'table_prescan', 'table_repair',
            'table_llm_fallback', 'seam_polish', 'seam_chars',
            'block_text_format', 'image_delivery', 'filter_block_types',
            'filename'):
        opts.pop(key, None)

    processors = processor_list(scope, opts.get('extract_images'))
    if processors:
        opts['processors'] = processors
    if opts.get('mode') == 'auto':
        opts.pop('mode')

    provenance = provenance_from_options(opts)
    config_parser = ConfigParser(opts)
    config_dict = config_parser.generate_config_dict()
    config_dict.pop('processors', None)
    config_dict['pdftext_workers'] = 1
    converter = PdfConverter(
        config=config_dict,
        artifact_dict=artifact_dict,
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service())
    rendered = converter(filepath)
    text, _, images = text_from_rendered(rendered)
    return {
        'format': opts.get('output_format', 'markdown'),
        'output': text,
        'images': _encode_images(images),
        'metadata': rendered.metadata,
        'success': True,
        **provenance}


def html_to_plainish(html: str) -> str:
    '''Lightweight HTML -> text for block_text_format=plain/markdown.'''
    if not html:
        return ''
    text = re.sub(r'(?is)<script[^>]*>.*?</script>', '', html)
    text = re.sub(r'(?is)<style[^>]*>.*?</style>', '', text)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</p>', '\n\n', text)
    text = re.sub(r'(?i)</div>', '\n', text)
    text = re.sub(r'(?i)</h[1-6]>', '\n\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def normalize_blocks(raw: Any, block_text_format: str = 'html') -> list:
    '''Flatten Marker chunks/json into a list of block dicts.'''
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = _blocks_from_chunks_text(raw)

    blocks = []
    if isinstance(raw, dict) and 'blocks' in raw:
        items = raw['blocks']
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    def walk(node, page=None):
        if not isinstance(node, dict):
            return
        btype = node.get('block_type')
        if btype == 'Page':
            page_id = page
            nid = node.get('id') or ''
            m = re.search(r'/page/(\d+)', str(nid))
            if m:
                page_id = int(m.group(1))
            for child in node.get('children') or []:
                walk(child, page_id)
            return
        html = node.get('html') or ''
        if block_text_format == 'html':
            text = html
        else:
            text = html_to_plainish(html)
        blocks.append({
            'id': node.get('id'),
            'block_type': btype,
            'text': text,
            'html': html,
            'page': node.get('page', page),
            'bbox': node.get('bbox'),
            'polygon': node.get('polygon'),
            'section_hierarchy': node.get('section_hierarchy'),
            'images': list((node.get('images') or {}).keys())})
        for child in node.get('children') or []:
            walk(child, page)

    if isinstance(raw, dict) and 'blocks' in raw:
        for item in items:
            if isinstance(item, dict) and 'html' in item and 'page' in item:
                html = item.get('html') or ''
                text = (
                    html if block_text_format == 'html'
                    else html_to_plainish(html))
                blocks.append({
                    'id': item.get('id'),
                    'block_type': item.get('block_type'),
                    'text': text,
                    'html': html,
                    'page': item.get('page'),
                    'bbox': item.get('bbox'),
                    'polygon': item.get('polygon'),
                    'section_hierarchy': item.get('section_hierarchy'),
                    'images': list((item.get('images') or {}).keys())})
            else:
                walk(item)
    else:
        for item in items:
            walk(item)
    return blocks


def shape_response(
        options: dict,
        markdown: Optional[str] = None,
        html: Optional[str] = None,
        blocks: Optional[list] = None,
        images: Optional[dict] = None,
        metadata: Any = None,
        provenance: Optional[dict] = None,
        chunking_meta: Optional[dict] = None) -> dict:
    '''Build the public API response from delivery flags.'''
    include_md = bool(options.get('include_markdown'))
    include_html = bool(options.get('include_html'))
    include_blocks = bool(options.get('include_blocks'))
    fmt = options.get('output_format') or 'markdown'

    # Legacy single-blob field: primary delivery
    if include_blocks and not include_md and not include_html:
        primary = json.dumps(
            {'blocks': blocks or []}, ensure_ascii=False) if blocks is not None else ''
        legacy_format = 'chunks' if fmt == 'chunks' else fmt
    elif include_html and not include_md:
        primary = html or ''
        legacy_format = 'html'
    else:
        primary = markdown or ''
        legacy_format = 'markdown'

    image_delivery = options.get('image_delivery') or 'inline_base64'
    out_images = images or {}
    if image_delivery == 'none':
        out_images = {}
    elif image_delivery == 'refs_only':
        out_images = {k: '' for k in (images or {})}

    resp = {
        'success': True,
        'profile': options.get('profile') or 'clean',
        'format': legacy_format,
        'output': primary,
        'markdown': markdown if include_md else None,
        'html': html if include_html else None,
        'blocks': blocks if include_blocks else None,
        'images': out_images,
        'metadata': metadata,
        'document': {
            'filename': options.get('filename'),
            'page_count': (chunking_meta or {}).get('n_pages')},
        **(provenance or {})}
    if chunking_meta is not None:
        resp['chunking'] = chunking_meta
    return resp
