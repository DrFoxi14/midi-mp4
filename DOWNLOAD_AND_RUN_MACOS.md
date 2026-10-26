# MIDI-MP4 — descarcă și pornește pe macOS

Acest proiect rulează local pe Mac. Scriptul descarcă sursele din ramura PR-ului, instalează dependențele Python și Node necesare la prima rulare și deschide interfața în browser.

## Descărcare directă

[**Descarcă MIDI-MP4 (ZIP)**](https://github.com/DrFoxi14/midi-mp4/archive/refs/heads/feature/conductor-volume-mixer.zip)

Dacă preferi Terminal, copiază comanda de mai jos într-o singură linie. Aceasta descarcă arhiva, o extrage într-un folder din directorul personal și pornește launcherul macOS:

```bash
cd "$HOME" && curl -fL "https://github.com/DrFoxi14/midi-mp4/archive/refs/heads/feature/conductor-volume-mixer.zip" -o midi-mp4.zip && ditto -xk midi-mp4.zip midi-mp4-download && cd midi-mp4-download/midi-mp4-feature-conductor-volume-mixer && chmod +x run-macos.command && ./run-macos.command
```

## Cerințe

- macOS și conexiune la internet la prima instalare.
- Python 3.11+ și Node.js/npm.
- FFmpeg este necesar pentru exportul video; fără el, aplicația poate porni, dar exportul video nu va funcționa.
- MuseScore 4 este opțional și este necesar pentru import direct de fișiere `.mscz`.

Launcherul creează un mediu Python local `.venv`, instalează pachetele Python și rendererul web, instalează Chromium pentru Playwright dacă lipsește, apoi pornește serverul la http://127.0.0.1:8765. Ține fereastra Terminal deschisă cât timp folosești aplicația; apasă Ctrl+C pentru a opri serverul.

## Instalarea FFmpeg (opțional, pentru video/MP3)

Dacă ai Homebrew instalat:

```bash
brew install ffmpeg
```

## Notă despre versiune

Arhiva și linkul de mai sus indică ramura `feature/conductor-volume-mixer`, asociată PR-ului #1. Modificările din PR nu sunt încă în `main`. O voce neurală nu este inclusă; pentru voci AI este necesar fluxul separat OpenUtau/DiffSinger și un voicebank compatibil.
