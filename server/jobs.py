#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Sequential job queue: one Marker conversion at a time (no GPU contention).

Conversions run in a killable subprocess that holds the Marker models. On
progress stall or cancel, the parent terminates that process and respawns it
so the FIFO queue can continue. A stall also stops Surya's detached OCR server
so the next worker does not reconnect to a hung vLLM/llama-server.
'''

import multiprocessing as mp
import os
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import config_path
from convert_worker import worker_main
from surya_cleanup import stop_surya_inference_servers

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _utcnow_ts():
    return time.time()


def _short_error(err: Optional[str]) -> str:
    if not err:
        return 'error'
    return str(err).split('\n', 1)[0].strip() or 'error'


@dataclass
class Job:
    job_id: str
    filepath: str
    options: dict
    # Only delete filepath after the job if we created it (uploads).
    delete_filepath: bool = False
    status: str = 'queued'  # queued|running|done|failed|cancelled
    created_at: str = field(default_factory=_utcnow)
    started_at: Optional[str] = None
    started_ts: Optional[float] = None
    finished_at: Optional[str] = None
    finished_ts: Optional[float] = None
    progress: dict = field(default_factory=lambda: {'done': 0, 'total': 0})
    progress_ts: Optional[float] = None
    stage: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    stall_timed_out: bool = False
    # True while the conversion subprocess may still be reading the input.
    worker_active: bool = False


class JobManager:
    '''
    In-process FIFO scheduler + killable conversion subprocess.

    Running jobs fail if progress stops for progress_stall_seconds.
    Stall kills the conversion worker and stops Surya OCR servers so the queue
    can recover. Cancel only marks the job; the worker loop performs the kill
    so HTTP handlers stay non-blocking.
    '''

    def __init__(
            self,
            upload_dir: Optional[str] = None,
            job_ttl_seconds: int = 3600,
            progress_stall_seconds: int = 7200,
            worker_ready_timeout: float = 1800.0):
        self._upload_dir = (
            os.path.realpath(upload_dir) if upload_dir else None)
        self._job_ttl_seconds = max(60, int(job_ttl_seconds))
        self._progress_stall_seconds = max(0, int(progress_stall_seconds))
        self._worker_ready_timeout = float(worker_ready_timeout)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._worker_lock = threading.Lock()
        self._queue: list[str] = []
        self._ctx = mp.get_context('spawn')
        self._cmd_q = None
        self._event_q = None
        self._proc = None
        self._current_job_id: Optional[str] = None
        self._shutdown = False
        self._kill_ocr_requested = False
        self._worker = threading.Thread(
            target=self._worker_loop, name='marker-job-worker', daemon=True)
        self._worker.start()
        self._reaper = threading.Thread(
            target=self._reaper_loop, name='marker-job-reaper', daemon=True)
        self._reaper.start()

    def submit(
            self,
            filepath: str,
            options: dict,
            delete_filepath: bool = False) -> Job:
        job_id = uuid.uuid4().hex
        job = Job(
            job_id=job_id,
            filepath=filepath,
            options=options,
            delete_filepath=bool(delete_filepath))
        with self._cv:
            self._purge_expired_locked()
            self._jobs[job_id] = job
            self._queue.append(job_id)
            self._cv.notify()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            self._purge_expired_locked()
            self._check_stall_locked()
            return self._jobs.get(job_id)

    def release(self, job_id: str) -> Optional[Job]:
        '''
        Drop a finished job, or request cancel for an active one.

        Running cancels are non-blocking: the scheduler thread kills the
        conversion worker. Callers (HTTP) must not join that process here.
        '''
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in ('done', 'failed', 'cancelled'):
                if not job.worker_active:
                    self._maybe_delete_input(job)
                    self._jobs.pop(job_id, None)
                return job
            job.cancel_requested = True
            if job.status == 'queued':
                job.status = 'cancelled'
                job.finished_at = _utcnow()
                job.finished_ts = _utcnow_ts()
                if job_id in self._queue:
                    self._queue.remove(job_id)
                self._maybe_delete_input(job)
            # running: worker loop / reaper will kill the subprocess
        return job

    def warm_start(self):
        '''Start the conversion worker in the background (model load at boot).'''
        threading.Thread(
            target=self._warm_start_bg, name='marker-warm-start',
            daemon=True).start()

    def _warm_start_bg(self):
        try:
            self._ensure_worker()
        except Exception:
            traceback.print_exc()

    def shutdown(self):
        self._shutdown = True
        with self._cv:
            self._cv.notify_all()
        self._kill_worker(stop_ocr=False)

    def status_dict(self, job: Job) -> dict:
        return {
            'job_id': job.job_id,
            'status': job.status,
            'created_at': job.created_at,
            'started_at': job.started_at,
            'finished_at': job.finished_at,
            'progress': {
                'done': int(job.progress.get('done') or 0),
                'total': int(job.progress.get('total') or 0)},
            'stage': job.stage,
            'error': job.error}

    def _under_upload_dir(self, filepath: str) -> bool:
        if not self._upload_dir or not filepath:
            return False
        try:
            real = os.path.realpath(filepath)
        except OSError:
            return False
        upload = self._upload_dir
        return real == upload or real.startswith(upload + os.sep)

    def _maybe_delete_input(self, job: Job):
        '''Delete only upload-owned files under the upload directory.'''
        if job.worker_active:
            return
        if not job.delete_filepath:
            return
        path = job.filepath
        if not path or not self._under_upload_dir(path):
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def _purge_expired_locked(self):
        now = _utcnow_ts()
        ttl = self._job_ttl_seconds
        expired = [
            jid for jid, job in self._jobs.items()
            if job.finished_ts is not None
            and job.status in ('done', 'failed', 'cancelled')
            and not job.worker_active
            and (now - job.finished_ts) >= ttl]
        for jid in expired:
            job = self._jobs.pop(jid, None)
            if job is not None:
                self._maybe_delete_input(job)

    def _check_stall_locked(self) -> bool:
        '''
        Fail + request kill when progress stops for progress_stall_seconds.
        Returns True if the current worker should be killed (and OCR stopped).
        '''
        stall = self._progress_stall_seconds
        if stall <= 0:
            return False
        now = _utcnow_ts()
        kill = False
        for job in self._jobs.values():
            if job.status != 'running' or job.stall_timed_out:
                continue
            last = job.progress_ts or job.started_ts
            if last is None:
                continue
            if (now - last) < stall:
                continue
            job.cancel_requested = True
            job.stall_timed_out = True
            job.error = (
                f'No progress for {stall}s '
                f'(runtime.progress_stall_seconds)')
            job.status = 'failed'
            job.finished_at = _utcnow()
            job.finished_ts = now
            job.stage = 'failed'
            if job.job_id == self._current_job_id:
                kill = True
                self._kill_ocr_requested = True
        return kill

    def _start_worker(self):
        with self._worker_lock:
            self._cmd_q = self._ctx.Queue()
            self._event_q = self._ctx.Queue()
            cfg = config_path() or ''
            self._proc = self._ctx.Process(
                target=worker_main,
                args=(self._cmd_q, self._event_q, cfg, _SERVER_DIR),
                name='marker-convert-worker',
                daemon=True)
            self._proc.start()
            event_q = self._event_q
            proc = self._proc
        deadline = time.time() + self._worker_ready_timeout
        while time.time() < deadline:
            if event_q is None:
                self._kill_worker(stop_ocr=False)
                raise RuntimeError('Conversion worker ready wait failed')
            try:
                evt = event_q.get(timeout=1.0)
            except queue.Empty:
                if proc is None or not proc.is_alive():
                    self._kill_worker(stop_ocr=False)
                    raise RuntimeError(
                        'Conversion worker exited before ready')
                continue
            except Exception:
                self._kill_worker(stop_ocr=False)
                raise RuntimeError(
                    'Conversion worker ready wait failed')
            if evt and evt[0] == 'ready':
                return
        self._kill_worker(stop_ocr=False)
        raise RuntimeError(
            f'Timed out waiting for conversion worker ready '
            f'({int(self._worker_ready_timeout)}s; '
            f'set runtime.worker_ready_seconds)')

    def _kill_worker(self, stop_ocr: bool = False):
        with self._worker_lock:
            proc = self._proc
            self._proc = None
            cmd_q, event_q = self._cmd_q, self._event_q
            self._cmd_q = None
            self._event_q = None
            if proc is not None:
                try:
                    if proc.is_alive():
                        proc.terminate()
                        proc.join(timeout=5)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(timeout=5)
                except Exception:
                    pass
            for q in (cmd_q, event_q):
                if q is None:
                    continue
                try:
                    q.close()
                except Exception:
                    pass
            do_ocr = stop_ocr or self._kill_ocr_requested
            self._kill_ocr_requested = False
        if do_ocr:
            try:
                n = stop_surya_inference_servers()
                if n:
                    print(
                        f'[marker-server] stopped {n} Surya OCR '
                        f'server sentinel(s) after stall/recovery',
                        flush=True)
            except Exception:
                traceback.print_exc()

    def _ensure_worker(self):
        with self._worker_lock:
            alive = self._proc is not None and self._proc.is_alive()
        if alive:
            return
        self._kill_worker(stop_ocr=False)
        self._start_worker()

    def _reaper_loop(self):
        while not self._shutdown:
            time.sleep(min(15, self._job_ttl_seconds))
            kill = False
            with self._lock:
                kill = self._check_stall_locked()
                self._purge_expired_locked()
                # Restart the scheduler thread if it died.
                if not self._worker.is_alive() and not self._shutdown:
                    print(
                        '[marker-server] job worker thread died; restarting',
                        flush=True)
                    self._worker = threading.Thread(
                        target=self._worker_loop,
                        name='marker-job-worker', daemon=True)
                    self._worker.start()
            if kill:
                self._kill_worker(stop_ocr=True)

    def _finish_job_locked(self, job: Job, status: str, error: Optional[str] = None):
        job.status = status
        if error is not None:
            job.error = _short_error(error)
        job.finished_at = _utcnow()
        job.finished_ts = _utcnow_ts()
        job.stage = status
        job.worker_active = False
        self._maybe_delete_input(job)

    def _handle_worker_gone(self, job: Job):
        with self._lock:
            if job.stall_timed_out or job.status == 'failed':
                job.worker_active = False
                self._maybe_delete_input(job)
            elif job.cancel_requested:
                self._finish_job_locked(job, 'cancelled')
            elif job.status == 'running':
                self._finish_job_locked(
                    job, 'failed', 'Conversion worker died unexpectedly')
            else:
                job.worker_active = False
                self._maybe_delete_input(job)
            self._current_job_id = None

    def _worker_loop(self):
        while not self._shutdown:
            try:
                self._worker_loop_once()
            except Exception:
                traceback.print_exc()
                with self._lock:
                    jid = self._current_job_id
                    job = self._jobs.get(jid) if jid else None
                    if job is not None and job.status == 'running':
                        self._finish_job_locked(
                            job, 'failed',
                            'Internal scheduler error; see server logs')
                    self._current_job_id = None
                try:
                    self._kill_worker(stop_ocr=False)
                except Exception:
                    traceback.print_exc()
                time.sleep(0.5)

    def _worker_loop_once(self):
        with self._cv:
            while not self._queue and not self._shutdown:
                self._cv.wait(timeout=1.0)
            if self._shutdown:
                return
            if not self._queue:
                return
            job_id = self._queue.pop(0)
            job = self._jobs[job_id]
            if job.cancel_requested or job.status == 'cancelled':
                self._finish_job_locked(job, 'cancelled')
                return
            job.status = 'running'
            job.started_at = _utcnow()
            job.started_ts = _utcnow_ts()
            job.progress_ts = job.started_ts
            job.stage = 'running'
            job.worker_active = True
            self._current_job_id = job_id
            filepath = job.filepath
            options = dict(job.options)

        try:
            self._ensure_worker()
        except Exception as exc:
            with self._lock:
                self._finish_job_locked(job, 'failed', str(exc))
                self._current_job_id = None
            return

        try:
            self._cmd_q.put(('run', job_id, filepath, options))
        except Exception as exc:
            with self._lock:
                self._finish_job_locked(
                    job, 'failed', f'Failed to dispatch job: {exc}')
                self._current_job_id = None
            self._kill_worker(stop_ocr=False)
            return

        while not self._shutdown:
            kill = False
            stop_ocr = False
            with self._lock:
                if job.stall_timed_out or (
                        job.cancel_requested and job.status == 'running'):
                    if job.stall_timed_out:
                        job.status = 'failed'
                        job.stage = 'failed'
                        if not job.finished_ts:
                            job.finished_at = _utcnow()
                            job.finished_ts = _utcnow_ts()
                        stop_ocr = True
                    elif job.cancel_requested:
                        self._finish_job_locked(job, 'cancelled')
                    kill = True
                elif self._check_stall_locked():
                    kill = True
                    stop_ocr = True

            if kill:
                self._kill_worker(stop_ocr=stop_ocr)
                with self._lock:
                    job.worker_active = False
                    self._maybe_delete_input(job)
                    self._current_job_id = None
                return

            event_q = self._event_q
            if event_q is None:
                self._handle_worker_gone(job)
                return
            try:
                evt = event_q.get(timeout=0.5)
            except queue.Empty:
                proc = self._proc
                if proc is None or not proc.is_alive():
                    self._handle_worker_gone(job)
                    return
                continue
            except Exception:
                self._handle_worker_gone(job)
                return

            kind = evt[0]
            if kind == 'progress':
                _, eid, update = evt
                if eid != job_id:
                    continue
                with self._lock:
                    if job.cancel_requested or job.stall_timed_out:
                        continue
                    job.progress = {
                        'done': int(update.get('done') or 0),
                        'total': int(update.get('total') or 0)}
                    job.progress_ts = _utcnow_ts()
                    if update.get('stage') is not None:
                        job.stage = update.get('stage')
            elif kind == 'done':
                _, eid, result = evt
                if eid != job_id:
                    continue
                with self._lock:
                    if job.stall_timed_out:
                        job.status = 'failed'
                        job.result = None
                        job.worker_active = False
                        self._maybe_delete_input(job)
                    elif job.cancel_requested:
                        self._finish_job_locked(job, 'cancelled')
                    else:
                        job.result = result
                        job.status = 'done'
                        job.finished_at = _utcnow()
                        job.finished_ts = _utcnow_ts()
                        job.stage = 'done'
                        if job.progress.get('total'):
                            job.progress['done'] = job.progress['total']
                        job.worker_active = False
                        self._maybe_delete_input(job)
                    self._current_job_id = None
                return
            elif kind == 'error':
                _, eid, err = evt
                if eid != job_id:
                    continue
                if err and '\n' in str(err):
                    print(err, flush=True)
                with self._lock:
                    if job.stall_timed_out:
                        job.status = 'failed'
                        job.worker_active = False
                        self._maybe_delete_input(job)
                    elif job.cancel_requested:
                        self._finish_job_locked(job, 'cancelled')
                    else:
                        self._finish_job_locked(job, 'failed', err)
                    self._current_job_id = None
                return
            elif kind == 'ready':
                continue
