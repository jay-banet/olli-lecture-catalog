"""BLC-012 merge: fold parsed OLLI announcement-email lectures into the catalog.

Reads data/olli-catalog-full.json plus the two announcement extractions
(data/olli-agdls-announcements.txt, data/olli-thursday-announcements.txt),
parses them with announcements.py, applies the curation overlay
(data/announcement-curation.json), and match-backs each distinct announcement
lecture against the catalog:

  ENRICH     — an existing record matched and had empty description /
               speaker_bio / speaker / time slots: fill ONLY the empty ones
               (a "Not printed in this edition" placeholder description counts
               as empty). Catalog-sourced non-empty fields are NEVER touched.
  NET-NEW    — no catalog record matched: add a lecture record with
               source: "email-announcement". Seasons with no printed catalog
               (pre-Fall-2018, W/S 2019/2020, Fall 2021, W/S 2022) get a
               derived edition named by OLLI's season convention, with a note
               that no catalog exists for that season.
  DUPLICATE  — matched an already-complete record: no-op, counted.
  CONFLICT   — announcement title/speaker/date disagrees with a matched
               catalog record: the CATALOG value wins; the disagreement is
               logged in the report, never "fixed".

Cancelled announcements (CANCELLED/CANCELLATION subjects) are dropped and
counted. Matching follows reconcile_video_library.py conventions (normalize,
difflib >= 0.85); a fuzzy title match tolerates a date within 21 days (the
catalog date wins, conflict logged) and also tries a "rescheduled from" date
when the announcement carries one.

Idempotent: net-new records match themselves on a re-run; enrichment notes
append once.

Usage:
    python3 merge_announcements.py [--dry-run] [-o data/olli-catalog-full.json]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
from datetime import date
from pathlib import Path

import announcements as ann

DATA = Path(__file__).parent / "data"
CATALOG_PATH = DATA / "olli-catalog-full.json"
CURATION_PATH = DATA / "announcement-curation.json"
SOURCES = [
    DATA / "olli-agdls-announcements.txt",
    DATA / "olli-thursday-announcements.txt",
]

PLACEHOLDER_DESC_RE = re.compile(r"^Not (printed|included)\b")
DATE_WINDOW_DAYS = 21
FUZZY = 0.85

# Season labels follow the catalog's own naming: the Jan-Jun season is
# "Winter/Spring YYYY" except the years OLLI branded it "Winter" (2024, 2026).
WINTER_NAMES = {2024: "Winter 2024", 2026: "Winter 2026"}


def norm(s):
    return ann.norm(s)


def fuzzy(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def season_for(d: date, existing_editions: list[str]) -> tuple[str, bool]:
    """(edition name, exists-in-catalog) for a lecture date with no series
    match. Jan-Aug -> the Winter/Spring season, Sep-Dec -> Fall."""
    if d.month >= 9:
        name = f"Fall {d.year}"
    else:
        name = WINTER_NAMES.get(d.year, f"Winter/Spring {d.year}")
    return name, name in existing_editions


def edition_sort_key(name: str) -> tuple[int, int]:
    year = int(name.split()[-1])
    return (year, 0 if name.startswith(("Winter", "Winter/Spring")) else 1)


def parse_date_range(dr: str) -> tuple[date, date] | None:
    m = re.match(r"(\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", dr or "")
    if not m:
        return None
    return date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))


# ---------------------------------------------------------------------------
# Curation overlay
# ---------------------------------------------------------------------------


def apply_curation(lectures: list[dict], curation: dict) -> tuple[list[dict], list[str]]:
    warnings = []
    out = []
    used = set()
    for lec in lectures:
        dropped = False
        for k, entry in enumerate(curation.get("entries", [])):
            m = entry["match"]
            if (
                lec.get("source_file") == m["source"]
                and lec.get("lecture_date") == m["lecture_date"]
                and (lec.get("title") or "").startswith(m["title_prefix"])
            ):
                used.add(k)
                if entry.get("drop"):
                    dropped = True
                    break
                for field, val in entry.get("set", {}).items():
                    lec[field] = val
                if entry.get("add_note"):
                    lec.setdefault("notes", [])
                    if entry["add_note"] not in lec["notes"]:
                        lec["notes"].append(entry["add_note"])
                lec["needs_review"] = []
        if not dropped:
            out.append(lec)
    for k, entry in enumerate(curation.get("entries", [])):
        if k not in used:
            warnings.append(f"curation entry did not match any lecture: {entry['match']}")
    for add in curation.get("additions", []):
        rec = dict(add)
        rec["source_file"] = rec.pop("source")
        out.append(rec)
    return out, warnings


# ---------------------------------------------------------------------------
# Match-back
# ---------------------------------------------------------------------------


def candidate_dates(lec: dict) -> list[date]:
    ds = []
    if lec.get("lecture_date"):
        ds.append(date.fromisoformat(lec["lecture_date"]))
    sources = list(lec.get("notes", [])) + [lec.get("subject") or ""]
    for n in sources:
        m = re.search(r"[Rr]escheduled from (\d{1,2})/(\d{1,2})/(\d{2,4})", n)
        if m:
            y = int(m.group(3))
            if y < 100:
                y += 2000
            try:
                ds.append(date(y, int(m.group(1)), int(m.group(2))))
            except ValueError:
                pass
    return ds


def speaker_close(a: str | None, b: str | None) -> bool:
    """Same person despite honorifics/initial/spacing drift: 'Dr. Ayanian' ~
    'John Z. Ayanian, M.D.', 'Matthew Van Besien' ~ 'Matthew VanBesien'."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    ca, cb = na.replace(" ", ""), nb.replace(" ", "")
    if na == nb or ca in cb or cb in ca:
        return True
    if fuzzy(na, nb) >= 0.8:
        return True
    drop = {"dr", "prof", "professor", "md", "phd", "jd", "mr", "ms", "mrs",
            "rev", "judge", "ambassador", "curator", "facilitator"}
    ta = [t for t in na.split() if len(t) > 2 and t not in drop]
    tb = [t for t in nb.split() if len(t) > 2 and t not in drop]
    if not ta or not tb:
        return False
    if ta[-1] == tb[-1] and len(ta[-1]) >= 4:
        return True
    # the shorter form's surname appearing anywhere in the fuller form
    # ("Dr. Ayanian" ~ "John Z. Ayanian, MD, MPP")
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(short[-1]) >= 5 and short[-1] in long_


