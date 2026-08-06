"""Email-announcement parser mode for OLLI lecture reminder emails (BLC-012).

A NEW source type, separate from the semester-catalog splitter in parser.py:
Bernie's Dropbox PDFs concatenate years of OLLI announcement emails (AGDLS =
Tuesday Distinguished, plus the Thursday Morning series), one email per page
run, including attachment furniture (campus maps, slide decks, committee
packets). This module owns the mechanically stable half per the project's
hybrid design (wiki tech/hybrid-deterministic-split-inline-llm-tagging.md):

  1. email-boundary split — a form-feed page whose first line starts with
     "Subject:" begins a new email; following pages (attachments) belong to it
  2. header parse — subject (with wrap continuation), Date Sent
  3. body isolation — everything up to the standard OLLI signature boilerplate
     ("Electronic Mail is not secure..."); attachment pages fall after it
  4. era classification + per-era field extraction:
       template  (2017-2021)  "Lecture Title:/Lecture Series:/When:" fields,
                              no description or bio printed
       rich2022  (2022)       bare title heading + date/venue line, prose
                              description + bio; speaker named only in the bio
       rich      (2023-2026)  branded series heading + date line + "Speaker:"
                              line + description + bio paragraphs
  5. cross-send dedup — the same lecture is announced 2-3x (*Upcoming* /
     *Tomorrow* / TODAY / RE variants); collapse on (date, fuzzy title),
     keeping the richest body
  6. year inference/correction — template "When:" lines print no year (taken
     from Date Sent); rich date lines occasionally misprint the year (e.g. a
     Feb 2026 send announcing "February 10, 2025"), corrected against Date
     Sent and recorded in notes, never silently

The semantically messy leftovers (series-vs-title colon splits with no known
series match, speakers for rich2022 emails whose bio heuristic fails) are
flagged in `needs_review` and resolved by the in-session LLM pass via the
committed curation table data/announcement-curation.json, applied in
merge_announcements.py — this module stays deterministic and pytest-able.

Usage:
    python3 announcements.py data/olli-agdls-announcements.txt -o out.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Email splitting + headers
# ---------------------------------------------------------------------------

_HEADER_NEXT_RE = re.compile(r"^(From|To|Cc|Bcc|Date Sent|Date Received|Attachments):")
_DATE_SENT_RE = re.compile(
    r"^Date Sent:\s*[A-Za-z]+,\s+([A-Za-z]+)\s+(\d{1,2}),\s+(20\d\d)"
)

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "January February March April May June July August September "
        "October November December".split()
    )
}


def split_emails(text: str) -> tuple[list[dict], int]:
    """Split raw extracted text into emails.

    An email starts at a form-feed page whose first non-blank line starts with
    "Subject:". Every following page until the next such page (attachment
    furniture included) belongs to that email. Returns (emails, n_prelude_pages)
    where prelude pages are attachment pages appearing before the first email.
    """
    pages = text.split("\f")
    emails: list[dict] = []
    prelude = 0
    current: dict | None = None
    for page in pages:
        first = next((l for l in page.splitlines() if l.strip()), "")
        if first.strip().startswith("Subject:"):
            current = {"pages": [page]}
            emails.append(current)
        elif current is not None:
            current["pages"].append(page)
        elif page.strip():
            prelude += 1
    return emails, prelude


def parse_headers(email: dict) -> None:
    """Fill subject / date_sent on the email dict (in place).

    The Subject line wraps: continuation lines run until the next header key
    (From:/To:/Date Sent:...).
    """
    # the To: recipient list can wrap for pages, pushing Date Sent off the
    # first page — scan the whole email
    lines = "\n".join(email["pages"]).splitlines()
    subject_parts: list[str] = []
    in_subject = False
    date_sent: date | None = None
    for line in lines:
        s = line.strip()
        if s.startswith("Subject:"):
            in_subject = True
            subject_parts.append(s[len("Subject:") :].strip())
            continue
        if in_subject:
            if _HEADER_NEXT_RE.match(s) or not s:
                in_subject = False
            else:
                subject_parts.append(s)
                continue
        m = _DATE_SENT_RE.match(s)
        if m and _MONTHS.get(m.group(1)):
            date_sent = date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
    email["subject"] = " ".join(subject_parts)
    email["date_sent"] = date_sent


_BODY_END_MARKER = "Electronic Mail is not secure"
_PAGE_NUM_RE = re.compile(r"\d{1,3}\s*/\s*\d{1,3}")  # "1/2", "2 / 10" page marks


def email_body(email: dict) -> str:
    """Email body text: header page + following pages, with the header block
    (Subject/From/.../Attachments, including wrapped continuation lines)
    removed, zero-width characters stripped, and everything truncated at the
    OLLI signature boilerplate. Attachment pages (maps, slide decks) fall
    after the boilerplate, so they drop out; emails without the marker keep
    everything (furniture stripping + era parsing ignore what's left)."""
    joined = "\n".join(email["pages"])
    joined = re.sub("[\ufeff\u200b\u200c\u200d]", "", joined)
    idx = joined.find(_BODY_END_MARKER)
    if idx != -1:
        joined = joined[:idx]
        # drop the "****...***" rule line right above the marker
        joined = re.sub(r"\*{10,}\s*$", "", joined)
    lines = joined.splitlines()
    last_hdr = -1
    for i, line in enumerate(lines[:40]):
        s = line.strip()
        if s.startswith("Subject:") or _HEADER_NEXT_RE.match(s):
            last_hdr = i
    if last_hdr >= 0:
        j = last_hdr + 1
        while j < len(lines) and lines[j].strip():
            j += 1  # continuation lines of the last header (wrapped values)
        lines = lines[j:]
    return "\n".join(lines)


def _join_lines(parts: list[str]) -> str:
    """Join hard-wrapped lines, re-attaching pdftotext's mid-word hyphen
    breaks ("State-" + "Socialism" -> "State-Socialism")."""
    out = ""
    for p in parts:
        p = p.strip()
        if not out:
            out = p
        elif out.endswith("-"):
            out += p
        else:
            out += " " + p
    return out


# ---------------------------------------------------------------------------
# Era classification
# ---------------------------------------------------------------------------

_TEMPLATE_TITLE_RE = re.compile(r"^Lecture [Tt]itle:", re.MULTILINE)
_SPEAKER_LINE_RE = re.compile(r"^(Speaker\(s\)|Speakers?|Facilitators?):\s*(.*)$")


def classify_era(body: str) -> str | None:
    if _TEMPLATE_TITLE_RE.search(body):
        return "template"
    has_speaker = any(_SPEAKER_LINE_RE.match(l.strip()) for l in body.splitlines())
    heading, datem = _find_heading_and_date(body)
    if datem and heading:
        return "rich" if has_speaker else "rich2022"
    return None


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_MONTH_RX = "|".join(_MONTHS)
_DASH = r"[-–—]"
_TIME_RX = (
    rf"\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm|AM|PM|a\.m\.|p\.m\.)?\s*{_DASH}\s*"
    rf"\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm|AM|PM|a\.m\.|p\.m\.)?"
)
# Rich-era date lines:
#   "Tuesday, February 10, 2025, from 10:00 - 11:30am"
#   "April 9, 2026, from 10:00 - 11:30am"
#   "Thursday, January 13th, 10:00am – 11:30pm"          (no year)
#   "Thursday, December 14th, 2023 from 2:00 - 3:30pm"
#   "October 11, 2022, 10:00 - 11:30 AM WCC-Morris Lawrence Building ..."
RICH_DATE_RE = re.compile(
    rf"^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    rf"(?P<month>{_MONTH_RX}),?\s+(?:\d{{1,2}}/)?(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?"
    rf"(?:\s*(?P<year>20\d\d))?,?\s*(?:from\s+)?(?P<time>{_TIME_RX})"
)
# "Rescheduled to 1/22/26 from 10/30/25" (time on the following line)
RESCHED_RE = re.compile(
    r"^\(?Rescheduled to (?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{2,4})"
    r"(?:\s+from\s+(?P<from>\d{1,2}/\d{1,2}/\d{2,4}))?\)?$"
)
# Date-only line ("Tuesday, October 10, 2023") with the time on the next line
# ("10:00 - 11:30am") — the 2023 layout. Requires weekday or year so prose
# date mentions never anchor. The month comma tolerates the "March, 21 2023"
# typo observed in the wild.
RICH_DATE_ONLY_RE = re.compile(
    rf"^(?P<wd>(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    rf"(?P<month>{_MONTH_RX}),?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s*(?P<year>20\d\d))?\.?$"
)
TIME_ONLY_RE = re.compile(rf"^(?:from\s+)?(?P<time>{_TIME_RX})\.?$")
# Template-era "When:" lines:
#   "When: 10:00-11:30am, October 10."
#   "When: 10:00-11:30am, Thursday, May 30."
WHEN_RE = re.compile(
    rf"^When:\s*(?P<time>{_TIME_RX})?[.,]?\s*"
    rf"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    rf"(?P<month>{_MONTH_RX})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s*(?P<year>20\d\d))?"
)


