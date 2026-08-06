#!/usr/bin/env python3
"""Render olli-catalog-full.json into a single self-contained HTML page.

The shareable OLLI lecture catalog page (BLC-002/003 delivery). Usage:
    python3 to_html.py data/olli-catalog-full.json out.html
"""
import html
import json
import sys
from collections import defaultdict

SERIES_TYPE_LABELS = {
    "tuesday-distinguished": "Tuesday Distinguished Lecture",
    "thursday-series": "Thursday Lecture Series",
    "special": "Special Series",
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def fmt_date(iso):
    if not iso or len(iso) < 10:
        return iso or ""
    y, m, d = iso[:10].split("-")
    return f"{MONTHS[int(m) - 1]} {int(d)}, {y}"


def esc(s):
    return html.escape(s or "", quote=True)


def lecture_html(r, idx):
    stype = r.get("series_type", "special")
    speaker_bits = []
    if r.get("speaker"):
        speaker_bits.append(f'<span class="speaker-name">{esc(r["speaker"])}</span>')
    if r.get("speaker_title"):
        speaker_bits.append(esc(r["speaker_title"]))
    if r.get("institution"):
        speaker_bits.append(f'<span class="institution">{esc(r["institution"])}</span>')
    speaker_line = " · ".join(speaker_bits) if speaker_bits else '<span class="muted">Speaker not listed in the catalog</span>'
    note = f'<p class="note">{esc(r["notes"])}</p>' if r.get("notes") else ""
    bio = ""
    if r.get("speaker_bio"):
        bio = (f'<details class="bio"><summary>About the speaker</summary>'
               f'<p>{esc(r["speaker_bio"])}</p></details>')
    series_name = r.get("series_name") or ""
    search_blob = " ".join([r.get("title", ""), r.get("speaker") or "", r.get("institution") or "",
                            r.get("description", ""), r.get("speaker_bio") or "",
                            series_name]).lower()
    return f'''
<article class="lecture" data-stype="{esc(stype)}" data-search="{esc(search_blob)}">
  <p class="entry-meta"><span class="chip chip-{esc(stype)}">{esc(SERIES_TYPE_LABELS.get(stype, stype))}</span>
     <time>{esc(fmt_date(r.get("date")))}</time>{" · " + esc(r["time"]) if r.get("time") else ""}</p>
  <h4 class="lecture-title">{esc(r.get("title"))}</h4>
  <p class="speaker">{speaker_line}</p>
  <p class="desc">{esc(r.get("description"))}</p>
  {bio}{note}
</article>'''


def series_html(r):
    printed = r.get("individual_lectures_printed", False)
    tail = "" if printed else '<p class="note">Individual speakers for this series were not printed in the catalogs.</p>'
    note = f'<p class="note">{esc(r["notes"])}</p>' if r.get("notes") else ""
    search_blob = " ".join([r.get("title", ""), r.get("description", "")]).lower()
    return f'''
<article class="series" data-stype="{esc(r.get("series_type", ""))}" data-search="{esc(search_blob)}">
  <p class="entry-meta"><span class="series-label">Series theme</span> <time>{esc(r.get("date_range"))}</time></p>
  <h4 class="series-title">{esc(r.get("title"))}</h4>
  <p class="desc">{esc(r.get("description"))}</p>
  {tail}{note}
</article>'''


def build(json_path, out_path):
    data = json.load(open(json_path))
    # display newest season first (the JSON stores editions oldest -> newest)
    editions = list(reversed(data["editions"]))
    by_edition = defaultdict(list)
    for r in data["records"]:
        by_edition[r["edition"]].append(r)

    n_lectures = sum(1 for r in data["records"] if r["type"] == "lecture")
    n_series = sum(1 for r in data["records"] if r["type"] == "series")

    season_opts = "".join(f'<option value="{esc(e)}">{esc(e)}</option>' for e in editions)
    sections = []
    for e in editions:
        recs = by_edition[e]
        lecs = [r for r in recs if r["type"] == "lecture"]
        sers = [r for r in recs if r["type"] == "series"]
        body = "".join(series_html(r) for r in sers) + "".join(
            lecture_html(r, i) for i, r in enumerate(lecs))
        sections.append(f'''
<section class="season" data-season="{esc(e)}">
  <header class="season-head">
    <h3>{esc(e)}</h3>
    <p class="season-count">{len(lecs)} lecture{"s" if len(lecs) != 1 else ""} · {len(sers)} series theme{"s" if len(sers) != 1 else ""}</p>
  </header>
  {body}
</section>''')

    gaps = "".join(f"<li>{esc(g)}</li>" for g in data.get("gaps", []))

    n_editions = len(editions)
    page = f'''<title>OLLI Lectures 2017–2026 · Unofficial Catalog</title>
<style>
:root {{
  --paper: #FAF9F5; --card: #FFFFFF; --ink: #22271F; --ink-soft: #565E52;
  --line: #E2E1D8; --accent: #1E6B52; --accent-ink: #FFFFFF;
  --amber: #8A5E1A; --amber-bg: #F5EDDD; --green-bg: #E4EFE7; --slate: #525C6B; --slate-bg: #E7EAEF;
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}}
@media (prefers-color-scheme: dark) {{ :root {{
  --paper: #171B18; --card: #1F241F; --ink: #E9EBE4; --ink-soft: #A9B2A4;
  --line: #333B33; --accent: #56B892; --accent-ink: #10241C;
  --amber: #D9A960; --amber-bg: #33291A; --green-bg: #1E332A; --slate: #A9B4C4; --slate-bg: #252B34;
}} }}
:root[data-theme="light"] {{
  --paper: #FAF9F5; --card: #FFFFFF; --ink: #22271F; --ink-soft: #565E52;
  --line: #E2E1D8; --accent: #1E6B52; --accent-ink: #FFFFFF;
  --amber: #8A5E1A; --amber-bg: #F5EDDD; --green-bg: #E4EFE7; --slate: #525C6B; --slate-bg: #E7EAEF;
}}
:root[data-theme="dark"] {{
  --paper: #171B18; --card: #1F241F; --ink: #E9EBE4; --ink-soft: #A9B2A4;
  --line: #333B33; --accent: #56B892; --accent-ink: #10241C;
  --amber: #D9A960; --amber-bg: #33291A; --green-bg: #1E332A; --slate: #A9B4C4; --slate-bg: #252B34;
}}
html {{ font-size: 18px; }}
body {{ background: var(--paper); color: var(--ink); font-family: var(--serif);
  line-height: 1.6; margin: 0; }}
.wrap {{ max-width: 46rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }}
.eyebrow {{ font-family: var(--sans); font-size: .72rem; letter-spacing: .12em;
  text-transform: uppercase; color: var(--ink-soft); margin: 0 0 .75rem; }}
h1 {{ font-size: 2.1rem; line-height: 1.2; margin: 0 0 .5rem; text-wrap: balance; font-weight: 600; }}
.lede {{ font-size: 1.02rem; color: var(--ink-soft); margin: 0 0 .4rem; }}
.stats {{ font-family: var(--sans); font-size: .85rem; color: var(--accent);
  font-weight: 600; margin: .8rem 0 0; font-variant-numeric: tabular-nums; }}
.controls {{ font-family: var(--sans); margin: 1.6rem 0 .4rem; display: flex;
  flex-direction: column; gap: .7rem; }}
.search-row {{ display: flex; gap: .6rem; flex-wrap: wrap; }}
#q {{ flex: 1 1 14rem; font: inherit; font-size: 1rem; padding: .55rem .8rem;
  border: 1.5px solid var(--line); border-radius: 6px; background: var(--card); color: var(--ink); }}
#q:focus, select:focus, button:focus {{ outline: 2.5px solid var(--accent); outline-offset: 1px; }}
select {{ font: inherit; font-size: .95rem; padding: .5rem .6rem; border: 1.5px solid var(--line);
  border-radius: 6px; background: var(--card); color: var(--ink); }}
.chips {{ display: flex; gap: .45rem; flex-wrap: wrap; }}
.chips button {{ font: inherit; font-size: .85rem; padding: .38rem .8rem; border-radius: 99px;
  border: 1.5px solid var(--line); background: var(--card); color: var(--ink); cursor: pointer; }}
.chips button[aria-pressed="true"] {{ background: var(--accent); color: var(--accent-ink);
  border-color: var(--accent); font-weight: 600; }}
.count {{ font-family: var(--sans); font-size: .85rem; color: var(--ink-soft);
  margin: .3rem 0 0; font-variant-numeric: tabular-nums; }}
.season {{ margin-top: 2.6rem; }}
.season-head {{ border-bottom: 2px solid var(--accent); padding-bottom: .35rem; margin-bottom: 1.1rem;
  display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }}
.season-head h3 {{ font-size: 1.45rem; margin: 0; font-weight: 600; }}
.season-count {{ font-family: var(--sans); font-size: .8rem; color: var(--ink-soft); margin: 0; }}
.lecture, .series {{ padding: 1.05rem 0 1.15rem; border-bottom: 1px solid var(--line); }}
.series {{ background: var(--green-bg); border: none; border-radius: 8px;
  padding: 1rem 1.1rem; margin: 0 0 1.1rem; }}
.entry-meta {{ font-family: var(--sans); font-size: .8rem; color: var(--ink-soft);
  margin: 0 0 .35rem; display: flex; align-items: center; gap: .55rem; flex-wrap: wrap; }}
.chip {{ font-size: .7rem; font-weight: 600; letter-spacing: .04em; padding: .18rem .55rem;
  border-radius: 4px; }}
.chip-tuesday-distinguished {{ background: var(--green-bg); color: var(--accent); }}
.chip-thursday-series {{ background: var(--amber-bg); color: var(--amber); }}
.chip-special {{ background: var(--slate-bg); color: var(--slate); }}
.series-label {{ font-size: .7rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
  color: var(--accent); }}
.lecture-title, .series-title {{ font-size: 1.18rem; margin: 0 0 .3rem; line-height: 1.35;
  font-weight: 600; text-wrap: balance; }}
.speaker {{ font-family: var(--sans); font-size: .92rem; margin: 0 0 .45rem; color: var(--ink); }}
.speaker-name {{ font-weight: 650; }}
.institution {{ color: var(--accent); font-weight: 600; }}
.desc {{ margin: 0; font-size: .98rem; }}
.note {{ font-family: var(--sans); font-size: .82rem; color: var(--ink-soft);
  margin: .5rem 0 0; font-style: italic; }}
.bio {{ margin: .5rem 0 0; }}
.bio summary {{ font-family: var(--sans); font-size: .85rem; font-weight: 600;
  color: var(--accent); cursor: pointer; }}
.bio summary:focus {{ outline: 2.5px solid var(--accent); outline-offset: 1px; }}
.bio p {{ font-size: .92rem; color: var(--ink-soft); margin: .35rem 0 0; }}
.muted {{ color: var(--ink-soft); }}
.no-results {{ display: none; font-family: var(--sans); color: var(--ink-soft);
  text-align: center; padding: 3rem 0; }}
.about {{ margin-top: 3.2rem; border-top: 2px solid var(--line); padding-top: 1.4rem;
  font-size: .92rem; color: var(--ink-soft); }}
.about h3 {{ font-size: 1.05rem; color: var(--ink); margin: 0 0 .5rem; }}
.about ul {{ padding-left: 1.2rem; margin: .4rem 0; }}
.about li {{ margin: .3rem 0; }}
footer {{ margin-top: 2rem; font-family: var(--sans); font-size: .8rem; color: var(--ink-soft); }}
@media (prefers-reduced-motion: no-preference) {{
  .chips button {{ transition: background .15s ease; }}
}}
</style>
<div class="wrap">
<header>
  <p class="eyebrow">Unofficial index · compiled from the published catalog brochures</p>
  <h1>OLLI Lectures, 2017–2026</h1>
  <p class="lede">Every lecture from {n_editions} seasons of the Osher Lifelong Learning Institute at the
  University of Michigan — the Distinguished Lecture Series, the Thursday Lecture Series,
  and special series — searchable by speaker, topic, or season.</p>
  <p class="stats">{n_lectures} lectures · {n_series} series themes · {n_editions} catalog seasons</p>
</header>

<div class="controls">
  <div class="search-row">
    <input id="q" type="search" placeholder="Search titles, speakers, topics…"
      aria-label="Search lectures">
    <select id="season" aria-label="Filter by season">
      <option value="">All seasons</option>{season_opts}
    </select>
  </div>
  <div class="chips" role="group" aria-label="Filter by series type">
    <button data-f="" aria-pressed="true">All types</button>
    <button data-f="tuesday-distinguished" aria-pressed="false">Tuesday Distinguished</button>
    <button data-f="thursday-series" aria-pressed="false">Thursday Series</button>
    <button data-f="special" aria-pressed="false">Special Series</button>
  </div>
  <p class="count" id="count" aria-live="polite"></p>
</div>

<main>
{"".join(sections)}
<p class="no-results" id="empty">No lectures match — try fewer words, or clear the filters.</p>
</main>

<section class="about">
  <h3>About this catalog</h3>
  <p>Compiled from the printed OLLI-UM catalog brochures (Fall 2018 through Winter 2026;
  not every season's brochure has surfaced yet), supplemented with OLLI's lecture-announcement
  emails (2017–2026), which fill in individual talks for seasons whose brochures listed only
  series themes — and for seasons with no surviving brochure at all. Email-sourced entries say
  so in their notes. Some entries note misprints found in the sources. Known gaps:</p>
  <ul>{gaps}</ul>
</section>

<footer>
  <p>An unofficial index, compiled from OLLI at U-M's published catalog brochures. Not affiliated
  with or endorsed by the Osher Lifelong Learning Institute or the University of Michigan.
  Lecture descriptions are the catalog authors' own text. Corrections welcome.</p>
</footer>
</div>

<script>
(function () {{
  var q = document.getElementById("q"), season = document.getElementById("season"),
      chips = document.querySelectorAll(".chips button"), count = document.getElementById("count"),
      empty = document.getElementById("empty"), stype = "";
  var entries = Array.prototype.slice.call(document.querySelectorAll(".lecture, .series"));
  var seasons = Array.prototype.slice.call(document.querySelectorAll(".season"));

  function apply() {{
    var terms = q.value.toLowerCase().split(/\\s+/).filter(Boolean);
    var shownLectures = 0;
    entries.forEach(function (el) {{
      var blob = el.getAttribute("data-search");
      var okType = !stype || el.getAttribute("data-stype") === stype;
      var okText = terms.every(function (t) {{ return blob.indexOf(t) !== -1; }});
      var show = okType && okText;
      el.style.display = show ? "" : "none";
      if (show && el.classList.contains("lecture")) shownLectures++;
    }});
    seasons.forEach(function (s) {{
      var okSeason = !season.value || s.getAttribute("data-season") === season.value;
      var any = okSeason && entries.some(function (el) {{
        return s.contains(el) && el.style.display !== "none";
      }});
      s.style.display = any ? "" : "none";
    }});
    var visibleSeasons = seasons.filter(function (s) {{ return s.style.display !== "none"; }});
    // recount lectures inside visible seasons only
    shownLectures = 0;
    visibleSeasons.forEach(function (s) {{
      shownLectures += s.querySelectorAll('.lecture:not([style*="none"])').length;
    }});
    count.textContent = "Showing " + shownLectures + " of {n_lectures} lectures";
    empty.style.display = visibleSeasons.length ? "none" : "block";
  }}

  q.addEventListener("input", apply);
  season.addEventListener("change", apply);
  chips.forEach(function (b) {{
    b.addEventListener("click", function () {{
      stype = b.getAttribute("data-f");
      chips.forEach(function (o) {{ o.setAttribute("aria-pressed", o === b ? "true" : "false"); }});
      apply();
    }});
  }});
  apply();
}})();
</script>
'''
    with open(out_path, "w") as f:
        f.write(page)
    print(f"wrote {out_path} ({len(page)} bytes)")


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
