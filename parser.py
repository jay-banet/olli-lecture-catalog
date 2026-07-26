"""Deterministic splitter for the OLLI-UM lecture catalog PDF (BLC-001).

Design (see Knowledge/wiki/tech/hybrid-deterministic-split-inline-llm-tagging.md):
this module owns everything mechanically stable — pdftotext extraction, page
splitting, edition + section classification, and date-anchored block chunking.
The semantically messy half (field tagging, de-hyphenation, institution-from-bio,
untangling interleaved two-column blocks) is done by the in-session LLM reading
this module's output; no API calls happen here.

Extraction mode: pdftotext DEFAULT (reading-order) mode, NOT -layout.
Default mode is what produced the Jay-verified golden sample
(data/olli-pages-3-8.txt) and it deterministically linearizes the two-column
lecture pages that -layout leaves side-by-side interleaved.

Usage:
    python3 parser.py <pdf-or-txt> -o blocks.json [--dump-text out.txt]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

PDFTOTEXT = "/opt/homebrew/bin/pdftotext"

# ---------------------------------------------------------------------------
# Edition detection
# ---------------------------------------------------------------------------

# Page-furniture marker printed on most content pages, e.g. "OLLI UM | FALL 2023"
EDITION_MARKER_RE = re.compile(
    r"OLLI UM \| (WINTER/SPRING|WINTER\s+SPRING|WINTER|FALL)\s+(20\d\d)"
)
# Cover pages announce the edition, e.g. "FALL\n2024" or "WINTER\nSPRING\n2025"
COVER_SEASON_RE = re.compile(r"\b(WINTER/SPRING|WINTER\s+SPRING|WINTER|FALL)\s+(20\d\d)\b")
COVER_TOKENS = ("OSHER LIFELONG LEARNING INSTITUTE", "WHERE LEARNING NEVER RETIRES")


def canonical_edition(season: str, year: str) -> str:
    season = re.sub(r"\s+", " ", season.strip())
    if season in ("WINTER/SPRING", "WINTER SPRING"):
        name = "Winter/Spring"
    elif season == "WINTER":
        name = "Winter"
    else:
        name = "Fall"
    return f"{name} {year}"


# ---------------------------------------------------------------------------
# Page section classification
# ---------------------------------------------------------------------------

# Sections that carry lecture/series content we extract records from.
LECTURE_SECTIONS = (
    "distinguished",
    "thursday",
    "dei",
    "art-tour",
    "conversations",
    "urgent",         # "URGENT AND CRITICAL LECTURE SERIES" (COVID-era editions)
    "medical-ethics",  # "MEDICAL ETHICS 101" three-lecture series (W/S 2021)
    "torn",           # "TORN FROM THE HEADLINES" pilot series (W/S 2021)
    "summer",         # standalone "SUMMER LECTURE SERIES" page (W/S 2021)
)


def classify_page(text: str) -> str:
    """Classify one physical text page by its (uppercase) furniture strings.

    Order matters: TOC pages mention section names, art-tour pages carry a
    DEI footer, etc. All furniture is uppercase in the source, so matching is
    case-sensitive on purpose (mixed-case prose mentions never collide).

    Older-edition drift (BLC-003): the Fall 2018 catalog prints its section
    headings in mixed case ("Distinguished Lecture Series") as one of the
    first lines of the page, and its TOC pages start with a mixed-case
    "Table of Contents". Mixed-case headings are only honored at the top of
    the page so prose mentions elsewhere never collide. The COVID-era
    editions (Fall 2020, W/S 2021) add uppercase special-series sections.
    The "SUMMER LECTURE SERIES" check sits AFTER the Thursday check because
    Thursday intro pages mention the summer series in their preamble/preview.
    """
    collapsed = " ".join(text.split())
    if any(tok in collapsed for tok in COVER_TOKENS):
        return "cover"
    if "TABLE OF CONTENTS" in collapsed or collapsed.startswith("Table of Contents"):
        return "toc"
    if "ART TOUR" in collapsed:
        return "art-tour"
    if "DEI LECTURE SERIES" in collapsed or "ZEKELMAN" in collapsed:
        return "dei"
    if "CONVERSATIONS" in collapsed:
        return "conversations"
    if "URGENT AND CRITICAL LECTURE SERIES" in collapsed:
        return "urgent"
    if "MEDICAL ETHICS 101" in collapsed:
        return "medical-ethics"
    if "TORN FROM THE HEADLINES" in collapsed:
        return "torn"
    if "DISTINGUISHED LECTURE SERIES" in collapsed:
        return "distinguished"
    if "THURSDAY MORNING LECTURE SERIES" in collapsed or "THURSDAY LECTURE SERIES" in collapsed:
        return "thursday"
    if "SUMMER LECTURE SERIES" in collapsed:
        return "summer"
    head = [l.strip() for l in text.splitlines() if l.strip()][:3]
    if "Distinguished Lecture Series" in head:
        return "distinguished"
    if "Thursday Morning Lecture Series" in head:
        return "thursday"
    return "other"


# ---------------------------------------------------------------------------
# Date-line anchors
# ---------------------------------------------------------------------------

_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)"
)
_WD = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_DASH = r"[-–—]"
_TIME = (
    rf"\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?\s*{_DASH}\s*"
    rf"\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?"
)
_ORD = r"(?:st|nd|rd|th)?"
_TRAIL = r"(?:\s*\*|\s*\([^)]*\))?"

# Single-day line: "Tuesday, February 14, 10:00 - 11:30am", "January 11th, 2024,
# 10:00 – 11:30am", "Thursday, January 11th, 2024", "September 11, 11 am–12:30 pm
# (NEW TIME)". Requires at least one of weekday/year/time so bare prose like
# "May 2020. He joined..." never anchors.
SINGLE_DATE_RE = re.compile(
    rf"(?P<wd>{_WD},?\s+)?{_MONTH},?\s+\d{{1,2}}{_ORD}"
    rf"(?P<year>,?\s*20\d\d)?"
    rf"(?P<time>[,|]?\s*{_TIME})?"
    rf"{_TRAIL}"
)
# "January 12 - February 16, 2023" / "January 30 – February 27" (year optional)
# / "January 14, 2021 – February 18, 2021" (COVID-era editions print a year on
# BOTH sides — BLC-003)
DAY_RANGE_RE = re.compile(
    rf"{_MONTH}\s+\d{{1,2}}{_ORD}(?:,?\s*20\d\d)?\s*{_DASH}\s*"
    rf"{_MONTH}\s+\d{{1,2}}{_ORD}(?:,?\s*20\d\d)?"
)
# "October - November 2024"
MONTH_RANGE_RE = re.compile(rf"{_MONTH}\s*{_DASH}\s*{_MONTH}\s+20\d\d")
# "June 2023" / "September 2023- June 2024"
MONTH_YEAR_RE = re.compile(rf"{_MONTH}\s+20\d\d(?:\s*{_DASH}\s*{_MONTH}\s+20\d\d)?")
# "2/22/24-4/6/24"
SLASH_RANGE_RE = re.compile(
    rf"\(?\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?\s*{_DASH}\s*\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?\)?"
)


def anchor_kind(line: str) -> str | None:
    """Return the anchor type of a (stripped) line, or None."""
    line = line.strip()
    if not line:
        return None
    m = SINGLE_DATE_RE.fullmatch(line)
    if m and (m.group("wd") or m.group("year") or m.group("time")):
        return "single"
    if DAY_RANGE_RE.fullmatch(line):
        return "day-range"
    if MONTH_RANGE_RE.fullmatch(line):
        return "month-range"
    if MONTH_YEAR_RE.fullmatch(line):
        return "month-year"
    if SLASH_RANGE_RE.fullmatch(line):
        return "slash-range"
    return None


def is_anchor(line: str) -> bool:
    return anchor_kind(line) is not None


# ---------------------------------------------------------------------------
# Furniture stripping
# ---------------------------------------------------------------------------

_FURNITURE_RES = [
    re.compile(r"\d{1,3}"),                      # bare page numbers
    re.compile(r"OLLI UM \|.*"),                 # edition marker lines
    re.compile(r"(ALFRED GOURDJI )?DISTINGUISHED LECTURE SERIES"),
    re.compile(r"THURSDAY( MORNING)? LECTURE SERIES(:.*)?"),
    re.compile(r"DEI LECTURE SERIES"),
    re.compile(r"CONVERSATIONS"),
    re.compile(r"THURSDAY MORNING"),
    re.compile(r"THURSDAY"),
    re.compile(r"DISTINGUISHED"),
    re.compile(r"LECTURE SERIES:?"),
    re.compile(r"Alfred Gourdji"),
    # Older-edition furniture (BLC-003)
    re.compile(r"Distinguished Lecture Series"),        # Fall 2018 mixed-case headers
    re.compile(r"Thursday Morning Lecture Series"),
    re.compile(r"URGENT AND CRITICAL LECTURE SERIES"),  # COVID-era running header
    re.compile(r"OLLI-UM"),                             # Fall 2020 / W/S 2021 page footer
    re.compile(r"Fall Catalog"),                        # Fall 2020 page footer
    re.compile(r"Winter/Spring Catalog 20\d\d"),        # W/S 2021 page footer
]


def is_furniture(line: str) -> bool:
    line = line.strip()
    return any(rx.fullmatch(line) for rx in _FURNITURE_RES)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def extract_text(pdf_path: Path) -> str:
    """Run pdftotext in default (reading-order) mode, returning the full text."""
    out = subprocess.run(
        [PDFTOTEXT, str(pdf_path), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def split_pages(text: str) -> list[str]:
    """Split extracted text on form feeds. Trailing image-only pages are empty."""
    return text.split("\f")


def assign_editions(
    pages: list[str], edition_override: str | None = None
) -> list[str | None]:
    """Assign an edition name to every page.

    A page with an "OLLI UM | <EDITION>" marker or a cover page sets the
    current edition; unmarked pages carry the previous edition forward.
    (Unmarked gap pages are covers/TOCs which never contribute records, so
    carry-forward slop there is harmless — asserted by tests on the
    lecture-bearing pages.)

    `edition_override` forces every page to one edition. Standalone
    single-edition catalogs need this: the Winter 2026 PDF brands its cover
    "WINTER 2026" but stamps page furniture "OLLI UM | WINTER/SPRING 2026",
    so marker-based detection would mislabel it (BLC-002).
    """
    if edition_override is not None:
        return [edition_override] * len(pages)
    editions: list[str | None] = []
    current: str | None = None
    for page in pages:
        collapsed = " ".join(page.split())
        m = EDITION_MARKER_RE.search(collapsed)
        if m:
            current = canonical_edition(m.group(1), m.group(2))
        elif any(tok in collapsed for tok in COVER_TOKENS):
            cm = COVER_SEASON_RE.search(collapsed)
            if cm:
                current = canonical_edition(cm.group(1), cm.group(2))
        editions.append(current)
    return editions


def clean_lines(page: str) -> list[str]:
    """Page text as lines with furniture removed (blank lines kept)."""
    out = []
    for line in page.splitlines():
        if is_furniture(line):
            continue
        out.append(line.rstrip())
    return out


def split_blocks(lines: list[str]) -> tuple[list[str], list[dict]]:
    """Split a section run's lines into (preamble_lines, blocks).

    A block starts at the title of a date-anchored lecture/series entry: for
    each anchor line, walk back over the contiguous non-blank, non-anchor lines
    above it (the title). Blocks partition the input exactly — nothing is
    dropped, so the LLM tagging pass can untangle any column-interleave noise.
    """
    anchor_idx = [i for i, l in enumerate(lines) if is_anchor(l)]
    cuts: list[int] = []
    for i in anchor_idx:
        j = i
        while j - 1 >= 0 and lines[j - 1].strip() and not is_anchor(lines[j - 1]):
            j -= 1
        if cuts and j <= cuts[-1]:
            j = max(cuts[-1] + 1, min(i, cuts[-1] + 1))
            j = min(j, i)
            if j <= cuts[-1]:
                continue  # two anchors in one contiguous run; keep in same block
        cuts.append(j)
    preamble = lines[: cuts[0]] if cuts else lines[:]
    blocks = []
    for k, start in enumerate(cuts):
        end = cuts[k + 1] if k + 1 < len(cuts) else len(lines)
        chunk = lines[start:end]
        anchor_line = next((l.strip() for l in chunk if is_anchor(l)), None)
        blocks.append(
            {
                "anchor": anchor_line,
                "anchor_kind": anchor_kind(anchor_line) if anchor_line else None,
                "text": "\n".join(chunk).strip("\n"),
            }
        )
    return preamble, blocks


def build_runs(pages: list[str], editions: list[str | None]) -> list[dict]:
    """Group consecutive lecture-section pages into runs and block-split them."""
    runs: list[dict] = []
    current: dict | None = None
    for idx, page in enumerate(pages):
        section = classify_page(page)
        if section not in LECTURE_SECTIONS:
            current = None
            continue
        edition = editions[idx]
        if current and current["section"] == section and current["edition"] == edition:
            current["pages"].append(idx)
            current["_lines"].extend([""] + clean_lines(page))
        else:
            current = {
                "edition": edition,
                "section": section,
                "pages": [idx],
                "_lines": clean_lines(page),
            }
            runs.append(current)
    for run in runs:
        preamble, blocks = split_blocks(run.pop("_lines"))
        run["preamble"] = "\n".join(preamble).strip("\n")
        run["blocks"] = blocks
    return runs


def parse_text(text: str, edition_override: str | None = None) -> dict:
    pages = split_pages(text)
    editions = assign_editions(pages, edition_override)
    runs = build_runs(pages, editions)
    summary: dict[str, dict[str, int]] = {}
    for run in runs:
        ed = summary.setdefault(run["edition"], {})
        ed[run["section"]] = ed.get(run["section"], 0) + len(run["blocks"])
    return {
        "n_pages": len(pages),
        "n_nonempty_pages": sum(1 for p in pages if p.strip()),
        "editions_seen": sorted({e for e in editions if e}),
        "block_counts": summary,
        "runs": runs,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="PDF file or previously extracted .txt file")
    ap.add_argument("-o", "--output", help="write blocks JSON here")
    ap.add_argument("--dump-text", help="also write the raw extracted text here")
    ap.add_argument(
        "--edition",
        help='force every page to this edition (standalone catalogs, e.g. "Winter 2026")',
    )
    args = ap.parse_args()

    src = Path(args.source)
    if src.suffix.lower() == ".pdf":
        text = extract_text(src)
    else:
        text = src.read_text()
    if args.dump_text:
        Path(args.dump_text).write_text(text)

    result = parse_text(text, edition_override=args.edition)
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(payload)
    else:
        print(payload)
    counts = {
        ed: sum(v.values()) for ed, v in sorted(result["block_counts"].items())
    }
    print(
        f"pages={result['n_pages']} nonempty={result['n_nonempty_pages']} "
        f"runs={len(result['runs'])} blocks_per_edition={counts}",
        file=__import__("sys").stderr,
    )


if __name__ == "__main__":
    main()
