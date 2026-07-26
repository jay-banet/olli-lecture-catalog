# OLLI Lecture Catalog (2018–2026)

A searchable, structured catalog of **128 lectures and 45 lecture-series themes** from eleven
seasons of the Osher Lifelong Learning Institute at the University of Michigan, extracted from
the printed catalog brochures (PDF) into clean, tagged data.

**Browse it:** https://jay-banet.github.io/olli-lecture-catalog/

> Unofficial project — compiled from OLLI-UM's published catalog brochures. Not affiliated with
> or endorsed by OLLI or the University of Michigan.

## Why

A family member volunteers with OLLI, helping staff make years of lecture history searchable.
The source material is beautiful print brochures — 30 to 100 pages each, laid out in InDesign
across two-column spreads, with layouts that drift every season. The lecture data inside
(title, speaker, institution, date, series, description, speaker bio) was trapped in PDFs.

## How it works — hybrid deterministic + LLM pipeline

The interesting engineering problem: **print layouts drift across eleven seasons** (renamed
series, COVID-era Zoom formats, changed date formats, column interleaves, even misprints).
A pure-regex parser breaks on drift; a pure-LLM extraction can't be regression-tested.

The pipeline splits the difference:

1. **Deterministic splitter** (`parser.py`) — pdftotext → pages → edition assignment →
   section classification (lecture series vs. courses/travel/membership pages) → per-lecture
   blocks anchored on date-line patterns. Pure Python, fully covered by pytest (147 tests),
   including regression tests that reproduce a hand-verified golden sample and negative tests
   that reject prose false-positives ("the November 5, 2024 U.S. Presidential election" is not
   a lecture).
2. **LLM tagging pass** — an LLM reads each block and emits tagged records: de-hyphenating
   line-broken text, untangling two-column interleaves, recovering institutions from bio
   paragraphs, and flagging (not guessing) ambiguities — TBA speakers, missing pages, and
   genuine misprints in the source brochures, which are preserved in each record's `notes`.
3. **Renderers** — `to_html.py` builds the single-file searchable page (no frameworks, no
   backend, senior-friendly typography); `to_csv.py` flattens records for spreadsheet use.

Every record traces back to its source text; verified fields are locked by golden-set tests
so parser changes can't silently corrupt them.

## Data

- [`data/olli-catalog-full.json`](data/olli-catalog-full.json) — full structured records
  (two types: `lecture` and `series`), with per-edition gap notes and dedup annotations
- [`data/olli-catalog-full.csv`](data/olli-catalog-full.csv) — flat lecture table
- [`data/olli-catalog-sheet.csv`](data/olli-catalog-sheet.csv) — spreadsheet-import variant
  (intra-cell newlines flattened)

### Coverage

| Season | Lectures | Notes |
|---|---|---|
| Fall 2018 | 5 | Distinguished only; descriptions not printed in this edition |
| Fall 2020 | 15 | COVID era — includes Election 2020 + COVID-19 urgent series |
| Winter/Spring 2021 | 13 | COVID era — includes Medical Ethics 101 |
| Fall 2022 | 5 | From a press proof |
| Winter/Spring 2023 → Fall 2025 | 73 | Six consecutive seasons |
| Winter 2026 | 17 | Individually-printed Thursday lectures |

Thursday series before Winter 2024 were printed as series *themes* only — individual Thursday
speakers for those seasons were never published in the brochures.

## Stack

Python 3 + poppler `pdftotext` (the brochures have native text layers — no OCR), pytest,
vanilla HTML/CSS/JS. No frameworks, no build system, no external services.
