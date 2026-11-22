#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Overlapping page-block Marker conversion with stitch and seam handling.

Ported from Zophia's marker_chunk.py for in-process use. Chunks run
sequentially; overlap pages are owned by the later chunk.
'''

import re
import subprocess
import warnings
from datetime import datetime, timezone
from typing import Callable, Optional

from config import llm_defaults_from_env


def _run_marker(*args, **kwargs):
    from convert import run_marker
    return run_marker(*args, **kwargs)


def _normalize_blocks(*args, **kwargs):
    from convert import normalize_blocks
    return normalize_blocks(*args, **kwargs)

PAGE_SEP = '-' * 48
PAGE_MARKER_RE = re.compile(
    r'\n\n\{(\d+)\}' + re.escape(PAGE_SEP) + r'\n\n')
PAGE_MARKER_ANYWHERE_RE = re.compile(
    r'\n*\n\{(\d+)\}' + re.escape(PAGE_SEP) + r'\n*')
MULTI_COL_RE = re.compile(r' {2,}')

ProgressCb = Optional[Callable[[dict], None]]


def extract_pdf_page_texts(pdffile):
    out = subprocess.check_output(
        ['pdftotext', '-layout', '-enc', 'UTF-8', pdffile, '-'],
        stderr=subprocess.DEVNULL)
    text = out.decode('utf-8', errors='replace')
    pages = text.split('\f')
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages


def page_text_looks_like_table(text):
    if not text or not text.strip():
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    multi_col = 0
    for ln in lines:
        parts = MULTI_COL_RE.split(ln.strip())
        if len(parts) >= 3:
            multi_col += 1
        elif ln.count('|') >= 2:
            multi_col += 1
    head = '\n'.join(lines[:3]).lower()
    if 'continued' in head and multi_col >= 1:
        return True
    return multi_col / len(lines) >= 0.35


def scan_pdf_table_flags(pdffile, n_pages, soft_errors=None):
    try:
        texts = extract_pdf_page_texts(pdffile)
    except Exception as exc:
        _note_soft_error(
            soft_errors, 'table_prescan',
            f'PDF table pre-scan failed; cuts unshifted: {exc}')
        return [False] * n_pages
    flags = [page_text_looks_like_table(t) for t in texts]
    if len(flags) < n_pages:
        flags.extend([False] * (n_pages - len(flags)))
    elif len(flags) > n_pages:
        flags = flags[:n_pages]
    return flags


def table_run_at(flags, page_idx):
    n = len(flags)
    if page_idx < 0 or page_idx >= n or not flags[page_idx]:
        return None
    lo = page_idx
    while lo > 0 and flags[lo - 1]:
        lo -= 1
    hi = page_idx
    while hi + 1 < n and flags[hi + 1]:
        hi += 1
    return lo, hi


def adjust_cut(candidate_own_end, flags, n_pages):
    if candidate_own_end >= n_pages:
        return n_pages
    if candidate_own_end <= 0:
        return candidate_own_end
    left, right = candidate_own_end - 1, candidate_own_end
    if flags[left] and right < n_pages and flags[right]:
        run = table_run_at(flags, left)
        if run:
            return min(run[1] + 1, n_pages)
    run = table_run_at(flags, candidate_own_end)
    if run and run[0] < candidate_own_end:
        return min(run[1] + 1, n_pages)
    return candidate_own_end


def plan_chunks(n_pages, chunk_pages, overlap, table_flags=None):
    '''
    Plan overlapping Marker page ranges with exclusive ownership.
    Overlap pages are owned by the later chunk.
    '''
    if n_pages < 1:
        raise ValueError(f'n_pages must be >= 1, got {n_pages}')
    if chunk_pages < 1:
        raise ValueError(f'chunk_pages must be >= 1, got {chunk_pages}')
    if overlap < 0 or overlap >= chunk_pages:
        raise ValueError(
            f'overlap must be in [0, chunk_pages), got {overlap}')

    flags = table_flags if table_flags is not None else [False] * n_pages
    if len(flags) != n_pages:
        raise ValueError('table_flags length must equal n_pages')

    chunks = []
    own_start = 0
    while own_start < n_pages:
        marker_start = max(0, own_start - overlap) if own_start > 0 else 0
        naive_own_end = own_start + chunk_pages - overlap
        if own_start == 0:
            naive_own_end = chunk_pages - overlap

        if naive_own_end >= n_pages:
            chunks.append({
                'marker_start': marker_start,
                'marker_end': n_pages - 1,
                'own_start': own_start,
                'own_end': n_pages,
                'page_range': f'{marker_start}-{n_pages - 1}'})
            break

        own_end = adjust_cut(naive_own_end, flags, n_pages)
        if own_end <= own_start:
            own_end = min(own_start + 1, n_pages)
        marker_end = max(own_start + chunk_pages - 1, own_end - 1)
        marker_end = min(marker_end, n_pages - 1)
        if own_end >= n_pages:
            chunks.append({
                'marker_start': marker_start,
                'marker_end': n_pages - 1,
                'own_start': own_start,
                'own_end': n_pages,
                'page_range': f'{marker_start}-{n_pages - 1}'})
            break

        chunks.append({
            'marker_start': marker_start,
            'marker_end': marker_end,
            'own_start': own_start,
            'own_end': own_end,
            'page_range': f'{marker_start}-{marker_end}'})
        own_start = own_end
    return chunks


def parse_paginated_markdown(text):
    text = text or ''
    if not text.startswith('\n\n'):
        text = '\n\n' + text
    parts = PAGE_MARKER_RE.split(text)
    pages = {}
    if len(parts) == 1:
        return pages
    it = iter(parts[1:])
    for page_id, body in zip(it, it):
        pages[int(page_id)] = body.strip('\n')
    return pages


def strip_page_markers(text):
    return PAGE_MARKER_ANYWHERE_RE.sub('\n\n', text or '').strip() + '\n'


def _prefix_images(images, prefix):
    renamed = {}
    mapping = {}
    for name, b64 in (images or {}).items():
        new_name = f'{prefix}{name}'
        renamed[new_name] = b64
        mapping[name] = new_name
    return renamed, mapping


def _rewrite_image_links(text, mapping):
    for old, new in mapping.items():
        text = text.replace(f']({old})', f']({new})')
        text = text.replace(f'](images/{old})', f'](images/{new})')
    return text


def _looks_like_table(text):
    if not text or not text.strip():
        return False
    lower = text.lower()
    if '<table' in lower:
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    pipe = sum(1 for ln in lines if '|' in ln)
    return pipe >= 2 and pipe / max(len(lines), 1) >= 0.4


def _table_span_around(pages, page_a, page_b, n_pages):
    lo, hi = min(page_a, page_b), max(page_a, page_b)
    while lo > 0 and _looks_like_table(pages.get(lo - 1, '')):
        lo -= 1
    while hi + 1 < n_pages and _looks_like_table(pages.get(hi + 1, '')):
        hi += 1
    return lo, hi


def detect_table_repair_spans(pages, join_pages, n_pages):
    spans = []
    seen = set()
    for left_last in join_pages:
        right_first = left_last + 1
        if right_first >= n_pages:
            continue
        left_txt = pages.get(left_last, '')
        right_txt = pages.get(right_first, '')
        if not (_looks_like_table(left_txt) and _looks_like_table(right_txt)):
            continue
        lo, hi = _table_span_around(pages, left_last, right_first, n_pages)
        key = (lo, hi)
        if key in seen:
            continue
        seen.add(key)
        spans.append(key)
    return spans


def stitch_pages(pages, n_pages):
    parts = []
    for i in range(n_pages):
        body = pages.get(i)
        if body is None:
            continue
        body = body.strip('\n')
        if body:
            parts.append(body)
    text = '\n\n'.join(parts).strip() + '\n'
    return strip_page_markers(text)


def _note_soft_error(soft_errors, step, message, **extra):
    if soft_errors is None:
        return
    record = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'step': step,
        'message': str(message)}
    record.update(extra)
    soft_errors.append(record)
    warnings.warn(f'{step}: {message}')


def _llm_chat(prompt, max_tokens=4096, timeout=180, soft_errors=None, step='llm'):
    import requests
    defaults = llm_defaults_from_env()
    base = str(defaults['openai_base_url']).rstrip('/')
    model = defaults['openai_model']
    api_key = defaults['openai_api_key']
    url = f'{base}/chat/completions'
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.1,
        'max_tokens': max_tokens}
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content']
        if not content or not str(content).strip():
            _note_soft_error(soft_errors, step, 'LLM returned empty content')
            return None
        return str(content).strip()
    except Exception as exc:
        _note_soft_error(soft_errors, step, exc)
        return None


def seam_windows_from_pages(pages, join_page, seam_chars):
    left = (pages.get(join_page) or '').strip()
    right = (pages.get(join_page + 1) or '').strip()
    if not left or not right:
        return None, None
    if _looks_like_table(left) or _looks_like_table(right):
        return None, None
    left_win = left[-seam_chars:] if len(left) > seam_chars else left
    right_win = right[:seam_chars] if len(right) > seam_chars else right
    return left_win, right_win


def llm_seam_polish(left_win, right_win, soft_errors=None, join_page=None):
    prompt = (
        'You are editing OCR markdown from a book. Two fragments meet at a '
        'page/chunk boundary. Rewrite ONLY the combined text so prose flows '
        'naturally across the join. Fix hyphenation splits. Preserve meaning, '
        'markdown structure, math, and citations. Do not add headings. Do not '
        'invent content. If the fragments are mostly a table, code, or image '
        'references, return them unchanged (left then right, separated by a '
        'blank line).\n\n'
        f'<<<LEFT>>>\n{left_win}\n<<<RIGHT>>>\n{right_win}\n<<<END>>>\n'
        'Reply with only the rewritten combined markdown.')
    step = 'seam_polish'
    if join_page is not None:
        step = f'seam_polish:{join_page}|{join_page + 1}'
    return _llm_chat(
        prompt, max_tokens=2048, soft_errors=soft_errors, step=step)


def apply_seam_polish(pages, join_page, polished, seam_chars):
    left = pages.get(join_page) or ''
    right = pages.get(join_page + 1) or ''
    left_prefix = left[:-seam_chars] if len(left) > seam_chars else ''
    right_suffix = right[seam_chars:] if len(right) > seam_chars else ''
    polished = polished.strip()
    mid = len(polished) // 2
    split_at = polished.find('\n\n', mid)
    if split_at < 0:
        split_at = polished.find('\n\n')
    if split_at < 0:
        split_at = mid
    new_left = polished[:split_at].rstrip()
    new_right = polished[split_at:].lstrip()
    pages[join_page] = (
        (left_prefix.rstrip() + '\n\n' + new_left).strip()
        if left_prefix else new_left)
    pages[join_page + 1] = (
        (new_right + '\n\n' + right_suffix.lstrip()).strip()
        if right_suffix else new_right)


def llm_merge_table_fragments(fragments, soft_errors=None, pages=None):
    blocks = []
    for i, frag in enumerate(fragments):
        frag = (frag or '').strip()
        if not frag:
            continue
        blocks.append(f'<<<FRAGMENT {i + 1}>>>\n{frag}')
    if not blocks:
        _note_soft_error(
            soft_errors, 'table_llm_merge',
            'No non-empty table fragments to merge', pages=pages)
        return None
    prompt = (
        'These fragments are pieces of one table split across PDF pages '
        '(OCR/markdown). Merge them into a SINGLE coherent markdown table '
        '(use an HTML <table> only if the fragments are already HTML). '
        'Keep every row and cell you can recover. Do not invent values. '
        'Do not include page numbers or commentary.\n\n'
        + '\n\n'.join(blocks)
        + '\n\n<<<END>>>\nReply with only the merged table.')
    step = 'table_llm_merge'
    if pages is not None:
        step = f'table_llm_merge:{pages[0]}-{pages[1]}'
    return _llm_chat(
        prompt, max_tokens=8192, timeout=300,
        soft_errors=soft_errors, step=step)


def apply_merged_table(pages, lo, hi, merged):
    pages[lo] = merged.strip()
    for p in range(lo + 1, hi + 1):
        pages[p] = ''


def _span_still_split(pages, lo, hi):
    for p in range(lo, hi):
        if _looks_like_table(pages.get(p, '')) and _looks_like_table(
                pages.get(p + 1, '')):
            return True
    return False


def repair_table_span(
        pdffile, pages, all_images, lo, hi, options, artifact_dict,
        soft_errors=None):
    if soft_errors is None:
        soft_errors = []
    remarker_ok = False
    try:
        result = _run_marker(
            pdffile, options, artifact_dict,
            page_range=f'{lo}-{hi}',
            force_paginate=True,
            force_format='markdown')
        prefix = f't{lo:04d}_'
        imgs, mapping = _prefix_images(result.get('images') or {}, prefix)
        all_images.update(imgs)
        text = _rewrite_image_links(result.get('output') or '', mapping)
        repaired = parse_paginated_markdown(text)
        if repaired:
            for p in range(lo, hi + 1):
                if p in repaired:
                    pages[p] = repaired[p]
            remarker_ok = True
        else:
            _note_soft_error(
                soft_errors, 'table_remarker',
                're-Marker returned no paginated pages', pages=[lo, hi])
    except Exception as exc:
        _note_soft_error(
            soft_errors, 'table_remarker', exc, pages=[lo, hi])

    need_llm = options.get('table_llm_fallback', True) and (
        not remarker_ok or _span_still_split(pages, lo, hi))
    if not need_llm:
        return 'remarker'

    fragments = [pages.get(p, '') for p in range(lo, hi + 1)]
    merged = llm_merge_table_fragments(
        fragments, soft_errors=soft_errors, pages=[lo, hi])
    if merged:
        apply_merged_table(pages, lo, hi, merged)
        return 'llm_merge'
    _note_soft_error(
        soft_errors, 'table_llm_merge',
        'LLM table merge produced no usable output', pages=[lo, hi])
    return 'failed'


def should_chunk(n_pages, options):
    mode = (options.get('chunking') or 'auto').lower()
    if mode == 'off':
        return False
    if mode == 'on':
        return n_pages > 0
    threshold = int(options.get('chunk_threshold') or 80)
    return n_pages >= threshold


def convert_chunked(
        pdffile,
        options,
        artifact_dict,
        n_pages,
        progress_cb: ProgressCb = None,
        want_markdown: bool = True,
        want_blocks: bool = False):
    '''
    Convert via overlapping chunks. Progress done/total counts chunks.

    Returns (markdown_or_none, images, blocks_or_none, meta).
    '''
    if not want_markdown and not want_blocks:
        want_markdown = True

    chunk_pages = int(options.get('chunk_pages') or 50)
    overlap = int(options.get('chunk_overlap') or 2)
    soft_errors = []
    table_flags = None
    n_table_pages = 0
    if want_markdown and options.get('table_prescan', True) and options.get(
            'table_aware_cuts', True):
        table_flags = scan_pdf_table_flags(
            pdffile, n_pages, soft_errors=soft_errors)
        n_table_pages = sum(1 for f in table_flags if f)

    chunks = plan_chunks(
        n_pages, chunk_pages, overlap,
        table_flags=table_flags if options.get('table_aware_cuts', True) else None)

    if progress_cb:
        progress_cb({'done': 0, 'total': len(chunks), 'stage': 'plan'})

    pages = {}
    all_images = {}
    all_blocks = []
    join_pages = []
    provenance = {}
    last_metadata = None

    for i, ch in enumerate(chunks):
        if progress_cb:
            progress_cb({
                'done': i,
                'total': len(chunks),
                'stage': 'chunk',
                'chunk_index': i,
                'page_range': ch['page_range']})

        if want_markdown:
            result = _run_marker(
                pdffile, options, artifact_dict,
                page_range=ch['page_range'],
                force_paginate=True,
                force_format='markdown')
            for key in ('marker_version', 'surya_version', 'llm_model'):
                if result.get(key) is not None:
                    provenance[key] = result.get(key)
            last_metadata = result.get('metadata')
            prefix = f'c{i:02d}_'
            imgs, mapping = _prefix_images(result.get('images') or {}, prefix)
            all_images.update(imgs)
            text = _rewrite_image_links(result.get('output') or '', mapping)
            chunk_pages_map = parse_paginated_markdown(text)
            for p in range(ch['own_start'], ch['own_end']):
                if p in chunk_pages_map:
                    pages[p] = chunk_pages_map[p]
                else:
                    pages.setdefault(p, '')

        if want_blocks:
            block_result = _run_marker(
                pdffile, options, artifact_dict,
                page_range=ch['page_range'],
                force_format='chunks')
            for key in ('marker_version', 'surya_version', 'llm_model'):
                if block_result.get(key) is not None:
                    provenance[key] = block_result.get(key)
            last_metadata = block_result.get('metadata')
            prefix = f'b{i:02d}_'
            imgs, mapping = _prefix_images(
                block_result.get('images') or {}, prefix)
            all_images.update(imgs)
            raw_blocks = _normalize_blocks(
                block_result.get('output'),
                options.get('block_text_format') or 'html')
            for blk in raw_blocks:
                page = blk.get('page')
                if page is None:
                    continue
                if ch['own_start'] <= int(page) < ch['own_end']:
                    all_blocks.append(blk)

        if ch['own_end'] < n_pages:
            join_pages.append(ch['own_end'] - 1)

        if progress_cb:
            progress_cb({
                'done': i + 1,
                'total': len(chunks),
                'stage': 'chunk'})

    repair_actions = []
    if want_markdown and options.get('table_repair', True):
        spans = detect_table_repair_spans(pages, join_pages, n_pages)
        for lo, hi in spans:
            if progress_cb:
                progress_cb({
                    'done': len(chunks),
                    'total': len(chunks),
                    'stage': 'table_repair'})
            action = repair_table_span(
                pdffile, pages, all_images, lo, hi, options, artifact_dict,
                soft_errors=soft_errors)
            repair_actions.append({'pages': [lo, hi], 'action': action})

    if (want_markdown and options.get('seam_polish', True)
            and options.get('use_llm', True)):
        seam_chars = int(options.get('seam_chars') or 600)
        for jp in join_pages:
            left_win, right_win = seam_windows_from_pages(
                pages, jp, seam_chars)
            if left_win is None:
                continue
            if progress_cb:
                progress_cb({
                    'done': len(chunks),
                    'total': len(chunks),
                    'stage': 'seam_polish'})
            polished = llm_seam_polish(
                left_win, right_win, soft_errors=soft_errors, join_page=jp)
            if polished:
                apply_seam_polish(pages, jp, polished, seam_chars)

    if progress_cb:
        progress_cb({
            'done': len(chunks),
            'total': len(chunks),
            'stage': 'stitch'})

    markdown = None
    if want_markdown:
        keep_paginate = bool(options.get('paginate_output'))
        if keep_paginate:
            debug_parts = []
            for p in range(n_pages):
                debug_parts.append(
                    f'\n\n{{{p}}}{PAGE_SEP}\n\n{pages.get(p, "")}')
            markdown = ''.join(debug_parts)
        else:
            markdown = stitch_pages(pages, n_pages)

    meta = {
        **provenance,
        'enabled': True,
        'n_pages': n_pages,
        'n_chunks': len(chunks),
        'chunk_pages': chunk_pages,
        'chunk_overlap': overlap,
        'join_pages': join_pages,
        'table_prescan_pages': n_table_pages,
        'table_repairs': repair_actions,
        'soft_errors': soft_errors,
        'metadata': last_metadata}
    return markdown, all_images, (all_blocks if want_blocks else None), meta
