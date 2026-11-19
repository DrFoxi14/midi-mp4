from pathlib import Path
import json
from xml.etree import ElementTree as ET

import pytest

from app.core import NoteEvent, ScoreData, TempoEvent, TimeSignatureEvent, parse_score
import app.romanian_singing as romanian


ENGLISH_PHONES = {
    "aa", "ae", "ah", "ao", "aw", "ay", "eh", "er", "ey", "ih", "iy",
    "ow", "oy", "uh", "uw", "b", "ch", "d", "dh", "f", "g", "hh",
    "jh", "k", "l", "m", "n", "ng", "p", "r", "s", "sh", "t", "th",
    "v", "w", "y", "z", "zh", "ts",
}


def _write_bank(root: Path, folder: str, gender: str) -> None:
    bank = root / folder
    dsdur = bank / "dsdur"
    dsdur.mkdir(parents=True)
    (bank / "character.txt").write_text(
        f"name={folder.title()} Singer\ngender={gender}\n",
        encoding="utf-8",
    )
    (bank / "dsconfig.yaml").write_text("phonemes: phonemes.txt\n", encoding="utf-8")
    (bank / "phonemes.txt").write_text("\n".join(sorted(ENGLISH_PHONES)), encoding="utf-8")
    (dsdur / "dsconfig.yaml").write_text("phonemes: phonemes.txt\n", encoding="utf-8")
    (dsdur / "phonemes.txt").write_text("\n".join(sorted(ENGLISH_PHONES)), encoding="utf-8")
    (dsdur / "dsdict-en.yaml").write_text("symbols: []\n", encoding="utf-8")


def _score(lyrics_on_soprano_only: bool = False) -> ScoreData:
    voices = ["Soprano", "Alto", "Tenor", "Bass"]
    notes = []
    pitches = {"Soprano": 72, "Alto": 67, "Tenor": 60, "Bass": 48}
    syllables = ["Și", "cân", "tă"]
    for voice in voices:
        for index, lyric in enumerate(syllables):
            notes.append(NoteEvent(
                voice=voice,
                part_name=voice,
                pitch=pitches[voice] + (index % 2),
                start_q=float(index * 2),
                duration_q=2.0,
                lyric=lyric if (not lyrics_on_soprano_only or voice == "Soprano") else None,
                lyric_syllabic="single",
            ))
    return ScoreData(
        "Cor românesc",
        {voice: voice for voice in voices},
        notes,
        [TempoEvent(0.0, 100.0), TempoEvent(4.0, 120.0)],
        6.0,
        "",
        voices,
        [TimeSignatureEvent(0.0, 4, 4), TimeSignatureEvent(4.0, 3, 4)],
    )