def infer_year(month: int, day: int, sent: date | None) -> int | None:
    """Choose the year that puts the lecture closest to the send date.
    Announcement emails go out days (at most a few weeks) before the talk."""
    if sent is None:
        return None
    best = min(
        (sent.year - 1, sent.year, sent.year + 1),
        key=lambda y: abs((_safe_date(y, month, day) or date(1, 1, 1)) - sent),
    )
    return best


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def resolve_date(
    month: int, day: int, year: int | None, sent: date | None
) -> tuple[date | None, str | None]:
    """Resolve a lecture date, correcting misprinted years against Date Sent.

    Returns (date, note). A stated year further than ~300 days from the send
    date is treated as a misprint if a neighboring year lands within 60 days
    (observed: a Feb 2026 send announcing "February 10, 2025")."""
    if year is not None:
        stated = _safe_date(year, month, day)
        if stated is None:
            return None, None
        if sent is None or abs((stated - sent).days) <= 300:
            return stated, None
        alt_year = infer_year(month, day, sent)
        alt = _safe_date(alt_year, month, day) if alt_year else None
        if alt and abs((alt - sent).days) <= 60:
            return alt, (
                f"announcement email misprints the year as {year}; "
                f"corrected to {alt.year} from the email send date"
            )
        return stated, None
    if sent is None:
        return None, None
    y = infer_year(month, day, sent)
    return (_safe_date(y, month, day), None) if y else (None, None)


