/**
 * Seecript — ASR uploader.
 *
 * Pipeline (per request):
 *   ① user picks a file (video or audio)
 *   ② if it's a video, ffmpeg.wasm extracts the audio track to mp3 (16kHz mono)
 *      else the file passes through as-is
 *   ③ upload the audio blob to /api/asr/transcribe (multipart)
 *   ④ backend base64-encodes the bytes inline → calls Doubao 极速版 (turbo / flash)
 *      → 1-5s later returns transcript (no polling, no temp files)
 *   ⑤ caller renders transcript into the page
 *
 * Why ffmpeg.wasm:
 *   - A 5-min 1080p video is ~80–200 MB; the audio track is ~5 MB. Uploading the whole
 *     video = wasted bandwidth + Doubao 25MB limit miss. Browser-side extract solves both.
 *   - ffmpeg.wasm 0.12 needs SharedArrayBuffer → page must be `crossOriginIsolated`.
 *     We achieve this via COOP/COEP headers in main.py (see middleware comment).
 *
 * Why import from `/vendor/ffmpeg/...` instead of jsdelivr CDN:
 *   The HTML spec FORBIDS constructing a Worker from a cross-origin script URL,
 *   regardless of CORP/CORS headers. ffmpeg.wasm 0.12.x's `classes.js` does:
 *
 *       new Worker(new URL("./worker.js", import.meta.url), { type: "module" })
 *
 *   If `import.meta.url` is `https://cdn.jsdelivr.net/...`, then the resolved
 *   worker.js URL is also cross-origin → browser throws
 *   "Failed to construct 'Worker': Script ... cannot be accessed from origin".
 *   The only fix is to host the ffmpeg + util ESM bundles on the SAME origin as
 *   the page that imports them. We mirror them under /vendor/ffmpeg/ to satisfy
 *   this constraint. (ffmpeg-core's wasm/js/worker are still loaded from jsdelivr
 *   because we wrap them in blob: URLs via `toBlobURL`, which IS allowed
 *   cross-origin.)
 *
 * Single-instance pattern:
 *   ffmpeg.wasm core is ~30 MB; we load it once per page session and reuse.
 */
import { FFmpeg } from "/vendor/ffmpeg/ffmpeg/index.js";
import { fetchFile, toBlobURL } from "/vendor/ffmpeg/util/index.js";

const FFMPEG_CORE_BASE = "https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.6/dist/esm";
const VIDEO_EXTENSIONS = /\.(mp4|mov|m4v|webm|mkv|avi|flv)$/i;
const AUDIO_EXTENSIONS = /\.(mp3|m4a|wav|aac|ogg)$/i;
const MAX_DIRECT_UPLOAD_BYTES = 25 * 1024 * 1024; // backend cap

let ffmpegInstance = null;
let ffmpegLoadingPromise = null;

/**
 * Lazy-load the ffmpeg.wasm core. First call downloads ~30 MB; subsequent calls return
 * the same instance immediately.
 */
async function getFfmpeg(onProgress) {
  if (ffmpegInstance) return ffmpegInstance;
  if (ffmpegLoadingPromise) return ffmpegLoadingPromise;

  ffmpegLoadingPromise = (async () => {
    if (!self.crossOriginIsolated) {
      throw new Error(
        "当前页面未启用 cross-origin isolation，ffmpeg.wasm 无法加载。" +
          "请确认通过 run.ps1/run.sh 启动后再访问 http://127.0.0.1:8090/feature-1.html，" +
          "而不是用 file:// 直接打开。"
      );
    }
    const ff = new FFmpeg();
    ff.on("log", ({ message }) => {
      if (message && /error|fail/i.test(message)) console.warn("[ffmpeg]", message);
    });
    ff.on("progress", ({ progress }) => {
      if (typeof onProgress === "function" && progress >= 0 && progress <= 1) {
        onProgress(Math.round(progress * 100));
      }
    });
    if (typeof onProgress === "function") onProgress(0, "downloading-core");
    await ff.load({
      coreURL: await toBlobURL(`${FFMPEG_CORE_BASE}/ffmpeg-core.js`, "text/javascript"),
      wasmURL: await toBlobURL(`${FFMPEG_CORE_BASE}/ffmpeg-core.wasm`, "application/wasm"),
      workerURL: await toBlobURL(`${FFMPEG_CORE_BASE}/ffmpeg-core.worker.js`, "text/javascript"),
    });
    ffmpegInstance = ff;
    return ff;
  })();
  return ffmpegLoadingPromise;
}

