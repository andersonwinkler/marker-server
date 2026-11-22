# marker-server HTTP API

Base URL (default): `http://127.0.0.1:8001`

Interactive OpenAPI: `GET /docs` · ReDoc: `GET /redoc`

All conversion work is **asynchronous**: submit a job, poll progress, fetch the result. Jobs run **one at a time** (FIFO). Vision LLM URL/key/model are **server-only** (`config.json`); clients never send them.

---

## Overview

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Short HTML index |
| `GET` | `/version` | Versions + `marker.home` (no LLM secrets) |
| `POST` | `/marker/upload` | Upload a file → start job |
| `POST` | `/marker` | Convert a **server-local** path → start job |
| `GET` | `/marker/jobs/{job_id}` | Job status + progress |
| `GET` | `/marker/jobs/{job_id}/result` | Final payload |
| `DELETE` | `/marker/jobs/{job_id}` | Cancel job |

Typical client flow:

```text
POST /marker/upload
        ↓
   { job_id, status, progress }
        ↓
GET /marker/jobs/{job_id}   (repeat until status is done|failed|cancelled)
        ↓
GET /marker/jobs/{job_id}/result
```

---

## `GET /version`

Returns install provenance. **Never** includes `openai_base_url` or `openai_api_key`.

### Response `200`

