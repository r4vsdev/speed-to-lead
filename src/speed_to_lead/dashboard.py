"""Stats page: plain SQL over the lead log, rendered as one HTML page (no JS framework, no chart library)."""

import html
import sqlite3

# Median and p95 without extensions: rank rows with window functions, pick the row at the percentile.
SPEED_SQL = """
with ranked as (
  select {col} as v,
         row_number() over (order by {col}) as rn,
         count(*)     over ()              as n
  from leads where {col} is not null and status = 'done'
)
select max(case when rn = (n + 1) / 2              then v end) as median,
       max(case when rn = max(1, (n * 95 + 99) / 100) then v end) as p95
from ranked
"""

KPI_SQL = """
select count(*)                                   as leads,
       sum(status = 'done')                       as done,
       sum(status = 'failed')                     as failed,
       sum(grade = 'A')                           as hot,
       round(avg(score), 0)                       as avg_score
from leads
"""

# One row per day for the last 14 days, including days with no leads (recursive CTE builds the calendar).
DAILY_SQL = """
with recursive days(d) as (
  select date('now', 'localtime', '-13 days')
  union all select date(d, '+1 day') from days where d < date('now', 'localtime')
)
select d                          as day,
       count(l.key)               as leads,
       coalesce(sum(l.grade = 'A'), 0) as hot
from days
left join leads l on date(l.received_at, 'unixepoch', 'localtime') = d
group by d order by d
"""

OWNER_SQL = """
select owner,
       count(*)                     as leads,
       sum(grade = 'A')             as hot,
       round(avg(score), 0)         as avg_score
from leads where owner is not null
group by owner order by leads desc
"""

GRADE_SQL = """
select grade, count(*) as leads, round(100.0 * count(*) / sum(count(*)) over (), 0) as pct
from leads where grade is not null
group by grade
order by case grade when 'A' then 1 when 'B' then 2 when 'C' then 3 else 4 end
"""

SOURCE_SQL = """
select coalesce(nullif(source, ''), 'unknown') as source,
       count(*)                      as leads,
       round(avg(score), 0)          as avg_score,
       round(100.0 * sum(grade = 'A') / count(*), 0) as hot_pct
from leads group by 1 order by leads desc limit 8
"""

INDUSTRY_SQL = """
select industry, count(*) as leads, round(avg(score), 0) as avg_score
from leads where industry is not null
group by industry order by leads desc limit 8
"""

RECENT_SQL = """
select strftime('%m-%d %H:%M', received_at, 'unixepoch', 'localtime') as received, domain, source, grade, score,
       owner, printf('%.1f s', ms / 1000.0) as to_crm, printf('%.1f s', qualified_ms / 1000.0) as routed, status
from leads order by received_at desc limit 12
"""


def collect(db: sqlite3.Connection) -> dict:
    q = lambda sql: [dict(r) for r in db.execute(sql)]  # noqa: E731
    return {
        "kpi": q(KPI_SQL)[0],
        "crm_speed": q(SPEED_SQL.format(col="ms"))[0],
        "qualified_speed": q(SPEED_SQL.format(col="qualified_ms"))[0],
        "daily": q(DAILY_SQL),
        "owners": q(OWNER_SQL),
        "grades": q(GRADE_SQL),
        "sources": q(SOURCE_SQL),
        "industries": q(INDUSTRY_SQL),
        "recent": q(RECENT_SQL),
    }


def num(v) -> str:
    return "—" if v is None else f"{v:.0f}"


def secs(ms) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} s"


def bars(rows: list[dict], label: str, value: str, note=None) -> str:
    """Horizontal bars, one series. Value text sits beside the bar in text ink."""
    top = max((r[value] or 0 for r in rows), default=0) or 1
    out = []
    for r in rows:
        v = r[value] or 0
        extra = f" · {html.escape(str(note(r)))}" if note else ""
        out.append(f'<div class="hrow" title="{html.escape(str(r[label]))}: {v}{extra}">'
                   f'<span class="hlabel">{html.escape(str(r[label]))}</span>'
                   f'<span class="htrack"><span class="hbar" style="width:{100 * v / top:.1f}%"></span></span>'
                   f'<span class="hval">{v}<small>{extra}</small></span></div>')
    return "".join(out) or '<p class="empty">No data yet</p>'


def columns(daily: list[dict]) -> str:
    top = max((d["leads"] for d in daily), default=0) or 1
    cols = []
    for d in daily:
        h = 100 * d["leads"] / top
        tip = f'{d["day"]}: {d["leads"]} leads, {d["hot"]} hot'
        cols.append(f'<div class="col" title="{tip}" tabindex="0" aria-label="{tip}">'
                    f'<span class="cbar" style="height:{h:.1f}%"></span>'
                    f'<span class="cday">{d["day"][8:]}</span></div>')
    return "".join(cols)


