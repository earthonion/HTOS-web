const CHUNK_SIZE = 50 * 1024 * 1024; // 50MB
const MAX_RETRIES = 3;

class ChunkedUploader {
  constructor(file, options = {}) {
    this.file = file;
    this.chunkSize = options.chunkSize || CHUNK_SIZE;
    this.onProgress = options.onProgress || (() => {});
  }

  async upload() {
    const totalChunks = Math.ceil(this.file.size / this.chunkSize);

    // 1. Init upload session
    const initResp = await fetch('/api/upload/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filename: this.file.name,
        total_size: this.file.size,
        chunk_size: this.chunkSize,
      }),
    });
    if (!initResp.ok) throw new Error('Failed to init upload');
    const { upload_id } = await initResp.json();

    // 2. Upload chunks
    for (let i = 0; i < totalChunks; i++) {
      const start = i * this.chunkSize;
      const end = Math.min(start + this.chunkSize, this.file.size);
      const chunk = this.file.slice(start, end);

      await this._uploadChunk(upload_id, i, chunk);
      this.onProgress((i + 1) / totalChunks);
    }

    // 3. Complete
    const completeResp = await fetch(`/api/upload/${upload_id}/complete`, {
      method: 'POST',
    });
    if (!completeResp.ok) throw new Error('Failed to complete upload');

    return upload_id;
  }

  async _uploadChunk(uploadId, index, blob) {
    let lastErr;
    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      try {
        const resp = await fetch(`/api/upload/${uploadId}/chunk/${index}`, {
          method: 'POST',
          body: blob,
        });
        if (resp.ok) return;
        lastErr = new Error(`Chunk ${index} failed: HTTP ${resp.status}`);
      } catch (e) {
        lastErr = e;
      }
      // Exponential backoff
      await new Promise(r => setTimeout(r, 1000 * Math.pow(2, attempt)));
    }
    throw lastErr;
  }
}

/**
 * Intercept a form submission to use chunked uploads for large files.
 * @param {HTMLFormElement} form
 * @param {Object} fieldMap - Maps input name to upload_ids form field name.
 *   e.g. { "saves": "upload_ids" } or { "saves": "saves_upload_ids", "sample": "sample_upload_ids" }
 */
function setupChunkedForm(form, fieldMap) {
  form.addEventListener('submit', async function (e) {
    // Check if any file needs chunking
    let needsChunking = false;
    for (const inputName of Object.keys(fieldMap)) {
      const input = form.querySelector(`input[name="${inputName}"]`);
      if (!input || !input.files) continue;
      for (const file of input.files) {
        if (file.size > CHUNK_SIZE) {
          needsChunking = true;
          break;
        }
      }
      if (needsChunking) break;
    }

    if (!needsChunking) return; // Let normal form submit proceed

    e.preventDefault();
    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.dataset.origText = submitBtn.textContent;
      submitBtn.textContent = 'Uploading...';
    }

    try {
      const formData = new FormData();

      // Copy non-file form fields
      for (const [key, value] of new FormData(form).entries()) {
        if (value instanceof File) continue;
        formData.set(key, value);
      }

      for (const [inputName, idsFieldName] of Object.entries(fieldMap)) {
        const input = form.querySelector(`input[name="${inputName}"]`);
        if (!input || !input.files || !input.files.length) continue;

        const hasLargeFile = Array.from(input.files).some(f => f.size > CHUNK_SIZE);

        if (hasLargeFile) {
          // Chunked upload all files for this input
          const uploadIds = [];
          const totalFiles = input.files.length;

          for (let fi = 0; fi < totalFiles; fi++) {
            const file = input.files[fi];
            const progressBar = getOrCreateProgressBar(input, fi);

            const uploader = new ChunkedUploader(file, {
              onProgress: (pct) => {
                updateProgressBar(progressBar, pct, file.name, fi + 1, totalFiles);
              },
            });
            const uploadId = await uploader.upload();
            uploadIds.push(uploadId);
            updateProgressBar(progressBar, 1, file.name, fi + 1, totalFiles);
          }

          formData.set(idsFieldName, JSON.stringify(uploadIds));
        } else {
          // Small files: attach normally
          for (const file of input.files) {
            formData.append(inputName, file);
          }
        }
      }

      // Submit via fetch
      const resp = await fetch(form.action || window.location.href, {
        method: 'POST',
        body: formData,
      });

      if (resp.redirected) {
        window.location.href = resp.url;
      } else if (resp.ok) {
        // Parse HTML response for flash messages or redirect
        const html = await resp.text();
        document.documentElement.innerHTML = html;
      } else {
        throw new Error(`Submit failed: HTTP ${resp.status}`);
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

function updateProgressBar(bar, pct, filename, fileNum, totalFiles) {
  const label = bar.querySelector('.chunked-progress-label');
  const fill = bar.querySelector('.chunked-progress-fill');
  const percent = Math.round(pct * 100);
  label.textContent = totalFiles > 1
    ? `${filename} (${fileNum}/${totalFiles}) ${percent}%`
    : `${filename} ${percent}%`;
  fill.style.width = percent + '%';
}
