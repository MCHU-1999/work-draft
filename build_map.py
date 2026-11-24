#!/usr/bin/env python3
"""
build_map.py — read the Change Detection Notion database and build a local
literature-map page (gap matrix, distributions, untagged list).

Usage
-----
    export NOTION_TOKEN="ntn_..."        # or put NOTION_TOKEN=... in a .env file
    python build_map.py                  # writes ./out/index.html and ./out/papers.json
    python build_map.py --open           # ...and opens the page in your browser
    python build_map.py --from-json out/papers.json   # rebuild the page offline

Only the Python standard library is used (3.8+).

The integration behind the token needs "Read content" and must be connected to
the database (database menu -> Connections). Keep the token out of git.
"""

import argparse
import datetime as dt
import html
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import webbrowser

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DATA_SOURCE_ID = "0d44d8a9-b221-8260-9995-07bff60f4523"   # collection:// id
DATABASE_ID = "4214d8a9b2218160b580d16fada99a02"          # fallback for old API

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"          # data-source API
LEGACY_VERSION = "2022-06-28"          # database-query API (fallback)

# Dimension -> value order shown on the page. Values found in Notion that are
# not listed here are appended automatically, so new tags never get lost.
DIMS = {
    "Input": [
        "Image → Image", "Image + Semantic map → Image", "Footprints → Image",
        "DSM → Image", "LoD2 → Stereo pair", "Multi-view → Multi-view",
        "GS → Multi-view",
    ],
    "Decision unit": ["Pixel", "Object", "Building", "Roof face", "3D primitive"],
    "Output level": ["Binary only", "1 Type", "2 Mask", "3 Footprint", "4 Height", "5 3D"],
    "Core": ["Rule-based", "ML", "DL", "NeRF/GS"],
}
OUTPUT_DIM = "Output level"
BINARY_ONLY = "Binary only"


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #
def load_token() -> str:
    token = os.environ.get("NOTION_TOKEN")
    if token:
        return token.strip()
    for candidate in (pathlib.Path.cwd() / ".env", pathlib.Path(__file__).with_name(".env")):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("NOTION_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("No token: set NOTION_TOKEN or add NOTION_TOKEN=... to a .env file.")


# --------------------------------------------------------------------------- #
# Notion API (stdlib HTTP)
# --------------------------------------------------------------------------- #
class NotionError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def post(url: str, token: str, version: str, payload: dict, retries: int = 3) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Notion-Version": version,
        "Content-Type": "application/json",
    })
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < retries:            # rate limited
                wait = float(e.headers.get("Retry-After", "1"))
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < retries:            # transient
                time.sleep(1.5 * (attempt + 1))
                continue
            raise NotionError(e.code, body) from None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            sys.exit(f"Network error reaching Notion: {e.reason}")
    raise RuntimeError("unreachable")


def query_all(token: str) -> list:
    """Return all pages of the data source, handling pagination."""
    attempts = [
        (f"{NOTION_API}/data_sources/{DATA_SOURCE_ID}/query", NOTION_VERSION),
        (f"{NOTION_API}/databases/{DATABASE_ID}/query", LEGACY_VERSION),
    ]
    last_error = None
    for url, version in attempts:
        pages, cursor = [], None
        try:
            while True:
                payload = {"page_size": 100}
                if cursor:
                    payload["start_cursor"] = cursor
                res = post(url, token, version, payload)
                pages.extend(res.get("results", []))
                if not res.get("has_more"):
                    return pages
                cursor = res.get("next_cursor")
        except NotionError as e:
            last_error = e
            if e.status == 401:
                sys.exit("Notion rejected the token (401). Check NOTION_TOKEN.")
            if e.status in (400, 404):
                continue                                   # try the fallback endpoint
            sys.exit(f"Notion error: {e}")
    msg = str(last_error)
    if last_error is not None and last_error.status == 404:
        msg = ("Database not found (404). Connect your integration to the database: "
               "open it in Notion -> ••• -> Connections -> add your integration.")
    sys.exit(msg)


# --------------------------------------------------------------------------- #
# Property parsing
# --------------------------------------------------------------------------- #
def plain(rich: list) -> str:
    return "".join(r.get("plain_text", "") for r in rich or [])


def prop_values(prop: dict) -> list:
    """Return a property's value(s) as a list of strings."""
    if not prop:
        return []
    t = prop.get("type")
    v = prop.get(t)
    if t == "multi_select":
        return [o["name"] for o in v or []]
    if t in ("select", "status"):
        return [v["name"]] if v else []
    if t in ("title", "rich_text"):
        s = plain(v)
        return [s] if s else []
    if t == "number":
        return [] if v is None else [v]
    if t == "url":
        return [v] if v else []
    return []


def first(prop: dict, default=None):
    vals = prop_values(prop)
    return vals[0] if vals else default


