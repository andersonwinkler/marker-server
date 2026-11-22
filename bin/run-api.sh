#!/usr/bin/env bash
# Launch the Marker API using the vanilla Marker interpreter from config.json.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${MARKER_SERVER_CONFIG:-$ROOT/config.json}"
export MARKER_SERVER_CONFIG="$CONFIG"

if [[ ! -f "$CONFIG" ]]; then
  echo "Missing config: $CONFIG" >&2
  echo "Copy config.example.json to config.json and set marker.home + llm.*" >&2
  exit 1
fi

# Probe uses stdlib only — does not import marker-pdf.
eval "$(python3.12 - "$CONFIG" <<'PY'
import json, os, shlex, sys
cfg_path = sys.argv[1]
with open(cfg_path, encoding='utf-8') as fp:
    cfg = json.load(fp)
marker = cfg.get('marker') or {}
home = os.path.abspath(marker.get('home') or '')
if not home:
    sys.stderr.write('config marker.home is required\n')
    sys.exit(2)
py = marker.get('python') or os.path.join(home, 'venv', 'bin', 'python')
if not os.path.isfile(py):
    sys.stderr.write(f'Marker python not found: {py}\n')
    sys.exit(2)
rt = cfg.get('runtime') or {}
surya = cfg.get('surya') or {}
gguf_dir = os.path.join(home, 'surya-ocr-2')
upload = rt.get('upload_dir') or os.path.join(os.path.dirname(cfg_path), 'uploads')
env = {
    'CUDA_VISIBLE_DEVICES': str(rt.get('cuda_visible_devices', '0')),
    'TORCH_DEVICE': str(rt.get('torch_device', 'cuda')),
    'MODEL_CACHE_DIR': os.path.join(home, 'cache'),
    'HF_HOME': os.path.join(home, 'hf'),
    'SURYA_INFERENCE_PARALLEL': str(int(surya.get('inference_parallel', 2))),
    'SURYA_INFERENCE_KEEP_ALIVE': (
        '1' if surya.get('inference_keep_alive', True) else '0'),
    'SURYA_GUIDED_LAYOUT': '1' if surya.get('guided_layout', False) else '0',
    'MARKER_UPLOAD_DIR': upload,
    'MARKER_SERVER_CONFIG': cfg_path}
backend = surya.get('inference_backend')
if backend:
    env['SURYA_INFERENCE_BACKEND'] = str(backend)
gguf_model = surya.get('gguf_model') or os.path.join(gguf_dir, 'surya-2.gguf')
gguf_mmproj = surya.get('gguf_mmproj') or os.path.join(
    gguf_dir, 'surya-2-mmproj.gguf')
if os.path.isfile(gguf_model):
    env['SURYA_GGUF_LOCAL_MODEL_PATH'] = gguf_model
if os.path.isfile(gguf_mmproj):
    env['SURYA_GGUF_LOCAL_MMPROJ_PATH'] = gguf_mmproj
llama_bin = surya.get('llama_cpp_binary') or os.environ.get('LLAMA_CPP_BINARY')
if llama_bin:
    env['LLAMA_CPP_BINARY'] = str(llama_bin)
extra = surya.get('llama_cpp_extra_args')
if extra:
    env['LLAMA_CPP_EXTRA_ARGS'] = str(extra)
host = str(rt.get('host', '127.0.0.1'))
port = str(int(rt.get('port', 8001)))
for k, v in env.items():
    print(f'export {k}={shlex.quote(str(v))}')
print(f'MARKER_PY={shlex.quote(py)}')
print(f'API_HOST={shlex.quote(host)}')
print(f'API_PORT={shlex.quote(port)}')
PY
)"

cd "$ROOT/server"
exec "$MARKER_PY" app.py --host "$API_HOST" --port "$API_PORT" "$@"
