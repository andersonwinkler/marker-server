#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Sequential job queue: one Marker conversion at a time (no GPU contention).'''

import os
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pipeline import convert_document


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    job_id: str
    filepath: str
    options: dict
    status: str = 'queued'  # queued|running|done|failed|cancelled
    created_at: str = field(default_factory=_utcnow)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress: dict = field(default_factory=lambda: {'done': 0, 'total': 0})
    stage: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    cancel_requested: bool = False


class JobManager:
    '''
    In-process FIFO worker. Jobs never run in parallel against each other.
    '''

    def __init__(self, artifact_getter: Callable[[], dict]):
        self._artifact_getter = artifact_getter
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue: list[str] = []
        self._worker = threading.Thread(
            target=self._worker_loop, name='marker-job-worker', daemon=True)
        self._worker.start()

    def submit(self, filepath: str, options: dict) -> Job:
        job_id = uuid.uuid4().hex
        job = Job(job_id=job_id, filepath=filepath, options=options)
        with self._cv:
            self._jobs[job_id] = job
            self._queue.append(job_id)
            self._cv.notify()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.cancel_requested = True
            if job.status == 'queued':
                job.status = 'cancelled'
                job.finished_at = _utcnow()
                if job_id in self._queue:
                    self._queue.remove(job_id)
            return job

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
                    else:
                        job.result = result
                        job.status = 'done'
                        job.finished_at = _utcnow()
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
                    job.stage = job.status
            finally:
                try:
                    if filepath and os.path.isfile(filepath):
                        os.remove(filepath)
                except OSError:
                    pass
