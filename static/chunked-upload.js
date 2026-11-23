function normalizedUploadFile(file) {
  const name = (file.webkitRelativePath || file.name).replace(/\\/g, '/');
  return new File([file], name, { type: file.type, lastModified: file.lastModified });
}

// 10 MB per chunk keeps each POST well inside Cloudflare's 100-second
// per-request cap even on 1 Mbps upstreams (~80 s wire time). Bumping this
// makes slow uploaders time out mid-chunk with "network error".
const CHUNK_SIZE = 10 * 1024 * 1024;
// Anything above this in a single input still chunks even if no file exceeds
// CHUNK_SIZE (CF Free caps request bodies at 100 MB).
const CF_BODY_LIMIT = 90 * 1024 * 1024;
const MAX_RETRIES = 3;
// Per-XHR timeout — a stalled chunk aborts at 3 min instead of hanging so
// the retry loop can kick in on a fresh connection.
const XHR_TIMEOUT_MS = 180 * 1000;
// Number of files uploaded concurrently per input. Matters for large folders
// (1000+ tiny files); sequential is dominated by per-request round-trip time.
const PARALLEL_UPLOADS = 5;

// XHR-backed POST with upload.progress events. fetch() still can't report
// upload bytes across browsers, so anything that needs a byte-level bar goes
// through here.
function xhrPost(url, body, onUploadProgress, timeoutMs) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.timeout = timeoutMs || 0;   // 0 = no timeout (final form submit)
    if (onUploadProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onUploadProgress(e.loaded, e.total);
      };
    }
    xhr.onload = () => {
      const finalUrl = xhr.responseURL || url;
      if (xhr.status >= 200 && xhr.status < 400) {
        resolve({ ok: true, status: xhr.status, body: xhr.responseText, url: finalUrl });
      } else {
        resolve({ ok: false, status: xhr.status, body: xhr.responseText, url: finalUrl });
      }
    };
    xhr.onerror = () => reject(new Error('Network error'));
    xhr.ontimeout = () => reject(new Error('Upload timed out (' + Math.round((timeoutMs||0)/1000) + 's)'));
    xhr.send(body);
  });
}

class ChunkedUploader {
  constructor(file, options = {}) {
    this.file = file;
    this.chunkSize = options.chunkSize || CHUNK_SIZE;
    // onProgress(bytesSent, totalSize)
    this.onProgress = options.onProgress || (() => {});
  }

  async upload() {
    const totalChunks = Math.ceil(this.file.size / this.chunkSize);

    const initResp = await xhrPost('/api/upload/init',
      new Blob([JSON.stringify({
        filename: (this.file.webkitRelativePath || this.file.name).replace(/\\/g, '/'),
        total_size: this.file.size,
        chunk_size: this.chunkSize,
      })], { type: 'application/json' }));
    if (!initResp.ok) throw new Error('Failed to init upload: HTTP ' + initResp.status);
    const { upload_id } = JSON.parse(initResp.body);

    let bytesDoneBefore = 0;
    for (let i = 0; i < totalChunks; i++) {
      const start = i * this.chunkSize;
      const end = Math.min(start + this.chunkSize, this.file.size);
      const chunk = this.file.slice(start, end);
      await this._uploadChunk(upload_id, i, chunk, (loaded) => {
        this.onProgress(bytesDoneBefore + loaded, this.file.size);
      });
      bytesDoneBefore += (end - start);
      this.onProgress(bytesDoneBefore, this.file.size);
    }

    const completeResp = await xhrPost(`/api/upload/${upload_id}/complete`, null);
    if (!completeResp.ok) throw new Error('Failed to complete upload: HTTP ' + completeResp.status);
    return upload_id;
  }

  async _uploadChunk(uploadId, index, blob, onChunkProgress) {
    let lastErr;
    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      try {
        const resp = await xhrPost(`/api/upload/${uploadId}/chunk/${index}`, blob, onChunkProgress, XHR_TIMEOUT_MS);
        if (resp.ok) return;
        lastErr = new Error(`Chunk ${index} failed: HTTP ${resp.status}`);
      } catch (e) {
        lastErr = e;
      }
      await new Promise(r => setTimeout(r, 1000 * Math.pow(2, attempt)));
    }
    throw lastErr;
  }
}

