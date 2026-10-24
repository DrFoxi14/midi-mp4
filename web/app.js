(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { scoreId: null, score: null, osmd: null, scoreResizeObserver: null, audioUrl: null, animating: false, lastCursorQ: 0, health: null, videoJobId: null, highlightedNotes: [] };
  const dropzone = $("dropzone");
  const fileInput = $("file-input");
  const uploadStatus = $("upload-status");
  const errorBanner = $("error-banner");
  const player = $("audio-player");

  function updatePianoStatus(piano) {
    piano = piano || {backend: "synthesized", message: "Se folosește sintetizarea offline."};
    const label = piano.sample_piano_available ? "SoundFont" : (piano.backend === "synthesized" ? "Synth local" : "De verificat");
    $("piano-backend").textContent = label;
    $("piano-backend-note").textContent = piano.message || "Audio local: " + piano.backend;
    $("piano-backend-note").classList.toggle("sampled-piano", piano.sample_piano_available === true);
  }

  function showError(message) { errorBanner.textContent = message || "A apărut o eroare neașteptată."; errorBanner.hidden = false; }
  function clearError() { errorBanner.hidden = true; errorBanner.textContent = ""; }
  function canExportVideo() {
    return Boolean(state.scoreId && state.health && state.health.video_export_available
      && $("accept-reflowed").checked && !state.videoJobId);
  }
  function updateVideoExportAvailability() {
    const button = $("export-video");
    if (button) button.disabled = !canExportVideo();
    if (!$("accept-reflowed").checked && state.scoreId && state.health?.video_export_available) {
      $("video-status").textContent = "Bifează confirmarea privind rearanjarea partiturii înainte de export.";
    }
  }
  function setBusy(busy, text) {
    $("browse").disabled = busy;
    $("new-score").disabled = busy || Boolean(state.videoJobId);
    const videoButton = $("export-video");
    if (videoButton) videoButton.disabled = busy || !canExportVideo();
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
      // Reveal the panel before OSMD measures its width; rendering inside a hidden
      // ancestor gives it a zero-width canvas until a later resize happens.
      $("score-panel").hidden = false;
      await renderScore(result);
      showAnalysis(result);
      $("preview-button").disabled = false;
      $("download-midi").disabled = false;
      document.querySelectorAll("[data-midi-voice]").forEach((b) => {
        const voice = b.dataset.midiVoice;
        b.disabled = voice !== "all" && !Object.prototype.hasOwnProperty.call(result.part_names || {}, voice);
      });
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
      // OSMD can load successfully before Safari has completed layout for a panel
      // that was just revealed. Wait for real layout, render, and verify visible SVG.
      await osmd.load(result.score_url);
      state.osmd = osmd;
      if (state.scoreResizeObserver) {
        state.scoreResizeObserver.disconnect();
        state.scoreResizeObserver = null;
      }

      const nextPaint = () => new Promise(resolve => requestAnimationFrame(() => resolve()));
      await nextPaint();
      await nextPaint();

      const shell = $("score-shell");
      if (!shell || shell.getBoundingClientRect().width < 100 || shell.getBoundingClientRect().height < 100) {
        throw new Error("Zona partiturii nu are dimensiuni vizibile. Reîncarcă pagina și încearcă din nou.");
      }

      // Keep the score in sync with changes to the visible viewport. Observing the
      // fixed-height shell avoids a render/resize feedback loop from the SVG itself.
      if (typeof ResizeObserver !== "undefined") {
        let resizeQueued = false;
        state.scoreResizeObserver = new ResizeObserver(() => {
          if (state.osmd !== osmd || resizeQueued) return;
          resizeQueued = true;
          requestAnimationFrame(() => {
            resizeQueued = false;
            if (state.osmd === osmd) {
              try { osmd.render(); } catch (err) { console.error("OSMD resize render failed", err); }
            }
          });
        });
        state.scoreResizeObserver.observe(shell);
      }

      osmd.render();
      await nextPaint();
      await nextPaint();

      const hasVisibleScore = () => {
        const svg = target.querySelector("svg");
        if (!svg || svg.getBoundingClientRect().width < 100 || svg.getBoundingClientRect().height < 30) return false;
        return svg.querySelectorAll("path,line,ellipse,rect,use,text").length > 10;
      };

      // Retry after Safari's first layout pass instead of depending on opening Web Inspector.
      if (!hasVisibleScore()) {
        await new Promise(resolve => setTimeout(resolve, 150));
        await nextPaint();
        osmd.render();
        await nextPaint();
        await nextPaint();
      }

      if (!hasVisibleScore()) {
        throw new Error("Rendererul a încărcat MusicXML, dar nu a desenat notația. Verifică dacă fișierul este MusicXML valid.");
      }

      try {
        osmd.cursor.CursorOptions = { type: 1, color: "#ef5b70", alpha: 0.96, follow: true };
        osmd.cursor.reset();
        osmd.cursor.show();
        updateBrowserNoteHighlights();
      } catch (_) {}
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
    populateVideoVoiceOptions(score);
    const voiceNames = Object.keys(score.part_names || {});
    const voiceRow = document.querySelector(".voice-row");
    const rowTitle = voiceRow.querySelector(".voice-row-title");
    voiceRow.innerHTML = "";
    voiceRow.appendChild(rowTitle);
    voiceNames.forEach((voice) => {
      const label = document.createElement("label");
      label.className = "voice-chip " + voice.toLowerCase();
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = voice;
      input.checked = true;
      label.append(input, document.createTextNode(" " + voice));
      voiceRow.appendChild(label);
    });
    populateVoiceMixer(score);
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
  function voiceVolumes() {
    const volumes = {};
    document.querySelectorAll(".voice-volume").forEach((input) => {
      volumes[input.dataset.voice] = Number(input.value);
    });
    return volumes;
  }

  function populateVoiceMixer(score) {
    const root = $("voice-mixer");
    const voices = Object.keys(score.part_names || {});
    root.innerHTML = "";
    root.hidden = voices.length === 0;
    voices.forEach((voice) => {
      const label = document.createElement("label");
      label.className = "mixer-part";
      const head = document.createElement("div");
      head.className = "mixer-part-head";
      const name = document.createElement("span");
      name.className = "mixer-part-name";
      name.textContent = voice;
      const value = document.createElement("output");
      value.className = "mixer-part-value";
      value.textContent = "100%";
      const slider = document.createElement("input");
      slider.className = "voice-volume";
      slider.type = "range";
      slider.min = "0";
      slider.max = "100";
      slider.step = "5";
      slider.value = "100";
      slider.dataset.voice = voice;
      slider.setAttribute("aria-label", "Volum " + voice);
      slider.addEventListener("input", () => {
        value.textContent = slider.value + "%";
      });
      head.append(name, value);
      label.append(head, slider);
      root.appendChild(label);
    });
    const help = document.createElement("p");
    help.className = "mixer-help";
    help.textContent = "0% = voce mută · 100% = nivel normal. Se aplică la următorul preview și la exportul video.";
    if (voices.length) root.appendChild(help);
  }

  function populateVideoVoiceOptions(score) {
    const select = $("video-voice");
    const current = select.value || "all";
    select.innerHTML = "";
    const all = document.createElement("option");
    all.value = "all";
    all.textContent = "SATB — toate vocile";
    select.appendChild(all);
    const voices = score.voice_order && score.voice_order.length ? score.voice_order : Object.keys(score.part_names || {});
    voices.forEach((voice) => {
      if (!Object.prototype.hasOwnProperty.call(score.part_names || {}, voice)) return;
      const option = document.createElement("option");
      option.value = voice;
      option.textContent = voice;
      select.appendChild(option);
    });
    select.value = [...select.options].some((o) => o.value === current) ? current : "all";
    updateVideoModeControl();
  }

  function updateVideoModeControl() {
    const all = $("video-voice").value === "all";
    $("video-mode").disabled = all;
    if (all) $("video-mode").value = "accompaniment";
    else if ($("video-mode").value === "accompaniment") $("video-mode").value = "isolated";
  }

  async function startVideoExport() {
    if (!state.scoreId || state.videoJobId) return;
    if (!$("accept-reflowed").checked) {
      $("video-status").textContent = "Confirmă explicit că accepți rearanjarea partiturii înainte de export.";
      return;
    }
    clearError();
    $("video-download").hidden = true;
    $("video-progress").value = 0;
    $("export-video").disabled = true;
    $("export-video").textContent = "Se pregătește…";
    $("video-status").textContent = "Trimit setările motorului local de randare.";
    const body = {
      reference_bpm: Number($("tempo").value),
      resolution: $("video-resolution").value,
      voice: $("video-voice").value,
      audio_mode: $("video-voice").value === "all" ? "accompaniment" : $("video-mode").value,
      offset_ms: Number($("offset").value),
      format: $("video-format").value,
      accept_reflowed_engraving: $("accept-reflowed").checked,
      volume_by_voice: voiceVolumes()
    };
    try {
      const response = await fetch("/api/scores/" + state.scoreId + "/video-jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || "Nu am putut porni exportul video.");
      state.videoJobId = result.job_id;
      $("new-score").disabled = true;
      $("video-status").textContent = "Export pornit. Procesarea poate dura în cazul partiturilor lungi sau al rezoluției 4K.";
      $("export-video").textContent = "Se randează…";
      pollVideoJob();
    } catch (err) {
      $("video-status").textContent = err.message || String(err);
      $("export-video").disabled = !canExportVideo();
      $("export-video").textContent = "▶ Randează video";
      $("new-score").disabled = false;
    }
  }

  async function pollVideoJob() {
    const jobId = state.videoJobId;
    if (!jobId) return;
    try {
      const response = await fetch("/api/video-jobs/" + jobId);
      const job = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(job.detail || "Nu pot citi starea exportului video.");
      $("video-progress").value = Math.round(Number(job.progress || 0) * 100);
      $("video-status").textContent = Math.round(Number(job.progress || 0) * 100) + "% · " + (job.message || "Se procesează…");
      if (job.status === "completed") {
        state.videoJobId = null;
        $("export-video").textContent = "▶ Randează video";
        $("export-video").disabled = !canExportVideo();
        $("new-score").disabled = false;
        $("video-progress").value = 100;
        $("video-status").textContent = "Gata: " + (job.resolution || "") + " · " + (job.format || "").toUpperCase() + " · " + formatTime(Number(job.duration_seconds || 0)) + " · " + Number(job.frames || 0) + " cadre.";
        const link = $("video-download");
        link.href = job.download_url;
        link.download = job.filename || "midi-mp4-video." + (job.format || "mp4");
        link.hidden = false;
        link.textContent = "↓ Descarcă " + (job.filename || "video-ul");
        return;
      }
      if (job.status === "failed") {
        state.videoJobId = null;
        $("export-video").textContent = "▶ Încearcă din nou";
        $("export-video").disabled = !canExportVideo();
        $("new-score").disabled = false;
        $("video-status").textContent = "Export eșuat: " + (job.error || job.message || "Eroare necunoscută.");
        return;
      }
      window.setTimeout(pollVideoJob, 1500);
    } catch (err) {
      state.videoJobId = null;
      $("export-video").textContent = "▶ Încearcă din nou";
      $("export-video").disabled = !canExportVideo();
      $("new-score").disabled = false;
      $("video-status").textContent = err.message || String(err);
    }
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
      let stalled = 0;
      const tolerance = 0.04;
      while (current < q - tolerance && guard++ < 2000) {
        const before = current;
        cursor.next();
        const next = getQ();
        if (next <= before) {
          stalled++;
          current = next;
        } else {
          stalled = 0;
          // Do not leave the cursor on the first event after the audio timestamp.
          // That made it visually lead the sound by as much as one note/rest.
          if (next > q + tolerance) {
            cursor.previous();
            current = getQ();
            let rewindGuard = 0;
            while (current > q + tolerance && rewindGuard++ < 128) {
              const previous = current;
              cursor.previous();
              current = getQ();
              if (current >= previous && rewindGuard > 64) break;
            }
            break;
          }
          current = next;
        }
        // SATB can expose several simultaneous voice entries with the same timestamp.
        if (stalled > 64) break;
      }
      state.lastCursorQ = q;
      updateBrowserNoteHighlights();
      $("cursor-label").textContent = "Cursor: " + q.toFixed(1) + " bătăi";
    } catch (_) {}
  }
  function removeBrowserNoteHighlights() {
    for (const item of state.highlightedNotes || []) {
      try {
        if (item.element && item.element.classList) {
          for (const className of item.classes || []) item.element.classList.remove(className);
        }
      } catch (_) {}
    }
    state.highlightedNotes = [];
  }

  function getGraphicalNoteElement(graphicalNote) {
    try {
      const element = graphicalNote && graphicalNote.getSVGGElement && graphicalNote.getSVGGElement();
      if (element && element.classList) return element;
    } catch (_) {}
    try {
      const id = graphicalNote && graphicalNote.getSVGId && graphicalNote.getSVGId();
      if (id) return document.getElementById(id);
    } catch (_) {}
    return null;
  }

  function updateBrowserNoteHighlights() {
    const osmd = state.osmd;
    const cursor = osmd && osmd.cursor;
    if (!cursor || !state.score) return;
    const selected = selectedVoices();
    const voices = state.score.voice_order && state.score.voice_order.length
      ? state.score.voice_order
      : Object.keys(state.score.part_names || {});
    const sheet = osmd.Sheet || osmd.sheet;
    const instruments = sheet && (sheet.Instruments || sheet.instruments) || [];
    const instrumentAt = (index) => {
      try { return instruments[index] || null; } catch (_) { return null; }
    };
    const classForVoice = (voice) => {
      const name = String(voice || "").toLowerCase();
      if (name.includes("soprano")) return "mp4-active-soprano";
      if (name.includes("alto")) return "mp4-active-alto";
      if (name.includes("tenor")) return "mp4-active-tenor";
      if (name.includes("bass")) return "mp4-active-bass";
      return "mp4-active-note";
    };
    const next = [];
    const seen = new Set();
    for (let index = 0; index < voices.length; index++) {
      const voice = voices[index];
      if (!selected.includes(voice)) continue;
      let graphicalNotes = [];
      try {
        graphicalNotes = cursor.GNotesUnderCursor(instrumentAt(index)) || [];
      } catch (_) {}
      for (const graphicalNote of graphicalNotes) {
        const element = getGraphicalNoteElement(graphicalNote);
        if (!element || seen.has(element)) continue;
        seen.add(element);
        next.push({ element, classes: [classForVoice(voice)] });
      }
    }
    // Some scores expose a combined instrument rather than separate parts. Use a
    // neutral highlight as a visible fallback instead of silently showing nothing.
    if (!next.length) {
      try {
        const graphicalNotes = cursor.GNotesUnderCursor() || [];
        const fallbackClass = selected.length === 1 ? classForVoice(selected[0]) : "mp4-active-note";
        for (const graphicalNote of graphicalNotes) {
          const element = getGraphicalNoteElement(graphicalNote);
          if (!element || seen.has(element)) continue;
          seen.add(element);
          next.push({ element, classes: [fallbackClass] });
        }
      } catch (_) {}
    }
    removeBrowserNoteHighlights();
    for (const item of next) {
      try {
        for (const className of item.classes) item.element.classList.add(className);
      } catch (_) {}
    }
    state.highlightedNotes = next;
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
    // The offset shifts the visual cursor against fixed audio; it must not move notes in the WAV.
    const params = new URLSearchParams({
      reference_bpm: $("tempo").value,
      voices: voices.join(","),
      volumes: JSON.stringify(voiceVolumes())
    });
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
    if (state.scoreId) fetch("/api/scores/" + state.scoreId, { method: "DELETE" }).catch(() => {});
    player.pause(); player.removeAttribute("src"); player.hidden = true;
    if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
    removeBrowserNoteHighlights();
    state.audioUrl = null; state.scoreId = null; state.score = null; state.osmd = null; state.lastCursorQ = 0; state.videoJobId = null;
    $("score-panel").hidden = true; $("score-render").innerHTML = "";
    $("video-progress").value = 0;
    $("video-download").hidden = true;
    $("accept-reflowed").checked = false;
    $("export-video").disabled = true;
    $("export-video").textContent = "▶ Randează video";
    $("video-status").textContent = "Alege o partitură pentru export video.";
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
  $("choose-soundfont").addEventListener("click", () => $("soundfont-input").click());
  $("soundfont-input").addEventListener("change", async () => {
    const file = $("soundfont-input").files && $("soundfont-input").files[0];
    if (!file) return;
    $("soundfont-status").textContent = "Salvez SoundFont-ul local…";
    $("choose-soundfont").disabled = true;
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/settings/soundfont", { method: "POST", body: form });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || "Nu am putut încărca SoundFont-ul.");
      updatePianoStatus(result.piano);
      $("soundfont-status").textContent = result.piano && result.piano.backend === "soundfont"
        ? "Activ: " + result.filename + ". Preview-ul va folosi FluidSynth cu acest SoundFont local."
        : "Fișier salvat local, dar motorul audio nu este încă pregătit: " + ((result.piano && result.piano.message) || "verifică FluidSynth.");
    } catch (err) {
      $("soundfont-status").textContent = err.message || String(err);
    } finally {
      $("choose-soundfont").disabled = false;
      $("soundfont-input").value = "";
    }
  });
  $("clear-soundfont").addEventListener("click", async () => {
    $("soundfont-status").textContent = "Revin la configurația audio anterioară…";
    $("clear-soundfont").disabled = true;
    try {
      const response = await fetch("/api/settings/soundfont", { method: "DELETE" });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || "Nu am putut elimina SoundFont-ul.");
      updatePianoStatus(result.piano);
      $("soundfont-status").textContent = result.removed
        ? "SoundFont-ul încărcat din interfață a fost eliminat. Preview-ul folosește configurația anterioară."
        : "Nu exista un SoundFont încărcat prin interfață.";
    } catch (err) {
      $("soundfont-status").textContent = err.message || String(err);
    } finally { $("clear-soundfont").disabled = false; }
  });
  $("new-score").addEventListener("click", resetSession);
  $("video-voice").addEventListener("change", updateVideoModeControl);
  document.querySelector(".voice-row").addEventListener("change", updateBrowserNoteHighlights);
  $("accept-reflowed").addEventListener("change", updateVideoExportAvailability);
  $("export-video").addEventListener("click", startVideoExport);
  $("preview-button").addEventListener("click", makePreview);
  $("pause-button").addEventListener("click", () => { player.pause(); state.animating = false; $("cursor-label").textContent = "Cursor: pauză"; });
  $("download-midi").addEventListener("click", () => downloadMidi("all"));
  document.querySelectorAll("[data-midi-voice]").forEach((button) => button.addEventListener("click", () => downloadMidi(button.dataset.midiVoice)));
  $("tempo").addEventListener("input", () => {
    $("tempo-value").textContent = $("tempo").value + " BPM";
    if (state.score) {
      $("duration").textContent = formatTime(quartersToSeconds(Number(state.score.total_quarters || 0), state.score.tempos || [], Number($("tempo").value)));
      // A WAV preview is rendered at the tempo selected when it was generated.
      // Invalidate it when tempo changes so the cursor cannot follow a stale tempo map.
      if (state.audioUrl) {
        player.pause();
        player.removeAttribute("src");
        player.load();
        URL.revokeObjectURL(state.audioUrl);
        state.audioUrl = null;
        player.hidden = true;
        $("pause-button").disabled = true;
        state.animating = false;
        $("cursor-label").textContent = "Tempo schimbat: generează din nou preview-ul";
      }
    }
  });
  $("offset").addEventListener("input", () => { $("offset-value").textContent = $("offset").value + " ms"; });
  player.addEventListener("play", () => { if (state.score) startCursorSync(); });
  player.addEventListener("pause", () => { state.animating = false; });
  player.addEventListener("ended", () => { state.animating = false; $("cursor-label").textContent = "Cursor: final"; });
  fetch("/api/health").then(r => r.json()).then(health => {
    state.health = health;
    if (!health.osmd_installed) uploadStatus.textContent = "Rendererul de partituri nu este instalat încă. Rulează npm install; API-ul local rămâne disponibil.";
    updatePianoStatus(health.piano_backend || health.piano || {backend: "synthesized", message: "Se folosește sintetizarea offline."});
    const missing = Object.entries(health.video_dependencies || {}).filter(([, ok]) => !ok).map(([name]) => name);
    const videoButton = $("export-video");
    videoButton.disabled = true;
    $("accept-reflowed").checked = false;
    if (health.video_export_available) {
      $("video-status").textContent = "Motorul local și FFmpeg sunt disponibile. Exportă după importul unei partituri; poate fi necesară instalarea Chromium cu python -m playwright install chromium.";
    } else {
      $("video-status").textContent = "Export indisponibil până la instalarea dependențelor locale: " + missing.join(", ") + ". Vezi README.md.";
    }
  }).catch(() => {
    $("video-status").textContent = "Nu pot verifica dependențele video. Repornește aplicația locală și reîncarcă pagina.";
  });
})();