```json
{
  "marker_server_config": "/opt/marker-server/0.1.0/config.json",
  "marker_home": "/opt/marker/2.0.0",
  "marker_python": "/opt/marker/2.0.0/venv/bin/python",
  "marker_home_error": null,
  "marker_version": "2.0.0",
  "surya_version": "0.22.1",
  "cuda_visible_devices": "0",
  "llm_enabled": false,
  "llm_model": null,
  "chunking": {
    "chunking": "auto",
    "chunk_threshold": 80,
    "chunk_pages": 50,
    "chunk_overlap": 2,
    "table_aware_cuts": true,
    "table_prescan": true,
    "table_repair": true,
    "table_llm_fallback": true,
    "seam_polish": true,
    "seam_chars": 600
  }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `marker_home_error` | string\|null | Set if `marker.home` is missing/invalid |
| `llm_enabled` | bool | From server config default |
| `llm_model` | string\|null | Model id only when LLM is enabled |
| `chunking` | object | Server default chunking knobs |

---

## Job object

Returned by upload, status, and cancel.

```json
{
  "job_id": "a1b2c3d4…",
  "status": "running",
  "created_at": "2026-11-22T19:00:00+00:00",
  "started_at": "2026-11-22T19:00:01+00:00",
  "finished_at": null,
  "progress": { "done": 2, "total": 7 },
  "stage": "chunk",
  "error": null
}
```

### `status`

| Value | Meaning |
|-------|---------|
| `queued` | Waiting behind other jobs |
| `running` | Worker is converting |
| `done` | Result available at `/result` |
| `failed` | See `error`; `/result` returns HTTP 500 JSON |
| `cancelled` | Cancelled before or during run |

### `progress`

| Field | Type | Meaning |
|-------|------|---------|
| `done` | int | Completed units (usually chunks finished) |
| `total` | int | Planned units (`0` until planning finishes) |

Clients that want a percentage compute `done / total` themselves. There is no fraction field and no human-readable progress string.

The server uses these updates for **stall detection**: if no progress callback arrives for `progress_stall_seconds`, the job is failed and the conversion worker process is killed so the queue continues. Total job duration can exceed that as long as units keep completing.

### `stage` (informational)

Examples: `probe`, `plan`, `chunk`, `table_repair`, `seam_polish`, `stitch`, `convert`, `done`, `failed`, `cancelled`. Treat as optional telemetry; **`done`/`total` are authoritative for progress.**

---

## `POST /marker/upload`

Multipart form upload. Starts a job and returns immediately with a job object.

### Request

`Content-Type: multipart/form-data`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `file` | file | **yes** | — | Document to convert (PDF and other Marker-supported types) |
| `profile` | string | no | `clean` | Delivery profile (see [Profiles](#profiles)) |
| `page_range` | string | no | all pages | e.g. `0,5-10,20` (0-based). **Disables auto-chunking** for this request |
| `mode` | string | no | Marker default | `balanced` \| `fast` |
| `force_ocr` | bool | no | `false` | Force OCR on every page |
| `paginate_output` | bool | no | from profile | Page separators in markdown |
| `output_format` | string | no | from profile | Usually leave unset; use `profile` |
| `use_llm` | bool | no | server config | Use server-configured vision LLM |
| `llm_scope` | string | no | — | `full` \| `tables_equations` \| `none` |
| `extract_images` | bool | no | Marker default | Extract figure crops |
| `ocr_inline_math` | bool | no | — | OCR inline math (useful with `fast`) |
| `disable_ocr` | bool | no | — | Skip OCR entirely |
| `highres_image_dpi` | int | no | — | DPI for OCR / equation crops |
| `lowres_image_dpi` | int | no | — | DPI for layout images |
| `chunking` | string | no | config | `auto` \| `on` \| `off` |
| `chunk_threshold` | int | no | config | Auto-chunk when pages ≥ this |
| `chunk_pages` | int | no | config | Pages per window (e.g. `50`) |
| `chunk_overlap` | int | no | config | Overlap pages (e.g. `2`); later window owns overlap |
| `table_aware_cuts` | bool | no | config | Don’t cut mid-table |
| `table_prescan` | bool | no | config | Pre-scan PDF text for tables |
| `table_repair` | bool | no | config | Re-extract / merge split tables |
| `table_llm_fallback` | bool | no | config | LLM merge if re-Marker still split |
| `seam_polish` | bool | no | config | LLM polish at chunk joins (needs LLM) |
| `seam_chars` | int | no | config | Characters of context each side of join |
| `include_markdown` | bool | no | from profile | Force markdown in result |
| `include_html` | bool | no | from profile | Force HTML in result |
| `include_blocks` | bool | no | from profile | Force `blocks[]` in result |
| `block_text_format` | string | no | `html` | `html` \| `plain` for block `text` |
| `image_delivery` | string | no | `inline_base64` | `inline_base64` \| `refs_only` \| `none` |
| `filter_block_types` | string | no | — | CSV of block types to drop, e.g. `PageHeader,PageFooter` |

Boolean form fields accept `true` / `false` / `1` / `0` / `yes` / `no` / `on` (case-insensitive).

### Not accepted (ignored / stripped)

| Field | Reason |
|-------|--------|
| `openai_base_url` | Server config only |
| `openai_api_key` | Server config only |
| `openai_model` | Server config only |
| `llm_service` | Server config only |

### Response `200`

Job object (see above). `status` is typically `queued`.

### Example

```bash
curl -sS \
  -F 'file=@paper.pdf' \
  -F 'profile=clean' \
  -F 'mode=balanced' \
  -F 'use_llm=false' \
  -F 'chunking=auto' \
  http://127.0.0.1:8001/marker/upload
```

RAG example:

```bash
curl -sS \
  -F 'file=@book.pdf' \
  -F 'profile=rag' \
  -F 'block_text_format=plain' \
  -F 'filter_block_types=PageHeader,PageFooter' \
  -F 'image_delivery=none' \
  http://127.0.0.1:8001/marker/upload
```

---

## `POST /marker`

Same conversion options as upload, but as **JSON**. The file must already exist on the server.

### Request

`Content-Type: application/json`

Body: same fields as [CommonParams](#post-markerupload) plus:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `filepath` | string | **yes** | Absolute path readable by the API process |
| `filename` | string | no | Display/provenance name |

### Response `200`

Job object.

### Errors

| HTTP | When |
|------|------|
| `400` | Missing `filepath` or file not found |
| `403` | `filepath` not under `runtime.allowed_input_dirs` (empty list disables this endpoint) |

### Example

```bash
# Requires runtime.allowed_input_dirs to include /data/inbox (default is []).
curl -sS http://127.0.0.1:8001/marker \
  -H 'Content-Type: application/json' \
  -d '{"filepath":"/data/inbox/doc.pdf","profile":"dual","use_llm":true,"llm_scope":"tables_equations"}'
