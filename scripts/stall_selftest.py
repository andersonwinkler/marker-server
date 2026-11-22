#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Self-test for killable stall recovery (MARKER_TEST_WORKER=1).'''

import os
import sys
import tempfile
import time
from pathlib import Path

# Isolate Surya cache. Spawn re-imports this module as __mp_main__, so only
# create a new temp home if the parent has not already chosen one.
_TMP_HOME = os.environ.get('MARKER_STALL_TMP_HOME')
if not _TMP_HOME:
    _TMP_HOME = tempfile.mkdtemp(prefix='marker-stall-home-')
    os.environ['MARKER_STALL_TMP_HOME'] = _TMP_HOME
os.environ['HOME'] = _TMP_HOME
os.environ['XDG_CACHE_HOME'] = os.path.join(_TMP_HOME, 'xdg-cache')
os.environ['MARKER_SURYA_CACHE_DIR'] = os.path.join(
    _TMP_HOME, '.cache', 'datalab', 'surya')
os.environ['MARKER_TEST_WORKER'] = '1'

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER = os.path.join(_ROOT, 'server')
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)


def _helper_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_status(mgr, job_id, want, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = mgr.get(job_id)
        if j and j.status == want:
            return j
        if j and j.status in ('done', 'failed', 'cancelled') and j.status != want:
            return j
        time.sleep(0.05)
    return mgr.get(job_id)


def main():
    import jobs as J

    upload = tempfile.mkdtemp()
    path = os.path.join(upload, 'x.pdf')
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write('x')

    # --- submit during slow ready (warm-start race regression) ---
    os.environ['MARKER_TEST_WORKER_READY_DELAY'] = '2'
    mgr_warm = J.JobManager(
        upload_dir=upload, job_ttl_seconds=60, progress_stall_seconds=8,
        worker_ready_timeout=60)
    # Job arrives while models are still "loading".
    time.sleep(0.3)
    jw = mgr_warm.submit(path, {'_test_mode': 'ok', 'n': 1}, delete_filepath=False)
    j = _wait_status(mgr_warm, jw.job_id, 'done', timeout=15)
    assert j and j.status == 'done', (
        f'warm-start race: expected done, got '
        f'{j.status if j else None}/{getattr(j, "error", None)}')
    print('submit-during-ready ok')
    mgr_warm.shutdown()
    os.environ['MARKER_TEST_WORKER_READY_DELAY'] = '0'

    mgr = J.JobManager(
        upload_dir=upload, job_ttl_seconds=60, progress_stall_seconds=1,
        worker_ready_timeout=30)

    cache_dir = Path(os.environ['MARKER_SURYA_CACHE_DIR'])
    cache_dir.mkdir(parents=True, exist_ok=True)
    sentinel = cache_dir / 'vllm_server.json'
    if sentinel.exists():
        sentinel.unlink()

    ja = mgr.submit(
        path, {'_test_mode': 'hang', '_test_helper': True},
        delete_filepath=False)
    jb = mgr.submit(path, {'_test_mode': 'ok', 'n': 2}, delete_filepath=False)

    # Capture helper PID from progress stage (chunk+helper:<pid>) before stall.
    helper_pid = None
    sentinel_seen = False
    for _ in range(100):
        a = mgr.get(ja.job_id)
        stage = (a.stage or '') if a else ''
        if stage.startswith('chunk+helper:'):
            helper_pid = int(stage.split(':', 1)[1])
            if sentinel.is_file():
                sentinel_seen = True
            break
        if a and a.status in ('failed', 'cancelled', 'done'):
            break
        time.sleep(0.02)
    assert helper_pid is not None, (
        f'hang job should report helper pid in stage, got '
        f'{getattr(mgr.get(ja.job_id), "stage", None)}/'
        f'{getattr(mgr.get(ja.job_id), "error", None)}')
    assert sentinel_seen, (
        'OCR sentinel missing in shared cache dir — parent/worker HOME disagree')
    assert _helper_alive(helper_pid), 'OCR helper should be running during hang'

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
    j = _wait_status(mgr, jc.job_id, 'cancelled', timeout=10)
    assert j and j.status == 'cancelled', j.status if j else None
    print(f'cancel non-blocking ok ({cancel_return:.3f}s return)')

    path2 = os.path.join(upload, 'y.pdf')
    with open(path2, 'w', encoding='utf-8') as fp:
        fp.write('y')
    mgr2 = J.JobManager(
        upload_dir=upload, job_ttl_seconds=60, progress_stall_seconds=1,
        worker_ready_timeout=30)
    jd = mgr2.submit(path2, {'_test_mode': 'hang'}, delete_filepath=True)
    j = _wait_status(mgr2, jd.job_id, 'failed', timeout=10)
    assert j and j.status == 'failed'
    time.sleep(0.8)
    assert not os.path.isfile(path2), 'upload should be removed after worker released'
    print('upload delete after worker idle ok')

    mgr.shutdown()
    mgr2.shutdown()
    print('all ok')


if __name__ == '__main__':
    main()
