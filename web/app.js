(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { scoreId: null, score: null, osmd: null, audioUrl: null, animating: false, lastCursorQ: 0 };
  const dropzone = $("dropzone");
  const fileInput = $("file-input");
  const uploadStatus = $("upload-status");
  const errorBanner = $("error-banner");
  const player = $("audio-player");

  function showError(message) { errorBanner.textContent = message || "A apărut o eroare neașteptată."; errorBanner.hidden = false; }
  function clearError() { errorBanner.hidden = true; errorBanner.textContent = ""; }
  function setBusy(busy, text) {
    $("browse").disabled = busy;
    $("new-score").disabled = busy;
    uploadStatus.textContent = text || (busy ? "Procesez local…" : "Fișierul rămâne pe acest dispozitiv.");
  }
  function selectedVoices() {
    return [...document.querySelectorAll(".voice-chip input:checked")].map((i) => i.value);
  }
  async function jsonRequest(url, options) {
    const response = await fetch(url, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || body.message || "Cererea a eșuat (" + response.status + ").");
    return body;
  }
  async function importFile(file) {
    if (!file) return;
    clearError();
    setBusy(true, "Import local: " + file.name);
    try {
      const form = new FormData();
      form.append("file", file);
      const result = await jsonRequest("/api/scores", { method: "POST", body: form });
      state.scoreId = result.score_id;
      state.score = result;
      await renderScore(result);
      showAnalysis(result);
      $("score-panel").hidden = false;
      $("preview-button").disabled = false;
      $("download-midi").disabled = false;
      document.querySelectorAll("[data-midi-voice]").forEach((b) => { b.disabled = false; });
      $("score-title").textContent = result.title || file.name;
      $("score-meta").textContent = (result.source_name || file.name) + " · " + result.note_count + " note detectate";
      uploadStatus.textContent = "Import reușit: " + file.name;
      $("cursor-label").textContent = "Cursor: pregătit pentru preview";
    } catch (err) {
      showError(err.message || String(err));
      uploadStatus.textContent = "Importul nu a reușit.";
    } finally { setBusy(false); }
  }
  async function renderScore(result) {
    const target = $("score-render");
    target.innerHTML = "";
    if (!window.opensheetmusicdisplay || !window.opensheetmusicdisplay.OpenSheetMusicDisplay) {
      target.innerHTML = '<div class="empty-note">Rendererul de partituri lipsește. Rulează npm install în proiect și reîncarcă pagina.</div>';
      return;
    }
    try {
      const osmd = new window.opensheetmusicdisplay.OpenSheetMusicDisplay(target, {
        autoResize: true, backend: "svg", drawTitle: true, drawComposer: true,
        drawPartNames: true, followCursor: true, pageFormat: "Endless",
        drawingParameters: "compacttight", newSystemFromXML: false
      });
      await osmd.load(result.score_url);
      osmd.render();
      state.osmd = osmd;
      try { osmd.cursor.reset(); osmd.cursor.show(); } catch (_) {}
    } catch (err) {
      target.innerHTML = '<div class="empty-note">MusicXML-ul a fost parsat, dar afișarea a eșuat: ' + escapeHtml(err.message || String(err)) + '</div>';
    }
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
  }
  function showAnalysis(score) {
    $("note-count").textContent = Number(score.note_count || 0).toLocaleString("ro-RO");
    $("tempo-count").textContent = String((score.tempos || []).length);
    $("voice-count").textContent = String((score.voices_found || []).length);
    $("duration").textContent = formatTime(quartersToSeconds(Number(score.total_quarters || 0), score.tempos || [], Number($("tempo").value)));
    const counts = {};
    for (const ev of score.notes || []) if (!ev.is_rest) counts[ev.voice] = (counts[ev.voice] || 0) + 1;
    const panel = $("voice-analysis");
    panel.innerHTML = "";
    const voiceNames = Object.keys(score.part_names || {});
    if (!voiceNames.length) { panel.innerHTML = "<p>Nu am identificat părțile SATB. Verifică denumirile partiturii.</p>"; return; }
    voiceNames.forEach((voice) => {
      const line = document.createElement("div");
      line.className = "part-item";
      const name = document.createElement("span");
      name.textContent = voice;
      const value = document.createElement("span");
      value.textContent = (counts[voice] || 0) + " note · " + (score.part_names[voice] || voice);
      line.append(name, value);
      panel.appendChild(line);
    });
  }
  function formatTime(seconds) {
    if (!Number.isFinite(seconds)) return "—";
    const s = Math.max(0, Math.round(seconds));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  function scaledTempoEvents(score, bpm) {
    const source = score.tempos && score.tempos.length ? score.tempos : [{ start_q: 0, bpm: 100 }];
    const firstMark = source.find(t => Number(t.start_q) <= 0) || source[0];
    const base = Number(firstMark.bpm) || 100;
    return source.map(t => ({ start_q: Number(t.start_q), bpm: Number(t.bpm) * Number(bpm) / base })).sort((a,b) => a.start_q-b.start_q);
  }
  function quartersToSeconds(q, rawTempos, bpm) {
    const tempos = rawTempos && rawTempos.length ? rawTempos : [{ start_q: 0, bpm: 100 }];
    const firstMark = tempos.find(t => Number(t.start_q) <= 0) || tempos[0];
    const base = Number(firstMark.bpm) || 100;
    const scaled = tempos.map(t => ({ start_q: Number(t.start_q), bpm: Number(t.bpm) * Number(bpm) / base })).sort((a,b) => a.start_q-b.start_q);
    if (!scaled.length || scaled[0].start_q > 0) scaled.unshift({ start_q: 0, bpm: scaled[0] ? scaled[0].bpm : Number(bpm) });
    let seconds = 0;
    for (let i=0; i<scaled.length; i++) {
      const start = scaled[i].start_q;
      const end = i+1 < scaled.length ? scaled[i+1].start_q : q;
      if (q <= start) break;
      seconds += Math.max(0, Math.min(q,end)-start) * 60 / scaled[i].bpm;
      if (q <= end) break;
    }
    return seconds;
  }
  function secondsToQuarters(seconds, score, bpm) {
    const tempos = scaledTempoEvents(score, bpm);
    let remaining = Math.max(0, seconds);
    for (let i=0; i<tempos.length; i++) {
      const span = i+1 < tempos.length ? Math.max(0,tempos[i+1].start_q-tempos[i].start_q) : Infinity;
      const duration = span * 60 / tempos[i].bpm;
      if (remaining <= duration) return tempos[i].start_q + remaining * tempos[i].bpm / 60;
      remaining -= duration;
    }
    return 0;
  }
  function syncCursor(q) {
    const cursor = state.osmd && state.osmd.cursor;
    if (!cursor) return;
    try {
      const getQ = () => {
        const stamp = cursor.iterator && cursor.iterator.currentTimeStamp;
        if (!stamp) return 0;
        if (Number.isFinite(stamp.RealValue)) return stamp.RealValue * 4;
        if (Number.isFinite(stamp.realValue)) return stamp.realValue * 4;
        if (Number.isFinite(stamp.numerator) && Number.isFinite(stamp.denominator)) return (stamp.numerator/stamp.denominator) * 4;
        return 0;
      };
      if (q < state.lastCursorQ - 0.15) cursor.reset();
      let current = getQ();
      let guard = 0;
      while (current < q - 0.04 && guard++ < 2000) {
        const before = current;
        cursor.next();
        current = getQ();
        if (current <= before && guard > 2) break;
      }
      state.lastCursorQ = q;
      $("cursor-label").textContent = "Cursor: " + q.toFixed(1) + " bătăi";
    } catch (_) {}
  }
  function startCursorSync() {
    state.animating = true;
    const tick = () => {
      if (!state.animating) return;
      if (!player.paused && state.score) {
        const offset = Number($("offset").value) / 1000;
        const q = secondsToQuarters(player.currentTime - offset, state.score, Number($("tempo").value));
        syncCursor(q);
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  async function makePreview() {
    if (!state.scoreId) return;
    const voices = selectedVoices();
    if (!voices.length) { showError("Selectează cel puțin o voce pentru preview."); return; }
    const params = new URLSearchParams({ reference_bpm: $("tempo").value, voices: voices.join(","), offset_ms: $("offset").value });
    $("preview-button").disabled = true;
    $("preview-button").textContent = "Se generează…";
    clearError();
    try {
      const response = await fetch("/api/scores/" + state.scoreId + "/preview.wav?" + params);
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.detail || "Preview-ul a eșuat (" + response.status + ").");
      }
      const blob = await response.blob();
      if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
      state.audioUrl = URL.createObjectURL(blob);
      player.src = state.audioUrl;
      player.hidden = false;
      $("pause-button").disabled = false;
      await player.play();
    } catch (err) { showError(err.message || String(err)); }
    finally { $("preview-button").disabled = false; $("preview-button").textContent = "▶ Generează preview"; }
  }
  function downloadMidi(voice) {
    if (!state.scoreId) return;
    const params = new URLSearchParams({ reference_bpm: $("tempo").value, voice: voice });
    window.location.assign("/api/scores/" + state.scoreId + "/midi?" + params);
  }
  function resetSession() {
    player.pause(); player.removeAttribute("src"); player.hidden = true;
    if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
    state.audioUrl = null; state.scoreId = null; state.score = null; state.osmd = null; state.lastCursorQ = 0;
    $("score-panel").hidden = true; $("score-render").innerHTML = "";
    $("preview-button").disabled = true; $("pause-button").disabled = true; $("download-midi").disabled = true;
    document.querySelectorAll("[data-midi-voice]").forEach((b) => { b.disabled = true; });
    $("note-count").textContent = "—"; $("duration").textContent = "—"; $("tempo-count").textContent = "—"; $("voice-count").textContent = "—";
    $("voice-analysis").innerHTML = "<p>Importă o partitură pentru a vedea analiza.</p>";
    clearError(); setBusy(false, "Fișierul rămâne pe acest dispozitiv.");
  }

  $("browse").addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("click", (event) => { if (event.target === dropzone || event.target.closest(".upload-symbol, strong, p")) fileInput.click(); });
  dropzone.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); } });
  fileInput.addEventListener("change", () => importFile(fileInput.files && fileInput.files[0]));
  dropzone.addEventListener("dragover", (event) => { event.preventDefault(); dropzone.classList.add("dragging"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragging"));
  dropzone.addEventListener("drop", (event) => { event.preventDefault(); dropzone.classList.remove("dragging"); importFile(event.dataTransfer && event.dataTransfer.files[0]); });
  $("new-score").addEventListener("click", resetSession);
  $("preview-button").addEventListener("click", makePreview);
  $("pause-button").addEventListener("click", () => { player.pause(); state.animating = false; $("cursor-label").textContent = "Cursor: pauză"; });
  $("download-midi").addEventListener("click", () => downloadMidi("all"));
  document.querySelectorAll("[data-midi-voice]").forEach((button) => button.addEventListener("click", () => downloadMidi(button.dataset.midiVoice)));
  $("tempo").addEventListener("input", () => {
    $("tempo-value").textContent = $("tempo").value + " BPM";
    if (state.score) $("duration").textContent = formatTime(quartersToSeconds(Number(state.score.total_quarters || 0), state.score.tempos || [], Number($("tempo").value)));
  });
  $("offset").addEventListener("input", () => { $("offset-value").textContent = $("offset").value + " ms"; });
  player.addEventListener("play", () => { if (state.score) startCursorSync(); });
  player.addEventListener("pause", () => { state.animating = false; });
  player.addEventListener("ended", () => { state.animating = false; $("cursor-label").textContent = "Cursor: final"; });
  fetch("/api/health").then(r => r.json()).then(health => {
    if (!health.osmd_installed) uploadStatus.textContent = "Rendererul de partituri nu este instalat încă. Rulează npm install; API-ul local rămâne disponibil.";
  }).catch(() => {});
})();