def test_musicxml_import_preserves_romanian_lyrics_and_syllabic_metadata(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    tree = ET.parse(fixture)
    root = tree.getroot()
    for part in root.findall("./part"):
        note = part.find("./measure/note")
        assert note is not None
        lyric = ET.SubElement(note, "lyric", {"number": "1"})
        ET.SubElement(lyric, "syllabic").text = "single"
        ET.SubElement(lyric, "text").text = "Și"

    source = tmp_path / "romanian-lyrics.musicxml"
    tree.write(source, encoding="utf-8", xml_declaration=True)
    score = parse_score(source, tmp_path / "parsed")

    for voice in ("Soprano", "Alto", "Tenor", "Bass"):
        event = next(note for note in score.notes if note.voice == voice)
        assert event.lyric == "Și"
        assert event.lyric_syllabic == "single"
        assert event.lyric_verse in (None, 1)

def test_romanian_phoneme_rules_preserve_romanian_diacritics():
    assert romanian.romanian_phonemes("Și") == ["ʃ", "i"]
    assert romanian.romanian_phonemes("țară") == ["t͡s", "a", "r", "ə"]
    assert romanian.romanian_phonemes("cheie") == ["k", "e", "i", "e"]
    assert romanian.romanian_phonemes("gheață") == ["g", "ea", "t͡s", "ə"]


def test_romanian_phone_mapping_reports_fallbacks():
    bank = romanian.DiffSingerBank(
        id="fake",
        name="Fake singer",
        gender="female",
        root=Path("/fake"),
        phonemes=frozenset({"t", "s", "aa", "r", "ah"}),
        dictionary_symbols=frozenset(),
        english_dictionary=True,
        phonemizer=romanian.OPENUTAU_DIFFSINGER_EN_PHONEMIZER,
    )
    mapped, approximations = romanian.map_romanian_phonemes(
        romanian.romanian_phonemes("țară"), bank
    )
    assert mapped == ["t", "s", "aa", "r", "ah"]
    assert approximations == ["t͡s", "r", "ə"]




def _write_romanian_multidict_bank(root: Path, folder: str, gender: str) -> None:
    bank = root / folder
    dsdur = bank / "dsdur"
    dsdur.mkdir(parents=True)
    (bank / "character.txt").write_text(
        f"name={folder.title()} Romanian Singer\ngender={gender}\n",
        encoding="utf-8",
    )
    (bank / "dsconfig.yaml").write_text("phonemes: phonemes.txt\n", encoding="utf-8")
    (dsdur / "dsconfig.yaml").write_text("phonemes: phonemes.txt\n", encoding="utf-8")
    phones = {
        "ro/a", "ro/ax", "ro/ux", "ro/b", "ro/ch", "ro/d", "ro/dg",
        "ro/e", "ro/ea", "ro/eo", "ro/f", "ro/g", "ro/h", "ro/i",
        "ro/j", "ro/k", "ro/l", "ro/m", "ro/n", "ro/ng", "ro/o",
        "ro/oa", "ro/p", "ro/r", "ro/rr", "ro/s", "ro/sh", "ro/t",
        "ro/ts", "ro/u", "ro/v", "ro/w", "ro/y", "ro/z", "SP", "AP",
    }
    (bank / "phonemes.txt").write_text("\n".join(sorted(phones)), encoding="utf-8")
    (dsdur / "phonemes.txt").write_text("\n".join(sorted(phones)), encoding="utf-8")
    symbols = "\n".join(f"  - symbol: {phone}\n    type: vowel" for phone in sorted(phones))
    (dsdur / "dsdict.yaml").write_text("symbols:\n" + symbols + "\n", encoding="utf-8")


def test_romanian_g2p_uses_native_diphthong_tokens_when_available():
    assert romanian.romanian_phonemes("soare") == ["s", "oa", "r", "e"]
    assert romanian.romanian_phonemes("seară") == ["s", "ea", "r", "ə"]
    assert romanian.romanian_phonemes("gheață") == ["g", "ea", "t͡s", "ə"]
    assert romanian.romanian_phonemes("ceață") == ["t͡ʃ", "ea", "t͡s", "ə"]


def test_builds_complete_satb_ustx_with_native_neodiva_romanian_phones(tmp_path, monkeypatch):
    singer_root = tmp_path / "singers"
    singer_root.mkdir()
    _write_romanian_multidict_bank(singer_root, "female-ro", "female")
    _write_romanian_multidict_bank(singer_root, "male-ro", "male")
    monkeypatch.setenv("MIDI_MP4_OPENUTAU_SINGERS", str(singer_root))

    output = tmp_path / "Native-Romanian-SATB.ustx"
    report = romanian.build_romanian_ustx(
        _score(),
        output,
        100.0,
        male_singer_id="male-ro",
        female_singer_id="female-ro",
    )
    project = json.loads(output.read_text(encoding="utf-8"))

    assert [track["singer"] for track in project["tracks"]] == [
        "female-ro", "female-ro", "male-ro", "male-ro"
    ]
    assert all(
        track["phonemizer"] == romanian.OPENUTAU_DIFFSINGER_PHONEMIZER
        for track in project["tracks"]
    )
    assert project["voice_parts"][0]["notes"][0]["lyric"] == "Și[ro/sh ro/i]"
    assert project["voice_parts"][0]["notes"][1]["lyric"] == "cân[ro/k ro/ux ro/n]"
    assert project["voice_parts"][0]["notes"][2]["lyric"] == "tă[ro/t ro/ax]"
    assert set(report["bank_modes"].values()) == {"NeoDIVA Romanian Multi-Dict"}
    assert report["approximations"] == {voice: [] for voice in ["Soprano", "Alto", "Tenor", "Bass"]}

def test_maps_english_diff_singer_language_prefixed_tokens(tmp_path):
    bank = romanian.DiffSingerBank(
        id="prefixed",
        name="Prefixed singer",
        gender="male",
        root=tmp_path,
        phonemes=frozenset({"en/t", "en/s", "en/aa", "en/r", "en/ah"}),
        dictionary_symbols=frozenset({"en/t", "en/s", "en/aa", "en/r", "en/ah"}),
        english_dictionary=True,
        phonemizer=romanian.OPENUTAU_DIFFSINGER_EN_PHONEMIZER,
    )
    mapped, approximations = romanian.map_romanian_phonemes(
        romanian.romanian_phonemes("țară"), bank
    )
    # USTX stores ARPAbet base symbols; OpenUtau resolves them to en/<phone>
    # from the selected singer's phonemes.txt when it validates the hint.
    assert mapped == ["t", "s", "aa", "r", "ah"]
    assert approximations == ["t͡s", "r", "ə"]


def test_maps_native_neodiva_romanian_multidict_phones_without_english_fallback(tmp_path):
    native = {"ro/a", "ro/sh", "ro/i", "ro/ts", "ro/r", "ro/ax"}
    bank = romanian.DiffSingerBank(
        id="neo-ro",
        name="NeoDIVA Romanian",
        gender="female",
        root=tmp_path,
        phonemes=frozenset(native),
        dictionary_symbols=frozenset(native),
        english_dictionary=False,
        phonemizer=romanian.OPENUTAU_DIFFSINGER_PHONEMIZER,
        romanian_multidict=True,
    )

    mapped, approximations = romanian.map_romanian_phonemes(
        romanian.romanian_phonemes("Și țară"), bank
    )
    assert mapped == ["ro/sh", "ro/i", "ro/ts", "ro/a", "ro/r", "ro/ax"]
    assert approximations == []


def test_native_neodiva_mapping_rejects_incomplete_romanian_inventory(tmp_path):
    available = {"ro/a", "ro/sh", "ro/i", "ro/ts", "ro/r"}
    bank = romanian.DiffSingerBank(
        id="neo-ro-incomplete",
        name="Incomplete NeoDIVA Romanian",
        gender="male",
        root=tmp_path,
        phonemes=frozenset(available),
        dictionary_symbols=frozenset(available),
        english_dictionary=False,
        phonemizer=romanian.OPENUTAU_DIFFSINGER_PHONEMIZER,
        romanian_multidict=True,
    )
    with pytest.raises(ValueError, match="ro/ax"):
        romanian.map_romanian_phonemes(romanian.romanian_phonemes("țară"), bank)


def test_discovers_neodiva_multidict_romanian_voicebank(tmp_path, monkeypatch):
    singer_root = tmp_path / "singers"
    bank = singer_root / "NeoDIVA Romanian"
    dsdur = bank / "dsdur"
    dsdur.mkdir(parents=True)
    (bank / "character.txt").write_text(
        "name=NeoDIVA Romanian\ngender=female\n",
        encoding="utf-8",
    )
    (bank / "dsconfig.yaml").write_text("phonemes: phonemes.txt\n", encoding="utf-8")
    (dsdur / "dsconfig.yaml").write_text("phonemes: phonemes.txt\n", encoding="utf-8")
    native = ["ro/a", "ro/ax", "ro/i", "ro/sh", "ro/ts", "ro/r"]
    (bank / "phonemes.txt").write_text("\n".join(native), encoding="utf-8")
    (dsdur / "phonemes.txt").write_text("\n".join(native), encoding="utf-8")
    (dsdur / "dsdict.yaml").write_text(
        "symbols:\n" + "".join(f"  - symbol: {phone}\n    type: vowel\n" for phone in native),
        encoding="utf-8",
    )
    monkeypatch.setenv("MIDI_MP4_OPENUTAU_SINGERS", str(singer_root))

    discovered = romanian.discover_diff_singers()
    assert len(discovered) == 1
    assert discovered[0].romanian_multidict is True
    assert discovered[0].english_dictionary is False
    assert discovered[0].phonemizer == romanian.OPENUTAU_DIFFSINGER_PHONEMIZER

def test_discovers_male_and_female_diff_singers_in_additional_path(tmp_path, monkeypatch):
    singer_root = tmp_path / "extra-singers"
    singer_root.mkdir()
    _write_bank(singer_root, "female", "female")
    _write_bank(singer_root, "male", "male")
    monkeypatch.setenv("MIDI_MP4_OPENUTAU_SINGERS", str(singer_root))

    banks = romanian.discover_diff_singers()
    by_gender = {bank.gender: bank for bank in banks}
    assert set(by_gender) == {"female", "male"}
    assert by_gender["female"].id == "female"
    assert by_gender["male"].id == "male"
    assert by_gender["female"].phonemizer == romanian.OPENUTAU_DIFFSINGER_EN_PHONEMIZER


def test_builds_ustx_with_romanian_lyrics_and_separate_gender_singers(tmp_path, monkeypatch):
    singer_root = tmp_path / "singers"
    singer_root.mkdir()
    _write_bank(singer_root, "female", "female")
    _write_bank(singer_root, "male", "male")
    monkeypatch.setenv("MIDI_MP4_OPENUTAU_SINGERS", str(singer_root))

    output = tmp_path / "Romanian-SATB.ustx"
    report = romanian.build_romanian_ustx(
        _score(),
        output,
        100.0,
        male_singer_id="male",
        female_singer_id="female",
    )
    project = json.loads(output.read_text(encoding="utf-8"))

    assert project["ustx_version"] == romanian.USTX_VERSION
    assert [track["singer"] for track in project["tracks"]] == [
        "female", "female", "male", "male"
    ]
    assert project["tracks"][0]["phonemizer"] == romanian.OPENUTAU_DIFFSINGER_EN_PHONEMIZER
    assert project["tempos"] == [
        {"position": 0, "bpm": 100.0},
        {"position": 1920, "bpm": 120.0},
    ]
    assert project["time_signatures"] == [
        {"bar_position": 0, "beat_per_bar": 4, "beat_unit": 4},
        {"bar_position": 1, "beat_per_bar": 3, "beat_unit": 4},
    ]
    soprano_notes = project["voice_parts"][0]["notes"]
    assert soprano_notes[0]["lyric"].startswith("Și[")
    assert soprano_notes[1]["lyric"].startswith("cân[")
    assert soprano_notes[2]["lyric"].startswith("tă[")
    assert report["lyric_note_count"] == 12
    assert report["banks"]["Soprano"] == "Female Singer"
    assert report["banks"]["Bass"] == "Male Singer"


def test_copies_soprano_lyrics_to_simultaneous_unlabelled_parts(tmp_path, monkeypatch):
    singer_root = tmp_path / "singers"
    singer_root.mkdir()
    _write_bank(singer_root, "female", "female")
    _write_bank(singer_root, "male", "male")
    monkeypatch.setenv("MIDI_MP4_OPENUTAU_SINGERS", str(singer_root))

    output = tmp_path / "homophonic.ustx"
    romanian.build_romanian_ustx(
        _score(lyrics_on_soprano_only=True),
        output,
        100.0,
        male_singer_id="male",
        female_singer_id="female",
    )
    project = json.loads(output.read_text(encoding="utf-8"))
    for part in project["voice_parts"]:
        assert [note["lyric"] for note in part["notes"]] == [
            note["lyric"] for note in project["voice_parts"][0]["notes"]
        ]


def test_rejects_missing_lyrics_instead_of_inventing_them(tmp_path, monkeypatch):
    singer_root = tmp_path / "singers"
    singer_root.mkdir()
    _write_bank(singer_root, "female", "female")
    _write_bank(singer_root, "male", "male")
    monkeypatch.setenv("MIDI_MP4_OPENUTAU_SINGERS", str(singer_root))
    score = _score()
    for event in score.notes:
        event.lyric = None

    with pytest.raises(ValueError, match="nu conține versuri"):
        romanian.build_romanian_ustx(
            score,
            tmp_path / "missing.ustx",
            100.0,
            male_singer_id="male",
            female_singer_id="female",
        )


def test_rejects_missing_selected_gender_bank(tmp_path, monkeypatch):
    singer_root = tmp_path / "singers"
    singer_root.mkdir()
    _write_bank(singer_root, "female", "female")
    monkeypatch.setenv("MIDI_MP4_OPENUTAU_SINGERS", str(singer_root))
    with pytest.raises(ValueError, match="voicebank DiffSinger masculin"):
        romanian.build_romanian_ustx(
            _score(),
            tmp_path / "no-male.ustx",
            100.0,
            male_singer_id="missing-male",
            female_singer_id="female",
        )