def find_catalog_match(lec: dict, cat_lectures: list[dict]) -> tuple[dict | None, str | None]:
    """Return (record, kind) — kind 'exact' (same date) or 'date-conflict'.

    A near-date match (within the window but not same-day) additionally
    requires the speakers to be compatible — weekly series talks often share
    one series-level title (the Fall 2021 Canada series), and only the
    speaker tells them apart."""
    lt = norm(lec.get("title"))
    ldates = candidate_dates(lec)
    if not lt or not ldates:
        return None, None
    best = None
    best_kind = None
    for r in cat_lectures:
        rt = norm(r["title"])
        rd = date.fromisoformat(r["date"])
        title_close = lt == rt or fuzzy(lt, rt) >= FUZZY
        sp_close = speaker_close(lec.get("speaker"), r.get("speaker"))
        sp_compatible = sp_close or not lec.get("speaker") or not r.get("speaker")
        for ld in ldates:
            delta = abs((rd - ld).days)
            if delta == 0 and title_close:
                return r, "exact"
            if delta == 0 and sp_close:
                return r, "exact"  # retitled between announcement and print
            if delta == 0 and sp_compatible and fuzzy(lt, rt) >= 0.55:
                # same day, loosely-similar title, speakers not contradictory
                # ("Antisemitism Today: Enduring Hate After the Holocaust" ~
                # the printed "Anti Semitism Since the Holocaust")
                return r, "exact"
            if (
                title_close
                and sp_compatible
                and delta <= DATE_WINDOW_DAYS
                and best is None
                and r.get("source") != "email-announcement"
            ):
                best, best_kind = r, "date-conflict"
    return best, best_kind


