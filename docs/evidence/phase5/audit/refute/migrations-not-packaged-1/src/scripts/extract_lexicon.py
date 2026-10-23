#!/usr/bin/env python3
"""Extract the BLS ATUS multi-year activity coding lexicon PDF into CSV.

This is a one-time generation script: its committed output
(``data/reference/activity_lexicon_<release>.csv``) is the reference data the
pipeline loads into ``atus.activity_tier1`` / ``atus.activity_tier2`` /
``atus.activity_codes``. Re-run it only when BLS publishes a new multi-year
lexicon, then review the diff.

The lexicon PDF (e.g. https://www.bls.gov/tus/lexicons/lexiconnoex0325.pdf)
prints each hierarchy level's code at a fixed x-position: tier-1 codes at the
far left, tier-2 codes indented, 6-digit codes further right, activity names
after the code, and harmonization notes in a far-right column. The parser
anchors on those fixed code columns and treats everything between two code
tokens as the name of the entry opened by the preceding code, so names that
visually overflow their column are still captured whole.

Usage:
    python scripts/extract_lexicon.py data/docs/lexiconnoex0325.pdf \
        data/reference/activity_lexicon_0325.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber

# Anchor x-positions (points) of the three code columns, measured from the
# 2003-25 lexicon. A future layout change makes validation fail loudly rather
# than silently misparse. The notes column has no fixed x: a note is separated
# from the activity name by a large horizontal gap (NOTE_GAP) and its wrapped
# continuation lines start far right of any name text (NOTE_CONTINUATION_X).
TIER1_X = 62.8
TIER2_X = 125.2
CODE_X = 203.6
NOTE_GAP = 40.0
NOTE_CONTINUATION_X = 600.0
X_TOLERANCE = 12.0
LINE_TOLERANCE = 5.0

RE_TIER1 = re.compile(r"^\d{2}$")
RE_TIER2 = re.compile(r"^\d{4}$")
RE_CODE = re.compile(r"^\d{6}$")

HEADER_TOKENS = {"6-digit", "Notes"}
TITLE_SNIPPET = "coding lexicon"


def group_lines(words: list[dict]) -> list[list[dict]]:
    """Group words into visual lines by vertical position, left-to-right."""
    lines: dict[float, list[dict]] = defaultdict(list)
    anchors: list[float] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        top = word["top"]
        anchor = next((a for a in anchors if abs(a - top) <= LINE_TOLERANCE), None)
        if anchor is None:
            anchors.append(top)
            anchor = top
        lines[anchor].append(word)
    return [sorted(lines[a], key=lambda w: w["x0"]) for a in sorted(lines)]


def classify_code_token(word: dict) -> str | None:
    """Return 'tier1'/'tier2'/'code' if the word is a code in its anchored column."""
    text = word["text"]
    x0 = word["x0"]
    if RE_TIER1.match(text) and abs(x0 - TIER1_X) <= X_TOLERANCE:
        return "tier1"
    if RE_TIER2.match(text) and abs(x0 - TIER2_X) <= X_TOLERANCE:
        return "tier2"
    if RE_CODE.match(text) and abs(x0 - CODE_X) <= X_TOLERANCE:
        return "code"
    return None


class LexiconParser:
    def __init__(self) -> None:
        self.tier1: dict[str, str] = {}
        self.tier2: dict[str, str] = {}
        self.codes: dict[str, dict[str, str]] = {}
        # (kind, code) of the most recently opened entry, for wrapped lines
        self._open: tuple[str, str] | None = None

    def feed_page(self, page) -> None:
        words = page.extract_words()
        if not any(w["text"] == "6-digit" for w in words):
            return  # cover page or other non-table page
        for line in group_lines(words):
            self._feed_line(line)

    # ------------------------------------------------------------------ #

    def _feed_line(self, line: list[dict]) -> None:
        if self._is_noise_line(line):
            return
        segments = self._segment(line)
        if segments:
            for kind, code, words in segments:
                name_words, note_words = self._split_name_and_note(kind, words)
                self._open_entry(kind, code, " ".join(w["text"] for w in name_words))
                if note_words:
                    self._append_note(" ".join(w["text"] for w in note_words))
        elif line and self._open is not None:
            # wrapped continuation: far-right text continues a note, the rest a name
            if line[0]["x0"] >= NOTE_CONTINUATION_X:
                self._append_note(" ".join(w["text"] for w in line))
            else:
                self._append_name(" ".join(w["text"] for w in line))

    def _segment(self, line: list[dict]) -> list[tuple[str, str, list[dict]]]:
        """Split a line at anchored code tokens: [(kind, code, words), ...]."""
        segments: list[tuple[str, str, list[dict]]] = []
        current: tuple[str, str, list[dict]] | None = None
        for word in line:
            kind = classify_code_token(word)
            if kind is not None:
                if current is not None:
                    segments.append(current)
                current = (kind, word["text"], [])
            elif current is not None:
                current[2].append(word)
        if current is not None:
            segments.append(current)
        return segments

    @staticmethod
    def _split_name_and_note(kind: str, words: list[dict]) -> tuple[list[dict], list[dict]]:
        """For 6-digit rows, a harmonization note follows the name after a
        large horizontal gap; tier rows never carry notes."""
        if kind != "code":
            return words, []
        for i in range(1, len(words)):
            if words[i]["x0"] - words[i - 1]["x1"] > NOTE_GAP:
                return words[:i], words[i:]
        return words, []

    def _open_entry(self, kind: str, code: str, name: str) -> None:
        if kind == "tier1":
            self.tier1[code] = name
        elif kind == "tier2":
            self.tier2[code] = name
        else:
            self.codes[code] = {"name": name, "note": ""}
        self._open = (kind, code)

    def _append_name(self, text: str) -> None:
        kind, code = self._open
        if kind == "tier1":
            self.tier1[code] = f"{self.tier1[code]} {text}".strip()
        elif kind == "tier2":
            self.tier2[code] = f"{self.tier2[code]} {text}".strip()
        else:
            self.codes[code]["name"] = f"{self.codes[code]['name']} {text}".strip()

    def _append_note(self, text: str) -> None:
        if self._open is None or self._open[0] != "code":
            return
        code = self._open[1]
        self.codes[code]["note"] = f"{self.codes[code]['note']} {text}".strip()

    @staticmethod
    def _is_noise_line(line: list[dict]) -> bool:
        text = " ".join(w["text"] for w in line)
        if TITLE_SNIPPET in text.lower():
            return True
        if any(w["text"] in HEADER_TOKENS for w in line):
            return True
        # column header fragments and page numbers
        if re.fullmatch(r"(Major|category|categories|First and second-tier|"
                        r"activity|code|Activity|Notes|\d{1,2})( .*)?", text) and len(line) <= 4:
            stripped = {w["text"] for w in line}
            header_words = {"Major", "category", "categories", "First", "and", "second-tier",
                            "activity", "code", "Activity", "Notes"}
            if stripped <= header_words or (len(line) == 1 and line[0]["text"].isdigit()):
                return True
        return False

    # ------------------------------------------------------------------ #

    def validate(self) -> list[str]:
        problems = []
        if not (10 <= len(self.tier1) <= 30):
            problems.append(f"implausible tier-1 count: {len(self.tier1)}")
        if not (80 <= len(self.tier2) <= 200):
            problems.append(f"implausible tier-2 count: {len(self.tier2)}")
        if not (300 <= len(self.codes) <= 600):
            problems.append(f"implausible 6-digit code count: {len(self.codes)}")
        for code in self.codes:
            if code[:4] not in self.tier2:
                problems.append(f"code {code}: tier2 {code[:4]} missing")
            if code[:2] not in self.tier1:
                problems.append(f"code {code}: tier1 {code[:2]} missing")
        for tier2 in self.tier2:
            if tier2[:2] not in self.tier1:
                problems.append(f"tier2 {tier2}: tier1 {tier2[:2]} missing")
        for code, name in {**self.tier1, **self.tier2}.items():
            if not name:
                problems.append(f"tier code {code}: empty name")
        for code, info in self.codes.items():
            if not info["name"]:
                problems.append(f"code {code}: empty name")
        return problems


def main() -> int:
    argp = argparse.ArgumentParser(description=__doc__)
    argp.add_argument("pdf", type=Path, help="path to the BLS lexicon PDF")
    argp.add_argument("output", type=Path, help="output CSV path")
    args = argp.parse_args()

    parser = LexiconParser()
    with pdfplumber.open(args.pdf) as pdf:
        for page in pdf.pages:
            parser.feed_page(page)

    problems = parser.validate()
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["level", "code", "name", "harmonization_note"])
        for code, name in sorted(parser.tier1.items()):
            writer.writerow(["1", code, name, ""])
        for code, name in sorted(parser.tier2.items()):
            writer.writerow(["2", code, name, ""])
        for code, info in sorted(parser.codes.items()):
            writer.writerow(["3", code, info["name"], info["note"]])

    print(
        f"Wrote {args.output}: {len(parser.tier1)} tier-1, "
        f"{len(parser.tier2)} tier-2, {len(parser.codes)} 6-digit codes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
