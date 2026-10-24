from app.conductor import analyze_score, measure_ranges, validate_annotations
from app.core import NoteEvent, ScoreData, TempoEvent


def score_with_measures():
    voices = ["Soprano", "Alto", "Tenor", "Bass"]
    notes = [
        NoteEvent("Soprano", "Soprano", 50, 0.0, 2.0, measure=1),
        NoteEvent("Soprano", "Soprano", 72, 2.0, 2.0, measure=1),
        NoteEvent("Alto", "Alto", 60, 0.0, 4.0, measure=1),
        NoteEvent("Tenor", "Tenor", 55, 0.0, 4.0, measure=1),
        NoteEvent("Bass", "Bass", 65, 0.0, 4.0, measure=1),
        NoteEvent("Soprano", "Soprano", 74, 4.0, 4.0, measure=2),
        NoteEvent("Alto", "Alto", 64, 4.0, 4.0, measure=2),
        NoteEvent("Tenor", "Tenor", 53, 4.0, 4.0, measure=2),
        NoteEvent("Bass", "Bass", 48, 4.0, 4.0, measure=2),
    ]
    return ScoreData(
        "Conductor tools fixture",
        {voice: voice for voice in voices},
        notes,
        [TempoEvent(0, 100)],
        8.0,
        "",
        voices,
    )


def test_measure_ranges_aggregate_across_all_parts():
    ranges = measure_ranges(score_with_measures())
    assert [item["number"] for item in ranges] == ["1", "2"]
    assert ranges[0]["start_q"] == 0.0
    assert ranges[0]["end_q"] == 4.0
    assert ranges[1]["start_q"] == 4.0
    assert ranges[1]["end_q"] == 8.0


def test_preflight_flags_out_of_range_notes_and_large_leaps():
    analysis = analyze_score(score_with_measures())
    codes = {issue["code"] for issue in analysis["issues"]}
    assert "outside-practical-range" in codes
    assert "large-leap" in codes
    assert analysis["summary"]["warning"] > 0
    assert analysis["measure_count"] == 2
    assert "nu se modifică automat" in analysis["disclaimer"].lower()


def test_voice_crossings_are_review_hints_not_errors():
    analysis = analyze_score(score_with_measures())
    crossing = [i for i in analysis["issues"] if i["code"] == "voice-crossing"]
    assert crossing
    assert all(item["severity"] == "info" for item in crossing)


def test_validate_measure_annotation_and_reject_unknown_measure():
    score = score_with_measures()
    saved = validate_annotations([{
        "id": "ann-1", "measure": "1", "voice": "Soprano",
        "category": "respiration", "status": "ready", "text": "Respirație înainte de intrare",
    }], score)
    assert saved[0]["id"] == "ann-1"
    assert saved[0]["measure"] == "1"
    assert saved[0]["status"] == "ready"
    try:
        validate_annotations([{
            "measure": "99", "voice": "all", "category": "general", "text": "Verifică",
        }], score)
    except ValueError as exc:
        assert "nu există" in str(exc)
    else:
        raise AssertionError("Unknown measures must be rejected")