def resolve_series(
    lec: dict, catalog: dict
) -> tuple[str, str, str | None]:
    """Return (series_type, series_name, edition|None-from-series)."""
    src = lec.get("source_file", "")
    hint = lec.get("series_hint") or ""
    family = lec.get("series_family")
    nh = norm(hint)
    if family == "distinguished" or (
        "agdls" in src and not hint and family is None
    ) or "distinguished lecture series" in nh:
        return "tuesday-distinguished", hint or "Distinguished Lecture Series", None
    if "zekelman" in nh:
        return "special", hint, None
    # Thursday/summer/special: try to match a catalog series record
    ldate = (
        date.fromisoformat(lec["lecture_date"]) if lec.get("lecture_date") else None
    )
    series_records = [r for r in catalog["records"] if r["type"] == "series"]
    if nh:
        for r in series_records:
            rt = norm(r["title"])
            rt_stripped = re.sub(r"^.*?lecture series\s*", "", rt)
            if rt == nh or rt_stripped == nh or fuzzy(rt_stripped or rt, nh) >= FUZZY:
                return r["series_type"], r["title"], r["edition"]
    if ldate:
        containing = []
        for r in series_records:
            rng = parse_date_range(r.get("date_range", ""))
            if rng and rng[0] <= ldate <= rng[1]:
                if not nh or fuzzy(norm(r["title"]), nh) >= 0.5:
                    containing.append(((rng[1] - rng[0]).days, r))
        if containing:
            r = min(containing, key=lambda t: t[0])[1]
            return r["series_type"], r["title"], r["edition"]
    fam_default = {
        "summer": "Summer Lecture Series",
        "special": hint or "Special Lecture Series",
        "dei": hint or "DEI Lecture Series",
    }
    if family in fam_default and family != "thursday":
        stype = "special" if family in ("summer", "special", "dei") else "thursday-series"
        return stype, hint or fam_default[family], None
    return "thursday-series", hint or "Thursday Morning Lecture Series", None


def dls_series_name_for_edition(edition: str, cat_lectures: list[dict]) -> str:
    names = {
        r["series_name"]
        for r in cat_lectures
        if r["edition"] == edition and r["series_type"] == "tuesday-distinguished"
    }
    if names:
        return sorted(names)[0]
    year = int(edition.split()[-1])
    return (
        "Distinguished Lecture Series"
        if year < 2022
        else "Alfred Gourdji Distinguished Lecture Series"
    )


TEMPLATE_PLACEHOLDER = (
    "Not included in the announcement email — the reminder-email format of "
    "this era lists only date, title, series, and venue."
)
SOURCE_NOTE = "Record sourced from OLLI announcement emails (BLC-012); not printed in any received catalog."


def build_net_new(lec: dict, catalog: dict, printed_editions: set[str]) -> dict | None:
    if not lec.get("lecture_date") or not lec.get("title"):
        return None
    d = date.fromisoformat(lec["lecture_date"])
    stype, sname, edition = resolve_series(lec, catalog)
    notes = list(lec.get("notes", []))
    if edition is None:
        edition, _ = season_for(d, catalog["editions"])
        if edition not in printed_editions:
            notes.append(
                f"No printed catalog exists for the {edition} season — "
                "record reconstructed from announcement emails."
            )
    if stype == "tuesday-distinguished":
        sname = dls_series_name_for_edition(
            edition, [r for r in catalog["records"] if r["type"] == "lecture"]
        )
    if not lec.get("speaker") and not any("not named" in n for n in notes):
        notes.append("Speaker not named in the announcement email.")
    desc = lec.get("description")
    if desc and desc.startswith(lec["title"]):
        # a few no-blank-line layouts glom the title line into the first
        # description paragraph — drop the literal duplicate prefix
        desc = desc[len(lec["title"]) :].lstrip(" .:—–-\n") or None
    return {
        "type": "lecture",
        "edition": edition,
        "series_type": stype,
        "series_name": sname,
        "title": lec["title"],
        "speaker": lec.get("speaker"),
        "speaker_title": lec.get("speaker_title"),
        "institution": None,
        "date": lec["lecture_date"],
        "time": lec.get("time"),
        "description": desc or TEMPLATE_PLACEHOLDER,
        "speaker_bio": lec.get("bio"),
        "notes": "; ".join([SOURCE_NOTE] + notes),
        "source": "email-announcement",
    }