```

---

## `GET /marker/jobs/{job_id}`

### Response `200`

Job object with current `status`, `progress`, `stage`.

### Errors

| HTTP | When |
|------|------|
| `404` | Unknown `job_id` |

### Example

```bash
curl -sS http://127.0.0.1:8001/marker/jobs/$ID
# {"job_id":"…","status":"running","progress":{"done":2,"total":7},…}
```

---

## `GET /marker/jobs/{job_id}/result`

### Response `200`

Conversion result (see [Result payload](#result-payload)) plus `job_id`.

### Errors

| HTTP | Body | When |
|------|------|------|
| `404` | `{ "detail": "Unknown job_id" }` | Bad id |
| `409` | `{ "detail": "Job was cancelled" }` | Cancelled |
| `409` | `{ "detail": { "message": "Job not finished", …job } }` | Still queued/running |
| `410` | `{ "detail": { "message": "Result expired", …job } }` | Job TTL elapsed (or result was released) |
| `500` | `{ "success": false, "error": "…", …job }` | Conversion failed |

Finished jobs (including the result payload) stay in memory until `runtime.job_ttl_seconds` (default 3600) or until `DELETE /marker/jobs/{id}`. `/result` is safe to retry after a dropped connection.

---

## `DELETE /marker/jobs/{job_id}`

Cancel an active job, or release a finished one early (free memory).

- If `queued`: removed from queue → `cancelled` immediately
- If `running`: the conversion subprocess is **terminated**, so cancel/stall unblocks the queue even when Marker is hung mid-pass (models reload on the next job).
- If `done` / `failed` / `cancelled`: job (and result) are dropped from memory immediately

### Response `200`

Updated job object (for a purged finished job, this is a final snapshot).

### Errors

| HTTP | When |
|------|------|
| `404` | Unknown `job_id` |

---

## Profiles

`profile` sets delivery defaults. Explicit `include_*` / `paginate_output` / `output_format` override the profile.

| Profile | `markdown` | `html` | `blocks` | Notes |
|---------|------------|--------|----------|-------|
| `clean` | yes | — | — | Single readable markdown |
| `paginated` | yes | — | — | Markdown with page markers |
| `rag` | — | — | yes | Flat chunks with page / bbox (RAG) |
| `layout` | — | — | yes | Layout-oriented block tree flatten |
| `html` | — | yes | — | HTML document (any HTML request forces single-pass; no page-block chunking) |
| `dual` | yes | — | yes | Markdown + blocks (**two** Marker passes today) |

---

## Result payload

```json
{
  "success": true,
  "job_id": "a1b2c3d4…",
  "profile": "rag",
  "format": "chunks",
  "output": "…",
  "markdown": null,
  "html": null,
  "blocks": [
    {
      "id": "/page/0/Text/3",
      "block_type": "Text",
      "text": "…",
      "html": "<p>…</p>",
      "page": 0,
      "bbox": [x0, y0, x1, y1],
      "polygon": [[x, y], …],
      "section_hierarchy": { "1": "…" },
      "images": []
    }
  ],
  "images": { "figure.jpg": "<base64 or empty>" },
  "metadata": {},
  "document": {
    "filename": "book.pdf",
    "page_count": 312
  },
  "chunking": {
    "enabled": true,
    "n_pages": 312,
    "n_chunks": 7,
    "chunk_pages": 50,
    "chunk_overlap": 2,
    "join_pages": [47, 95],
    "table_prescan_pages": 12,
    "table_repairs": [],
    "soft_errors": []
  },
  "marker_version": "2.0.0",
  "surya_version": "0.22.1",
  "llm_model": null
}
```

### Field rules

| Field | Present when |
|-------|----------------|
| `markdown` | Profile/flags request markdown |
| `html` | Profile/flags request HTML |
| `blocks` | Profile/flags request blocks |
| `output` | Legacy single blob: primary delivery (markdown, html, or JSON string of blocks) |
| `images` | Per `image_delivery` (`none` → `{}`; `refs_only` → keys with empty strings) |
| `chunking` | Always; `enabled: false` for single-pass jobs |
| `llm_model` | Non-null only if this job used the vision LLM |

### Block object

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Marker block id |
| `block_type` | string | e.g. `Text`, `Table`, `SectionHeader`, `Equation` |
| `text` | string | Per `block_text_format` |
| `html` | string | Block HTML from Marker |
| `page` | int | 0-based page index |
| `bbox` | number[4]\|null | Axis-aligned box |
| `polygon` | number[][]\|null | Quad corners |
| `section_hierarchy` | object\|null | Heading ancestry |
| `images` | string[] | Image ids referenced by the block |

---

## Full client recipe

```bash
BASE=http://127.0.0.1:8001

