#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Stop Surya OCR inference servers left behind after a killed conversion worker.

Surya records server metadata under ~/.cache/datalab/surya/<backend>_server.json
(cleanup_kind = docker|process). A hung vLLM container / llama-server can still
answer /health, so a new worker would reuse it and stall again. Call this after
a progress stall before spawning a fresh conversion worker.
'''

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# Only kill process PIDs whose cmdline looks like an OCR inference server.
_PROCESS_CMDLINE_HINTS = ('llama-server', 'vllm')


def _candidate_dirs() -> List[Path]:
    dirs = []
    # Prefer an explicit override (tests isolate HOME/XDG this way).
    override = os.environ.get('MARKER_SURYA_CACHE_DIR')
    if override:
        dirs.append(Path(override))
    home = Path(os.path.expanduser('~'))
    dirs.append(home / '.cache' / 'datalab' / 'surya')
    xdg = os.environ.get('XDG_CACHE_HOME')
    if xdg:
        dirs.append(Path(xdg) / 'datalab' / 'surya')
    seen = set()
    out = []
    for d in dirs:
        key = str(d.resolve()) if d.exists() else str(d)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _cmdline(pid: int) -> Optional[str]:
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    return raw.replace(b'\x00', b' ').decode('utf-8', 'replace')


def _pid_looks_like_ocr(pid: int) -> bool:
    text = _cmdline(pid)
    if text is None:
        return False
    lower = text.lower()
    return any(h in lower for h in _PROCESS_CMDLINE_HINTS)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _stop_process(pid: int) -> bool:
    '''Kill pid only if it still looks like an OCR server. Returns True if signaled.'''
    if not _pid_exists(pid):
        return False
    if not _pid_looks_like_ocr(pid):
        # PID reused by something else — leave it alone.
        return False
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return False
    except Exception:
        return False
    for _ in range(20):
        if not _pid_exists(pid):
            return True
        time.sleep(0.25)
    # Hard kill if it is still the same-looking process (or cmdline vanished).
    if not _pid_exists(pid):
        return True
    if _cmdline(pid) is None or _pid_looks_like_ocr(pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            return True
        except Exception:
            return False
        for _ in range(10):
            if not _pid_exists(pid):
                return True
            time.sleep(0.1)
    return not _pid_exists(pid)


def _stop_docker(name: str) -> bool:
    stopped = False
    try:
        r = subprocess.run(
            ['docker', 'stop', name],
            check=False, capture_output=True, timeout=60)
        if r.returncode == 0:
            stopped = True
    except Exception:
        pass
    try:
        r = subprocess.run(
            ['docker', 'rm', '-f', name],
            check=False, capture_output=True, timeout=30)
        if r.returncode == 0:
            stopped = True
    except Exception:
        pass
    return stopped


def _cleanup_sentinel(path: Path) -> Tuple[bool, bool]:
    '''
    Stop the server described by one sentinel file.

    Returns (stopped_something, deleted_sentinel).
    '''
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        try:
            path.unlink()
            return False, True
        except OSError:
            return False, False
    if not isinstance(data, dict):
        try:
            path.unlink()
            return False, True
        except OSError:
            return False, False

    kind = (data.get('cleanup_kind') or '').strip().lower()
    cleanup_id = data.get('cleanup_id')
    pid = data.get('pid')
    stopped = False
    if kind == 'docker' and cleanup_id:
        stopped = _stop_docker(str(cleanup_id))
    elif kind == 'process':
        target = None
        if pid is not None:
            try:
                target = int(pid)
            except (TypeError, ValueError):
                target = None
        if target is None and cleanup_id:
            try:
                target = int(cleanup_id)
            except (TypeError, ValueError):
                target = None
        if target is not None:
            stopped = _stop_process(target)
    elif pid is not None:
        try:
            stopped = _stop_process(int(pid))
        except (TypeError, ValueError):
            pass

    deleted = False
    try:
        path.unlink()
        deleted = True
    except OSError:
        pass
    return stopped, deleted


def stop_surya_inference_servers(
        backends: Iterable[str] = ('vllm', 'llamacpp')) -> int:
    '''
    Stop known Surya OCR servers and delete their sentinel files.

    Returns the number of servers actually signaled to stop (not merely
    deleted corrupt sentinels).
    '''
    names = {f'{b}_server.json' for b in backends}
    names |= {f'{b}_server.json' for b in ('vllm', 'llamacpp')}
    stopped = 0
    for base in _candidate_dirs():
        if not base.is_dir():
            continue
        paths = set()
        for name in names:
            p = base / name
            if p.is_file():
                paths.add(p)
        try:
            for p in base.glob('*_server.json'):
                paths.add(p)
        except OSError:
            pass
        for path in paths:
            did_stop, _deleted = _cleanup_sentinel(path)
            if did_stop:
                stopped += 1
    return stopped