def enrich(rec: dict, lec: dict) -> tuple[list[str], list[str]]:
    """Fill empty fields on a matched catalog record. Returns
    (filled_fields, conflicts)."""
    filled, conflicts = [], []
    desc_empty = not rec.get("description") or PLACEHOLDER_DESC_RE.match(
        rec["description"] or ""
    )
    if desc_empty and lec.get("description"):
        rec["description"] = lec["description"]
        filled.append("description")
    if not rec.get("speaker_bio") and lec.get("bio"):
        rec["speaker_bio"] = lec["bio"]
        filled.append("speaker_bio")
    if not rec.get("speaker") and lec.get("speaker"):
        rec["speaker"] = lec["speaker"]
        if lec.get("speaker_title") and not rec.get("speaker_title"):
            rec["speaker_title"] = lec["speaker_title"]
        filled.append("speaker")
    if not rec.get("time") and lec.get("time"):
        rec["time"] = lec["time"]
        filled.append("time")
    # conflicts: log-only, catalog wins
    if lec.get("speaker") and rec.get("speaker") and not speaker_close(lec["speaker"], rec["speaker"]):
        conflicts.append(
            f"speaker: announcement says {lec['speaker']!r}, catalog says {rec['speaker']!r}"
        )
    if lec.get("title") and norm(lec["title"]) != norm(rec["title"]) and fuzzy(norm(lec["title"]), norm(rec["title"])) < FUZZY:
        conflicts.append(
            f"title: announcement says {lec['title']!r} (catalog title kept)"
        )
    if lec.get("lecture_date") and lec["lecture_date"] != rec["date"]:
        conflicts.append(
            f"date: announcement says {lec['lecture_date']}, catalog says {rec['date']}"
        )
    if filled:
        note = "Description/bio enriched from OLLI announcement emails (BLC-012)."
        existing = rec.get("notes") or ""
        if note not in existing:
            rec["notes"] = (existing + "; " + note).strip("; ") if existing else note
    return filled, conflicts


# ---------------------------------------------------------------------------
# Summary / editions regeneration
# ---------------------------------------------------------------------------


def rebuild_summary(catalog: dict) -> None:
    recs = catalog["records"]
    lectures = [r for r in recs if r["type"] == "lecture"]
    series = [r for r in recs if r["type"] == "series"]
    s = catalog["summary"]
    s["total_records"] = len(recs)
    s["lectures"] = len(lectures)
    s["series"] = len(series)
    s["lectures_by_edition"] = {
        e: sum(1 for r in lectures if r["edition"] == e) for e in catalog["editions"]
    }
    s["series_by_edition"] = {
        e: sum(1 for r in series if r["edition"] == e) for e in catalog["editions"]
    }
    by_type: dict[str, int] = {}
    for r in lectures:
        by_type[r["series_type"]] = by_type.get(r["series_type"], 0) + 1
    s["lectures_by_series_type"] = by_type
    if "speaker_bio_by_edition" in s:
        s["speaker_bio_by_edition"] = {
            e: sum(
                1 for r in lectures if r["edition"] == e and r.get("speaker_bio")
            )
            for e in catalog["editions"]
        }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def known_series_from_catalog(catalog: dict) -> list[str]:
    known: set[str] = set()

    def add(name):
        if not name:
            return
        known.add(name)
        m = re.search(r"Lecture Series:\s*", name)
        if m:
            known.add(name[m.end() :].strip())

    for r in catalog["records"]:
        add(r["title"] if r["type"] == "series" else r.get("series_name"))
    extra = json.loads((DATA / "announcement-series.json").read_text())
    known.update(extra["series"])
    return sorted(k for k in known if k)


