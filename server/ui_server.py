#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''Browser UI for Marker: upload, poll job progress, download result.'''

import os

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from http_limits import max_upload_bytes, reject_oversized_content_length

MARKER_API = os.getenv('MARKER_API_BASE', 'http://127.0.0.1:8001').rstrip('/')

PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Marker</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font: 16px/1.45 "Segoe UI", system-ui, sans-serif;
    background: #f4f1ea;
    color: #1c1915;
  }
  main { max-width: 820px; margin: 0 auto; padding: 32px 20px 64px; }
  h1 { font-size: 28px; font-weight: 650; margin: 0 0 4px; }
  p.lead { margin: 0 0 24px; color: #5c564c; }
  form { display: grid; gap: 14px; }
  .drop {
    border: 1.5px dashed #8a8175;
    background: #fffdf8;
    border-radius: 12px;
    min-height: 160px;
    display: grid;
    place-items: center;
    text-align: center;
    padding: 24px;
    cursor: pointer;
  }
  .drop.over { border-color: #1c1915; background: #fff; }
  .drop strong { display: block; font-size: 18px; }
  .drop span { color: #5c564c; }
  input[type="file"] { display: none; }
  .row { display: flex; flex-wrap: wrap; gap: 12px; }
  label.field { display: grid; gap: 4px; font-size: 13px; color: #5c564c; }
  label.field input, label.field select {
    font: inherit; color: inherit; background: #fffdf8;
    border: 1px solid #c9c1b4; border-radius: 8px; padding: 8px 10px;
  }
  label.check { display: flex; align-items: center; gap: 8px; font-size: 14px; }
  button {
    font: inherit; background: #1c1915; color: #f4f1ea;
    border: 0; border-radius: 8px; padding: 10px 16px; cursor: pointer;
  }
  button:disabled { opacity: 0.55; cursor: wait; }
  button.secondary { background: transparent; color: #1c1915; border: 1px solid #1c1915; }
  #status { min-height: 1.4em; color: #5c564c; }
  #status.error { color: #8d2a2a; }
  #result { display: none; margin-top: 8px; }
  .actions { display: flex; gap: 8px; margin: 8px 0 12px; }
  pre, iframe {
    width: 100%; margin: 0; background: #fffdf8; border: 1px solid #c9c1b4;
    border-radius: 12px;
  }
  pre { white-space: pre-wrap; padding: 16px; max-height: 480px; overflow: auto; }
  iframe { height: 480px; border: 1px solid #c9c1b4; }
  .bar {
    height: 8px; background: #e4ddd2; border-radius: 999px; overflow: hidden;
    display: none; margin-top: 8px;
  }
  .bar > span { display: block; height: 100%; width: 0; background: #1c1915; }
</style>
</head>
<body>
<main>
  <h1>Marker</h1>
  <p class="lead">Drop a file, or choose one. Long documents are converted in page blocks; progress updates while you wait.</p>
  <form id="form">
    <label class="drop" id="drop">
      <input id="file" type="file" accept=".pdf,.png,.jpg,.jpeg,.gif,.webp,.tif,.tiff,.bmp,.docx,.pptx,.xlsx,.html,.htm,.epub,application/pdf">
      <div><strong>Drop a file here</strong><span id="filename">PDF, image, Word, PowerPoint, Excel, HTML, or EPUB</span></div>
    </label>
    <div><button type="button" id="pick" class="secondary">Choose a file</button></div>
    <div class="row">
      <label class="field">Profile
        <select name="profile">
          <option value="clean">Clean markdown</option>
          <option value="paginated">Paginated markdown</option>
          <option value="rag">RAG chunks</option>
          <option value="dual">Dual (markdown + chunks)</option>
          <option value="layout">Layout JSON</option>
          <option value="html">HTML</option>
        </select>
      </label>
      <label class="field">Mode
        <select name="mode">
          <option value="balanced">Balanced</option>
          <option value="fast">Fast</option>
        </select>
      </label>
      <label class="field">Chunking
        <select name="chunking">
          <option value="auto">Auto</option>
          <option value="on">On</option>
          <option value="off">Off</option>
        </select>
      </label>
      <label class="field">Pages
        <input name="page_range" placeholder="all, or 0,5-10" autocomplete="off">
      </label>
    </div>
    <label class="check"><input type="checkbox" name="force_ocr"> Force OCR on every page</label>
    <label class="check"><input type="checkbox" name="use_llm" checked> Use vision LLM (server-configured)</label>
    <div><button id="go" type="submit">Convert</button></div>
  </form>
  <p id="status"></p>
  <div class="bar" id="bar"><span id="barfill"></span></div>
  <section id="result">
    <div class="actions">
      <button type="button" id="save" class="secondary">Download</button>
      <button type="button" id="savezip" class="secondary">Download with images</button>
    </div>
    <pre id="preview"></pre>
    <iframe id="frame" hidden></iframe>
  </section>
</main>
<script>
const form = document.getElementById("form");
const fileInput = document.getElementById("file");
const drop = document.getElementById("drop");
const filename = document.getElementById("filename");
const status = document.getElementById("status");
const result = document.getElementById("result");
const preview = document.getElementById("preview");
const frame = document.getElementById("frame");
const go = document.getElementById("go");
const bar = document.getElementById("bar");
const barfill = document.getElementById("barfill");
let last = null;

document.getElementById("pick").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  filename.textContent = fileInput.files[0] ? fileInput.files[0].name : "PDF, image, Word, PowerPoint, Excel, HTML, or EPUB";
});
["dragover", "dragenter"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault();
  drop.classList.add("over");
}));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault();
  drop.classList.remove("over");
}));
drop.addEventListener("drop", e => {
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  const dt = new DataTransfer();
  dt.items.add(f);
  fileInput.files = dt.files;
  filename.textContent = f.name;
});

