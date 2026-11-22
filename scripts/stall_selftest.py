#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Self-test for killable stall recovery (MARKER_TEST_WORKER=1).'''

import os
import sys
import tempfile
import time
from pathlib import Path

# Ensure spawn children import this package from disk, not a stdin main.
os.environ['MARKER_TEST_WORKER'] = '1'
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER = os.path.join(_ROOT, 'server')
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

import jobs as J  # noqa: E402


def _helper_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    upload = tempfile.mkdtemp()
    path = os.path.join(upload, 'x.pdf')
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write('x')

    mgr = J.JobManager(
        upload_dir=upload, job_ttl_seconds=60, progress_stall_seconds=1,
        worker_ready_timeout=30)
    ja = mgr.submit(
        path, {'_test_mode': 'hang', '_test_helper': True},
        delete_filepath=False)
    jb = mgr.submit(path, {'_test_mode': 'ok', 'n': 2}, delete_filepath=False)

    # Read helper PID from sentinel once the hang job is running.
    sentinel = Path(os.path.expanduser('~/.cache/datalab/surya/vllm_server.json'))
    helper_pid = None
    for _ in range(50):
        if sentinel.is_file():
            import json
            helper_pid = int(json.loads(sentinel.read_text())['pid'])
            break
        time.sleep(0.05)

    t0 = time.time()
    for _ in range(200):
        a, b = mgr.get(ja.job_id), mgr.get(jb.job_id)
        if a and a.status == 'failed' and b and b.status == 'done':
            break
        time.sleep(0.1)
    else:
        a, b = mgr.get(ja.job_id), mgr.get(jb.job_id)
        raise AssertionError(
            f'timeout a={a.status if a else None}/{getattr(a, "error", None)} '
            f'b={b.status if b else None}/{getattr(b, "error", None)}')

    elapsed = time.time() - t0
    assert 'No progress' in (a.error or ''), a.error
    assert b.result and b.result.get('ok') is True
    assert elapsed < 15, f'queue should recover quickly, took {elapsed:.1f}s'
    print(f'stall+queue recovery ok in {elapsed:.1f}s')

    if helper_pid is not None:
        # Stall path should stop the detached OCR helper via sentinel cleanup.
        time.sleep(0.3)
        assert not sentinel.is_file(), 'OCR sentinel should be removed on stall'
        assert not _helper_alive(helper_pid), 'OCR helper should be killed on stall'
        print('ocr helper cleanup ok')

    jc = mgr.submit(path, {'_test_mode': 'hang'}, delete_filepath=False)
    for _ in range(50):
        j = mgr.get(jc.job_id)
        if j and j.status == 'running':
            break
        time.sleep(0.05)
    t_cancel = time.time()
    mgr.release(jc.job_id)
    cancel_return = time.time() - t_cancel
    assert cancel_return < 0.5, f'release() blocked for {cancel_return:.2f}s'
    for _ in range(50):
        j = mgr.get(jc.job_id)
        if j and j.status == 'cancelled':
            break
        time.sleep(0.05)
    j = mgr.get(jc.job_id)
    assert j and j.status == 'cancelled', j.status if j else None
    print(f'cancel non-blocking ok ({cancel_return:.3f}s return)')

    path2 = os.path.join(upload, 'y.pdf')
    with open(path2, 'w', encoding='utf-8') as fp:
        fp.write('y')
    mgr2 = J.JobManager(
        upload_dir=upload, job_ttl_seconds=60, progress_stall_seconds=1,
        worker_ready_timeout=30)
    jd = mgr2.submit(path2, {'_test_mode': 'hang'}, delete_filepath=True)
    for _ in range(80):
        j = mgr2.get(jd.job_id)
        if j and j.status == 'failed':
            break
        time.sleep(0.05)
    time.sleep(0.8)
    assert not os.path.isfile(path2), 'upload should be removed after worker released'
    print('upload delete after worker idle ok')

    mgr.shutdown()
    mgr2.shutdown()
    print('all ok')


if __name__ == '__main__':
    main()