JOB=$(curl -sS -F 'file=@doc.pdf' -F 'profile=clean' "$BASE/marker/upload")
ID=$(printf '%s' "$JOB" | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')

while true; do
  ST=$(curl -sS "$BASE/marker/jobs/$ID")
  echo "$ST"
  STATUS=$(printf '%s' "$ST" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  case "$STATUS" in done|failed|cancelled) break ;; esac
  sleep 2
done

curl -sS "$BASE/marker/jobs/$ID/result" -o result.json
```

Python sketch:

```python
import time
import requests

base = 'http://127.0.0.1:8001'
with open('doc.pdf', 'rb') as fp:
    r = requests.post(
        f'{base}/marker/upload',
        files={'file': fp},
        data={'profile': 'rag', 'image_delivery': 'none'})
r.raise_for_status()
job_id = r.json()['job_id']

while True:
    st = requests.get(f'{base}/marker/jobs/{job_id}').json()
    done, total = st['progress']['done'], st['progress']['total']
    print(st['status'], f'{done}/{total}')
    if st['status'] in ('done', 'failed', 'cancelled'):
        break
    time.sleep(2)

result = requests.get(f'{base}/marker/jobs/{job_id}/result')
result.raise_for_status()
data = result.json()
blocks = data.get('blocks') or []
```

---

## UI proxy paths

The browser UI (`:8002`) does not redefine the API; it proxies:

| UI path | API path |
|---------|----------|
| `POST /convert` | `POST /marker/upload` |
| `GET /jobs/{id}` | `GET /marker/jobs/{id}` |
| `GET /jobs/{id}/result` | `GET /marker/jobs/{id}/result` |

---

## Notes for integrators

1. **Auth:** this API has no built-in authentication. Keep it on localhost or protect it at the reverse proxy.
2. **Timeouts:** conversion jobs have **no absolute wall-clock limit**. They fail if no progress update arrives for `runtime.progress_stall_seconds` (default 7200); the conversion **subprocess is then killed** so queued jobs can run (Marker models reload on the next job). HTTP timeouts on clients/proxies should cover upload/result transfer and status polls only — not total conversion time. Single-pass jobs only emit progress at start/end, so the stall timer is effectively a per-pass limit — raise it for very long one-shot converts.
3. **Concurrency:** submitting many jobs is fine; they queue. Only one runs. Don’t also run Marker CLI on the same GPU during a heavy job.
4. **Chunking + `page_range`:** a non-empty `page_range` turns off auto page-block splitting for that request. Any request that includes HTML (`profile=html` or `include_html=true`) always uses a single Marker pass (no HTML stitcher).
5. **Dual / mixed deliveries:** `profile=dual` (or any request that asks for both markdown and blocks) currently runs Marker once per output format. Expect roughly 2× GPU time until a single-build multi-render path lands.
6. **`POST /marker` filepath:** converts a server-local path and **never** deletes it. Default `allowed_input_dirs: []` disables path convert (upload only). Set dirs or `null` (any readable path) to enable. Only uploads under `upload_dir` are removed after the job.
7. **Upload size:** capped by `runtime.max_upload_bytes` (default 512 MiB); also set `LimitRequestBody` on the reverse proxy. Non-PDFs / unreadable page counts skip chunking and always use a single pass.
8. **Service user:** systemd units run as `marker` (not root).
9. **Secrets:** rotate keys in server `config.json` and restart `marker-server-api`; never put keys in client requests.
