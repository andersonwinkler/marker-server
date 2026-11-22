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
from typing import Iterable, List


def _candidate_dirs() -> List[Path]:
    dirs = []
    home = Path(os.path.expanduser('~'))
    dirs.append(home / '.cache' / 'datalab' / 'surya')
    xdg = os.environ.get('XDG_CACHE_HOME')
    if xdg:
        dirs.append(Path(xdg) / 'datalab' / 'surya')
    # Dedupe while preserving order.
    seen = set()
    out = []
    for d in dirs:
        key = str(d.resolve()) if d.exists() else str(d)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _stop_process(pid: int) -> None:
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return
    except Exception:
        pass
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.25)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    except Exception:
        pass


def _stop_docker(name: str) -> None:
    try:
        subprocess.run(
            ['docker', 'stop', name],
            check=False, capture_output=True, timeout=60)
    except Exception:
        pass
    try:
        subprocess.run(
            ['docker', 'rm', '-f', name],
            check=False, capture_output=True, timeout=30)
    except Exception:
        pass


def _cleanup_sentinel(path: Path) -> bool:
    '''Stop the server described by one sentinel file. Returns True if acted.'''
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        return False
    if not isinstance(data, dict):
        try:
            path.unlink()
        except OSError:
            pass
        return False

    kind = (data.get('cleanup_kind') or '').strip().lower()
    cleanup_id = data.get('cleanup_id')
    pid = data.get('pid')
    acted = False
    if kind == 'docker' and cleanup_id:
        _stop_docker(str(cleanup_id))
        acted = True
    elif kind == 'process':
        target = None
        if pid:
            try:
                target = int(pid)
            except (TypeError, ValueError):
                target = None
        if target is None and cleanup_id:
            try:
                target = int(cleanup_id)
            except (TypeError, ValueError):
                target = None
        if target:
            _stop_process(target)
            acted = True
    elif pid:
        # Older / partial sentinel: best-effort PID kill.
        try:
            _stop_process(int(pid))
            acted = True
        except (TypeError, ValueError):
            pass

    try:
        path.unlink()
    except OSError:
        pass
    return acted


def stop_surya_inference_servers(
        backends: Iterable[str] = ('vllm', 'llamacpp')) -> int:
    '''
    Stop known Surya OCR servers and delete their sentinel files.

    Returns the number of sentinel files processed.
    '''
    names = {f'{b}_server.json' for b in backends}
    names |= {f'{b}_server.json' for b in ('vllm', 'llamacpp')}
    count = 0
    for base in _candidate_dirs():
        if not base.is_dir():
            continue
        # Named backends plus any leftover *_server.json.
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
            if _cleanup_sentinel(path):
                count += 1
            else:
                # Still count deleted corrupt sentinels as handled.
                count += 1
    return count