def table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">No leads yet</p>'
    head = "".join(f"<th>{html.escape(k)}</th>" for k in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{html.escape('' if v is None else str(v))}</td>" for v in r.values())
                   + "</tr>" for r in rows)
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render(data: dict, crm: str) -> str:
    k, cs, qs = data["kpi"], data["crm_speed"], data["qualified_speed"]
    total = k["leads"] or 0
    tiles = [
        ("Leads", f'{total}', f'{k["failed"] or 0} failed'),
        ("Form → CRM", secs(cs["median"]), f'median · p95 {secs(cs["p95"])}'),
        ("Scored & routed", secs(qs["median"]), f'median · p95 {secs(qs["p95"])}'),
        ("Hot leads (A)", f'{k["hot"] or 0}', f'avg score {num(k["avg_score"])}'),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="tlabel">{t}</div><div class="tval">{v}</div>'
                         f'<div class="tnote">{n}</div></div>' for t, v, n in tiles)
    return PAGE.format(
        crm=html.escape(crm), tiles=tiles_html, daily=columns(data["daily"]),
        grades=bars(data["grades"], "grade", "leads", lambda r: f'{num(r["pct"])}%'),
        owners=bars(data["owners"], "owner", "leads", lambda r: f'{r["hot"] or 0} hot · avg {num(r["avg_score"])}'),
        sources=bars(data["sources"], "source", "leads", lambda r: f'{num(r["hot_pct"])}% hot'),
        industries=bars(data["industries"], "industry", "leads", lambda r: f'avg score {num(r["avg_score"])}'),
        recent=table(data["recent"]),
        daily_table=table(data["daily"]),
    )


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lead stats</title>
<style>
:root {{ color-scheme: light; --surface:#fcfcfb; --card:#ffffff; --border:#e6e5e0; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#8a8984; --series:#2a78d6; --track:#f0efea; }}
@media (prefers-color-scheme: dark) {{ :root {{ color-scheme: dark; --surface:#121211; --card:#1a1a19; --border:#2e2e2c;
  --ink:#ffffff; --ink2:#c3c2b7; --muted:#8f8e86; --series:#3987e5; --track:#262624; }} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--surface); color:var(--ink); font:14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:14px; margin:0 0 12px; color:var(--ink2); font-weight:600; }}
.sub {{ color:var(--ink2); margin:0 0 20px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:12px; }}
.tile, .card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }}
.tlabel {{ color:var(--ink2); font-size:13px; }} .tval {{ font-size:28px; font-weight:650; margin:4px 0 2px; }}
.tnote {{ color:var(--muted); font-size:12px; }}
.grid {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; margin-bottom:12px; }}
.cols {{ display:flex; align-items:flex-end; gap:4px; height:160px; padding-top:8px; }}
.col {{ flex:1; height:100%; display:flex; flex-direction:column; justify-content:flex-end; align-items:center; gap:4px; outline:none; }}
.cbar {{ width:100%; max-width:28px; min-height:2px; background:var(--series); border-radius:4px 4px 0 0; }}
.col:hover .cbar, .col:focus .cbar {{ filter:brightness(1.15); }}
.cday {{ font-size:11px; color:var(--muted); }}
.hrow {{ display:grid; grid-template-columns:minmax(90px, 40%) 1fr auto; gap:8px; align-items:center; margin:6px 0; }}
.hlabel {{ color:var(--ink2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.htrack {{ background:var(--track); border-radius:4px; height:10px; overflow:hidden; }}
.hbar {{ display:block; height:100%; background:var(--series); border-radius:0 4px 4px 0; }}
.hval {{ font-variant-numeric:tabular-nums; }} .hval small {{ color:var(--muted); }}
@media (max-width: 720px) {{ .grid {{ grid-template-columns:1fr; }} }}
.tablewrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; font-variant-numeric:tabular-nums; }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); white-space:nowrap; }}
th {{ color:var(--ink2); font-weight:600; }}
details summary {{ cursor:pointer; color:var(--ink2); margin-top:12px; }}
.empty {{ color:var(--muted); }}
</style></head>
<body><main>
<h1>Lead stats</h1>
<p class="sub">Every number is a SQL query over the lead log · CRM: {crm}</p>
<div class="tiles">{tiles}</div>
<div class="card" style="margin-bottom:12px"><h2>Leads per day, last 14 days</h2><div class="cols">{daily}</div>
<details><summary>Table view</summary>{daily_table}</details></div>
<div class="grid">
<div class="card"><h2>By grade</h2>{grades}</div>
<div class="card"><h2>By owner</h2>{owners}</div>
<div class="card"><h2>By source</h2>{sources}</div>
<div class="card"><h2>By industry (AI)</h2>{industries}</div>
</div>
<div class="card"><h2>Latest leads</h2>{recent}</div>
</main></body></html>"""
