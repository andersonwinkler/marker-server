#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Delivery profiles: map client intent to Marker output options.'''

PROFILES = {
    'clean': {
        'output_format': 'markdown',
        'paginate_output': False,
        'include_markdown': True,
        'include_html': False,
        'include_blocks': False},
    'paginated': {
        'output_format': 'markdown',
        'paginate_output': True,
        'include_markdown': True,
        'include_html': False,
        'include_blocks': False},
    'rag': {
        'output_format': 'chunks',
        'paginate_output': False,
        'include_markdown': False,
        'include_html': False,
        'include_blocks': True},
    'layout': {
        'output_format': 'json',
        'paginate_output': False,
        'include_markdown': False,
        'include_html': False,
        'include_blocks': True},
    'html': {
        'output_format': 'html',
        'paginate_output': False,
        'include_markdown': False,
        'include_html': True,
        'include_blocks': False},
    'dual': {
        'output_format': 'chunks',
        'paginate_output': False,
        'include_markdown': True,
        'include_html': False,
        'include_blocks': True}}


def apply_profile(options):
    '''
    Resolve profile into concrete delivery flags.

    Explicit client fields win over profile defaults. Unknown profile raises.
    '''
    options = dict(options)
    profile = options.get('profile') or 'clean'
    if profile not in PROFILES:
        raise ValueError(
            f'Unknown profile {profile!r}; expected one of '
            f'{sorted(PROFILES)}')
    base = PROFILES[profile]
    options['profile'] = profile
    if not options.get('output_format'):
        options['output_format'] = base['output_format']
    if options.get('paginate_output') is None:
        options['paginate_output'] = base['paginate_output']
    for key in ('include_markdown', 'include_html', 'include_blocks'):
        if options.get(key) is None:
            options[key] = base[key]
    return options
