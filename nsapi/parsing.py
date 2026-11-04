"""Parsing and validation of path parameters."""

from __future__ import annotations

import re

_NUM = r"[-+]?\d+(?:\.\d+)?"
_COORDS = re.compile(rf"^\s*({_NUM})\s*[_,]\s*({_NUM})\s*[_,]\s*({_NUM})\s*$")
MNI_LIMIT = 150.0  # mm; generously larger than the MNI bounding box
MAX_TERM = 60  # longest vocabulary term has 31 characters


class InputError(ValueError):
    """Bad user input; carries a hint that the API returns to the client."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


def parse_coords(text: str) -> tuple[float, float, float]:
    """``"0_-52_26"`` -> ``(0.0, -52.0, 26.0)``. Commas are accepted too."""
    m = _COORDS.match(text)
    if not m:
        raise InputError(
            f"Cannot read coordinates {text!r}.",
            "Use x_y_z in MNI millimetres, e.g. 0_-52_26.",
        )
    xyz = tuple(float(v) for v in m.groups())
    if any(abs(v) > MNI_LIMIT for v in xyz):
        raise InputError(
            f"Coordinates {text!r} are outside the brain.",
            f"Each value must be within ±{MNI_LIMIT:g} mm.",
        )
    return xyz  # type: ignore[return-value]


def normalize_term(text: str) -> str:
    """URL form -> vocabulary form: underscores/pluses become spaces, lower case."""
    term = re.sub(r"[_+\s]+", " ", text).strip().lower()
    if not term:
        raise InputError("Empty term.", "Give a Neurosynth term, e.g. amygdala.")
    if len(term) > MAX_TERM:
        raise InputError(f"Term is longer than {MAX_TERM} characters.", "Give a Neurosynth term, e.g. amygdala.")
    return term


def fmt_coords(xyz: tuple[float, float, float]) -> list[float]:
    return [int(v) if float(v).is_integer() else v for v in xyz]
