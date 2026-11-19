"""Romanian lyric preparation and OpenUtau/DiffSinger project export.

The module never fabricates a human voice. It converts Romanian lyric syllables to
phonetic hints accepted by a selected local DiffSinger voicebank, writes a native
USTX project, and reports phoneme approximations before the user renders WAV stems.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import unicodedata
from math import isfinite
from typing import Any


OPENUTAU_DIFFSINGER_PHONEMIZER = "OpenUtau.Core.DiffSinger.DiffSingerPhonemizer"
OPENUTAU_DIFFSINGER_EN_PHONEMIZER = "OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer"
USTX_VERSION = "0.6"
TICKS_PER_QUARTER = 480


@dataclass(frozen=True)
class DiffSingerBank:
    id: str
    name: str
    gender: str
    root: Path
    phonemes: frozenset[str]
    dictionary_symbols: frozenset[str]
    english_dictionary: bool
    phonemizer: str
    romanian_multidict: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "gender": self.gender,
            "phoneme_count": len(self.phonemes),
            "english_dictionary": self.english_dictionary,
            "romanian_multidict": self.romanian_multidict,
            "phonemizer": self.phonemizer,
        }


# Each item is one Romanian phoneme. A candidate may expand to two ARPAbet
# phones (e.g. Romanian /t͡s/ -> T + S) when the singer has no affricate token.
# These phonemes are inherently approximate in an English ARPAbet-based singer,
# even when the preferred token exists in the bank.
ALWAYS_APPROXIMATE_PHONES = {"a", "e", "o", "r", "ɨ", "ea", "eo", "oa"}

# Native NeoDIVA Multi-Dict Romanian phones. These correspond to the
# NeoDIVA RO inventory (not English ARPAbet approximations).
ROMANIAN_NATIVE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "a": ("a",), "e": ("e",), "i": ("i",), "o": ("o",), "u": ("u",),
    "ea": ("ea",), "eo": ("eo",), "oa": ("oa",),
    "ə": ("ax",), "ɨ": ("ux",),
    "b": ("b",), "d": ("d",), "f": ("f",), "g": ("g",), "h": ("h",),
    "k": ("k",), "l": ("l",), "m": ("m",), "n": ("n",), "p": ("p",),
    "r": ("r",), "s": ("s",), "ʃ": ("sh",), "t": ("t",), "t͡s": ("ts",),
    "t͡ʃ": ("ch",), "v": ("v",), "z": ("z",), "ʒ": ("j",),
    "d͡ʒ": ("dg",), "j": ("y",), "w": ("w",),
}


PHONE_CANDIDATES: dict[str, tuple[tuple[str, ...], ...]] = {
    "a": (("aa",), ("ah",), ("a",)),
    "e": (("eh",), ("ey",), ("e",)),
    "ea": (("eh", "aa"), ("ey", "aa"), ("eh", "ah")),
    "eo": (("eh", "ow"), ("eh", "ao")),
    "oa": (("ow", "aa"), ("ao", "aa"), ("ow", "ah")),
    "i": (("iy",), ("ih",), ("i",)),
    "o": (("ao",), ("ow",), ("o",)),
    "u": (("uw",), ("uh",), ("u",)),
    "ə": (("ax",), ("ah",), ("uh",)),
    "ɨ": (("ix",), ("ih",), ("iy",), ("uh",)),
    "b": (("b",),),
    "d": (("d",),),
    "f": (("f",),),
    "g": (("g",),),
    "h": (("hh",), ("h",)),
    "k": (("k",),),
    "l": (("l",),),
    "m": (("m",),),
    "n": (("n",),),
    "p": (("p",),),
    "r": (("r",),),
    "s": (("s",),),
    "ʃ": (("sh",), ("S",), ("ʃ",)),
    "t": (("t",),),
    "t͡s": (("ts",), ("t", "s"), ("t͡s",)),
    "t͡ʃ": (("ch",), ("tS",), ("t", "sh"), ("t͡ʃ",)),
    "v": (("v",),),
    "z": (("z",),),
    "ʒ": (("zh",), ("Z",), ("ʒ",)),
    "d͡ʒ": (("jh",), ("dZ",), ("d", "zh"), ("d͡ʒ",)),
    "j": (("y",), ("j",)),
    "w": (("w",),),
}


def _normalise_romanian(text: str) -> str:
    value = unicodedata.normalize("NFC", str(text or "")).casefold()
    # Older Romanian texts sometimes use cedilla instead of comma-below.
    return value.replace("ş", "ș").replace("ţ", "ț")


def romanian_phonemes(text: str) -> list[str]:
    """Return a compact Romanian phoneme sequence for one word or syllable.

    This is a deterministic grapheme-to-phoneme pass, not a trained Romanian
    pronunciation model. It covers standard Romanian spelling and common
    digraph rules. Accent and lexical exceptions are kept visible to the caller.
    """
    value = _normalise_romanian(text)
    value = re.sub(r"[\[\]{}()'’\".,!?;:…]", " ", value)
    value = value.replace("-", " ")
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return []

    result: list[str] = []
    i = 0
    while i < len(value):
        if value[i].isspace():
            i += 1
            continue
        tail = value[i:]

        # Romanian spelling digraphs: h preserves the hard velar before e/i.
        if tail.startswith("chea"):
            result.extend(("k", "ea")); i += 4; continue
        if tail.startswith("ghea"):
            result.extend(("g", "ea")); i += 4; continue
        if tail.startswith("che"):
            result.extend(("k", "e")); i += 3; continue
        if tail.startswith("chi"):
            result.extend(("k", "i")); i += 3; continue
        if tail.startswith("ghe"):
            result.extend(("g", "e")); i += 3; continue
        if tail.startswith("ghi"):
            result.extend(("g", "i")); i += 3; continue
        if tail.startswith("cea"):
            result.extend(("t͡ʃ", "ea")); i += 3; continue
        if tail.startswith("gea"):
            result.extend(("d͡ʒ", "ea")); i += 3; continue
        if tail.startswith("ce") or tail.startswith("ci"):
            result.extend(("t͡ʃ", "e" if tail[1] == "e" else "i")); i += 2; continue
        if tail.startswith("ge") or tail.startswith("gi"):
            result.extend(("d͡ʒ", "e" if tail[1] == "e" else "i")); i += 2; continue
        # Romanian falling/rising diphthongs that have dedicated NeoDIVA RO
        # phones. MusicXML lyrics are already split into sung syllables.
        if tail.startswith("ea"):
            result.append("ea"); i += 2; continue
        if tail.startswith("eo"):
            result.append("eo"); i += 2; continue
        if tail.startswith("oa"):
            result.append("oa"); i += 2; continue

        char = value[i]
        if char == "ă":
            result.append("ə")
        elif char in {"â", "î"}:
            result.append("ɨ")
        elif char == "ș":
            result.append("ʃ")
        elif char == "ț":
            result.append("t͡s")
        elif char == "j":
            result.append("ʒ")
        elif char == "c":
            result.append("k")
        elif char == "g":
            result.append("g")
        elif char == "x":
            result.extend(("k", "s"))
        elif char == "q":
            result.append("k")
        elif char == "w":
            result.append("w")
        elif char == "y":
            result.append("j")
        elif char in {"a", "e", "i", "o", "u"}:
            result.append(char)
        elif char in "bdfhklmnprstvz":
            result.append(char)
        # Ignore whitespace and non-phonemic score punctuation.
        i += 1
    return result


def _read_symbol_file(path: Path) -> set[str]:
    symbols: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";", "//")):
                continue
            token = re.split(r"[\s,]+", line, maxsplit=1)[0].strip().strip("\"'")
            if token:
                symbols.add(token)
    except OSError:
        pass
    return symbols


def _read_dictionary_symbols(path: Path) -> set[str]:
    """Extract symbol declarations from a DiffSinger dsdict YAML file."""
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return set()
    found = set(re.findall(r"(?im)^\s*-\s*\{?\s*symbol\s*:\s*[\"']?([^,}\s\"']+)", raw))
    found.update(re.findall(r"(?im)^\s*symbol\s*:\s*[\"']?([^,}\s\"']+)", raw))
    return {item.strip() for item in found if item.strip()}


def _parse_character_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().casefold()
        if key:
            values[key] = value.strip()
    return values


def _infer_gender(values: dict[str, str], fallback_text: str) -> str:
    explicit = " ".join(values.get(key, "") for key in ("gender", "sex", "voice", "other")).casefold()
    explicit = re.sub(r"[^a-z]+", " ", explicit).strip()
    if re.search(r"\b(female|feminine|woman|women|girl|soprano|alto)\b", explicit):
        return "female"
    if re.search(r"\b(male|masculine|man|men|boy|tenor|bass|baritone)\b", explicit):
        return "male"
    raw = _normalise_romanian(fallback_text)
    if re.search(r"\b(female|feminine|woman|girl|soprano|alto)\b", raw):
        return "female"
    if re.search(r"\b(male|masculine|man|boy|tenor|bass|baritone)\b", raw):
        return "male"
    if values.get("gender", "").strip().casefold() in {"f", "w"}:
        return "female"
    if values.get("gender", "").strip().casefold() in {"m", "b"}:
        return "male"
    return "unspecified"


def singer_roots() -> list[Path]:
    """Resolve standard OpenUtau singer roots and its configured Additional Singer Path."""
    home = Path.home()
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    data_dirs = [
        home / "Library" / "OpenUtau",
        Path(xdg_data_home).expanduser() / "OpenUtau" if xdg_data_home else home / ".local" / "share" / "OpenUtau",
        home / ".local" / "share" / "OpenUtau",
        home / ".local" / "share" / "openutau",
        home / ".config" / "OpenUtau",
        home / "AppData" / "Roaming" / "OpenUtau",
        home / "Library" / "Application Support" / "OpenUtau",
        home / "OpenUtau",
    ]
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        data_dirs.insert(0, Path(appdata).expanduser() / "OpenUtau")

    candidates: list[Path] = []
    configured = (
        os.environ.get("MIDI_MP4_OPENUTAU_SINGERS", "").strip()
        or os.environ.get("OPENUTAU_SINGERS", "").strip()
    )
    if configured:
        candidates.append(Path(configured).expanduser())

    for data_dir in data_dirs:
        candidates.extend([
            data_dir / "Singers",
            data_dir / "Content" / "Singers",
        ])
        preferences_path = data_dir / "prefs.json"
        try:
            preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
            additional = str(preferences.get("AdditionalSingerPath") or "").strip()
            if additional:
                candidates.append(Path(additional).expanduser())
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def discover_diff_singers() -> list[DiffSingerBank]:
    banks: list[DiffSingerBank] = []
    seen: set[str] = set()
    for root in singer_roots():
        try:
            character_files = list(root.rglob("character.txt")) + list(root.rglob("character.yaml"))
        except OSError:
            continue
        for character_file in character_files:
            bank_dir = character_file.parent
            # OpenUtau identifies the Singer from its root dsconfig.yaml.
            # Its DiffSinger phonemizer separately requires dsdur/dsconfig.yaml.
            root_config_path = bank_dir / "dsconfig.yaml"
            dsdur = bank_dir / "dsdur"
            duration_config_path = dsdur / "dsconfig.yaml"
            if not root_config_path.is_file() or not duration_config_path.is_file():
                continue
            relative_id = bank_dir.relative_to(root).as_posix()
            if not relative_id or relative_id in seen:
                continue
            seen.add(relative_id)
            metadata = _parse_character_file(character_file) if character_file.name == "character.txt" else {}
            if character_file.name == "character.yaml":
                try:
                    raw_meta = character_file.read_text(encoding="utf-8-sig", errors="replace")
                    for key in ("name", "author", "voice", "gender", "sex", "other"):
                        match = re.search(rf"(?im)^\s*{key}\s*:\s*[\"']?([^#\n\"']+)", raw_meta)
                        if match:
                            metadata[key] = match.group(1).strip()
                except OSError:
                    pass
            fallback = " ".join([bank_dir.name, relative_id, *metadata.values()])
            name = metadata.get("name") or bank_dir.name
            try:
                root_config_text = root_config_path.read_text(encoding="utf-8-sig", errors="replace")
                duration_config_text = duration_config_path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue

            def configured_phoneme_file(config_text: str, base_dir: Path) -> Path:
                match = re.search(r"""(?im)^\s*phonemes\s*:\s*["']?([^#\s"']+)""", config_text)
                filename = match.group(1) if match else "phonemes.txt"
                return base_dir / filename

            # Acoustic rendering and duration prediction each load a phoneme
            # inventory. Only offer phones supported by both models.
            acoustic_path = configured_phoneme_file(root_config_text, bank_dir)
            duration_path = configured_phoneme_file(duration_config_text, dsdur)
            if not acoustic_path.is_file() or not duration_path.is_file():
                continue
            acoustic_phonemes = _read_symbol_file(acoustic_path)
            duration_phonemes = _read_symbol_file(duration_path)
            phonemes = acoustic_phonemes & duration_phonemes
            if not phonemes:
                continue
            en_dict = dsdur / "dsdict-en.yaml"
            generic_dict = dsdur / "dsdict.yaml"
            if not en_dict.is_file() and not generic_dict.is_file():
                # DiffSingerBasePhonemizer refuses to load without either dictionary.
                continue
            use_english = en_dict.is_file()
            dictionary_path = en_dict if use_english else generic_dict
            dictionary_symbols = _read_dictionary_symbols(dictionary_path)
            romanian_multidict = bool(
                not use_english
                and any(symbol.startswith("ro/") for symbol in dictionary_symbols)
                and any(symbol.startswith("ro/") for symbol in phonemes)
            )
            # The DiffSinger English phonemizer contributes ARPAbet symbols itself.
            if use_english:
                dictionary_symbols.update({
                    "aa", "ae", "ah", "ao", "aw", "ay", "eh", "er", "ey",
                    "ih", "iy", "ow", "oy", "uh", "uw", "b", "ch", "d",
                    "dh", "f", "g", "hh", "jh", "k", "l", "m", "n", "ng",
                    "p", "r", "s", "sh", "t", "th", "v", "w", "y", "z", "zh",
                })
                phonemizer = OPENUTAU_DIFFSINGER_EN_PHONEMIZER
            else:
                phonemizer = OPENUTAU_DIFFSINGER_PHONEMIZER
            banks.append(DiffSingerBank(
                id=relative_id,
                name=name,
                gender=_infer_gender(metadata, fallback),
                root=bank_dir,
                phonemes=frozenset(phonemes),
                dictionary_symbols=frozenset(dictionary_symbols),
                english_dictionary=use_english,
                phonemizer=phonemizer,
                romanian_multidict=romanian_multidict,
            ))
    return sorted(banks, key=lambda bank: (bank.gender, bank.name.casefold(), bank.id))


def _case_match(token: str, available: set[str]) -> str | None:
    if token in available:
        return token
    lowered = token.casefold()
    matches = [candidate for candidate in available if candidate.casefold() == lowered]
    return sorted(matches)[0] if matches else None


ENGLISH_BASE_PHONES = frozenset({
    "aa", "ae", "ah", "ao", "aw", "ay", "eh", "er", "ey",
    "ih", "iy", "ow", "oy", "uh", "uw", "b", "ch", "d",
    "dh", "f", "g", "hh", "jh", "k", "l", "m", "n", "ng",
    "p", "r", "s", "sh", "t", "th", "v", "w", "y", "z", "zh",
})


def map_romanian_phonemes(phones: list[str], bank: DiffSingerBank) -> tuple[list[str], list[str]]:
    available = set(bank.phonemes)
    dictionary_symbols = set(bank.dictionary_symbols)

    if bank.romanian_multidict:
        mapped: list[str] = []
        for phone in phones:
            candidates = ROMANIAN_NATIVE_CANDIDATES.get(phone)
            if candidates is None:
                raise ValueError(
                    f"Nu există mapare nativă NeoDIVA RO pentru sunetul {phone!r}. "
                    "Nu voi substitui un fonem românesc cu unul englezesc."
                )
            token = "ro/" + candidates[0]
            if token not in available or token not in dictionary_symbols:
                raise ValueError(
                    f"Voicebank-ul «{bank.name}» nu expune fonemul românesc {token}. "
                    "Este necesar un NeoDIVA Multi-Dict cu inventarul RO complet."
                )
            mapped.append(token)
        return mapped, []

    def resolve(candidate: str) -> str | None:
        # DiffSinger's built-in English phonemizer accepts base ARPAbet names
        # and transparently resolves them to en/<phone> when the singer's
        # phonemes.txt uses language-prefixed tokens.
        if bank.english_dictionary and candidate in ENGLISH_BASE_PHONES:
            # OpenUtau's English phonemizer accepts base ARPAbet symbols and
            # resolves them to en/<phone> when a singer uses language prefixes.
            if candidate in available or "en/" + candidate in available:
                return candidate
            return None

        direct = _case_match(candidate, available)
        if direct and (not dictionary_symbols or _case_match(candidate, dictionary_symbols)):
            return direct
        return None

    mapped: list[str] = []
    approximations: list[str] = []
    for phone in phones:
        chosen: tuple[str, ...] | None = None
        chosen_candidate: tuple[str, ...] | None = None
        for candidate_sequence in PHONE_CANDIDATES.get(phone, ((phone,),)):
            resolved = []
            for candidate in candidate_sequence:
                match = resolve(candidate)
                if match is None:
                    resolved = []
                    break
                resolved.append(match)
            if resolved:
                chosen = tuple(resolved)
                chosen_candidate = candidate_sequence
                break
        if chosen is None:
            raise ValueError(
                f"Voicebank-ul «{bank.name}» nu are foneme compatibile pentru sunetul românesc {phone!r}. "
                "Alege un alt singer sau un voicebank cu foneme mai apropiate de română."
            )
        mapped.extend(chosen)
        inherently_approximate = phone in ALWAYS_APPROXIMATE_PHONES
        if phone == "ə" and chosen_candidate != ("ax",):
            inherently_approximate = True
        if phone == "t͡s" and chosen_candidate != ("ts",):
            inherently_approximate = True
        if chosen_candidate != PHONE_CANDIDATES.get(phone, ((phone,),))[0] or inherently_approximate:
            approximations.append(phone)
    return mapped, approximations


def _clean_syllable(text: str) -> str:
    value = unicodedata.normalize("NFC", str(text or "")).strip()
    value = re.sub(r"[-‐‑‒–—]+$", "", value)
    value = re.sub(r"^[-‐‑‒–—]+", "", value)
    return value.strip()


def _pitch_payload() -> dict[str, Any]:
    return {
        "data": [
            {"x": -100, "y": 0, "shape": "io"},
            {"x": 0, "y": 0, "shape": "io"},
        ],
        "snap_first": True,
    }


def _vibrato_payload() -> dict[str, Any]:
    return {
        "length": 0,
        "period": 175,
        "depth": 25,
        "in": 10,
        "out": 10,
        "shift": 0,
        "drift": 0,
        "vol_link": 0,
    }


def _extract_voice_lyrics(events: list[Any]) -> list[tuple[float, str, str | None]]:
    return [
        (float(event.start_q), _clean_syllable(event.lyric or ""), event.lyric_syllabic)
        for event in events
        if not event.is_rest and _clean_syllable(event.lyric or "")
    ]


def _lyric_at_position(lyrics: list[tuple[float, str, str | None]], position: float) -> tuple[str, str | None] | None:
    for lyric_position, text, syllabic in lyrics:
        if abs(lyric_position - position) <= 0.025:
            return text, syllabic
    return None


def _resolve_voice_singer(
    voice: str,
    male_singer_id: str,
    female_singer_id: str,
    bank_by_id: dict[str, DiffSingerBank],
) -> DiffSingerBank:
    singer_id = female_singer_id if voice in {"Soprano", "Alto"} else male_singer_id
    bank = bank_by_id.get(singer_id)
    if bank is None:
        role = "feminin" if voice in {"Soprano", "Alto"} else "masculin"
        raise ValueError(f"Nu este selectat un voicebank DiffSinger {role} pentru {voice}.")
    return bank


def _ustx_tempo_map(score: Any, reference_bpm: float) -> list[dict[str, Any]]:
    """Scale the complete source tempo map, preserving every tempo change."""
    source_tempos = sorted(
        list(getattr(score, "tempos", None) or []),
        key=lambda item: float(getattr(item, "start_q", 0.0)),
    )
    source_start = next(
        (float(item.bpm) for item in source_tempos if float(item.start_q) <= 0),
        float(source_tempos[0].bpm) if source_tempos else 100.0,
    )
    if source_start <= 0:
        source_start = 100.0
    scale = float(reference_bpm) / source_start
    if not source_tempos:
        return [{"position": 0, "bpm": float(reference_bpm)}]
    result = [
        {
            "position": max(0, int(round(float(item.start_q) * TICKS_PER_QUARTER))),
            "bpm": round(float(item.bpm) * scale, 6),
        }
        for item in source_tempos
    ]
    if result[0]["position"] > 0:
        result.insert(0, {"position": 0, "bpm": float(reference_bpm)})
    return result


def _ustx_time_signatures(score: Any) -> list[dict[str, int]]:
    """Convert MusicXML meter changes from quarter offsets to USTX bar positions."""
    signatures = sorted(
        list(getattr(score, "time_signatures", None) or []),
        key=lambda item: float(getattr(item, "start_q", 0.0)),
    )
    if not signatures:
        return [{"bar_position": 0, "beat_per_bar": 4, "beat_unit": 4}]
    result: list[dict[str, int]] = []
    bar_position = 0
    previous = None
    for item in signatures:
        if previous is not None:
            previous_bar_q = float(previous.numerator) * 4.0 / float(previous.denominator)
            if previous_bar_q > 0:
                delta_q = max(0.0, float(item.start_q) - float(previous.start_q))
                bar_position += max(0, int(round(delta_q / previous_bar_q)))
        result.append({
            "bar_position": bar_position,
            "beat_per_bar": int(item.numerator),
            "beat_unit": int(item.denominator),
        })
        previous = item
    if result[0]["bar_position"] != 0:
        result.insert(0, {"bar_position": 0, "beat_per_bar": 4, "beat_unit": 4})
    return result


def build_romanian_ustx(
    score: Any,
    output_path: Path,
    reference_bpm: float,
    male_singer_id: str,
    female_singer_id: str,
) -> dict[str, Any]:
    """Write a complete four-part Romanian USTX project using installed singers."""
    banks = discover_diff_singers()
    bank_by_id = {bank.id: bank for bank in banks}
    voices = [
        voice for voice in (score.voice_order or list(score.part_names))
        if voice in score.part_names
    ]
    if not voices:
        raise ValueError("Partitura nu conține partide vocale exportabile.")
    if not 30 <= float(reference_bpm) <= 240:
        raise ValueError("Tempo-ul trebuie să fie între 30 și 240 BPM.")

    voice_songs: dict[str, list[Any]] = {}
    lyrics_by_voice: dict[str, list[tuple[float, str, str | None]]] = {}
    for voice in voices:
        events = [
            event for event in sorted(score.notes, key=lambda item: (item.start_q, item.pitch or -1))
            if event.voice == voice and not event.is_rest and event.pitch is not None
        ]
        if not events:
            raise ValueError(f"Partida {voice} nu conține note cântabile.")
        lyrics = _extract_voice_lyrics(events)
        voice_songs[voice] = events
        lyrics_by_voice[voice] = lyrics

    canonical_lyrics = next((items for items in lyrics_by_voice.values() if items), [])
    if not canonical_lyrics:
        raise ValueError(
            "MusicXML-ul nu conține versuri. Adaugă versurile în partitură și reimportă fișierul; "
            "motorul nu inventează versuri."
        )

    # Some choral scores print the text only under Soprano. For SATB homophony,
    # transfer each syllable to simultaneous notes in the other parts.
    for voice in voices:
        if lyrics_by_voice[voice]:
            continue
        copied = []
        for event in voice_songs[voice]:
            match = _lyric_at_position(canonical_lyrics, float(event.start_q))
            if match:
                copied.append((float(event.start_q), match[0], match[1]))
        if copied:
            lyrics_by_voice[voice] = copied

    track_payload = []
    part_payload = []
    approximation_summary: dict[str, list[str]] = {}
    alignment_timeline: list[dict[str, Any]] = []
    total_ticks = max(480, int(round(float(score.total_quarters) * TICKS_PER_QUARTER)))

    for track_no, voice in enumerate(voices):
        events = voice_songs[voice]
        lyrics = lyrics_by_voice[voice]
        if not lyrics:
            raise ValueError(
                f"Partida {voice} nu are versuri și nu s-a putut transfera textul de la o altă voce. "
                "Adaugă versurile pentru această partidă în MusicXML."
            )
        bank = _resolve_voice_singer(voice, male_singer_id, female_singer_id, bank_by_id)
        notes_payload = []
        used_approximations: set[str] = set()
        previous_lyric_end_q: float | None = None

        # DiffSinger expects a single melodic line per track. Refuse divisi/chords
        # rather than silently overlap notes and produce a broken vocal phrase.
        for left, right in zip(events, events[1:]):
            left_end = float(left.start_q) + float(left.duration_q)
            if float(right.start_q) < left_end - 0.015:
                raise ValueError(
                    f"Partida {voice} conține note suprapuse/divisi. Exportul vocal automat cere "
                    "o singură linie melodică per partidă."
                )

        for event in events:
            event_start_q = float(event.start_q)
            event_duration_q = float(event.duration_q)
            if (
                not isfinite(event_start_q)
                or not isfinite(event_duration_q)
                or event_start_q < 0
                or event_duration_q <= 0
            ):
                raise ValueError(
                    f"Partida {voice} conține o notă cu poziție sau durată invalidă."
                )

            syllable: str | None = None
            syllabic: str | None = None
            mapped_phones: list[str] = []
            syllable_info = _lyric_at_position(lyrics, event_start_q)
            if syllable_info:
                syllable, syllabic = syllable_info
                raw_phones = romanian_phonemes(syllable)
                if not raw_phones:
                    raise ValueError(
                        f"Nu am putut interpreta silaba românească {syllable!r} din partida {voice}."
                    )
                mapped_phones, approximations = map_romanian_phonemes(raw_phones, bank)
                used_approximations.update(approximations)
                lyric_with_hint = f"{syllable}[{' '.join(mapped_phones)}]"
                alignment_kind = "syllable"
                previous_lyric_end_q = event_start_q + event_duration_q
            else:
                # OpenUtau's DiffSinger phonemizer treats +~ as a vowel-extension
                # note. Use it only for a directly contiguous continuation; gaps
                # and rests must never accidentally prolong the previous syllable.
                contiguous = (
                    previous_lyric_end_q is not None
                    and abs(event_start_q - previous_lyric_end_q) <= 0.015
                )
                if contiguous:
                    lyric_with_hint = "+~"
                    alignment_kind = "melisma"
                    previous_lyric_end_q = event_start_q + event_duration_q
                else:
                    lyric_with_hint = "SP"
                    alignment_kind = "pause"
                    previous_lyric_end_q = None

            start_tick = int(round(event_start_q * TICKS_PER_QUARTER))
            duration_tick = int(round(event_duration_q * TICKS_PER_QUARTER))
            if duration_tick < 1:
                raise ValueError(
                    f"Nota din partida {voice} este mai scurtă decât rezoluția USTX de un tick."
                )
            alignment_timeline.append({
                "voice": voice,
                "kind": alignment_kind,
                "syllable": syllable,
                "syllabic": syllabic,
                "phonemes": mapped_phones,
                "ustx_lyric": lyric_with_hint,
                "start_quarters": round(event_start_q, 6),
                "duration_quarters": round(event_duration_q, 6),
                "end_quarters": round(event_start_q + event_duration_q, 6),
                "start_ticks": start_tick,
                "duration_ticks": duration_tick,
                "end_ticks": start_tick + duration_tick,
            })
            note_payload = {
                "position": start_tick,
                "duration": duration_tick,
                "tone": int(event.pitch),
                "lyric": lyric_with_hint,
                "pitch": _pitch_payload(),
                "vibrato": _vibrato_payload(),
                "phoneme_expressions": [],
                "phoneme_overrides": [],
            }
            notes_payload.append(note_payload)

        if not any(note["lyric"] not in {"SP", "+~"} for note in notes_payload):
            raise ValueError(f"Partida {voice} nu are silabe utilizabile.")
        track_payload.append({
            "singer": bank.id,
            "phonemizer": bank.phonemizer,
            "track_name": voice,
            "track_color": {"Soprano": "Red", "Alto": "Orange", "Tenor": "Blue", "Bass": "Purple"}.get(voice, "Blue"),
            "mute": False,
            "solo": False,
            "volume": 0.0,
            "pan": 0.0,
            "voice_color_names": [""],
        })
        part_payload.append({
            "duration": total_ticks,
            "name": voice,
            "track_no": track_no,
            "position": 0,
            "notes": notes_payload,
            "curves": [],
        })
        approximation_summary[voice] = sorted(used_approximations)

    project = {
        "name": str(score.title or "Cor SATB"),
        "comment": "Generat local de MIDI-MP4. Versurile românești sunt păstrate, cu phonetic hints pentru DiffSinger.",
        "output_dir": "Vocal",
        "cache_dir": "UCache",
        "ustx_version": USTX_VERSION,
        "expressions": {},
        "exp_selectors": ["dyn", "pitd", "clr", "eng", "vel", "vol", "atk", "dec", "gen", "bre"],
        "exp_primary": 0,
        "exp_secondary": 1,
        "key": 0,
        "time_signatures": _ustx_time_signatures(score),
        "tempos": _ustx_tempo_map(score, reference_bpm),
        "tracks": track_payload,
        "voice_parts": part_payload,
        "wave_parts": [],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # JSON is a YAML 1.2 subset and is accepted by OpenUtau's USTX loader.
    output_path.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    selected_banks = {
        voice: _resolve_voice_singer(voice, male_singer_id, female_singer_id, bank_by_id)
        for voice in voices
    }
    return {
        "path": str(output_path),
        "voices": voices,
        "banks": {voice: bank.name for voice, bank in selected_banks.items()},
        "bank_modes": {
            voice: ("NeoDIVA Romanian Multi-Dict" if bank.romanian_multidict else
                    "DiffSinger English phoneme fallback" if bank.english_dictionary else
                    "Generic DiffSinger phonemizer")
            for voice, bank in selected_banks.items()
        },
        "approximations": approximation_summary,
        "note_count": sum(len(part["notes"]) for part in part_payload),
        "lyric_note_count": sum(
            1 for part in part_payload for note in part["notes"]
            if note["lyric"] not in {"SP", "+~"}
        ),
        "lyric_alignment": {
            "ticks_per_quarter": TICKS_PER_QUARTER,
            "events": alignment_timeline,
            "syllable_count": sum(1 for item in alignment_timeline if item["kind"] == "syllable"),
            "melisma_count": sum(1 for item in alignment_timeline if item["kind"] == "melisma"),
            "pause_count": sum(1 for item in alignment_timeline if item["kind"] == "pause"),
        },
    }


def romanian_singing_status() -> dict[str, Any]:
    banks = discover_diff_singers()
    native_count = sum(1 for bank in banks if bank.romanian_multidict)
    return {
        "available": bool(banks),
        "singer_count": len(banks),
        "romanian_multidict_count": native_count,
        "singers": [bank.public_dict() for bank in banks],
        "message": (
            f"Am detectat {len(banks)} voicebank-uri DiffSinger locale, dintre care {native_count} cu foneme românești NeoDIVA Multi-Dict. "
            "Pentru pronunție românească nativă, alege două voicebank-uri marcate RO Multi-Dict."
            if banks else
            "Nu am găsit voicebank-uri DiffSinger în folderul OpenUtau. "
            "Instalează voicebank-uri compatibile și repornește MIDI-MP4."
        ),
    }