/**
 * Extract the audio track from a video file as 16kHz mono mp3.
 * Returns a Blob.
 */
async function extractAudioFromVideo(file, onProgress) {
  const ff = await getFfmpeg((p) => onProgress && onProgress(p, "loading-ffmpeg"));
  const inputName = "in." + (file.name.split(".").pop() || "mp4");
  const outputName = "out.mp3";
  await ff.writeFile(inputName, await fetchFile(file));
  await ff.exec([
    "-i", inputName,
    "-vn",            // drop video track
    "-ac", "1",       // mono
    "-ar", "16000",   // 16 kHz, optimal for ASR
    "-b:a", "32k",    // low bitrate; ASR doesn't need fidelity
    outputName,
  ]);
  const data = await ff.readFile(outputName);
  // Cleanup virtual FS so a subsequent extraction starts clean.
  try {
    await ff.deleteFile(inputName);
    await ff.deleteFile(outputName);
  } catch (_) {
    /* non-fatal */
  }
  return new Blob([data.buffer], { type: "audio/mpeg" });
}

/**
 * Upload an audio Blob to the backend and return the transcript.
 *
 * @param {Blob|File} blob
 * @param {string} filename
 * @returns {Promise<{transcript: string, provider: string, elapsed_ms: number}>}
 */
