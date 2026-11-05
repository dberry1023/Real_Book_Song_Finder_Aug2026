"""Saved chord charts and instrument transposition.

Chords are generated once per song by generate_chords.py and saved in concert
pitch in data/chords.json. The web server only reads that file. It never calls
an AI model, and changing instrument is plain arithmetic here, so a lookup costs
nothing no matter how many people use it.
"""
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("chords")

# id -> (label, semitones added to concert pitch to get the written chord)
INSTRUMENTS: dict[str, tuple[str, int]] = {
    "concert": ("C instruments (piano, guitar, flute, trombone, vibes)", 0),
    "bb": ("Bb instruments (trumpet, tenor sax, soprano sax, clarinet)", 2),
    "eb": ("Eb instruments (alto sax, baritone sax)", 9),
    "f": ("F instruments (French horn)", 7),
    "bass": ("Bass (bass clef, concert pitch)", 0),
}

_PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
_MAJOR_KEYS = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_MINOR_KEYS = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B"]
_SHARP_KEYS = {"G", "D", "A", "E", "B", "F#", "Em", "Bm", "F#m", "C#m", "G#m"}

_CHORD = re.compile(
    r"^(?P<root>[A-G][#b]?)"
    r"(?P<quality>[^\s/]*(?:/\d+)?[^\s/]*)"
    r"(?:/(?P<bass>[A-G][#b]?))?$"
)
_MARKERS = {"%", "N.C.", "NC", "-"}


def _pitch(note: str) -> int:
    value = _PITCH[note[0]]
    if note.endswith("#"):
        value += 1
    elif note.endswith("b"):
        value -= 1
    return value % 12


def transpose_key(key: str, semitones: int) -> str:
    """Transpose a key name such as "Bb" or "F#m".

    Args:
        key: Key name, with a trailing "m" for minor, or "unknown".
        semitones: Semitones to move up.

    Returns:
        The new key name, or the input unchanged if it is not a known key.
    """
    minor = key.endswith("m")
    root = key[:-1] if minor else key
    if not re.fullmatch(r"[A-G][#b]?", root):
        return key
    pitch = (_pitch(root) + semitones) % 12
    return _MINOR_KEYS[pitch] + "m" if minor else _MAJOR_KEYS[pitch]


def _spell(note: str, semitones: int, sharps: bool) -> str:
    names = _SHARP_NAMES if sharps else _FLAT_NAMES
    return names[(_pitch(note) + semitones) % 12]


def is_chord_token(token: str) -> bool:
    """Return True if a token is a chord symbol or a bar marker such as %."""
    return token in _MARKERS or _CHORD.match(token) is not None


def transpose_chord(chord: str, semitones: int, target_key: str = "") -> str:
    """Transpose one chord symbol, leaving its quality untouched.

    Args:
        chord: Symbol such as "Bbmaj7", "F#m7b5", "C6/9" or "Dm7/G".
        semitones: Semitones to move up.
        target_key: Key being written in, which decides sharps or flats.

    Returns:
        The transposed symbol. Anything that is not a chord is returned as is.
    """
    match = _CHORD.match(chord)
    if semitones % 12 == 0 or match is None:
        return chord
    sharps = target_key in _SHARP_KEYS
    out = _spell(match["root"], semitones, sharps) + match["quality"]
    if match["bass"]:
        out += "/" + _spell(match["bass"], semitones, sharps)
    return out


def transpose_bar(bar: str, semitones: int, target_key: str = "") -> str:
    """Transpose every chord in one bar, such as "Dm7 G7"."""
    return " ".join(
        transpose_chord(token, semitones, target_key) for token in bar.split()
    )


def transpose_chart(entry: dict, instrument: str) -> dict:
    """Return a saved chart written for an instrument.

    Args:
        entry: One saved song from data/chords.json, in concert pitch.
        instrument: A key of INSTRUMENTS.

    Returns:
        A new dict with the written key and transposed sections.

    Raises:
        KeyError: If the instrument is not in INSTRUMENTS.
    """
    label, semitones = INSTRUMENTS[instrument]
    key = transpose_key(entry.get("key", "unknown"), semitones)
    return {
        "instrument": label,
        "key": key,
        "concert_key": entry.get("key", "unknown"),
        "form": entry.get("form", ""),
        "sections": [
            {
                "name": section["name"],
                "bars": [transpose_bar(b, semitones, key) for b in section["bars"]],
            }
            for section in entry["sections"]
        ],
    }


def validate_sections(sections: object) -> str | None:
    """Check a chart read from the model or from a file.

    Returns:
        None if the chart is usable, otherwise the reason it is not.
    """
    if not isinstance(sections, list) or not sections:
        return "no sections"
    for section in sections:
        if not isinstance(section, dict) or not section.get("bars"):
            return "section has no bars"
        for bar in section["bars"]:
            if not isinstance(bar, str) or not bar.strip():
                return "empty bar"
            for token in bar.split():
                if not is_chord_token(token):
                    return f"not a chord: {token}"
    return None


def is_servable(entry: dict | None, reviewed_only: bool = False) -> bool:
    """Decide whether a saved chart may be shown to users.

    A rejected chart is never shown. An approved chart always is. Anything else
    is shown only if the app is not limited to reviewed charts and the model
    rated its own answer high confidence. In the November 2026 test the one
    medium-confidence chart was wrong, so medium is not enough on its own.
    """
    if not entry or entry.get("review") == "rejected":
        return False
    if entry.get("review") == "approved":
        return True
    if reviewed_only:
        return False
    return entry.get("confidence") == "high"


def load_store(path: Path) -> dict[str, dict]:
    """Load saved charts, keeping only those that pass validation."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8")).get("songs", {})
    good: dict[str, dict] = {}
    for song_id, entry in raw.items():
        problem = validate_sections(entry.get("sections"))
        if problem:
            logger.warning("skipping saved chords for %s: %s", song_id, problem)
            continue
        good[song_id] = entry
    return good


def save_store(path: Path, songs: dict[str, dict]) -> None:
    """Write saved charts, replacing the file only once the write is complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"songs": songs}, indent=1, sort_keys=True), encoding="utf-8"
    )
    os.replace(tmp, path)
