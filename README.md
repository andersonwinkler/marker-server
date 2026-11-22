# marker-server

Jobs API and browser UI for [Marker](https://github.com/datalab-to/marker) / Surya.

This package does **not** vendor `marker-pdf`, Torch, or Surya weights. You install vanilla Marker somewhere else, then point `config.json` → `marker.home` at that tree. The API process runs with that install’s Python.

Version: see [`VERSION`](VERSION) (independent of the `marker-pdf` version).

**API reference:** [`docs/API.md`](docs/API.md) (endpoints, job lifecycle, request fields, result schema). Live OpenAPI: `http://<host>:8001/docs`.

---

## Why this exists

Marker ships a small demo server (`marker_server` → `marker.scripts.server`). It converts one PDF per HTTP request and returns when finished.

**marker-server** is a separate stack meant for real use:

| | Stock `marker_server` | This project |
|--|----------------------|--------------|
| Request model | Blocking convert | Job queue → poll → result |
| Progress | None | `{done, total}` |
| Long PDFs | One shot | Sequential overlapping page blocks |
| Delivery | Single `output` blob | Profiles: clean / RAG / dual / … |
| Vision LLM | Ad hoc / env | Server `config.json` only (clients never see URL/key) |
| UI | `/docs` only | Drop-zone UI with progress bar |
| Install | Inside Marker’s venv | `/opt/marker-server/<ver>/` next to Marker |

Same engine underneath (`PdfConverter`). Jobs never run in parallel against each other (one GPU-friendly worker).

---

## Layout

```text
marker-server/
  VERSION
  config.example.json
  README.md                 ← this file
  bin/run-api.sh            ← starts API with Marker’s python + env from config
  bin/run-ui.sh
  server/                   ← FastAPI app (no Marker weights)
  scripts/install.sh        → /opt/marker-server/<VERSION>/
  scripts/uninstall.sh      ← stop services; remove prefix (optional --all/--purge)
  systemd/
    marker-server-api.service
    marker-server-ui.service
```

Typical prefixes:

```text
/opt/marker/<marker-pdf-ver>/     # vanilla Marker (separate package)
/opt/marker-server/<this-ver>/    # this project
/opt/marker/current
/opt/marker-server/current
```

---

## Prerequisites

1. **Vanilla Marker** installed and importable, e.g. `/opt/marker/2.0.0/venv/bin/python` with `marker-pdf` installed.
2. **NVIDIA GPU + driver** if you want GPU OCR (recommended).
3. **Docker** (with NVIDIA Container Toolkit) if you leave Surya’s backend on the default **vLLM** path (NVIDIA). You do **not** need a hand-built `llama-server` unless you set `surya.inference_backend` to `llamacpp`.
4. Optional **OpenAI-compatible vision** endpoint for Marker `use_llm` (tables/equations refinement, seam polish). Configure later in `config.json`; default is off.

---

## Install

```bash
# 1) Vanilla Marker (example; use your own installer or pip + GGUFs)
#    Result: /opt/marker/2.0.0/venv/bin/python works with `import marker`

# 2) This server
export MARKER_HOME=/opt/marker/2.0.0
./scripts/install.sh

# 3) Edit secrets / GPU / LLM
sudoedit /opt/marker-server/current/config.json   # mode 600

# 4) Enable
sudo systemctl enable --now marker-server-api.service marker-server-ui.service
```

Uninstall (leaves Marker itself alone):

```bash
./scripts/uninstall.sh              # stop services, remove this VERSION under /opt
./scripts/uninstall.sh --all        # remove every /opt/marker-server/* version
./scripts/uninstall.sh --purge      # also drop marker user, state dir, and Marker ACLs
```

`install.sh`:

- Creates system user `marker` (override with `MARKER_USER=…`) with home `/var/lib/marker-server`; generates `auth.api_key` on first install (or use `MARKER_SERVER_API_KEY`)
- Adds `video` / `render` for GPU nodes; adds `docker` only for vLLM (or `MARKER_DOCKER=1`) — docker membership is effectively root
- Installs code as **root-owned**; only `uploads/` is writable by `marker`; `config.json` is `root:marker` mode `640`
- Grants `marker` read access to `marker.home` via ACL + **default ACL** on directories (so later pip/GGUF files stay readable), and keeps only `cache/` + `hf/` mode `700` owned by `marker` (CLI remains usable by other local users)
- Installs thin HTTP deps into **Marker’s venv** and a small local UI venv; systemd units use `User=marker`

API: `http://127.0.0.1:8001` · UI: `http://127.0.0.1:8002` · OpenAPI: `/docs`

---

## Configuration

File: `/opt/marker-server/<ver>/config.json` (see `config.example.json`).

Path resolution order: `MARKER_SERVER_CONFIG` → `MARKER_CONFIG_FILE` → `<prefix>/config.json` → `/opt/marker-server/current/config.json`.

### `marker`

| Key | Meaning |
|-----|---------|
| `home` | Absolute vanilla Marker prefix (required) |
| `python` | Optional interpreter override; default `home/venv/bin/python` |

### `auth`

| Key | Meaning |
|-----|---------|
| `api_key` | When non-empty, API routes (except `/`, `/docs`, `/redoc`, `/openapi.json`) require `Authorization: Bearer <api_key>` (Basic with that password also works). Override with `MARKER_SERVER_API_KEY`. Install generates a key on first install **and** on upgrade if missing/empty |
| `ui_password` | Optional separate UI login (HTTP Basic / Bearer). `null`/omit → fall back to `api_key`. Does **not** protect the API by itself. Override with `MARKER_SERVER_UI_PASSWORD` |

### `runtime`

| Key | Meaning |
|-----|---------|
| `cuda_visible_devices` | GPU index or UUID for the API process |
| `torch_device` | Usually `cuda` |
| `host` / `port` / `ui_port` | Bind addresses |
| `upload_dir` | Temp uploads; default `<prefix>/uploads` |
| `max_upload_bytes` | Reject larger uploads (default 512 MiB); also enforce at the reverse proxy |
| `job_ttl_seconds` | Drop finished jobs (including results) after this many seconds (default 3600); `DELETE` releases early |
| `progress_stall_seconds` | Fail a job if no progress update arrives for this long (default **7200**), then **kill the conversion subprocess**, **stop Surya’s OCR server** (docker/PID from `~/.cache/datalab/surya/*_server.json`), and continue the queue. A 10-hour job is fine if progress keeps moving. `0` disables. Single-pass jobs only update at start/end, so this is effectively a per-pass limit — raise it for huge one-shot HTML converts |
| `worker_ready_seconds` | How long to wait for the conversion worker to load models and report ready (default **1800**). The job-scheduler thread loads the worker at startup (jobs submitted during load stay queued) |
| `allowed_input_dirs` | Allowlist for `POST /marker` paths. `[]` (default) disables path convert; `null` allows any path the service user can read |

### `surya`

| Key | Meaning |
|-----|---------|
| `inference_backend` | `null`/omit = Surya auto (vLLM on NVIDIA). Or `vllm` / `llamacpp` |
| `llama_cpp_binary` | Only if using `llamacpp` |
| `gguf_model` / `gguf_mmproj` | Optional; default under `marker.home/surya-ocr-2/` if files exist |
| `inference_parallel` | Concurrent OCR slots |
| `inference_keep_alive` | Keep spawned OCR server between jobs |
| `guided_layout` | Surya guided layout flag |
| `llama_cpp_extra_args` | Extra args for llama-server when used |

### `llm` (server-only; never accept these from clients)

| Key | Meaning |
|-----|---------|
| `use_llm` | Default for hybrid vision refinement |
| `openai_base_url` | OpenAI-compatible `/v1` base |
| `openai_model` | Model id |
| `openai_api_key` | Bearer token |
| `timeout` | Per-call timeout (seconds) |
| `llm_service` | Marker service class path |

Clients may still send per-request `use_llm` / `llm_scope`. They cannot set the endpoint.

### `chunking`

| Key | Default | Meaning |
|-----|---------|---------|
| `chunking` | `auto` | `auto` / `on` / `off` |
| `chunk_threshold` | `80` | In `auto`, chunk when page count ≥ this |
| `chunk_pages` | `50` | Target pages per Marker window |
| `chunk_overlap` | `2` | Overlap; **later chunk owns** overlap pages |
| `table_aware_cuts` | `true` | Shift cuts out of table runs |
| `table_repair` / `seam_polish` | `true` | Post-join fixes (seam polish needs LLM) |

Restart `marker-server-api` after editing config.

---

## API

Full reference: **[`docs/API.md`](docs/API.md)**.

Summary:

```text
POST /marker/upload  →  { job_id, status, progress: { done, total } }
GET  /marker/jobs/{id}
GET  /marker/jobs/{id}/result
DELETE /marker/jobs/{id}
```

Jobs are FIFO (one conversion at a time). Progress is numeric `done` / `total` only.

---

## UI

`marker-server-ui` proxies:

- `POST /convert` → API `/marker/upload`
- `GET /jobs/{id}` and `/jobs/{id}/result`

The page polls progress and shows `done/total`. It does not expose LLM endpoint fields.

---

## Chunking behavior

When enabled, the server:

1. Plans overlapping windows (`chunk_pages`, `chunk_overlap`)
2. Runs Marker **one window after another** (never parallel chunks)
3. Keeps exclusive ownership of pages (overlap owned by the later window)
4. Optionally repairs tables across joins and LLM-polishes prose seams
5. Stitches markdown and/or merges RAG blocks by owned pages

Progress `total` is the number of planned chunks.

**Limits:** any HTML delivery always uses a single pass (no HTML stitcher). Prefer `clean` / `rag` / `dual` for long PDFs so chunk progress is visible. Stall/cancel kills the conversion subprocess (models reload on the next job). `dual` may run Marker twice (progress `total` reflects the pass count).

---

## Relation to vanilla Marker CLI

Marker under `marker.home` remains usable on its own:

```bash
"$MARKER_HOME/venv/bin/marker_single" file.pdf --output_dir /tmp/out
```

Stopping marker-server does not remove Marker. Avoid running a heavy CLI convert and an API job on the same GPU at once (VRAM contention only — installs are not coupled beyond sharing that venv).

Stock `marker_server` in the Marker venv is **not** this project; do not confuse the two.

---

## Reverse proxy (optional)

See [`apache/marker-proxy.conf.example`](apache/marker-proxy.conf.example) for a full httpd example (`/marker/` → UI, `/marker-api/` → API, long timeouts, fetch URL rewrite).

---

## Development layout

Run from a checkout (Marker already installed):

```bash
cp config.example.json config.json
# set marker.home, runtime.cuda_visible_devices, llm.* as needed
./bin/run-api.sh
./bin/run-ui.sh
```

---

## Security notes

- systemd runs API and UI as **`marker`** (not root). Code under `/opt/marker-server/<ver>/` is root-owned; the service only writes `uploads/` and Marker `cache/`/`hf/`.
- **Docker ≈ root:** a **default install** (`surya.inference_backend` unset → vLLM) still adds `marker` to the `docker` group and prints a warning. That undoes much of the privilege drop. Choose deliberately: keep vLLM, or set `surya.inference_backend=llamacpp` / `MARKER_DOCKER=0` to skip docker.
- **API key:** set `auth.api_key` (install generates one, including on upgrade if empty). API clients send `Authorization: Bearer <key>`. The UI uses `auth.ui_password` if set, otherwise the same API key (browser Basic-auth prompt, or Bearer). Default bind is `127.0.0.1`. Off localhost, put **HTTPS** (or equivalent TLS) in front — Basic auth is only base64.
- `auth.ui_password` only protects the UI. An empty `auth.api_key` leaves the **API unauthenticated** even if `ui_password` is set. Empty both → neither is authenticated (not recommended).
- Optionally set a distinct `auth.ui_password` so a browser-saved UI login is not the full API token.
- Marker under `marker.home` stays readable for local CLI use; only `cache/` and `hf/` are mode `700` owned by `marker` (HF tokens). The service user gets ACL `rX` on the tree.
- LLM credentials and `auth.api_key` live only in server `config.json` (`root:marker` mode `640`). Never commit that file; clients cannot supply the LLM endpoint or key.
- Uploaded filenames are sanitized (basename only). Only files under `upload_dir` created by `/marker/upload` are deleted after a job; `POST /marker` never deletes its `filepath` and is disabled by default (`allowed_input_dirs: []`).
- Uploads are capped by `max_upload_bytes` (and `LimitRequestBody` in the Apache example).
- HTTP / reverse-proxy timeouts cover **upload and result transfer only**. Conversion jobs are async; a **progress stall** fails the job, kills the conversion worker, and stops Surya’s OCR server so the next job does not reuse a hung inference backend (models reload on the next job).

## License

MIT — see [`LICENSE`](LICENSE).

This project does **not** redistribute Marker or Surya; those have their own licenses. Operators supply `marker.home` and secrets in the installed `config.json` (not in this repository).