# ---------------------------------------------------------------------------
# Heading (rich eras) location
# ---------------------------------------------------------------------------

_SKIP_HEAD_PREFIXES = (
    "Our lectures",
    "Annual Meeting",
    "This OLLI lecture",
    "*This OLLI lecture",
    "Good morning",
    "Good afternoon",
    "Hello",
    "Dear ",
    "Speaker will be",
    "Speakers will be",
    "*Speaker will be",
    "**Speaker will be",
    "We highly encourage",
    "*ONLINE ONLY*",
    "*Rescheduled",
    "Exported using Save Emails",
    "Towsley Auditorium",
    "WCC-",
    "WCC ",
    "$",
    "Unless pre-registered",
    "*Please note",
    "Please note",
    "*Note:",
    "*NOTE",
    "**Speaker",
    "*Speaker",
    "Speaker is",
    "Speakers are",
    "Below is the link",
    "Greetings",
    "Thank you for registering",
    "**VIRTUAL ONLY**",
    "*WE WILL NOT BE",
    "SPONSORSHIP",
    "Lecture sponsored by",
    "*Lecture sponsored",
    "**Lecture sponsored",
    "Sponsorship:",
    "Update:",
    "UPDATE",
    "*Update",
    "Janice Montroy",
    "Ryan J. Luttermoser",
    "Ben Richards",
    "Administrative Assistant",
    "Assistant Director",
    "Osher Lifelong Learning",
    "Turner Senior Resource",
    "2401 Plymouth",
    "Ann Arbor, MI",
    "P: 734",
    "F: 734",
    "Fax 734",
    "Is today,",
    "Good Morning",
    "Good morning",
)
_DROP_LINE_RE = re.compile(
    r"^(Exported using Save Emails as PDF.*|https?://\S+"
    r"|(?:Zoom [Ll]ink|Webinar Link|Link to the livestream):.*"
    r"|To call in.*|Audio only.*"
    r"|WCC[- ].*|Towsley Auditorium.*|Washtenaw Community College.*"
    r"|\$\d+\.?\d*\s*[Aa]t (the )?[Dd]oor.*|and livestreaming on Zoom\.?"
    r"|and ONLINE|\*?ONLINE ONLY\*?|Please attend in person.*"
    r"|Unless pre-registered\.?|pwd=\S+|\S+@\S+\.\S+)$"
)


def _dateline_info(lines: list[str], i: int) -> dict | None:
    """If lines[i] is a rich-era date anchor, return its parsed info dict
    ({month, day, year, time, idx}) — idx is the last date/time line
    consumed (the time may sit on the following line, 2023 layout)."""
    s = lines[i].strip()
    m = RICH_DATE_RE.match(s)
    if m:
        return {
            "month": _MONTHS[m.group("month")],
            "day": int(m.group("day")),
            "year": int(m.group("year")) if m.group("year") else None,
            "time": m.group("time"),
            "idx": i,
        }
    m = RICH_DATE_ONLY_RE.match(s)
    if m and (m.group("wd") or m.group("year")):
        time_val = None
        idx_last = i
        seen = 0
        for k in range(i + 1, min(i + 5, len(lines))):
            t = lines[k].strip()
            if not t:
                continue
            seen += 1
            tm = TIME_ONLY_RE.match(t)
            if tm:
                time_val = tm.group("time")
                idx_last = k
                break
            if seen >= 2:
                break
        return {
            "month": _MONTHS[m.group("month")],
            "day": int(m.group("day")),
            "year": int(m.group("year")) if m.group("year") else None,
            "time": time_val,
            "idx": idx_last,
        }
    m = RESCHED_RE.match(s)
    if m:
        y = int(m.group("y"))
        if y < 100:
            y += 2000
        time_val = None
        idx_last = i
        for k in range(i + 1, min(i + 5, len(lines))):
            t = lines[k].strip()
            if not t:
                continue
            tm = TIME_ONLY_RE.match(t)
            if tm:
                time_val = tm.group("time")
                idx_last = k
            break
        return {
            "month": int(m.group("m")),
            "day": int(m.group("d")),
            "year": y,
            "time": time_val,
            "idx": idx_last,
            "resched_from": m.group("from"),
        }
    return None


# Description paragraphs open with these when the title/desc run together in
# the date-first 2022 layout (no blank line between title and prose)
_DESC_STARTER_RE = re.compile(
    r"^(This (presentation|talk|lecture|series|session)\b|The speakers? will\b|"
    r"In this (talk|lecture|presentation)\b|Join (us|our)\b)"
)


_NAME_CONNECTORS = {"&", "and", "van", "von", "de", "der"}


def _looks_like_name(s: str) -> bool:
    """A bare person-name line: 1-5 tokens, each capitalized (or a connector),
    no colon — 'Dr. Elizabeth Ferris', 'Norman & Ilene Tyler'."""
    s = s.strip().rstrip(",")
    if not s or ":" in s or len(s) > 45:
        return False
    toks = s.split()
    if not 1 <= len(toks) <= 5:
        return False
    ok = 0
    for t in toks:
        tt = t.strip(".,")
        if tt.lower() in _NAME_CONNECTORS:
            continue
        if not tt or not tt[0].isupper():
            return False
        ok += 1
    return ok >= 2


