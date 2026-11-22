#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Conversion subprocess: loads Marker models once and runs jobs until killed.

The parent JobManager can terminate this process on stall/cancel, then spawn a
fresh worker so the FIFO queue is not blocked by a hung Marker call.
'''

import os
import sys
import time
import traceback


def _test_worker_main(cmd_queue, event_queue, config_path: str, server_dir: str):
    '''Importable dummy worker for stall/cancel tests (no Marker).'''
    event_queue.put(('ready', None, None))
    while True:
        msg = cmd_queue.get()
        if msg is None or msg[0] == 'shutdown':
            break
        if msg[0] != 'run':
            continue
        _, job_id, filepath, options = msg
        mode = (options or {}).get('_test_mode', 'ok')
        event_queue.put((
            'progress', job_id,
            {'done': 0, 'total': 3, 'stage': 'chunk'}))
        if mode == 'hang':
            time.sleep(60)
            event_queue.put(('done', job_id, {'ok': True}))
            continue
        time.sleep(0.05)
        event_queue.put((
            'progress', job_id,
            {'done': 3, 'total': 3, 'stage': 'done'}))
        event_queue.put((
            'done', job_id,
            {'ok': True, 'n': (options or {}).get('n')}))


def worker_main(cmd_queue, event_queue, config_path: str, server_dir: str):
    '''
    Entry point for the spawn'ed conversion process.

    Protocol (cmd_queue ← parent):
      ('run', job_id, filepath, options)
      ('shutdown',)

    Protocol (event_queue → parent):
      ('ready', None, None)
      ('progress', job_id, update_dict)
      ('done', job_id, result_dict)
      ('error', job_id, error_str)
    '''
    if os.environ.get('MARKER_TEST_WORKER') == '1':
        return _test_worker_main(
            cmd_queue, event_queue, config_path, server_dir)

    if server_dir and server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    if config_path:
        os.environ['MARKER_SERVER_CONFIG'] = config_path

    from config import apply_runtime_env
    apply_runtime_env()

    from marker.models import create_model_dict, shutdown_models
    from pipeline import convert_document

    models = create_model_dict()
    event_queue.put(('ready', None, None))
    try:
        while True:
            msg = cmd_queue.get()
            if msg is None:
                break
            cmd = msg[0]
            if cmd == 'shutdown':
                break
            if cmd != 'run':
                continue
            _, job_id, filepath, options = msg

            def progress_cb(update, _jid=job_id):
                event_queue.put(('progress', _jid, dict(update or {})))

            try:
                result = convert_document(
                    filepath, options, models, progress_cb=progress_cb)
                event_queue.put(('done', job_id, result))
            except Exception as exc:
                event_queue.put((
                    'error', job_id,
                    f'{exc}\n{traceback.format_exc()}'))
    finally:
        try:
            shutdown_models(models)
        except Exception:
            pass