function setupChunkedForm(form, fieldMap) {
  form.addEventListener('submit', async function (e) {
    // Gather all file inputs we're responsible for
    const inputs = [];
    let grandTotal = 0;
    for (const inputName of Object.keys(fieldMap)) {
      const input = form.querySelector(`input[name="${inputName}"]`);
      if (!input || !input.files || !input.files.length) continue;
      const files = Array.from(input.files, normalizedUploadFile);
      const total = files.reduce((s, f) => s + f.size, 0);
      grandTotal += total;
      inputs.push({ inputName, idsFieldName: fieldMap[inputName], files, total });
    }
    if (inputs.length === 0) return; // No files — normal submit

    e.preventDefault();
    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.dataset.origText = submitBtn.textContent;
      submitBtn.textContent = 'Uploading...';
    }

    const overallBar = ensureOverallProgressBar(form);
    const perFileTracker = new Map(); // fileKey -> bytesSent
    let filesDone = 0;
    let filesTotal = inputs.reduce((s, i) => s + i.files.length, 0);
    const bumpOverall = () => {
      let sent = 0;
      for (const v of perFileTracker.values()) sent += v;
      updateOverallProgressBar(overallBar, sent, grandTotal, filesDone, filesTotal);
    };

    try {
      const formData = new FormData();
      for (const [key, value] of new FormData(form).entries()) {
        if (value instanceof File) continue;
        formData.set(key, value);
      }

      for (const { inputName, idsFieldName, files, total } of inputs) {
        const input = form.querySelector(`input[name="${inputName}"]`);
        const chunkThisInput = files.some(f => f.size > CHUNK_SIZE) || total > CF_BODY_LIMIT;
        if (chunkThisInput) {
          const uploadIds = new Array(files.length);
          let nextIdx = 0;

          const uploadOne = async (fi) => {
            const file = files[fi];
            const perBar = getOrCreateProgressBar(input, fi);
            const key = `${inputName}#${fi}`;
            perFileTracker.set(key, 0);
            const uploader = new ChunkedUploader(file, {
              onProgress: (loaded, tot) => {
                perFileTracker.set(key, loaded);
                updateProgressBar(perBar, loaded, tot, file.name, fi + 1, files.length);
                bumpOverall();
              },
            });
            uploadIds[fi] = await uploader.upload();
            perFileTracker.set(key, file.size);
            filesDone++;
            removeProgressBar(input, fi);   // free the UI slot for the next worker
            bumpOverall();
          };

          const worker = async () => {
            while (true) {
              const my = nextIdx++;
              if (my >= files.length) return;
              await uploadOne(my);
            }
          };

          const workerCount = Math.min(PARALLEL_UPLOADS, files.length);
          await Promise.all(Array.from({ length: workerCount }, () => worker()));
          formData.set(idsFieldName, JSON.stringify(uploadIds));
        } else {
          // Small enough to send in the final POST — its bytes get tracked
          // in the overall bar via that POST's upload progress.
          for (const file of files) formData.append(inputName, file);
        }
      }

      // Final form submit — includes any non-chunked files. Track its upload bytes
      // as a single virtual entry so the overall bar keeps advancing to 100%.
      const smallFilesTotal = inputs
        .filter(i => !(i.files.some(f => f.size > CHUNK_SIZE) || i.total > CF_BODY_LIMIT))
        .reduce((s, i) => s + i.total, 0);
      const virtualKey = '__final_post__';
      if (smallFilesTotal > 0) perFileTracker.set(virtualKey, 0);

      const resp = await xhrPost(form.action || window.location.href, formData, (loaded) => {
        if (smallFilesTotal > 0) {
          perFileTracker.set(virtualKey, Math.min(loaded, smallFilesTotal));
          bumpOverall();
        }
      });

      if (!resp.ok) throw new Error('Submit failed: HTTP ' + resp.status);
      updateOverallProgressBar(overallBar, grandTotal, grandTotal, filesTotal, filesTotal);

      // Follow response — server usually 302s to job status
      if (resp.url && resp.url !== (form.action || window.location.href)) {
        window.location.href = resp.url;
      } else {
        document.documentElement.innerHTML = resp.body;
      }
    } catch (err) {
      alert('Upload failed: ' + err.message);
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = submitBtn.dataset.origText || 'Submit';
      }
    }
  });
}