def _find_heading_and_date(body: str) -> tuple[str | None, dict | None]:
    """Locate the lecture heading + its date in a rich-era body.

    The date anchor is the first rich-era date line in the body. The heading
    is the LAST contiguous run of content lines above it (venue notes and
    livestream boilerplate paragraphs can precede the heading); in the
    date-first 2022 layout (no lines above the date), the heading is instead
    the first line(s) below it, ending where description prose starts.
    Returns (heading, {month, day, year, time, idx}) — idx is the last
    date/time/title line consumed, i.e. where paragraph collection starts."""
    lines = body.splitlines()
    info: dict | None = None
    date_idx = len(lines)
    for i in range(len(lines)):
        s = lines[i].strip()
        if _HEADER_NEXT_RE.match(s) or s.startswith("Subject:"):
            continue
        info = _dateline_info(lines, i)
        if info:
            date_idx = i
            break
    # content runs above the date line; the last one is the heading
    runs: list[list[str]] = []
    cur: list[str] = []
    skipping = False  # a skip-prefix line skips its whole wrapped paragraph
    for j in range(date_idx):
        s = lines[j].strip()
        if (
            _HEADER_NEXT_RE.match(s)
            or s.startswith("Subject:")
            or _PAGE_NUM_RE.fullmatch(s)
            or _DROP_LINE_RE.match(s)
        ):
            continue
        if not s:
            skipping = False
            if cur:
                runs.append(cur)
                cur = []
            continue
        if any(s.startswith(p) for p in _SKIP_HEAD_PREFIXES) or _SPEAKER_LINE_RE.match(s):
            skipping = True
            if cur:
                runs.append(cur)
                cur = []
            continue
        if skipping:
            continue
        cur.append(s)
    if cur:
        runs.append(cur)
    # a heading never wraps past ~6 lines — longer runs are prose/venue notes
    runs = [r for r in runs if len(r) <= 6]
    head = runs[-1] if runs else []
    speaker_guess = None
    if len(runs) >= 2 and _looks_like_name(_join_lines(runs[-1])):
        # a bare speaker-name line floats between the heading and the date in
        # several 2023 layouts — the run above it is the real heading
        speaker_guess = _join_lines(runs[-1])
        head = runs[-2]
    if not head and info:
        # date-first layout: title line(s) sit directly below the date/time,
        # running straight into the description prose with no blank line
        k = info["idx"] + 1
        collected: list[str] = []
        last_idx = info["idx"]
        while k < len(lines) and len(collected) < 3:
            s = lines[k].strip()
            if not s or _PAGE_NUM_RE.fullmatch(s) or _DROP_LINE_RE.match(s):
                if collected:
                    break
                k += 1
                continue
            if _SPEAKER_LINE_RE.match(s) or _DESC_STARTER_RE.match(s):
                break
            if any(s.startswith(p) for p in _SKIP_HEAD_PREFIXES):
                if collected:
                    break
                k += 1
                continue
            collected.append(s)
            last_idx = k
            if s.endswith((".", "!", "?")):
                break
            k += 1
        if collected:
            head = collected
            info = dict(info, idx=last_idx)
    if info is not None and speaker_guess:
        info = dict(info, speaker_guess=speaker_guess)
    heading = _join_lines(head)
    return (heading or None), info


# ---------------------------------------------------------------------------
# Series-prefix stripping (rich headings)
# ---------------------------------------------------------------------------

# (regex on the heading start, canonical series family)
_SERIES_PREFIXES = [
    (re.compile(r"^Alfred Gourdji Distinguished Lecture Series(?: Presents)?:?\s*", re.I),
     "distinguished"),
    (re.compile(r"^(?:The )?Distinguished Lecture Series(?: Presents)?:?\s*", re.I),
     "distinguished"),
    (re.compile(r"^Thursday Morning Lecture Series:?\s*", re.I), "thursday"),
    (re.compile(r"^Thursday Lecture Series:?\s*", re.I), "thursday"),
    (re.compile(r"^THURSDAY MORNING LECTURE:?\s*"), "thursday"),
    (re.compile(r"^THURSDAY LECTURE:?\s*"), "thursday"),
    (re.compile(r"^OLLI DEI PRESENTS:?\s*", re.I), "dei"),
    (re.compile(r"^(?:OLLI )?Summer Lecture Series:?\s*", re.I), "summer"),
    (re.compile(r"^Science Pop[- ]?Up Talks:?\s*", re.I), "special"),
]


def strip_series_prefix(heading: str) -> tuple[str, str | None]:
    """Strip a leading series-brand prefix; return (rest, family|None)."""
    for rx, family in _SERIES_PREFIXES:
        m = rx.match(heading)
        if m:
            return heading[m.end() :].strip(), family
    return heading, None