def run(dry_run: bool = False, out_path: Path | None = None) -> dict:
    catalog = json.loads(CATALOG_PATH.read_text())
    curation = json.loads(CURATION_PATH.read_text())
    known = known_series_from_catalog(catalog)

    parse_stats = {}
    all_lectures: list[dict] = []
    for src in SOURCES:
        res = ann.parse_file(src, known)
        parse_stats[src.name] = {
            k: res[k]
            for k in (
                "n_emails",
                "n_lecture_emails",
                "n_skipped_emails",
                "n_distinct_lectures",
            )
        }
        all_lectures.extend(res["lectures"])

    lectures, warnings = apply_curation(all_lectures, curation)

    cancelled = [l for l in lectures if l.get("cancelled")]
    lectures = [l for l in lectures if not l.get("cancelled")]

    # cross-source dedup (a lecture announced through both lists)
    def _cross_dup(a: dict, b: dict) -> bool:
        if ann._same_lecture(a, b):
            return True
        da, db = a.get("lecture_date"), b.get("lecture_date")
        if not da or not db:
            return False
        delta = abs(date.fromisoformat(da).toordinal() - date.fromisoformat(db).toordinal())
        if delta > 10:
            return False
        if not speaker_close(a.get("speaker"), b.get("speaker")):
            return False
        ta, tb = norm(a.get("title")), norm(b.get("title"))
        return delta == 0 or (bool(ta and tb) and fuzzy(ta, tb) >= FUZZY)

    deduped: list[dict] = []
    cross_dups = 0
    for lec in lectures:
        if any(_cross_dup(lec, m) for m in deduped):
            cross_dups += 1
            continue
        deduped.append(lec)
    lectures = deduped

    unparseable = [
        l for l in lectures if not l.get("lecture_date") or not l.get("title")
    ]
    lectures = [l for l in lectures if l.get("lecture_date") and l.get("title")]

    cat_lectures = [r for r in catalog["records"] if r["type"] == "lecture"]
    printed_editions = set(catalog["editions"])  # before any derived additions
    enriched, net_new, duplicates, conflicts = [], [], [], []
    for lec in sorted(lectures, key=lambda l: (l["lecture_date"], norm(l["title"]))):
        rec, kind = find_catalog_match(lec, cat_lectures)
        if rec is not None:
            filled, confs = enrich(rec, lec)
            if kind == "date-conflict":
                confs = confs or [
                    f"date: announcement says {lec['lecture_date']}, catalog says {rec['date']}"
                ]
            for c in confs:
                conflicts.append(f"{rec['edition']} / {rec['title'][:60]!r}: {c}")
            if filled:
                enriched.append((rec, filled))
            else:
                duplicates.append(lec)
        else:
            new_rec = build_net_new(lec, catalog, printed_editions)
            if new_rec is None:
                unparseable.append(lec)
                continue
            catalog["records"].append(new_rec)
            cat_lectures.append(new_rec)
            if new_rec["edition"] not in catalog["editions"]:
                catalog["editions"].append(new_rec["edition"])
                catalog["editions"].sort(key=edition_sort_key)
            net_new.append(new_rec)

    catalog["records"].sort(
        key=lambda r: (
            edition_sort_key(r["edition"]),
            r.get("date") or (parse_date_range(r.get("date_range", "")) or (date.min,))[0].isoformat(),
            norm(r["title"]),
        )
    )
    rebuild_summary(catalog)
    catalog.setdefault("announcement_sources", {})
    catalog["announcement_sources"] = {
        "method": "BLC-012 email-announcement parser mode (announcements.py + curation overlay + merge_announcements.py)",
        "files": [s.name for s in SOURCES],
        "parse_stats": parse_stats,
    }

    report = {
        "parse_stats": parse_stats,
        "curation_warnings": warnings,
        "cancelled_dropped": [
            {"date": l.get("lecture_date"), "title": l.get("title")} for l in cancelled
        ],
        "cross_source_duplicates": cross_dups,
        "unparseable": [
            {"date": l.get("lecture_date"), "title": (l.get("title") or "")[:80], "subject": (l.get("subject") or "")[:80]}
            for l in unparseable
        ],
        "enriched": [
            {"edition": r["edition"], "title": r["title"], "filled": f}
            for r, f in enriched
        ],
        "net_new": [
            {
                "edition": r["edition"],
                "series_type": r["series_type"],
                "series_name": r["series_name"],
                "date": r["date"],
                "title": r["title"],
                "speaker": r["speaker"],
            }
            for r in net_new
        ],
        "duplicate_noops": [
            {"date": l["lecture_date"], "title": l["title"]} for l in duplicates
        ],
        "conflicts_catalog_wins": conflicts,
        "totals": {
            "enriched": len(enriched),
            "net_new": len(net_new),
            "duplicate_noops": len(duplicates),
            "conflicts": len(conflicts),
            "records_after": len(catalog["records"]),
            "editions_after": len(catalog["editions"]),
        },
    }

    if not dry_run:
        target = out_path or CATALOG_PATH
        target.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
        (DATA / "blc012-merge-report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        )
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()
    report = run(dry_run=args.dry_run, out_path=Path(args.output) if args.output else None)
    t = report["totals"]
    print(json.dumps({k: report[k] for k in ("parse_stats", "curation_warnings", "totals")}, indent=2))
    print(
        f"\nenriched={t['enriched']} net_new={t['net_new']} "
        f"duplicates={t['duplicate_noops']} conflicts={t['conflicts']} "
        f"records={t['records_after']} editions={t['editions_after']}"
    )


if __name__ == "__main__":
    main()