// ── UI helpers ──────────────────────────────────────────────────────────

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function ensureOverallProgressBar(form) {
  let bar = form.querySelector('.upload-overall-progress');
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'upload-overall-progress chunked-progress';
    bar.innerHTML =
      '<div class="chunked-progress-label">Preparing…</div>' +
      '<div class="chunked-progress-track"><div class="chunked-progress-fill"></div></div>';
    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn && submitBtn.parentElement) {
      submitBtn.parentElement.insertBefore(bar, submitBtn);
    } else {
      form.appendChild(bar);
    }
  }
  bar.dataset.startedAt = String(Date.now());
  return bar;
}

function updateOverallProgressBar(bar, loaded, total, filesDone, filesTotal) {
  const label = bar.querySelector('.chunked-progress-label');
  const fill  = bar.querySelector('.chunked-progress-fill');
  const pct   = total > 0 ? Math.round(loaded / total * 100) : 0;
  const started = Number(bar.dataset.startedAt) || Date.now();
  const elapsed = (Date.now() - started) / 1000;
  const rate    = elapsed > 0 ? loaded / elapsed : 0;
  const eta     = rate > 0 && loaded < total ? Math.max(0, Math.round((total - loaded) / rate)) : 0;
  const rateStr = rate > 0 ? ` @ ${fmtBytes(rate)}/s` : '';
  const etaStr  = eta > 0 ? `, ~${eta}s left` : '';
  const fileStr = (filesTotal && filesTotal > 1) ? ` — ${filesDone}/${filesTotal} files` : '';
  label.textContent = `${fmtBytes(loaded)} / ${fmtBytes(total)} (${pct}%)${fileStr}${rateStr}${etaStr}`;
  fill.style.width = pct + '%';
}

function removeProgressBar(input, fileIndex) {
  const drop = input.closest('.file-drop');
  const container = drop || input.parentElement;
  const wrapper = container.querySelector('.chunked-progress-wrapper');
  if (!wrapper) return;
  const bar = wrapper.querySelector(`[data-file-index="${fileIndex}"]`);
  if (bar) bar.remove();
}

function getOrCreateProgressBar(input, fileIndex) {
  const drop = input.closest('.file-drop');
  const container = drop || input.parentElement;
  let wrapper = container.querySelector('.chunked-progress-wrapper');
  if (!wrapper) {
    wrapper = document.createElement('div');
    wrapper.className = 'chunked-progress-wrapper';
    container.appendChild(wrapper);
  }
  let bar = wrapper.querySelector(`[data-file-index="${fileIndex}"]`);
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'chunked-progress';
    bar.dataset.fileIndex = fileIndex;
    bar.innerHTML =
      '<div class="chunked-progress-label"></div>' +
      '<div class="chunked-progress-track"><div class="chunked-progress-fill"></div></div>';
    wrapper.appendChild(bar);
  }
  return bar;
}

function updateProgressBar(bar, loaded, total, filename, fileNum, totalFiles) {
  const label = bar.querySelector('.chunked-progress-label');
  const fill  = bar.querySelector('.chunked-progress-fill');
  const pct   = total > 0 ? Math.round(loaded / total * 100) : 0;
  const prefix = totalFiles > 1 ? `${filename} (${fileNum}/${totalFiles})` : filename;
  label.textContent = `${prefix} — ${fmtBytes(loaded)}/${fmtBytes(total)} (${pct}%)`;
  fill.style.width = pct + '%';
}