def norm(t: str | None) -> str:
    """Normalization shared with reconcile_video_library.py conventions."""
    if t is None:
        return ""
    t = re.sub(r"[^\x20-\x7e]+", " ", str(t))
    t = t.replace("&nbsp;", " ").replace("&amp;", " and ").replace("&", " and ")
    t = t.lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def split_series_title(rest: str, known_series: list[str]) -> tuple[str | None, str]:
    """Split "Series Name: Lecture Title" using the known-series list.

    Longest known series whose normalized form prefixes the normalized rest
    wins; the split point is the punctuation boundary (colon, question mark,
    dash) where the prefix ends — "Will Democracy Survive? The Hollow
    Parties" splits on the "?" just as "Global Waters: Rivers of Power"
    splits on the ":". Returns (series_name|None, title)."""
    nrest = norm(rest)
    best: str | None = None
    for s in sorted(known_series, key=lambda s: -len(norm(s))):
        ns = norm(s)
        if ns and (nrest == ns or nrest.startswith(ns + " ")):
            best = s
            break
    if best is None:
        return None, rest.strip()
    # find the punctuation boundary where the normalized prefix ends
    for m in re.finditer(r"[:?!]|[-–—]\s", rest):
        if norm(rest[: m.start()]) == norm(best):
            return best, rest[m.end() :].strip()
    if norm(rest) == norm(best):
        return best, rest.strip()  # heading IS the series name; keep as title
    return best, rest[len(best) :].lstrip(" :?–—-").strip()


# ---------------------------------------------------------------------------
# Speaker parsing
# ---------------------------------------------------------------------------

_SPEAKER_TAIL_RE = re.compile(
    r"\s+(?:will be presenting|will present|will be joining|is presenting|"
    r"presents|will be live|joins us)\b.*$"
    r"|\s+Speakers? will be\b.*$|\s+The speakers? will\b.*$"
    r"|\s*\*+\s*Speakers?\s+(?:is|are|will|via).*$"
    r"|\s+This (?:talk|lecture|presentation|series)\b.*$",
    re.S,
)
_HONORIFIC_RE = re.compile(r"^(Professor|Prof\.?|Dr\.?|Judge|Rev\.?)\s+")


def parse_speaker(raw: str) -> tuple[str | None, str | None]:
    """Split a Speaker:-line value into (speaker, speaker_title).

    Strips livestream tails ("... will be presenting in-person at WCC and
    ...") and pulls what follows the first comma (credentials — ", JD" — or
    role lines — ", President of the University Musical Society ...") into
    speaker_title. Honorifics stay on the name (catalog keeps e.g. "Judge")."""
    s = _SPEAKER_TAIL_RE.sub("", raw).strip().rstrip(".,;")
    if not s:
        return None, None
    title = None
    if "," in s:
        name, _, tail = s.partition(",")
        tail = tail.strip().rstrip(".,;")
        if tail:
            title = tail
        s = name.strip()
    return (s or None), title


# "Judith E. Levy is a judge on ..." — bio-leading name for speakerless emails
_BIO_NAME_RE = re.compile(
    r"^((?:[A-Z][\w.'’-]*\s+){1,4}[A-Z][\w.'’-]*?),?\s+(?:is|was|has|serves|works)\s"
)


def speaker_from_bio(paragraph: str) -> str | None:
    m = _BIO_NAME_RE.match(paragraph.strip())
    if not m:
        return None
    name = m.group(1).strip().rstrip(",")
    # guard against sentence-y false positives ("The Office of National ...")
    if name.split()[0] in ("The", "This", "In", "As", "Join", "Our", "A", "An", "It"):
        return None
    return name


# ---------------------------------------------------------------------------
# Body furniture / paragraphing (rich eras)
# ---------------------------------------------------------------------------

_TERMINATOR_PREFIXES = (
    "*This OLLI lecture",
    "*This lecture will be livestreamed",
    "This lecture will be livestreamed",
    "**The Lunch Bunch",
    "The Lunch Bunch invites",
    "This lecture series was planned by",
    "This series was planned by",
    "Directions to WCC",
    "Additional Resources:",
    "*If you need technical assistance",
    "If you need technical assistance",
    "Technical Information:",
    "Do you know that you can now find links",
    "Kind Regards",
    "Best,",
    "Thanks,",
    "Thank you,",
    "Sincerely",
    "Warm regards",
)
_SKIP_PARA_PREFIXES = (
    "We highly encourage you",
    "*ONLINE ONLY*",
    "Coffee and pastries",
    "Please arrive no later",
    "$",
    "Unless pre-registered",
    "Masks are",
    "Parking:",
    "Accessibility:",
    "Where:",
    "Also, because some people have sensitivity",
    "Listening devices",
    "We look forward to seeing you",
    "Don’t forget",
    "Don't forget",
    "Towsley Auditorium",
    "http",
    "www.",
    "*Rescheduled",
)
_SIGNATURE_NAME_RE = re.compile(
    r"^(Ryan J\.? Luttermoser|Janice Montroy|Ben Richards|Benjamin Richards|Ryan)$"
)


def body_paragraphs(body: str, start_after: int) -> list[str]:
    """Blank-line-separated paragraphs from line-index start_after on, with
    page-number lines dropped and hard-wrapped lines rejoined; stops at the
    first terminator line."""
    lines = body.splitlines()[start_after + 1 :]
    paras: list[str] = []
    cur: list[str] = []
    stopped = False
    for line in lines:
        s = line.strip()
        if _PAGE_NUM_RE.fullmatch(s) or _DROP_LINE_RE.match(s):
            continue
        if any(s.startswith(p) for p in _TERMINATOR_PREFIXES) or _SIGNATURE_NAME_RE.match(s):
            stopped = True
            break
        if not s:
            if cur:
                paras.append(_join_lines(cur))
                cur = []
            continue
        cur.append(s)
    if cur:
        paras.append(_join_lines(cur))
    # drop boilerplate paragraphs
    return [
        p
        for p in paras
        if not any(p.startswith(pre) for pre in _SKIP_PARA_PREFIXES)
    ]