def to_paper(page: dict) -> dict:
    props = page.get("properties", {})
    title = first(props.get("Short Title")) or first(props.get("Name")) or "Untitled"
    year = first(props.get("Year"))
    paper = {
        "title": title,
        "year": int(year) if isinstance(year, (int, float)) else None,
        "url": page.get("url"),
        "fullTitle": first(props.get("Title"), ""),
    }
    for dim in DIMS:
        paper[dim] = [str(x) for x in prop_values(props.get(dim))]
    others = [d for d in DIMS if d != OUTPUT_DIM]
    if not paper[OUTPUT_DIM] and any(paper[d] for d in others):
        paper[OUTPUT_DIM] = [BINARY_ONLY]          # every paper is at least binary CD
    return paper


# --------------------------------------------------------------------------- #
# Page template
# --------------------------------------------------------------------------- #
TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Change detection literature map</title>
<style>
:root{
  --ign-dark:#7AA43E;--ign-primary:#90C149;--ign-secondary:#B6D688;--ign-light:#EEF5E3;
  --bg:#fafaf8;--card:#fff;--ink:#1d1d1b;--muted:#6b6a65;--faint:#9a988f;--line:#e6e4dc;--gap:#f1efe8;
  --chip:var(--ign-light);--chip-line:var(--ign-secondary);--chip-ink:#1d1d1b;
  --accent:var(--ign-dark);--bar:var(--ign-primary);--track:var(--ign-light);
}
@media (prefers-color-scheme: dark){
  :root{--bg:#161615;--card:#1f1f1e;--ink:#ecebe6;--muted:#a3a198;--faint:#77756d;--line:#33322f;--gap:#262624;
  --chip:rgba(144,193,73,.18);--chip-line:rgba(182,214,136,.45);--chip-ink:#EEF5E3;
  --accent:var(--ign-secondary);--track:rgba(144,193,73,.14);}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;font-weight:600;margin:0 0 4px;padding-left:12px;border-left:4px solid var(--ign-primary)}
