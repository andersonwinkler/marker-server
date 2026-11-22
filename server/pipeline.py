#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Top-level conversion pipeline: single pass or sequential chunks.'''

from typing import Callable, Optional

from chunking import convert_chunked, should_chunk
from convert import (
    normalize_blocks, pdf_page_count, provenance_from_options, run_marker,
    shape_response, merge_llm_options)
from profiles import apply_profile

ProgressCb = Optional[Callable[[dict], None]]


def _filter_blocks(blocks, filter_types):
    if not filter_types or not blocks:
        return blocks
    drop = {t.strip() for t in filter_types.split(',') if t.strip()}
    if not drop:
        return blocks
    return [b for b in blocks if b.get('block_type') not in drop]


def convert_document(filepath, options, artifact_dict, progress_cb: ProgressCb = None):
    '''
    Convert a document. Updates progress as {done, total} (and optional stage).

    Chunks, when used, run strictly one after another on this process.
    '''
    options = apply_profile(options)
    options = merge_llm_options(options)
    n_pages = pdf_page_count(filepath, required=False)
    options.setdefault('filename', None)

    want_md = bool(options.get('include_markdown'))
    want_html = bool(options.get('include_html'))
    want_blocks = bool(options.get('include_blocks'))

    # Page-block chunking needs a known page count and is markdown/blocks oriented.
    # Any HTML delivery has no chunk stitcher — force a single Marker pass.
    can_chunk = (
        n_pages is not None
        and should_chunk(n_pages, options)
        and not options.get('page_range')
        and not want_html)
    use_chunking = can_chunk

    if progress_cb:
        progress_cb({'done': 0, 'total': 1, 'stage': 'probe'})

    if not use_chunking:
        fmt = options.get('output_format') or 'markdown'
        need_blocks = want_blocks or fmt in ('chunks', 'json')
        need_md = want_md or (
            fmt == 'markdown' and not want_blocks and not want_html)
        need_html = want_html or fmt == 'html'
        n_passes = sum(1 for flag in (need_blocks, need_md, need_html) if flag)
        n_passes = max(1, n_passes)
        done_passes = 0
        if progress_cb:
            progress_cb({
                'done': 0, 'total': n_passes, 'stage': 'convert'})

        markdown = html = None
        blocks = None
        images = {}
        metadata = None
        provenance = provenance_from_options(options)

        # Prefer a single renderer when only one delivery is needed.
        # dual / mixed requests may run Marker more than once (known limitation).
        if need_blocks:
            force_fmt = 'chunks' if options.get('profile') == 'rag' else (
                'json' if options.get('profile') == 'layout' else
                ('chunks' if want_blocks else fmt))
            result = run_marker(
                filepath, options, artifact_dict, force_format=force_fmt)
            provenance = {
                k: result.get(k)
                for k in ('marker_version', 'surya_version', 'llm_model')}
            metadata = result.get('metadata')
            images.update(result.get('images') or {})
            blocks = normalize_blocks(
                result.get('output'),
                options.get('block_text_format') or 'html')
            blocks = _filter_blocks(blocks, options.get('filter_block_types'))
            done_passes += 1
            if progress_cb:
                progress_cb({
                    'done': done_passes, 'total': n_passes, 'stage': 'convert'})

        if need_md:
            result = run_marker(
                filepath, options, artifact_dict, force_format='markdown')
            provenance = {
                k: result.get(k)
                for k in ('marker_version', 'surya_version', 'llm_model')}
            metadata = result.get('metadata')
            images.update(result.get('images') or {})
            markdown = result.get('output') or ''
            done_passes += 1
            if progress_cb:
                progress_cb({
                    'done': done_passes, 'total': n_passes, 'stage': 'convert'})

        if need_html:
            result = run_marker(
                filepath, options, artifact_dict, force_format='html')
            provenance = {
                k: result.get(k)
                for k in ('marker_version', 'surya_version', 'llm_model')}
            metadata = result.get('metadata')
            images.update(result.get('images') or {})
            html = result.get('output') or ''
            done_passes += 1
            if progress_cb:
                progress_cb({
                    'done': done_passes, 'total': n_passes, 'stage': 'convert'})

        if progress_cb:
            progress_cb({
                'done': n_passes, 'total': n_passes, 'stage': 'done'})

        return shape_response(
            options,
            markdown=markdown,
            html=html,
            blocks=blocks,
            images=images,
            metadata=metadata,
            provenance=provenance,
            chunking_meta={
                'enabled': False,
                'n_pages': n_pages,
                'n_chunks': 1})

    # Chunked path: sequential windows (HTML requests never reach here).
    markdown, images, blocks, meta = convert_chunked(
        filepath,
        options,
        artifact_dict,
        n_pages,
        progress_cb=progress_cb,
        want_markdown=want_md or not want_blocks,
        want_blocks=want_blocks)

    if blocks:
        blocks = _filter_blocks(blocks, options.get('filter_block_types'))

    provenance = {
        'marker_version': meta.get('marker_version'),
        'surya_version': meta.get('surya_version'),
        'llm_model': meta.get('llm_model')}
    chunking_meta = {
        'enabled': True,
        'n_pages': meta.get('n_pages'),
        'n_chunks': meta.get('n_chunks'),
        'chunk_pages': meta.get('chunk_pages'),
        'chunk_overlap': meta.get('chunk_overlap'),
        'join_pages': meta.get('join_pages'),
        'table_prescan_pages': meta.get('table_prescan_pages'),
        'table_repairs': meta.get('table_repairs'),
        'soft_errors': meta.get('soft_errors')}

    if progress_cb:
        progress_cb({
            'done': chunking_meta['n_chunks'],
            'total': chunking_meta['n_chunks'],
            'stage': 'done'})

    return shape_response(
        options,
        markdown=markdown if want_md else None,
        html=None,
        blocks=blocks if want_blocks else None,
        images=images,
        metadata=meta.get('metadata'),
        provenance=provenance,
        chunking_meta=chunking_meta)