_TALK_VERB_RE = re.compile(
    r"\bwill (?:be )?(?:giv|talk|discuss|explor|present|address|explain|argu|"
    r"describ|shar|speak|examin|lead|demonstrat|touch|tell|review|walk|look|"
    r"trace|cover|show|highlight|focus|offer|take)"
)


def split_description_bio(
    paras: list[str], speaker: str | None
) -> tuple[str | None, str | None, str | None]:
    """Partition paragraphs into (description, bio, inferred_speaker).

    A paragraph opening (first ~15 words) with a token of the speaker's name
    starts the bio — unless it talks about what the speaker WILL do (talk-
    future verbs mean it's still describing the lecture). With no speaker
    known (rich2022), a paragraph matching the "<Name> is/was ..." shape
    starts the bio and names the speaker."""
    inferred = None
    bio_start = None
    if speaker:
        tokens = {
            w.lower().strip(".,")
            for w in re.split(r"\s+|,|\band\b", speaker)
            if len(w.strip(".,")) >= 3
        }
        for i, p in enumerate(paras):
            first_words = {w.lower().strip(".,’'") for w in p.split()[:15]}
            if tokens & first_words and not _TALK_VERB_RE.search(p[:300].lower()):
                bio_start = i
                break
    else:
        for i, p in enumerate(paras):
            name = speaker_from_bio(p)
            if name and not _TALK_VERB_RE.search(p[:300].lower()):
                bio_start, inferred = i, name
                break
    if bio_start is None:
        desc = "\n\n".join(paras).strip() or None
        return desc, None, None
    desc = "\n\n".join(paras[:bio_start]).strip() or None
    bio = "\n\n".join(paras[bio_start:]).strip() or None
    return desc, bio, inferred


# ---------------------------------------------------------------------------
# Per-era parsers
# ---------------------------------------------------------------------------

def _collect_speaker_value(lines: list[str], i: int) -> str:
    """The Speaker: value, following wraps only while the value clearly
    continues (ends with a comma or a lowercase word, no period) — the next
    body line often starts flush against it with no blank in between."""
    val = _SPEAKER_LINE_RE.match(lines[i].strip()).group(2).strip()
    j = i + 1
    if not val:  # "Speaker:" alone, name on the following line (2022 layout)
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and not _dateline_info(lines, j):
            val = lines[j].strip()
            j += 1
    while j < len(lines):
        s = lines[j].strip()
        last = val.rsplit(None, 1)[-1] if val.split() else ""
        cont = val.endswith(",") or (last and last[0].islower() and not val.endswith("."))
        if not cont or not s or _PAGE_NUM_RE.fullmatch(s) or _DROP_LINE_RE.match(s):
            break
        val += " " + s
        j += 1
    return val


_TEMPLATE_FIELD_RES = {
    "title": re.compile(r"^Lecture [Tt]itle:\s*(.+)$"),
    "series": re.compile(r"^(?:Lecture Series|Series Title):\s*(.+)$"),
}
_WITH_SPLIT_RE = re.compile(r"^(?P<title>.+)\s+with\s+(?P<speaker>[A-Z][^:]*?)\.?$")


def _collect_wrapped(lines: list[str], i: int) -> str:
    """A field value may wrap onto following lines (until a blank line, the
    next Field: line, a page number, or a date/time line)."""
    out = [lines[i].strip()]
    j = i + 1
    while j < len(lines):
        s = lines[j].strip()
        if (
            not s
            or re.match(r"^[A-Z][A-Za-z ]{0,20}:", s)
            or _PAGE_NUM_RE.fullmatch(s)
            or _DROP_LINE_RE.match(s)
            or RICH_DATE_RE.match(s)
            or (RICH_DATE_ONLY_RE.match(s) and RICH_DATE_ONLY_RE.match(s).group("wd"))
            or TIME_ONLY_RE.match(s)
        ):
            break
        out.append(s)
        j += 1
    return _join_lines(out)