h2{font-size:16px;font-weight:600;margin:0 0 12px}
.sub{color:var(--muted);font-size:13px;margin:0 0 20px}
select{font:inherit;font-size:13px;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 10px}
select:focus{outline:2px solid var(--ign-primary);outline-offset:1px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--ign-primary);border-radius:12px;padding:12px 14px}
.kpi .l{font-size:12px;color:var(--muted)}.kpi .v{font-size:24px;font-weight:600}
.ctl{display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:13px;color:var(--muted);margin-bottom:12px}
.mwrap{overflow-x:auto}
table.m{border-collapse:collapse;width:100%;font-size:12px;min-width:560px}
table.m th{font-weight:600;color:var(--muted);text-align:left;padding:6px 8px;vertical-align:bottom}
table.m th.r{white-space:nowrap}
table.m td{border:1px solid var(--line);padding:6px;vertical-align:top;min-width:90px;height:42px}
table.m td.gap{background:var(--gap)}
table.m td .n{float:right;color:var(--faint);font-size:11px}
.pill{display:inline-block;background:var(--chip);color:var(--chip-ink);border:1px solid var(--chip-line);border-radius:6px;padding:1px 7px;margin:2px 3px 0 0;font-size:11.5px;text-decoration:none}
a.pill:hover{outline:1px solid var(--accent)}
.dists{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.dist h3{font-size:13px;font-weight:600;margin:0 0 8px}
.row{display:grid;grid-template-columns:140px 1fr 24px;gap:8px;align-items:center;font-size:12px;margin:3px 0}
.row .lab{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track{background:var(--track);border-radius:4px;height:10px;overflow:hidden}
.fill{background:var(--bar);height:100%}
.row .c{text-align:right;color:var(--muted)}
.row.zero .lab{color:var(--faint)}
ul.untag{margin:0;padding-left:18px;font-size:13px}
.note{font-size:12px;color:var(--faint)}
</style>
</head>
<body>
<main>
  <h1>Change detection literature map</h1>
  <p class="sub">__SOURCE__</p>
  <div class="kpis" id="kpis"></div>
  <section class="card">
    <h2>Gap matrix</h2>
    <div class="ctl">
      <label>Rows <select id="ry"></select></label>
      <label>Columns <select id="cx"></select></label>
      <span id="stat"></span>
    </div>
    <div class="mwrap" id="matrix"></div>
    <p class="note">Grey cells: no tagged paper covers this combination. Every paper outputs binary change; "Binary only" means nothing beyond that. Mask = building extent at t1 as raster; Footprint = vector polygons only; Height = DSM or height difference; 3D = planes, Gaussians, points, polyhedra.</p>
  </section>
  <section class="card">
    <h2>Distribution per dimension</h2>
    <div class="dists" id="dists"></div>
  </section>
  <section class="card">
    <h2>Not yet tagged</h2>
    <div id="untagged"></div>
  </section>
</main>
<script>
const DIMS = __DIMS__;
const papers = __PAPERS__;
const OUT = "Output level";
const K = Object.keys(DIMS);
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const values = d => { const v = DIMS[d].slice(); papers.forEach(p=>p[d].forEach(x=>{ if(!v.includes(x)) v.push(x); })); return v; };
const core = p => K.filter(k=>k!==OUT);
const tagged = () => papers.filter(p=>K.some(k=>k!==OUT && p[k].length));
const pill = p => { const t = esc(p.title), tip = esc(p.fullTitle||p.title);
  return p.url ? '<a class="pill" title="'+tip+'" href="'+esc(p.url)+'" target="_blank" rel="noopener">'+t+'</a>' : '<span class="pill" title="'+tip+'">'+t+'</span>'; };

function kpis(){
  const t = tagged(), others = K.filter(k=>k!==OUT);
  const full = t.filter(p=>others.every(k=>p[k].length)).length;
  const c3d = t.filter(p=>p[OUT].includes("5 3D")).length;
  $("kpis").innerHTML = [["Papers in database",papers.length],["Tagged",t.length],["Input, unit and core filled",full],["Output 3D",c3d]]
    .map(c=>'<div class="kpi"><div class="l">'+c[0]+'</div><div class="v">'+c[1]+'</div></div>').join("");
}
function matrix(){
  const rd=$("ry").value, cd=$("cx").value, R=values(rd), C=values(cd), T=tagged(); let empty=0;
  let h='<table class="m"><tr><th></th>'+C.map(c=>'<th>'+esc(c)+'</th>').join("")+'</tr>';
  R.forEach(r=>{ h+='<tr><th class="r">'+esc(r)+'</th>';
    C.forEach(c=>{ const hit = T.filter(p=>p[rd].includes(r) && p[cd].includes(c) && (rd!==cd || r===c));
      if(!hit.length) empty++;
      h+='<td class="'+(hit.length?'':'gap')+'">'+(hit.length?'<span class="n">'+hit.length+'</span>':'')+hit.map(pill).join("")+'</td>'; });
    h+='</tr>'; });
  $("matrix").innerHTML = h+'</table>';
  $("stat").textContent = empty+" of "+(R.length*C.length)+" cells empty";
}
function dists(){
  const T=tagged();
  $("dists").innerHTML = K.map(d=>{ const vs=values(d), n=vs.map(v=>T.filter(p=>p[d].includes(v)).length), mx=Math.max(1,...n);
    return '<div class="dist"><h3>'+esc(d)+'</h3>'+vs.map((v,i)=>'<div class="row'+(n[i]?'':' zero')+'"><span class="lab" title="'+esc(v)+'">'+esc(v)+'</span><div class="track"><div class="fill" style="width:'+Math.round(100*n[i]/mx)+'%"></div></div><span class="c">'+n[i]+'</span></div>').join("")+'</div>'; }).join("");
}
function untagged(){
  const others = K.filter(k=>k!==OUT);
  const u = papers.filter(p=>!others.some(k=>p[k].length));
  const part = papers.filter(p=>others.some(k=>p[k].length) && !others.every(k=>p[k].length));
  let h = u.length ? '<ul class="untag">'+u.map(p=>'<li>'+pill(p)+(p.year?' <span class="note">'+p.year+'</span>':'')+'</li>').join("")+'</ul>' : '<p class="note">Every paper has at least one tag.</p>';
  if(part.length) h += '<p class="note" style="margin-top:10px">Partly tagged: '+part.map(p=>esc(p.title)+' (missing '+others.filter(k=>!p[k].length).join(", ")+')').join("; ")+'</p>';
  $("untagged").innerHTML = h;
}
K.forEach(k=>{ $("ry").add(new Option(k,k)); $("cx").add(new Option(k,k)); });
$("ry").value="Input"; $("cx").value="Output level";
$("ry").onchange=matrix; $("cx").onchange=matrix;
kpis(); matrix(); dists(); untagged();
</script>
</body>
</html>
"""


def render(papers: list, source_note: str) -> str:
    def js(obj):  # safe to embed inside <script>
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    return (TEMPLATE
            .replace("__SOURCE__", html.escape(source_note))
            .replace("__DIMS__", js(DIMS))
            .replace("__PAPERS__", js(papers)))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Build the change-detection literature map from Notion.")
    ap.add_argument("--out", default="out", help="output folder (default: ./out)")
    ap.add_argument("--from-json", metavar="FILE", help="skip Notion; rebuild from a saved papers.json")
    ap.add_argument("--open", action="store_true", help="open the page in your browser when done")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().strftime("%d %b %Y, %H:%M")

    if args.from_json:
        papers = json.loads(pathlib.Path(args.from_json).read_text(encoding="utf-8"))
        note = f"Built {now} from {args.from_json} ({len(papers)} papers)"
    else:
        pages = query_all(load_token())
        papers = sorted((to_paper(p) for p in pages), key=lambda p: (p["year"] or 0, p["title"]))
        (out / "papers.json").write_text(json.dumps(papers, ensure_ascii=False, indent=1), encoding="utf-8")
        note = f"From Notion · built {now} · {len(papers)} papers"

    page = out / "index.html"
    page.write_text(render(papers, note), encoding="utf-8")

    tagged = sum(1 for p in papers if any(p[d] for d in DIMS if d != OUTPUT_DIM))
    print(f"{len(papers)} papers ({tagged} tagged) -> {page.resolve()}")
    if args.open:
        webbrowser.open(page.resolve().as_uri())


if __name__ == "__main__":
    main()