async function uploadToBackend(blob, filename) {
  const fd = new FormData();
  fd.append("file", blob, filename);
  const resp = await fetch("/api/asr/transcribe", { method: "POST", body: fd });
  let payload = null;
  try { payload = await resp.json(); } catch (_) { /* */ }
  if (!resp.ok) {
    const detail = (payload && (payload.detail || payload.message)) || `HTTP ${resp.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

/**
 * High-level: pick file → maybe extract audio → upload → return transcript.
 *
 * @param {File} file
 * @param {object} hooks
 *   - onStage(stageName, optionalText): 'detect' | 'extract' | 'upload' | 'transcribe' | 'done'
 *   - onProgress(percent, label?): 0-100
 *   - onError(err): called instead of throwing if you want non-throw style
 * @returns {Promise<{transcript: string, provider: string, elapsed_ms: number}>}
 */
export async function startAsrFlow(file, hooks = {}) {
  const stage = (s, t) => hooks.onStage && hooks.onStage(s, t);
  const progress = (p, label) => hooks.onProgress && hooks.onProgress(p, label);

  if (!(file instanceof File)) throw new Error("startAsrFlow: file required");

  stage("detect", `检测文件：${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`);

  const isVideo = VIDEO_EXTENSIONS.test(file.name);
  const isAudio = AUDIO_EXTENSIONS.test(file.name);
  if (!isVideo && !isAudio) {
    throw new Error(`不支持的格式：${file.name}。请上传 mp4/mov/mp3/m4a/wav 等常见音视频。`);
  }

  let audioBlob;
  let audioName;
  if (isVideo) {
    stage("extract", "正在抽取音频轨道（首次会下载 ffmpeg.wasm 约 30 MB，仅一次）…");
    progress(0, "extract");
    audioBlob = await extractAudioFromVideo(file, (p) => progress(p, "extract"));
    audioName = file.name.replace(/\.[^.]+$/, "") + ".mp3";
  } else {
    if (file.size > MAX_DIRECT_UPLOAD_BYTES) {
      throw new Error(`音频文件 ${(file.size / 1024 / 1024).toFixed(1)} MB 超过 25 MB 上限，请压缩后再传。`);
    }
    audioBlob = file;
    audioName = file.name;
  }

  if (audioBlob.size > MAX_DIRECT_UPLOAD_BYTES) {
    throw new Error(
      `抽取后音频仍然 ${(audioBlob.size / 1024 / 1024).toFixed(1)} MB，超过 25 MB 上限。` +
        "请缩短视频或压缩音频（建议单次上传素材约 1 分钟量级；更长易导致抽取体积超限）。"
    );
  }

  stage("upload", `音频 ${(audioBlob.size / 1024).toFixed(0)} KB，正在上传到后端…`);
  progress(50, "upload");

  stage("transcribe", "豆包 ASR 识别中（异步任务，约 10–60 秒）…");
  progress(75, "transcribe");
  const result = await uploadToBackend(audioBlob, audioName);
  progress(100, "done");
  stage("done", `识别完成 · 耗时 ${result.elapsed_ms}ms`);
  return result;
}

// Export to global so the non-module interactions.js can also call us.
window.SeecriptAsr = { startAsrFlow };

// ----------------------------------------------------------------------------
// Auto-binding for feature-1.html
// ----------------------------------------------------------------------------
// We bind here (not in interactions.js) because asr-uploader.js is an ES module
// and may finish loading after interactions.js's DOMContentLoaded handler.
function bindFeatureOneUploader() {
  const fileInput = document.getElementById("seecript-asr-file");
  if (!fileInput) return;
  const status = document.getElementById("seecript-asr-status");

  function setStatus(html, kind) {
    if (!status) return;
    status.hidden = !html;
    status.innerHTML = html || "";
    status.dataset.kind = kind || "info";
  }

  function ensureTextareaThenFill(transcript) {
    // After ASR succeeds, the user is currently looking at the "上传视频/音频" tab,
    // so they cannot see either the transcript textarea or the "用 AI 拆解骨架"
    // button — both live inside the hidden "粘贴台词文本" tab. We programmatically
    // switch tabs so video-path and text-path users converge on the same exit:
    // a single, prominent "用 AI 拆解骨架" CTA after ASR. (See bindInputTabs in
    // interactions.js for the click handler that toggles aria-selected / hidden.)
    function tryFill(attempt = 0) {
      const ta = document.getElementById("seecript-transcript-input");
      if (ta) {
        const textTab = document.querySelector('.seecript-input-tab[data-input-tab="text"]');
        if (textTab && !textTab.classList.contains("is-active")) {
          textTab.click();
        }
        ta.value = transcript;
        // Wait one rAF so the now-shown textarea has layout before scrolling.
        requestAnimationFrame(() => {
          ta.scrollIntoView({ behavior: "smooth", block: "center" });
          ta.focus();
        });
        const btn = document.querySelector('[data-seecript-action="extract-skeleton"]');
        if (btn) btn.classList.add("seecript-pulse");
      } else if (attempt < 10) {
        setTimeout(() => tryFill(attempt + 1), 200);
      }
    }
    tryFill();
  }

  fileInput.addEventListener("change", async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    fileInput.disabled = true;

    try {
      const result = await startAsrFlow(file, {
        onStage: (s, t) => setStatus(`<b>${s}</b> · ${t || ""}`, "info"),
        onProgress: (pct, label) =>
          setStatus(`<b>${label || "处理中"}</b> · ${pct}%`, "info"),
      });

      setStatus(
        `<b>识别完成</b>（provider=${result.provider}, 用时 ${result.elapsed_ms}ms） — 文本已自动填入下方，请校对后点「用 AI 拆解骨架」。`,
        "success"
      );
      ensureTextareaThenFill(result.transcript);
      if (window.SeecriptApi && typeof window.SeecriptApi.showToast === "function") {
        window.SeecriptApi.showToast("ASR 识别完成", "success");
      }
    } catch (e) {
      console.error(e);
      const msg = (e && e.message) || "ASR 失败";
      setStatus(`<b>识别失败</b> · ${msg}`, "error");
      if (window.SeecriptApi && typeof window.SeecriptApi.showToast === "function") {
        window.SeecriptApi.showToast(msg, "error");
      }
    } finally {
      fileInput.disabled = false;
      // Reset value so re-selecting the same file fires change again.
      fileInput.value = "";
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bindFeatureOneUploader);
} else {
  bindFeatureOneUploader();
}