def parse_template(body: str, sent: date | None) -> dict:
    lines = body.splitlines()
    fields: dict[str, str] = {}
    when = None
    for i, line in enumerate(lines):
        s = line.strip()
        if when is None:
            m = WHEN_RE.match(s)
            if m:
                when = m
        for key, rx in _TEMPLATE_FIELD_RES.items():
            if key not in fields:
                fm = rx.match(s)
                if fm:
                    full = _collect_wrapped(lines, i)
                    fields[key] = rx.match(full).group(1).strip()
    out: dict = {"era": "template", "needs_review": [], "notes": []}
    raw_title = fields.get("title", "").strip()
    # session-number furniture: "Session 1 Distinguished Lecture Series."
    series = re.sub(r"^Session \d+\s+", "", fields.get("series", "").strip()).rstrip(".")
    # split "“Title” with Speaker Name."
    speaker = None
    m = _WITH_SPLIT_RE.match(raw_title)
    if m:
        raw_title, speaker = m.group("title"), m.group("speaker").strip()
    title = _clean_title(raw_title)
    speaker_title = None
    if speaker:
        hm = _HONORIFIC_RE.match(speaker)
        if hm and hm.group(1).startswith("Prof"):
            speaker_title, speaker = "Professor", speaker[hm.end() :].strip()
    if speaker:
        sp, extra_title = parse_speaker(speaker)
        speaker = sp
        if extra_title and not speaker_title:
            speaker_title = extra_title
    out.update(
        {
            "title": title,
            "speaker": speaker,
            "speaker_title": speaker_title,
            "series_hint": series or None,
            "description": None,
            "bio": None,
        }
    )
    if when:
        d, note = resolve_date(
            _MONTHS[when.group("month")],
            int(when.group("day")),
            int(when.group("year")) if when.group("year") else None,
            sent,
        )
        out["lecture_date"] = d.isoformat() if d else None
        out["time"] = _clean_time(when.group("time"))
        if note:
            out["notes"].append(note)
    else:
        out["lecture_date"] = None
        out["time"] = None
        out["needs_review"].append("no When: date line")
    if not title:
        out["needs_review"].append("no Lecture Title field")
    # the 2020-21 template emails drift toward prose: description and bio
    # paragraphs follow the fields — mine them the same way as the rich eras
    lines = body.splitlines()
    last_field = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if WHEN_RE.match(s) or any(rx.match(s) for rx in _TEMPLATE_FIELD_RES.values()) or s.startswith("Where:"):
            last_field = max(last_field, i)
    paras = body_paragraphs(body, last_field)
    desc, bio, inferred = split_description_bio(paras, out.get("speaker"))
    if desc and len(desc) >= 120:
        out["description"] = desc
    if bio:
        out["bio"] = bio
        if inferred and not out.get("speaker"):
            out["speaker"] = inferred
            out["notes"].append(
                "speaker taken from the bio paragraph (announcement names no speaker in the title line)"
            )
    return out


def parse_rich(body: str, sent: date | None, known_series: list[str]) -> dict:
    heading, info = _find_heading_and_date(body)
    out: dict = {"needs_review": [], "notes": []}
    if heading is None or info is None:
        out["needs_review"].append("no heading/date anchor")
        return out
    lines = body.splitlines()
    d, note = resolve_date(info["month"], info["day"], info["year"], sent)
    if note:
        out["notes"].append(note)
    if info.get("resched_from"):
        out["notes"].append(f"rescheduled from {info['resched_from']}")
    rest, family = strip_series_prefix(heading)
    series_name, title = split_series_title(rest, known_series)
    title = _clean_title(title)
    if family in ("thursday", "dei") and series_name is None and ":" in rest:
        out["needs_review"].append("series/title colon split unresolved")
    # Speaker line (rich) — usually below the date/time lines, but the
    # Dec-2022 layout floats it above them
    speaker = speaker_title = None
    speaker_idx = None
    for i, line in enumerate(lines):
        m = _SPEAKER_LINE_RE.match(line.strip())
        if m:
            speaker_idx = i
            sp = _collect_speaker_value(lines, i)
            speaker, speaker_title = parse_speaker(sp)
            break
    era = "rich" if speaker_idx is not None else "rich2022"
    if speaker is None and info.get("speaker_guess"):
        speaker, speaker_title = parse_speaker(info["speaker_guess"])
        out["notes"].append(
            "speaker taken from the name line under the lecture heading"
        )
    paras = body_paragraphs(
        body, max(speaker_idx if speaker_idx is not None else 0, info["idx"])
    )
    desc, bio, inferred = split_description_bio(paras, speaker)
    if era == "rich2022":
        if inferred:
            speaker = inferred
            out["notes"].append("speaker taken from the bio paragraph (2022-era announcements print no Speaker line)")
        else:
            out["needs_review"].append("no speaker (2022-era) and bio heuristic found none")
    out.update(
        {
            "era": era,
            "title": title or None,
            "speaker": speaker,
            "speaker_title": speaker_title,
            "series_hint": series_name or (family and _FAMILY_DEFAULT.get(family)),
            "series_family": family,
            "lecture_date": d.isoformat() if d else None,
            "time": _clean_time(info["time"]),
            "description": desc,
            "bio": bio,
        }
    )
    if not title:
        out["needs_review"].append("empty title after series strip")
    if d is None:
        out["needs_review"].append("unresolvable lecture date")
    return out


_FAMILY_DEFAULT = {
    "distinguished": "Alfred Gourdji Distinguished Lecture Series",
    "thursday": None,
    "dei": None,
}


def _clean_title(t: str | None) -> str | None:
    if not t:
        return None
    t = t.strip().strip("“”\"'‘’").strip()
    t = re.sub(r"^[-–—:;,\s]+", "", t)
    t = re.sub(r"[.,;\s]+$", "", t)
    return t or None


def _clean_time(t: str | None) -> str | None:
    if not t:
        return None
    return re.sub(r"\s+", " ", t.replace("–", "-").replace("—", "-")).strip()


# ---------------------------------------------------------------------------
# File-level pipeline
# ---------------------------------------------------------------------------


def parse_email(email: dict, known_series: list[str]) -> dict | None:
    """Parse one email into a lecture candidate, or None if it isn't a
    lecture announcement (weather notices, committee packets, bare replies)."""
    body = email_body(email)
    era = classify_era(body)
    if era is None:
        return None
    if era == "template":
        rec = parse_template(body, email["date_sent"])
    else:
        rec = parse_rich(body, email["date_sent"], known_series)
        if "era" not in rec:
            return None
    rec["subject"] = email["subject"]
    rec["date_sent"] = email["date_sent"].isoformat() if email["date_sent"] else None
    rec["cancelled"] = bool(re.search(r"cancell?ed|cancellation", email["subject"], re.I))
    return rec


