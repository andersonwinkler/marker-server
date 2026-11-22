#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Server-side configuration for marker-server.

Lives under /opt/marker-server/<version>/config.json. Points at a separate
vanilla Marker install via marker.home. Clients never see LLM credentials.
'''

import json
import os
from functools import lru_cache

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVER_ROOT = os.path.dirname(_SERVER_DIR)  # …/marker-server/<version>


def env_bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


def env_int(name, default=None):
    val = os.getenv(name)
    if val is None or str(val).strip() == '':
        return default
    return int(val)


def _candidates():
    return (
        os.getenv('MARKER_SERVER_CONFIG'),
        os.getenv('MARKER_CONFIG_FILE'),
        os.path.join(_SERVER_ROOT, 'config.json'),
        os.path.join(_SERVER_DIR, 'config.json'),
        '/opt/marker-server/current/config.json')


def config_path():
    for path in _candidates():
        if path and os.path.isfile(path):
            return path
    return None


@lru_cache(maxsize=1)
def load_config():
    path = config_path()
    if not path:
        return {}
    with open(path, encoding='utf-8') as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError(f'Config root must be an object: {path}')
    return data


def reload_config():
    load_config.cache_clear()
    return load_config()


def _section(name):
    section = load_config().get(name)
    return section if isinstance(section, dict) else {}


def marker_home():
    '''Absolute path to the vanilla Marker install (…/opt/marker/<ver>).'''
    home = _section('marker').get('home') or os.getenv('MARKER_HOME')
    if not home:
        raise RuntimeError(
            'marker.home is not set in config.json '
            '(expected a vanilla Marker prefix such as /opt/marker/2.0.0)')
    return os.path.abspath(home)


def marker_python():
    '''Interpreter that has marker-pdf installed.'''
    explicit = _section('marker').get('python')
    if explicit:
        return explicit
    env = os.getenv('MARKER_PYTHON')
    if env:
        return env
    return os.path.join(marker_home(), 'venv', 'bin', 'python')


def runtime_settings():
    rt = _section('runtime')
    home = None
    try:
        home = marker_home()
    except RuntimeError:
        home = None
    upload = rt.get('upload_dir') or os.getenv('MARKER_UPLOAD_DIR')
    if not upload:
        upload = (
            os.path.join(_SERVER_ROOT, 'uploads')
            if os.path.isdir(_SERVER_ROOT) else './uploads')
    max_upload = rt.get('max_upload_bytes')
    if max_upload is None:
        max_upload = env_int('MARKER_MAX_UPLOAD_BYTES', 512 * 1024 * 1024)
    job_ttl = rt.get('job_ttl_seconds')
    if job_ttl is None:
        job_ttl = env_int('MARKER_JOB_TTL_SECONDS', 3600)
    stall = rt.get('progress_stall_seconds')
    if stall is None:
        stall = env_int('MARKER_PROGRESS_STALL_SECONDS', 7200)
    worker_ready = rt.get('worker_ready_seconds')
    if worker_ready is None:
        worker_ready = env_int('MARKER_WORKER_READY_SECONDS', 1800)
    # null → any readable path; [] → POST /marker disabled; else allowlist.
    allowed = rt.get('allowed_input_dirs', [])
    if allowed is None:
        allowed_dirs = None
    elif isinstance(allowed, (list, tuple)):
        allowed_dirs = [os.path.abspath(str(p)) for p in allowed if p]
    else:
        allowed_dirs = []
    return {
        'cuda_visible_devices': rt.get(
            'cuda_visible_devices',
            os.getenv('CUDA_VISIBLE_DEVICES', '0')),
        'torch_device': rt.get('torch_device', os.getenv('TORCH_DEVICE', 'cuda')),
        'host': rt.get('host', '127.0.0.1'),
        'port': int(rt.get('port', 8001)),
        'ui_port': int(rt.get('ui_port', 8002)),
        'upload_dir': upload,
        'max_upload_bytes': int(max_upload),
        'job_ttl_seconds': int(job_ttl),
        'progress_stall_seconds': int(stall),
        'worker_ready_seconds': int(worker_ready),
        'allowed_input_dirs': allowed_dirs,
        'marker_home': home}


def surya_env_from_config():
    '''
    Environment variables Marker/Surya expect, derived from config + marker.home.

    On NVIDIA GPUs Surya defaults to spawning vLLM (Docker) when
    inference_backend is left unset. llama.cpp / LLAMA_CPP_BINARY is optional
    (CPU/Metal or an explicit llamacpp backend). GGUF paths are only set when
    the weights exist under marker.home.
    '''
    home = marker_home()
    surya = _section('surya')
    rt = runtime_settings()
    gguf_dir = os.path.join(home, 'surya-ocr-2')
    gguf_model = surya.get('gguf_model') or os.path.join(
        gguf_dir, 'surya-2.gguf')
    gguf_mmproj = surya.get('gguf_mmproj') or os.path.join(
        gguf_dir, 'surya-2-mmproj.gguf')
    env = {
        'CUDA_VISIBLE_DEVICES': str(rt['cuda_visible_devices']),
        'TORCH_DEVICE': str(rt['torch_device']),
        'MODEL_CACHE_DIR': os.path.join(home, 'cache'),
        'HF_HOME': os.path.join(home, 'hf'),
        'SURYA_INFERENCE_PARALLEL': str(
            int(surya.get('inference_parallel', 2))),
        'SURYA_INFERENCE_KEEP_ALIVE': (
            '1' if surya.get('inference_keep_alive', True) else '0'),
        'SURYA_GUIDED_LAYOUT': (
            '1' if surya.get('guided_layout', False) else '0'),
        'MARKER_UPLOAD_DIR': str(rt['upload_dir'])}
    backend = surya.get('inference_backend')
    if backend:
        env['SURYA_INFERENCE_BACKEND'] = str(backend)
    if os.path.isfile(gguf_model):
        env['SURYA_GGUF_LOCAL_MODEL_PATH'] = gguf_model
    if os.path.isfile(gguf_mmproj):
        env['SURYA_GGUF_LOCAL_MMPROJ_PATH'] = gguf_mmproj
    llama_bin = surya.get('llama_cpp_binary') or os.getenv('LLAMA_CPP_BINARY')
    if llama_bin:
        env['LLAMA_CPP_BINARY'] = str(llama_bin)
    extra = surya.get('llama_cpp_extra_args')
    if extra:
        env['LLAMA_CPP_EXTRA_ARGS'] = str(extra)
    return env


def apply_runtime_env():
    '''Set process env from config so Marker/Surya see the right paths.'''
    try:
        for key, value in surya_env_from_config().items():
            os.environ[key] = value
    except RuntimeError:
        # Config incomplete (e.g. docs import); leave existing env alone.
        pass
    rt = runtime_settings()
    os.environ['MARKER_UPLOAD_DIR'] = str(rt['upload_dir'])
    if rt.get('cuda_visible_devices'):
        os.environ['CUDA_VISIBLE_DEVICES'] = str(rt['cuda_visible_devices'])
    if rt.get('torch_device'):
        os.environ['TORCH_DEVICE'] = str(rt['torch_device'])


def llm_defaults_from_env():
    '''Vision LLM settings from config.json ["llm"] (server-only).'''
    llm = _section('llm')
    use_llm = llm.get('use_llm')
    if use_llm is None:
        use_llm = env_bool('MARKER_USE_LLM', False)

    defaults = {
        'use_llm': bool(use_llm),
        'llm_service': (
            llm.get('llm_service')
            or os.getenv(
                'MARKER_LLM_SERVICE',
                'marker.services.openai.OpenAIService')),
        'openai_base_url': (
            llm.get('openai_base_url')
            or os.getenv('MARKER_OPENAI_BASE_URL', 'http://127.0.0.1:8083/v1')),
        'openai_model': (
            llm.get('openai_model')
            or os.getenv('MARKER_OPENAI_MODEL', 'Qwen3VL-8B-Instruct')),
        'openai_api_key': (
            llm.get('openai_api_key')
            or os.getenv('MARKER_OPENAI_API_KEY', '')),
        'openai_image_format': (
            llm.get('openai_image_format')
            or os.getenv('MARKER_OPENAI_IMAGE_FORMAT', 'png'))}

    timeout_s = llm.get('timeout')
    if timeout_s is None:
        timeout_s = env_int('MARKER_LLM_TIMEOUT', None)
    if timeout_s is not None:
        defaults['timeout'] = int(timeout_s)
    return defaults


def chunking_defaults_from_env():
    ch = _section('chunking')

    def _bool(key, env_name, default):
        if key in ch and ch[key] is not None:
            return bool(ch[key])
        return env_bool(env_name, default)

    def _int(key, env_name, default):
        if key in ch and ch[key] is not None:
            return int(ch[key])
        val = env_int(env_name, None)
        return default if val is None else val

    def _str(key, env_name, default):
        if key in ch and ch[key] is not None:
            return str(ch[key])
        return os.getenv(env_name, default)

    return {
        'chunking': _str('chunking', 'MARKER_CHUNKING', 'auto'),
        'chunk_threshold': _int('chunk_threshold', 'MARKER_CHUNK_THRESHOLD', 80),
        'chunk_pages': _int('chunk_pages', 'MARKER_CHUNK_PAGES', 50),
        'chunk_overlap': _int('chunk_overlap', 'MARKER_CHUNK_OVERLAP', 2),
        'table_aware_cuts': _bool(
            'table_aware_cuts', 'MARKER_TABLE_AWARE_CUTS', True),
        'table_prescan': _bool('table_prescan', 'MARKER_TABLE_PRESCAN', True),
        'table_repair': _bool('table_repair', 'MARKER_TABLE_REPAIR', True),
        'table_llm_fallback': _bool(
            'table_llm_fallback', 'MARKER_TABLE_LLM_FALLBACK', True),
        'seam_polish': _bool('seam_polish', 'MARKER_SEAM_POLISH', True),
        'seam_chars': _int('seam_chars', 'MARKER_SEAM_CHARS', 600)}


def upload_dir():
    return runtime_settings()['upload_dir']


def jobs_dir():
    return os.getenv(
        'MARKER_JOBS_DIR',
        os.path.join(_SERVER_ROOT, 'jobs'))