function stem(name) {
  return name.replace(/\\.[^.]+$/, "") || "document";
}
function ext(fmt) {
  return fmt === "markdown" ? "md" : fmt === "html" ? "html" : "json";
}
function downloadBlob(name, blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
const CRC = new Uint32Array(256);
for (let n = 0; n < 256; n++) {
  let c = n;
  for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
  CRC[n] = c >>> 0;
}
function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = CRC[(c ^ bytes[i]) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function u16(n) { return [n & 255, (n >>> 8) & 255]; }
function u32(n) { return [n & 255, (n >>> 8) & 255, (n >>> 16) & 255, (n >>> 24) & 255]; }
function zipStored(files) {
  const parts = [];
  const central = [];
  let offset = 0;
  const enc = new TextEncoder();
  for (const file of files) {
    const name = enc.encode(file.name);
    const data = file.data;
    const crc = crc32(data);
    const local = [
      ...u32(0x04034b50), ...u16(20), ...u16(0), ...u16(0), ...u16(0), ...u16(0),
      ...u32(crc), ...u32(data.length), ...u32(data.length), ...u16(name.length), ...u16(0),
    ];
    parts.push(new Uint8Array(local), name, data);
    central.push([
      ...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0), ...u16(0), ...u16(0), ...u16(0),
      ...u32(crc), ...u32(data.length), ...u32(data.length), ...u16(name.length), ...u16(0), ...u16(0),
      ...u16(0), ...u16(0), ...u32(0), ...u32(offset),
    ]);
    central.push(name);
    offset += local.length + name.length + data.length;
  }
  const centralBytes = [];
  let centralSize = 0;
  for (const part of central) {
    const bytes = part instanceof Uint8Array ? part : new Uint8Array(part);
    centralBytes.push(bytes);
    centralSize += bytes.length;
  }
  const end = new Uint8Array([
    ...u32(0x06054b50), ...u16(0), ...u16(0), ...u16(files.length), ...u16(files.length),
    ...u32(centralSize), ...u32(offset), ...u16(0),
  ]);
  return new Blob([...parts, ...centralBytes, end]);
}
function b64bytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function setProgress(done, total) {
  bar.style.display = "block";
  const pct = total > 0 ? Math.min(100, Math.round(100 * done / total)) : 0;
  barfill.style.width = pct + "%";
  status.textContent = total > 0
    ? ("Working… " + done + "/" + total)
    : "Queued…";
}

form.addEventListener("submit", async e => {
  e.preventDefault();
  const file = fileInput.files[0];
  if (!file) {
    status.textContent = "Choose a PDF first.";
    status.className = "error";
    return;
  }
  const body = new FormData();
  body.append("file", file);
  body.append("profile", form.profile.value);
  body.append("mode", form.mode.value);
  body.append("chunking", form.chunking.value);
  body.append("force_ocr", form.force_ocr.checked ? "true" : "false");
  body.append("use_llm", form.use_llm.checked ? "true" : "false");
  if (form.page_range.value.trim()) body.append("page_range", form.page_range.value.trim());
  go.disabled = true;
  result.style.display = "none";
  status.className = "";
  setProgress(0, 0);
  const started = Date.now();
  try {
    const startRes = await fetch("/convert", { method: "POST", body });
    const start = await startRes.json();
    if (!startRes.ok) throw new Error(start.detail || start.error || "Upload failed");
    const jobId = start.job_id;
    if (!jobId) throw new Error("No job_id returned");

    let st = start;
    while (st.status === "queued" || st.status === "running") {
      setProgress(st.progress?.done || 0, st.progress?.total || 0);
      await sleep(1500);
      const poll = await fetch("/jobs/" + jobId);
      st = await poll.json();
      if (!poll.ok) throw new Error(st.detail || "Poll failed");
    }
    if (st.status === "failed") throw new Error(st.error || "Conversion failed");
    if (st.status === "cancelled") throw new Error("Job cancelled");

    const res = await fetch("/jobs/" + jobId + "/result");
    const data = await res.json();
    if (!res.ok || data.success === false) throw new Error(data.error || data.detail || "Result failed");

    const out = data.markdown != null ? data.markdown
      : (data.html != null ? data.html
      : (data.blocks != null ? JSON.stringify({blocks: data.blocks}, null, 2)
      : (data.output || "")));
    const fmt = data.markdown != null ? "markdown"
      : (data.html != null ? "html" : (data.format || "markdown"));
    last = { name: file.name, format: fmt, output: out, images: data.images || {} };
    const seconds = Math.round((Date.now() - started) / 1000);
    const imageCount = Object.keys(last.images).length;
    const prog = st.progress || {};
    status.textContent = "Finished in " + seconds + "s"
      + (prog.total ? (" (" + prog.done + "/" + prog.total + ")") : "")
      + "." + (imageCount ? (" " + imageCount + " image" + (imageCount === 1 ? "" : "s") + " extracted.") : "");
    bar.style.display = "none";
    document.getElementById("savezip").hidden = imageCount === 0;
    if (fmt === "html") {
      preview.hidden = true;
      frame.hidden = false;
      frame.srcdoc = last.output;
    } else {
      frame.hidden = true;
      preview.hidden = false;
      preview.textContent = last.output;
    }
    result.style.display = "block";
  } catch (err) {
    status.textContent = err.message || String(err);
    status.className = "error";
    bar.style.display = "none";
  } finally {
    go.disabled = false;
  }
});

document.getElementById("save").addEventListener("click", () => {
  if (!last) return;
  const name = stem(last.name) + "." + ext(last.format);
  downloadBlob(name, new Blob([last.output], { type: "text/plain;charset=utf-8" }));
});
document.getElementById("savezip").addEventListener("click", () => {
  if (!last) return;
  const enc = new TextEncoder();
  const files = [{ name: stem(last.name) + "." + ext(last.format), data: enc.encode(last.output) }];
  for (const [name, b64] of Object.entries(last.images)) {
    if (!b64) continue;
    files.push({ name, data: b64bytes(b64) });
  }
  downloadBlob(stem(last.name) + ".zip", zipStored(files));
});
</script>
</body>
</html>
'''

app = FastAPI()


@app.get('/')
def index():
    return HTMLResponse(PAGE)


@app.post('/convert')
async def convert(request: Request):
    reject_oversized_content_length(request)
    content_type = request.headers.get('content-type', '')
    max_bytes = max_upload_bytes()
    # Upload transfer only — conversion is async; no job-duration HTTP timeout.
    timeout = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)

    async def body_iter():
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if max_bytes > 0 and total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f'Upload exceeds max_upload_bytes ({max_bytes})')
            yield chunk

    headers = {'content-type': content_type}
    cl = request.headers.get('content-length')
    if cl:
        headers['content-length'] = cl
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.post(
                f'{MARKER_API}/marker/upload',
                content=body_iter(),
                headers=headers)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code, content={'detail': exc.detail})
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type='application/json')


@app.get('/jobs/{job_id}')
async def jobs_proxy(job_id: str):
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream = await client.get(f'{MARKER_API}/marker/jobs/{job_id}')
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type='application/json')


@app.get('/jobs/{job_id}/result')
async def jobs_result_proxy(job_id: str):
    timeout = httpx.Timeout(120.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream = await client.get(f'{MARKER_API}/marker/jobs/{job_id}/result')
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type='application/json')


if __name__ == '__main__':
    host = os.getenv('MARKER_UI_HOST', '127.0.0.1')
    port = int(os.getenv('MARKER_UI_PORT', '8002'))
    uvicorn.run(app, host=host, port=port)