def _richness(rec: dict) -> int:
    return len(rec.get("description") or "") + len(rec.get("bio") or "")


def _same_lecture(a: dict, b: dict) -> bool:
    da, db = a.get("lecture_date"), b.get("lecture_date")
    ta, tb = norm(a.get("title")), norm(b.get("title"))
    fuzzy = bool(ta and tb) and (
        ta == tb or difflib.SequenceMatcher(None, ta, tb).ratio() >= 0.85
    )
    if da and db and da == db:
        if fuzzy:
            return True
        sa, sb = norm(a.get("speaker")), norm(b.get("speaker"))
        return bool(sa and sb and sa == sb)  # retitled between sends
    if fuzzy and da and db:
        # a correction send may move the date a day or two
        delta = abs(date.fromisoformat(da).toordinal() - date.fromisoformat(db).toordinal())
        return delta <= 2
    return False


def dedup_candidates(cands: list[dict]) -> list[dict]:
    """Collapse cross-send variants of the same lecture (*Upcoming* /
    *Tomorrow* / TODAY / corrections).

    Variants match on same date + fuzzy title >= 0.85, same date + same
    speaker (retitled sends), or fuzzy title + date within 2 days (correction
    sends move the date). The richest body wins; the LATEST send is
    authoritative for date and title (corrections supersede); missing fields
    fill from the other variants."""
    groups: list[list[dict]] = []
    for c in cands:
        placed = False
        for g in groups:
            if any(_same_lecture(c, m) for m in g):
                g.append(c)
                placed = True
                break
        if not placed:
            groups.append([c])
    out = []
    for g in groups:
        best = max(g, key=_richness)
        latest = max(g, key=lambda c: c.get("date_sent") or "")
        merged = dict(best)
        merged["lecture_date"] = latest.get("lecture_date") or merged.get("lecture_date")
        # longest title wins (short variants are usually truncated headings);
        # a genuinely retitled lecture gets its alternate title noted
        longest = max(g, key=lambda c: len(c.get("title") or ""))
        if longest.get("title"):
            if merged.get("title") and norm(longest["title"]) != norm(merged["title"]):
                r = difflib.SequenceMatcher(
                    None, norm(longest["title"]), norm(merged["title"])
                ).ratio()
                if r < 0.85:
                    merged["notes"].append(
                        f"also announced under the title {merged['title']!r}"
                    )
            merged["title"] = longest["title"]
        for other in sorted(g, key=_richness, reverse=True):
            if other is best:
                continue
            for k in ("speaker", "speaker_title", "series_hint", "description", "bio", "time"):
                if not merged.get(k) and other.get(k):
                    merged[k] = other[k]
            for n in other.get("notes", []):
                if n not in merged["notes"]:
                    merged["notes"].append(n)
        merged["n_sends"] = len(g)
        merged["cancelled"] = any(c.get("cancelled") for c in g)
        # a variant may have resolved something the richest send flagged
        merged["needs_review"] = [
            r
            for r in merged.get("needs_review", [])
            if not (r.startswith("no speaker") and merged.get("speaker"))
        ]
        out.append(merged)
    out.sort(key=lambda r: (r.get("lecture_date") or "9999", norm(r.get("title"))))
    return out


def parse_file(
    path: Path, known_series: list[str] | None = None, source_label: str | None = None
) -> dict:
    text = path.read_text()
    emails, prelude = split_emails(text)
    for e in emails:
        parse_headers(e)
    candidates = []
    skipped = []
    for e in emails:
        rec = parse_email(e, known_series or [])
        if rec is None:
            skipped.append(
                {"subject": e["subject"], "date_sent": e["date_sent"].isoformat() if e["date_sent"] else None}
            )
        else:
            rec["source_file"] = source_label or path.name
            candidates.append(rec)
    lectures = dedup_candidates(candidates)
    return {
        "source": source_label or path.name,
        "n_pages_prelude": prelude,
        "n_emails": len(emails),
        "n_lecture_emails": len(candidates),
        "n_skipped_emails": len(skipped),
        "n_distinct_lectures": len(lectures),
        "skipped": skipped,
        "lectures": lectures,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="extracted announcements .txt")
    ap.add_argument("-o", "--output", help="write candidates JSON here")
    ap.add_argument(
        "--known-series",
        help="JSON file with a list of known series titles for colon-splitting",
    )
    args = ap.parse_args()
    known = []
    if args.known_series:
        known = json.loads(Path(args.known_series).read_text())
    result = parse_file(Path(args.source), known)
    payload = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    if args.output:
        Path(args.output).write_text(payload)
    else:
        print(payload)
    import sys

    eras = {}
    for l in result["lectures"]:
        eras[l["era"]] = eras.get(l["era"], 0) + 1
    flagged = sum(1 for l in result["lectures"] if l["needs_review"])
    print(
        f"emails={result['n_emails']} lecture_emails={result['n_lecture_emails']} "
        f"skipped={result['n_skipped_emails']} distinct={result['n_distinct_lectures']} "
        f"eras={eras} needs_review={flagged}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
