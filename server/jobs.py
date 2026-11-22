#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Sequential job queue: one Marker conversion at a time (no GPU contention).'''

import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from pipeline import convert_document


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
    finished_at: Optional[str] = None
    finished_ts: Optional[float] = None
    progress: dict = field(default_factory=lambda: {'done': 0, 'total': 0})
    stage: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    cancel_requested: bool = False


class JobManager:
    '''
    In-process FIFO worker. Jobs never run in parallel against each other.
    '''

    def __init__(
            self,
            artifact_getter: Callable[[], dict],
            upload_dir: Optional[str] = None,
            job_ttl_seconds: int = 3600):
        self._artifact_getter = artifact_getter
        self._upload_dir = (
            os.path.realpath(upload_dir) if upload_dir else None)
        self._job_ttl_seconds = max(60, int(job_ttl_seconds))
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue: list[str] = []
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
            return self._jobs.get(job_id)

    def clear_result(self, job_id: str):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.result = None

    def release(self, job_id: str) -> Optional[Job]:
        '''
        Drop a finished job from memory (result + metadata).
        Queued → cancel; running → request cancel; terminal → purge now.
        '''
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in ('done', 'failed', 'cancelled'):
                self._maybe_delete_input(job)
                snapshot = job
                self._jobs.pop(job_id, None)
                return snapshot
            job.cancel_requested = True
            if job.status == 'queued':
                job.status = 'cancelled'
                job.finished_at = _utcnow()
                job.finished_ts = _utcnow_ts()
                if job_id in self._queue:
                    self._queue.remove(job_id)
                self._maybe_delete_input(job)
            return job

    def cancel(self, job_id: str) -> Optional[Job]:
        '''Alias for release (cancel active jobs; purge finished ones).'''
        return self.release(job_id)

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
            and (now - job.finished_ts) >= ttl]
        for jid in expired:
            job = self._jobs.pop(jid, None)
            if job is not None:
                self._maybe_delete_input(job)

    def _reaper_loop(self):
        while True:
            time.sleep(min(60, self._job_ttl_seconds))
            with self._lock:
                self._purge_expired_locked()

    def _worker_loop(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                job_id = self._queue.pop(0)
                job = self._jobs[job_id]
                if job.cancel_requested or job.status == 'cancelled':
                    job.status = 'cancelled'
                    job.finished_at = _utcnow()
                    job.finished_ts = _utcnow_ts()
                    self._maybe_delete_input(job)
                    continue
                job.status = 'running'
                job.started_at = _utcnow()
                job.stage = 'running'
                filepath = job.filepath
                options = dict(job.options)

            def progress_cb(update: dict):
                with self._lock:
                    job.progress = {
                        'done': int(update.get('done') or 0),
                        'total': int(update.get('total') or 0)}
                    if update.get('stage') is not None:
                        job.stage = update.get('stage')
                    if job.cancel_requested:
                        raise RuntimeError('Job cancelled')

            try:
                artifact_dict = self._artifact_getter()
                result = convert_document(
                    filepath, options, artifact_dict, progress_cb=progress_cb)
                with self._lock:
                    if job.cancel_requested:
                        job.status = 'cancelled'
                        job.finished_at = _utcnow()
                        job.finished_ts = _utcnow_ts()
                        job.stage = 'cancelled'
                    else:
                        job.result = result
                        job.status = 'done'
                        job.finished_at = _utcnow()
                        job.finished_ts = _utcnow_ts()
                        job.stage = 'done'
                        if job.progress.get('total'):
                            job.progress['done'] = job.progress['total']
            except Exception as exc:
                traceback.print_exc()
                with self._lock:
                    job.status = (
                        'cancelled' if job.cancel_requested else 'failed')
                    job.error = str(exc)
                    job.finished_at = _utcnow()
                    job.finished_ts = _utcnow_ts()
                    job.stage = job.status
            finally:
                with self._lock:
                    self._maybe_delete_input(job)
