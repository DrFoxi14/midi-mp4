(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { scoreId: null, score: null, osmd: null, scoreResizeObserver: null, audioUrl: null, animating: false, lastCursorQ: 0, health: null, videoJobId: null, highlightedNotes: [], measures: [], annotations: [], setlist: [], activeSetlistId: null, loop: { active: false, processing: false, startQ: 0, endQ: 0 }, progression: { active: false, target: 120, step: 5 } };
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
      await loadConductorData(result);
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
    if (voices.length) {
      const heading = document.createElement("div");
      heading.className = "mixer-heading";
      const title = document.createElement("strong");
      title.textContent = "Mixaj pe voci";
      const hint = document.createElement("small");
      hint.textContent = "Reglează volumul fiecărei secțiuni.";
      const reset = document.createElement("button");
      reset.type = "button";
      reset.className = "mixer-reset";
      reset.textContent = "Resetează 100%";
      reset.addEventListener("click", () => {
        root.querySelectorAll(".voice-volume").forEach((slider) => {
          slider.value = "100";
          const output = slider.closest(".mixer-part")?.querySelector(".mixer-part-value");
          if (output) output.textContent = "100%";
        });
      });
      heading.append(title, hint, reset);
      root.appendChild(heading);
    }
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


  async function loadConductorData(score) {
    try {
      const [analysis, annotationResult] = await Promise.all([
        jsonRequest("/api/scores/" + state.scoreId + "/analysis"),
        jsonRequest("/api/scores/" + state.scoreId + "/annotations")
      ]);
      state.measures = analysis.measures || [];
      state.annotations = annotationResult.annotations || [];
      populateMeasureSelects(state.measures);
      renderScoreAnalysis(analysis);
      populateAnnotationVoices(score);
      populateIntonationVoices(score);
      renderAnnotations();
      $("save-annotation").disabled = !state.measures.length;
      updateIntonationAvailability();
      $("practice-loop").disabled = !state.measures.length;
      $("concert-add").disabled = false;
      $("create-pack").disabled = false;
      $("practice-status").textContent = state.measures.length
        ? state.measures.length + " măsuri detectate. Selectează fragmentul pentru repetiție."
        : "Nu există numere de măsură în fișierul importat. Exportă din nou ca MusicXML cu măsuri numerotate.";
    } catch (err) {
      $("analysis-summary").textContent = "Verificările nu au putut fi încărcate: " + (err.message || String(err));
      $("analysis-issues").textContent = "";
    }
  }

  function populateMeasureSelects(measures) {
    const selectors = [$("loop-start-measure"), $("loop-end-measure"), $("annotation-measure")];
    selectors.forEach((select) => {
      select.innerHTML = "";
      measures.forEach((measure) => {
        const option = document.createElement("option");
        option.value = String(measure.number);
        option.textContent = "Măsura " + measure.number;
        select.appendChild(option);
      });
      select.disabled = measures.length === 0;
    });
    if (measures.length) {
      selectors[0].value = String(measures[0].number);
      selectors[1].value = String(measures[measures.length - 1].number);
      selectors[2].value = String(measures[0].number);
    }
  }

  function renderScoreAnalysis(analysis) {
    const summary = $("analysis-summary");
    summary.innerHTML = "";
    const counts = analysis.summary || {};
    const line = document.createElement("div");
    line.className = "analysis-counts";
    [["error", "Erori"], ["warning", "Atenționări"], ["info", "De verificat"]].forEach(([key, label]) => {
      const stat = document.createElement("span");
      stat.className = "analysis-count " + key;
      stat.textContent = label + ": " + Number(counts[key] || 0);
      line.appendChild(stat);
    });
    summary.appendChild(line);
    const intro = document.createElement("p");
    intro.textContent = Number(analysis.note_count || 0).toLocaleString("ro-RO") + " note · "
      + Number(analysis.measure_count || 0) + " măsuri · " + Object.keys(analysis.tempo_marks || {}).length + " marcaje de tempo.";
    summary.appendChild(intro);
    if (analysis.omr_music_intelligence) {
      const omr = analysis.omr_music_intelligence;
      const card = document.createElement("div");
      card.className = "omr-intelligence";
      const heading = document.createElement("strong");
      heading.textContent = "Music Intelligence · OMR-SATB";
      const details = document.createElement("p");
      const keys = (omr.keys || []).map(k => k.name || k.key || "").filter(Boolean).join(", ");
      const chordCounts = (omr.chord_functions || []).slice(0, 5).map(x =>
        Array.isArray(x) ? x[0] + " × " + x[1] : String(x)
      ).join(" · ");
      const cadenceCount = (omr.cadences || []).length;
      details.textContent = [
        keys ? "Tonalitate: " + keys : "",
        omr.measures ? omr.measures + " măsuri analizate" : "",
        cadenceCount ? cadenceCount + " cadențe" : "",
        chordCounts ? "Funcții: " + chordCounts : ""
      ].filter(Boolean).join(" · ") || "Analiză muzicală disponibilă.";
      const report = document.createElement("a");
      report.href = "/api/scores/" + encodeURIComponent(state.scoreId) + "/omr-report";
      report.target = "_blank";
      report.rel = "noreferrer";
      report.textContent = "Deschide raportul Music Intelligence complet ↗";
      card.append(heading, details, report);
      summary.appendChild(card);
    }

    const root = $("analysis-issues");
    root.innerHTML = "";
    if (!(analysis.issues || []).length) {
      const empty = document.createElement("p");
      empty.className = "analysis-empty";
      empty.textContent = "Nu au fost găsite avertismente prin verificările activate. Asta nu garantează că partitura este perfectă.";
      root.appendChild(empty);
    }
    (analysis.issues || []).slice(0, 200).forEach((issue) => {
      const card = document.createElement("article");
      card.className = "analysis-issue " + (issue.severity || "info");
      const head = document.createElement("div");
      head.className = "analysis-issue-head";
      const title = document.createElement("strong");
      title.textContent = (issue.voice ? issue.voice + " · " : "") + (issue.measure ? "M. " + issue.measure + " · " : "") + issue.code;
      const severity = document.createElement("small");
      severity.textContent = issue.severity === "warning" ? "Atenție" : issue.severity === "error" ? "Eroare" : "Review";
      head.append(title, severity);
      const text = document.createElement("p");
      text.textContent = issue.message || "";
      const recommendation = document.createElement("small");
      recommendation.className = "analysis-recommendation";
      recommendation.textContent = issue.recommendation || "";
      card.append(head, text, recommendation);
      if (issue.measure && state.measures.some(m => String(m.number) === String(issue.measure))) {
        const selectMeasure = document.createElement("button");
        selectMeasure.type = "button";
        selectMeasure.className = "analysis-jump";
        selectMeasure.textContent = "Adnotează măsura";
        selectMeasure.addEventListener("click", () => {
          $("annotation-measure").value = String(issue.measure);
          $("annotation-text").focus();
        });
        card.appendChild(selectMeasure);
      }
      root.appendChild(card);
    });
    $("analysis-disclaimer").textContent = analysis.disclaimer || "Alertele sunt orientative; nicio notă nu este modificată automat.";
  }

  function populateAnnotationVoices(score) {
    const select = $("annotation-voice");
    const current = select.value || "all";
    select.innerHTML = "";
    const all = document.createElement("option");
    all.value = "all"; all.textContent = "Toate vocile"; select.appendChild(all);
    Object.keys(score.part_names || {}).forEach((voice) => {
      const option = document.createElement("option");
      option.value = voice; option.textContent = voice; select.appendChild(option);
    });
    select.value = [...select.options].some(o => o.value === current) ? current : "all";
  }

  function populateIntonationVoices(score) {
    const select = $("intonation-voice");
    const current = select.value || "";
    select.innerHTML = "";
    Object.keys(score.part_names || {}).forEach((voice) => {
      const option = document.createElement("option");
      option.value = voice;
      option.textContent = voice;
      select.appendChild(option);
    });
    select.disabled = !select.options.length;
    select.value = [...select.options].some(option => option.value === current)
      ? current : (select.options[0]?.value || "");
  }

  function updateIntonationAvailability() {
    const selectedFile = $("intonation-file").files && $("intonation-file").files[0];
    $("run-intonation").disabled = !state.scoreId || !$("intonation-voice").value || !selectedFile;
  }

  function renderIntonationResult(data) {
    const summary = $("intonation-summary");
    const results = $("intonation-results");
    const counts = data.summary || {};
    summary.hidden = false;
    summary.innerHTML = "";
    results.innerHTML = "";
    const heading = document.createElement("strong");
    heading.textContent = data.voice + " · " + data.title;
    const metrics = document.createElement("div");
    metrics.className = "intonation-metrics";
    const rows = [
      ["Note verificate", counts.notes_checked ?? 0, "neutral"],
      ["În toleranță", counts.matched ?? 0, "matched"],
      ["Mai sus", counts.sharp ?? 0, "sharp"],
      ["Mai jos", counts.flat ?? 0, "flat"],
      ["Neclare", counts.unclear ?? 0, "unclear"],
      ["Potrivire estimată", counts.match_percent == null ? "—" : counts.match_percent + "%", "neutral"]
    ];
    rows.forEach(([label, value, status]) => {
      const item = document.createElement("div");
      item.className = "intonation-metric " + status;
      const name = document.createElement("span"); name.textContent = label;
      const number = document.createElement("strong"); number.textContent = String(value);
      item.append(name, number); metrics.appendChild(item);
    });
    summary.append(heading, metrics);
    const deviations = (data.notes || []).filter(note => note.status !== "matched");
    if (!deviations.length) {
      const empty = document.createElement("p");
      empty.className = "intonation-empty";
      empty.textContent = "Nu au apărut abateri în notele evaluabile. Verifică totuși înregistrarea cu urechea.";
      results.appendChild(empty);
    } else {
      deviations.slice(0, 100).forEach(note => {
        const card = document.createElement("article");
        card.className = "intonation-note " + note.status;
        const top = document.createElement("div"); top.className = "intonation-note-top";
        const title = document.createElement("strong");
        title.textContent = (note.measure ? "M. " + note.measure + " · " : "")
          + (note.expected_note || "?") + " → " + (note.detected_note || "nedetectată");
        const status = document.createElement("span");
        status.textContent = note.status === "sharp" ? "DIEZ / PREA SUS"
          : note.status === "flat" ? "BEMOL / PREA JOS"
          : note.status === "unclear" ? "NECLARĂ" : "NEEVALUATĂ";
        top.append(title, status);
        const detail = document.createElement("p");
        const cents = note.cents == null ? "fără estimare de centaj" : (note.cents > 0 ? "+" : "") + note.cents + " cenți";
        detail.textContent = "La " + formatTime(note.time_seconds) + " · " + cents
          + " · încredere estimare " + Math.round((note.confidence || 0) * 100) + "%.";
        card.append(top, detail);
        results.appendChild(card);
      });
      if (deviations.length > 100) {
        const more = document.createElement("p");
        more.className = "intonation-empty";
        more.textContent = "Sunt afișate primele 100 din " + deviations.length + " note de verificat.";
        results.appendChild(more);
      }
    }
    const disclaimer = document.createElement("p");
    disclaimer.className = "intonation-disclaimer";
    disclaimer.textContent = data.disclaimer || "Rezultat orientativ; verifică auditiv.";
    results.appendChild(disclaimer);
  }

  async function runIntonationCheck() {
    if (!state.scoreId) return;
    const file = $("intonation-file").files && $("intonation-file").files[0];
    if (!file) {
      $("intonation-status").textContent = "Alege un fișier WAV.";
      return;
    }
    $("run-intonation").disabled = true;
    $("run-intonation").textContent = "Se analizează…";
    $("intonation-summary").hidden = true;
    $("intonation-results").innerHTML = "";
    $("intonation-status").textContent = "Analizez local înălțimile notei și le aliniez la tempo-ul selectat. Fișierul nu este trimis la un serviciu extern.";
    try {
      const params = new URLSearchParams({
        voice: $("intonation-voice").value,
        reference_bpm: $("tempo").value,
        offset_ms: $("intonation-offset").value || "0",
        tolerance_cents: $("intonation-tolerance").value || "40"
      });
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/scores/" + encodeURIComponent(state.scoreId) + "/intonation-check?" + params, {
        method: "POST", body: form
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "Analiza înregistrării a eșuat.");
      renderIntonationResult(data);
      const s = data.summary || {};
      $("intonation-status").textContent = "Gata · " + (s.notes_checked || 0) + " note evaluate · "
        + (s.ambiguous_overlaps_skipped || 0) + " note suprapuse omise. Compară abaterile cu înregistrarea originală.";
    } catch (err) {
      $("intonation-status").textContent = err.message || String(err);
    } finally {
      $("run-intonation").textContent = "Analizează intonația";
      updateIntonationAvailability();
    }
  }

  async function saveAnnotations(nextAnnotations) {
    if (!state.scoreId) return;
    $("save-annotation").disabled = true;
    $("annotation-status").textContent = "Salvez adnotările pe acest dispozitiv…";
    try {
      const result = await jsonRequest("/api/scores/" + state.scoreId + "/annotations", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(nextAnnotations)
      });
      state.annotations = result.annotations || [];
      renderAnnotations();
      $("annotation-status").textContent = "Salvat local · " + state.annotations.length + " adnotări.";
    } catch (err) {
      $("annotation-status").textContent = err.message || String(err);
    } finally {
      $("save-annotation").disabled = !state.measures.length;
    }
  }

  function renderAnnotations() {
    const root = $("annotation-list");
    root.innerHTML = "";
    if (!state.annotations.length) {
      const empty = document.createElement("p");
      empty.textContent = "Nu există adnotări salvate.";
      root.appendChild(empty);
      return;
    }
    state.annotations.forEach((annotation) => {
      const item = document.createElement("article");
      item.className = "annotation-item";
      const title = document.createElement("strong");
      title.textContent = "M. " + annotation.measure + " · " + (annotation.voice === "all" ? "Toate vocile" : annotation.voice);
      const category = document.createElement("span");
      category.className = "annotation-category";
      category.textContent = annotation.category;
      const text = document.createElement("p");
      text.textContent = annotation.text;
      const remove = document.createElement("button");
      remove.type = "button"; remove.className = "annotation-remove"; remove.textContent = "Șterge";
      remove.addEventListener("click", () => saveAnnotations(state.annotations.filter(a => a.id !== annotation.id)));
      item.append(title, category, text, remove);
      root.appendChild(item);
    });
  }

  function loopBounds() {
    if (!state.score || !state.measures.length) throw new Error("Nu am putut identifica măsurile partiturii.");
    const startLabel = $("loop-start-measure").value;
    const endLabel = $("loop-end-measure").value;
    const startMeasure = state.measures.find(m => String(m.number) === startLabel);
    const endMeasure = state.measures.find(m => String(m.number) === endLabel);
    if (!startMeasure || !endMeasure) throw new Error("Alege măsuri valide pentru buclă.");
    if (Number(startMeasure.start_q) >= Number(endMeasure.end_q)) throw new Error("Măsura de sfârșit trebuie să fie după măsura de început.");
    const bpm = Number($("tempo").value);
    return {
      startQ: Number(startMeasure.start_q),
      endQ: Number(endMeasure.end_q),
      startSeconds: quartersToSeconds(Number(startMeasure.start_q), state.score.tempos || [], bpm),
      endSeconds: quartersToSeconds(Number(endMeasure.end_q), state.score.tempos || [], bpm)
    };
  }

  async function runCountIn() {
    const measureCount = Number($("count-in").value || 0);
    if (!measureCount) return;
    const bpm = Number($("tempo").value) || 100;
    const beats = measureCount * 4;
    const durationMs = beats * 60000 / bpm;
    $("practice-status").textContent = "Count-in: " + beats + " bătăi la " + bpm + " BPM…";
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass) {
      const context = new AudioContextClass();
      try {
        await context.resume();
        const origin = context.currentTime + 0.06;
        for (let i = 0; i < beats; i++) {
          const oscillator = context.createOscillator();
          const gain = context.createGain();
          oscillator.type = "sine";
          oscillator.frequency.value = i % 4 === 0 ? 1100 : 760;
          gain.gain.setValueAtTime(0.0001, origin + i * 60 / bpm);
          gain.gain.exponentialRampToValueAtTime(0.22, origin + i * 60 / bpm + 0.004);
          gain.gain.exponentialRampToValueAtTime(0.0001, origin + i * 60 / bpm + 0.065);
          oscillator.connect(gain); gain.connect(context.destination);
          oscillator.start(origin + i * 60 / bpm);
          oscillator.stop(origin + i * 60 / bpm + 0.075);
        }
      } catch (_) {
        /* The silence-based delay below still works when Web Audio is unavailable. */
      }
      await new Promise(resolve => window.setTimeout(resolve, durationMs));
      await context.close().catch(() => {});
    } else {
      await new Promise(resolve => window.setTimeout(resolve, durationMs));
    }
  }

  async function startLoopPractice() {
    if (!state.scoreId) return;
    try {
      const bounds = loopBounds();
      state.loop = { active: true, processing: false, ...bounds };
      state.progression = {
        active: $("tempo-progression").checked && Number($("tempo-target").value) !== Number($("tempo").value),
        target: Math.max(30, Math.min(240, Number($("tempo-target").value) || Number($("tempo").value))),
        step: Math.max(1, Math.min(30, Number($("tempo-step").value) || 5))
      };
      if (state.progression.active && ((state.progression.target > Number($("tempo").value) && state.loop.endSeconds <= state.loop.startSeconds)
          || (state.progression.target < Number($("tempo").value) && state.loop.endSeconds <= state.loop.startSeconds))) {
        throw new Error("Nu pot calcula intervalul de repetiție.");
      }
      $("practice-loop").disabled = true;
      $("stop-practice").disabled = false;
      $("practice-loop").textContent = "Se pregătește bucla…";
      player.pause();
      await preparePreview();
      await runCountIn();
      const refreshed = loopBounds();
      state.loop = { active: true, processing: false, ...refreshed };
      player.currentTime = Math.max(0, refreshed.startSeconds);
      await player.play();
      $("practice-status").textContent = "Repetă măsurile " + $("loop-start-measure").value + "–" + $("loop-end-measure").value + ".";
      $("practice-loop").textContent = "Bucla rulează…";
    } catch (err) {
      state.loop.active = false;
      $("practice-status").textContent = err.message || String(err);
      $("practice-loop").disabled = !state.measures.length;
      $("practice-loop").textContent = "▶ Repetă fragmentul";
      $("stop-practice").disabled = true;
    }
  }

  async function advanceLoopTempo() {
    if (!state.progression.active) {
      player.currentTime = state.loop.startSeconds;
      return;
    }
    const current = Number($("tempo").value);
    const target = state.progression.target;
    if (current === target) {
      player.currentTime = state.loop.startSeconds;
      return;
    }
    const direction = target > current ? 1 : -1;
    const next = current + direction * Math.min(state.progression.step, Math.abs(target - current));
    $("tempo").value = String(next);
    $("tempo").dispatchEvent(new Event("input"));
    await preparePreview();
    const bounds = loopBounds();
    state.loop = { ...state.loop, ...bounds, active: true, processing: true };
    await runCountIn();
    player.currentTime = Math.max(0, bounds.startSeconds);
    await player.play();
    $("practice-status").textContent = "Progresie de tempo: " + next + " BPM · țintă " + target + " BPM.";
  }

  async function handleLoopTimeUpdate() {
    if (!state.loop.active || state.loop.processing || player.paused) return;
    if (player.currentTime + 0.025 < state.loop.endSeconds) return;
    state.loop.processing = true;
    try {
      await advanceLoopTempo();
    } catch (err) {
      state.loop.active = false;
      player.pause();
      $("practice-status").textContent = "Bucla s-a oprit: " + (err.message || String(err));
      $("practice-loop").disabled = !state.measures.length;
      $("practice-loop").textContent = "▶ Repetă fragmentul";
      $("stop-practice").disabled = true;
    } finally {
      if (state.loop.active) state.loop.processing = false;
    }
  }

  function stopLoopPractice() {
    state.loop.active = false;
    state.loop.processing = false;
    state.progression.active = false;
    player.pause();
    $("practice-loop").disabled = !state.measures.length;
    $("practice-loop").textContent = "▶ Repetă fragmentul";
    $("stop-practice").disabled = true;
    $("practice-status").textContent = "Repetiția pe buclă s-a oprit.";
  }

  async function loadSavedScore(scoreId) {
    const result = await jsonRequest("/api/scores/" + encodeURIComponent(scoreId));
    state.scoreId = result.score_id;
    state.score = result;
    $("score-panel").hidden = false;
    await renderScore(result);
    showAnalysis(result);
    await loadConductorData(result);
    $("preview-button").disabled = false;
    $("download-midi").disabled = false;
    document.querySelectorAll("[data-midi-voice]").forEach((button) => {
      const voice = button.dataset.midiVoice;
      button.disabled = voice !== "all" && !Object.prototype.hasOwnProperty.call(result.part_names || {}, voice);
    });
    $("score-title").textContent = result.title || result.source_name || "Partitură salvată";
    $("score-meta").textContent = (result.source_name || "Bibliotecă locală") + " · " + result.note_count + " note";
    updateVideoExportAvailability();
    $("concert-status").textContent = "Partitura salvată a fost redeschisă din biblioteca locală.";
    return result;
  }

  function loadSetlist() {
    try {
      const stored = JSON.parse(window.localStorage.getItem("midi-mp4-concert-setlist") || "[]");
      state.setlist = Array.isArray(stored) ? stored.filter(item => item && typeof item.title === "string") : [];
    } catch (_) { state.setlist = []; }
    renderSetlist();
  }

  function saveSetlist() {
    try { window.localStorage.setItem("midi-mp4-concert-setlist", JSON.stringify(state.setlist)); }
    catch (_) { $("concert-status").textContent = "Browserul nu permite salvarea locală a setlist-ului."; }
    renderSetlist();
  }

  function renderSetlist() {
    const root = $("concert-setlist");
    root.innerHTML = "";
    if (!state.setlist.length) {
      const empty = document.createElement("p");
      empty.textContent = "Importă o partitură și adaug-o în program.";
      root.appendChild(empty);
    }
    state.setlist.forEach((item, index) => {
      const row = document.createElement("article");
      row.className = "setlist-item" + (item.id === state.activeSetlistId ? " active" : "");
      const title = document.createElement("strong"); title.textContent = (index + 1) + ". " + item.title;
      const meta = document.createElement("small"); meta.textContent = item.tempo ? item.tempo + " BPM" : "Tempo nedefinit";
      const controls = document.createElement("div"); controls.className = "setlist-actions";
      const make = (label, action, disabled = false) => {
        const button = document.createElement("button"); button.type = "button"; button.textContent = label; button.disabled = disabled;
        button.addEventListener("click", action); controls.appendChild(button);
      };
      make("Curentă", () => selectSetlistItem(item.id));
      make("▲", () => { if (index > 0) { [state.setlist[index - 1], state.setlist[index]] = [state.setlist[index], state.setlist[index - 1]]; saveSetlist(); } }, index === 0);
      make("▼", () => { if (index < state.setlist.length - 1) { [state.setlist[index + 1], state.setlist[index]] = [state.setlist[index], state.setlist[index + 1]]; saveSetlist(); } }, index === state.setlist.length - 1);
      make("▶", async () => {
        selectSetlistItem(item.id);
        try {
          if (item.scoreId !== state.scoreId || !state.score) await loadSavedScore(item.scoreId);
          $("tempo").value = String(item.tempo || 100);
          $("tempo").dispatchEvent(new Event("input"));
          await makePreview();
        } catch (err) {
          $("concert-status").textContent = "Nu pot deschide „" + item.title + "”: " + (err.message || String(err)) + ". Reimportă partitura dacă fișierul local a fost șters.";
        }
      });
      make("×", () => {
        state.setlist = state.setlist.filter(entry => entry.id !== item.id);
        if (state.activeSetlistId === item.id) state.activeSetlistId = state.setlist[0]?.id || null;
        saveSetlist();
      });
      row.append(title, meta, controls); root.appendChild(row);
    });
    const activeIndex = state.setlist.findIndex(item => item.id === state.activeSetlistId);
    const active = activeIndex >= 0 ? state.setlist[activeIndex] : state.setlist[0];
    const next = active ? state.setlist[activeIndex + 1] : null;
    $("concert-current-title").textContent = active ? active.title : "Nicio piesă selectată";
    $("concert-next-title").textContent = "Următoarea: " + (next ? next.title : "—");
  }

  function selectSetlistItem(id) {
    state.activeSetlistId = id;
    const item = state.setlist.find(entry => entry.id === id);
    if (item) $("concert-status").textContent = "Piesa curentă: " + item.title + ".";
    renderSetlist();
  }

  function addCurrentScoreToSetlist() {
    if (!state.score) return;
    const item = {
      id: "set-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7),
      scoreId: state.scoreId,
      title: state.score.title || state.score.source_name || "Partitură fără titlu",
      sourceName: state.score.source_name || "",
      tempo: Number($("tempo").value) || 100
    };
    state.setlist.push(item);
    if (!state.activeSetlistId) state.activeSetlistId = item.id;
    saveSetlist();
    $("concert-status").textContent = "Adăugată în repertoriu. Setlist-ul este salvat local în browser.";
  }

  async function createRehearsalPack() {
    if (!state.scoreId) return;
    $("create-pack").disabled = true;
    $("pack-status").textContent = $("pack-include-videos").checked
      ? "Generez audio, MIDI și cinci videoclipuri; poate dura considerabil mai mult…"
      : "Generez pachetul offline cu fișierele pentru fiecare voce…";
    try {
      const response = await fetch("/api/scores/" + state.scoreId + "/rehearsal-pack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reference_bpm: Number($("tempo").value),
          resolution: $("video-resolution").value,
          include_videos: $("pack-include-videos").checked,
          volume_by_voice: voiceVolumes()
        })
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.detail || "Nu am putut crea pachetul de repetiție.");
      }
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = ((state.score.title || "partitura").replace(/[^\p{L}\p{N} _-]/gu, "").trim() || "partitura") + "-rehearsal-pack.zip";
      document.body.appendChild(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(href), 30000);
      $("pack-status").textContent = "Pachet generat și descărcat. Conține materialele SATB, materialele pe voci și raportul preflight.";
    } catch (err) {
      $("pack-status").textContent = err.message || String(err);
    } finally {
      $("create-pack").disabled = !state.scoreId;
    }
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
  async function preparePreview() {
    if (!state.scoreId) throw new Error("Importă o partitură mai întâi.");
    const voices = selectedVoices();
    if (!voices.length) throw new Error("Selectează cel puțin o voce pentru preview.");
    const params = new URLSearchParams({
      reference_bpm: $("tempo").value,
      voices: voices.join(","),
      volumes: JSON.stringify(voiceVolumes())
    });
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
    player.load();
    await new Promise((resolve, reject) => {
      if (player.readyState >= 1) return resolve();
      const ready = () => { cleanup(); resolve(); };
      const failed = () => { cleanup(); reject(new Error("Browserul nu a putut încărca audio-ul generat.")); };
      const cleanup = () => {
        player.removeEventListener("loadedmetadata", ready);
        player.removeEventListener("error", failed);
      };
      player.addEventListener("loadedmetadata", ready, { once: true });
      player.addEventListener("error", failed, { once: true });
    });
    $("pause-button").disabled = false;
    return true;
  }

  async function makePreview() {
    if (!state.scoreId) return;
    state.loop.active = false;
    state.progression.active = false;
    $("practice-loop").textContent = "▶ Repetă fragmentul";
    $("stop-practice").disabled = true;
    $("practice-loop").disabled = !state.measures.length;
    $("preview-button").disabled = true;
    $("preview-button").textContent = "Se generează…";
    clearError();
    try {
      await preparePreview();
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
    // "New score" resets only the workspace. The local source, annotations and
    // concert setlist entries remain available for later use.
    state.loop.active = false; state.loop.processing = false; state.progression.active = false;
    state.measures = []; state.annotations = [];
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
    $("pack-status").textContent = "Importă o partitură pentru a pregăti pachetul.";
    $("pack-include-videos").checked = false;
    $("create-pack").disabled = true;
    $("practice-status").textContent = "Importă o partitură cu numere de măsură pentru a activa buclele.";
    $("practice-loop").disabled = true; $("stop-practice").disabled = true;
    $("loop-start-measure").innerHTML = "<option value=\"\">—</option>"; $("loop-end-measure").innerHTML = "<option value=\"\">—</option>";
    $("annotation-measure").innerHTML = "<option value=\"\">—</option>"; $("annotation-measure").disabled = true;
    $("annotation-list").innerHTML = "<p>Nu există adnotări salvate.</p>";
    $("analysis-summary").innerHTML = "<p>Importă o partitură pentru verificare.</p>"; $("analysis-issues").innerHTML = "";
    $("concert-add").disabled = true;
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
  $("practice-loop").addEventListener("click", startLoopPractice);
  $("stop-practice").addEventListener("click", stopLoopPractice);
  $("save-annotation").addEventListener("click", () => {
    const measure = $("annotation-measure").value;
    const text = $("annotation-text").value.trim();
    if (!measure || !text) {
      $("annotation-status").textContent = "Alege o măsură și scrie o observație.";
      return;
    }
    const annotation = {
      id: "ann-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8),
      measure,
      voice: $("annotation-voice").value,
      category: $("annotation-category").value,
      text
    };
    saveAnnotations([...state.annotations, annotation]).then(() => {
      $("annotation-text").value = "";
    });
  });
  $("concert-add").addEventListener("click", addCurrentScoreToSetlist);
  $("create-pack").addEventListener("click", createRehearsalPack);
  loadSetlist();

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
  player.addEventListener("timeupdate", handleLoopTimeUpdate);
  player.addEventListener("pause", () => { state.animating = false; });
  player.addEventListener("ended", () => { state.animating = false; $("cursor-label").textContent = "Cursor: final"; });
  fetch("/api/health").then(r => r.json()).then(health => {
    state.health = health;
    const omr = health.omr || {};
    $("omr-status").textContent = omr.available
      ? "OMR-SATB local activ: poți importa PDF-uri și fotografii ale partiturilor. " + (omr.directory || "")
      : "Import PDF/foto dezactivat până configurezi OMR_SATB_DIR și activele OMR-SATB. MusicXML/MXL/MSCZ funcționează normal.";
    $("omr-status").classList.toggle("omr-ready", omr.available === true);
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
