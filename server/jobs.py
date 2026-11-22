#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Sequential job queue: one Marker conversion at a time (no GPU contention).

Conversions run in a killable subprocess that holds the Marker models. On
progress stall or cancel, the parent terminates that process and respawns it
so the FIFO queue can continue.
'''

import multiprocessing as mp
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import config_path
from convert_worker import worker_main

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _utcnow_ts():
    return time.time()


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

    Running jobs fail if progress stops for progress_stall_seconds (including
    single-pass jobs, measured from the last progress event / start). There is
    no separate absolute wall-clock limit: a long job is fine if progress keeps
    moving. Set progress_stall_seconds to 0 to disable.
    '''

    def __init__(
            self,
            upload_dir: Optional[str] = None,
            job_ttl_seconds: int = 3600,
            progress_stall_seconds: int = 7200,
            worker_ready_timeout: float = 600.0):
        self._upload_dir = (
            os.path.realpath(upload_dir) if upload_dir else None)
        self._job_ttl_seconds = max(60, int(job_ttl_seconds))
        self._progress_stall_seconds = max(0, int(progress_stall_seconds))
        self._worker_ready_timeout = float(worker_ready_timeout)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue: list[str] = []
        self._ctx = mp.get_context('spawn')
        self._cmd_q = None
        self._event_q = None
        self._proc = None
        self._current_job_id: Optional[str] = None
        self._shutdown = False
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
        Drop a finished job from memory (result + metadata).
        Queued → cancel; running → request cancel (+ kill worker);
        terminal → purge now (unless worker still holds the input).
        '''
        kill_worker = False
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
            elif job.status == 'running':
                kill_worker = True
        if kill_worker:
            self._kill_worker()
        return job

    def shutdown(self):
        self._shutdown = True
        with self._cv:
            self._cv.notify_all()
        self._kill_worker()

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
        Returns True if the current worker should be killed.
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
        return kill

    def _start_worker(self):
        self._cmd_q = self._ctx.Queue()
        self._event_q = self._ctx.Queue()
        cfg = config_path() or ''
        self._proc = self._ctx.Process(
            target=worker_main,
            args=(self._cmd_q, self._event_q, cfg, _SERVER_DIR),
            name='marker-convert-worker',
            daemon=True)
        self._proc.start()
        deadline = time.time() + self._worker_ready_timeout
        while time.time() < deadline:
            try:
                evt = self._event_q.get(timeout=1.0)
            except queue.Empty:
                if not self._proc.is_alive():
                    raise RuntimeError('Conversion worker exited before ready')
                continue
            if evt and evt[0] == 'ready':
                return
        raise RuntimeError('Timed out waiting for conversion worker ready')

    def _kill_worker(self):
        proc = self._proc
        self._proc = None
        cmd_q, event_q = self._cmd_q, self._event_q
        self._cmd_q = None
        self._event_q = None
        if proc is None:
            return
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

    def _ensure_worker(self):
        if self._proc is not None and self._proc.is_alive():
            return
        self._kill_worker()
        self._start_worker()

    def _reaper_loop(self):
        while not self._shutdown:
            time.sleep(min(15, self._job_ttl_seconds))
            kill = False
            with self._lock:
                kill = self._check_stall_locked()
                self._purge_expired_locked()
            if kill:
                self._kill_worker()

    def _finish_job_locked(self, job: Job, status: str, error: Optional[str] = None):
        job.status = status
        if error is not None:
            job.error = error
        job.finished_at = _utcnow()
        job.finished_ts = _utcnow_ts()
        job.stage = status
        job.worker_active = False
        self._maybe_delete_input(job)

    def _worker_loop(self):
        while not self._shutdown:
            with self._cv:
                while not self._queue and not self._shutdown:
                    self._cv.wait(timeout=1.0)
                if self._shutdown:
                    break
                if not self._queue:
                    continue
                job_id = self._queue.pop(0)
                job = self._jobs[job_id]
                if job.cancel_requested or job.status == 'cancelled':
                    self._finish_job_locked(job, 'cancelled')
                    continue
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
                continue

            try:
                self._cmd_q.put(('run', job_id, filepath, options))
            except Exception as exc:
                with self._lock:
                    self._finish_job_locked(
                        job, 'failed', f'Failed to dispatch job: {exc}')
                    self._current_job_id = None
                self._kill_worker()
                continue

            finished = False
            while not finished and not self._shutdown:
                kill = False
                with self._lock:
                    if job.stall_timed_out or (
                            job.cancel_requested and job.status == 'running'):
                        if job.stall_timed_out:
                            job.status = 'failed'
                            job.stage = 'failed'
                            if not job.finished_ts:
                                job.finished_at = _utcnow()
                                job.finished_ts = _utcnow_ts()
                        elif job.cancel_requested:
                            self._finish_job_locked(job, 'cancelled')
                        kill = True
                    elif self._check_stall_locked():
                        kill = True

                if kill:
                    self._kill_worker()
                    with self._lock:
                        job.worker_active = False
                        self._maybe_delete_input(job)
                        self._current_job_id = None
                    finished = True
                    break

                try:
                    evt = self._event_q.get(timeout=0.5)
                except queue.Empty:
                    if self._proc is None or not self._proc.is_alive():
                        with self._lock:
                            if job.stall_timed_out or job.status == 'failed':
                                job.worker_active = False
                                self._maybe_delete_input(job)
                            elif job.cancel_requested:
                                self._finish_job_locked(job, 'cancelled')
                            elif job.status == 'running':
                                self._finish_job_locked(
                                    job, 'failed',
                                    'Conversion worker died unexpectedly')
                            else:
                                job.worker_active = False
                                self._maybe_delete_input(job)
                            self._current_job_id = None
                        finished = True
                    continue

                kind = evt[0]
                if kind == 'progress':
                    _, eid, update = evt
                    if eid != job_id:
                        continue
                    with self._lock:
                        if job.cancel_requested or job.stall_timed_out:
                            # Ignore late progress after stall/cancel.
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
                    finished = True
                elif kind == 'error':
                    _, eid, err = evt
                    if eid != job_id:
                        continue
                    with self._lock:
                        if job.stall_timed_out:
                            job.status = 'failed'
                            job.worker_active = False
                            self._maybe_delete_input(job)
                        elif job.cancel_requested:
                            self._finish_job_locked(job, 'cancelled', err)
                        else:
                            # Keep the short message; full traceback is in err.
                            short = (err or '').split('\n', 1)[0] or 'error'
                            self._finish_job_locked(job, 'failed', short)
                        self._current_job_id = None
                    finished = True
                elif kind == 'ready':
                    # Late ready from a respawn; ignore.
                    continue
