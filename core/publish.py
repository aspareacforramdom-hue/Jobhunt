"""
Render one person's board as a standalone HTML page.

The portal needs a login and a laptop that is awake. This does not: the
page is self-contained, so it can be published once to a permanent URL
and read at any hour with the machine that produced it switched off.

Read and write are decoupled — the scan still runs on the operator's
laptop, but nobody has to be waiting on it to look at the results.

TWO RULES THIS FILE ENFORCES

1. No personal data leaves. The page carries public job listings and
   this tool's reasoning about them. It never carries a CV, a phone
   number, an email, a salary floor, or anything else that would harm
   the person if the URL were forwarded. See `_SAFE_TO_PUBLISH`.
2. No external requests. Published pages run under a strict CSP, so
   there are no font CDNs, no analytics and no remote images. Type is
   built from stacks already present on the reader's machine.
"""

import html
from datetime import datetime, timezone

from core import db, dimensions

# Profile fields allowed onto a public page. Everything omitted is
# omitted deliberately: `angles` is CV-derived, and hard_filters carries
# the salary floor — the single most sensitive number in the profile.
_SAFE_TO_PUBLISH = ("role_family", "seniority")


def _esc(value):
    return html.escape("" if value is None else str(value))


def _score(value):
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "—"


def _href(value):
    """
    Only http(s) links get an anchor.

    Listing URLs are scraped. `_esc` stops one breaking out of the
    attribute, but `javascript:` does not need to break out — a reader
    clicking "Open the listing" is exactly what it is waiting for, and
    this page is meant to be forwarded to people who did not run the
    scan. A non-http URL renders as plain text with no link at all.
    """
    text = str(value or "").strip()
    return _esc(text) if text[:7].lower() in ("http://", "https:/") else ""


def render(display_name, profile, jobs, distribution, note=None):
    """Returns a complete HTML document for one person's board."""
    target = (profile or {}).get("target") or {}
    headline = " · ".join(
        _esc(target.get(k)) for k in _SAFE_TO_PUBLISH if target.get(k))
    weights = (profile or {}).get("weights") or {}
    updated = datetime.now(timezone.utc).astimezone(db.IST)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(display_name)} · roles worth a look</title>
<style>{_CSS}</style>
</head>
<body>
<main class="page">
  <header class="masthead">
    <p class="eyebrow">Roles worth a look</p>
    <h1>{_esc(display_name)}</h1>
    {f'<p class="target">{headline}</p>' if headline else ''}
    <p class="stamp">{len(jobs)} listings · updated
      {updated.strftime('%d %b, %H:%M')} IST</p>
  </header>

  {_note_html(note)}
  {_distribution_html(distribution)}

  <ol class="roles">
    {''.join(_job_html(j, weights) for j in jobs)}
  </ol>

  {_empty_html() if not jobs else ''}

  <footer class="footer">
    <h2>How to read this</h2>
    <p>Scores are relative to this run, not to ten. A 7 on a thin week
      and a 7 on a busy one are not the same thing — read each score
      against the spread above it.</p>
    <p>Every role gets a reasons-against section. If it looks thin, that
      is a prompt to read the listing yourself, not a green light.</p>
    <p>Nothing here has been applied to on your behalf, and nothing will
      be. Every draft is yours to send.</p>
  </footer>
</main>
</body>
</html>"""


def _note_html(note):
    if not note:
        return ""
    return f'<p class="notice">{_esc(note)}</p>'


def _empty_html():
    return """<p class="empty">Nothing cleared the floor this run. That is a
      quiet market, not a broken page — the next scan will add to it.</p>"""


def _distribution_html(dist):
    if not dist or not dist.get("count"):
        return ""
    bars = "".join(
        f'<span class="bar" style="--h:{b["pct"]}%" '
        f'title="{b["count"]} at {b["score"]}"></span>'
        for b in dist["buckets"])
    return f"""
  <section class="spread">
    <div class="bars">{bars}</div>
    <dl class="stats">
      <div><dt>Scored</dt><dd>{dist['count']}</dd></div>
      <div><dt>Median</dt><dd>{dist['median']}</dd></div>
      <div><dt>Top 10%</dt><dd>{dist['p90']}+</dd></div>
    </dl>
  </section>"""


def _job_html(job, weights):
    breakdown = job.get("score_breakdown") or {}
    weakest = dimensions.weakest(breakdown, weights)

    dims = "".join(
        f'<div class="dim{" is-weak" if d == weakest else ""}">'
        f'<span class="dim-name">{_esc(dimensions.label(d))}</span>'
        f'<span class="dim-bar"><i style="--w:{min(100, breakdown[d] * 10):.0f}%"></i></span>'
        f'<span class="dim-num">{breakdown[d]:.0f}</span></div>'
        for d in dimensions.ordered_ids(weights) if d in breakdown)

    against = []
    if job.get("risk"):
        against.append(_esc(job["risk"]))
    if weakest and breakdown.get(weakest, 10) <= 6:
        against.append(
            f"Weakest on {_esc(dimensions.label(weakest).lower())} — "
            f"{breakdown[weakest]:.0f}/10.")

    against_html = ("".join(f"<li>{a}</li>" for a in against)
                    if against else
                    "<li class='muted'>Nothing stood out against this one. "
                    "Read the listing before trusting that.</li>")

    where = " · ".join(filter(None, [_esc(job.get("company")),
                                     _esc(job.get("location"))]))

    return f"""
    <li class="role">
      <p class="score">{_score(job.get('score'))}</p>
      <div class="body">
        <h3>{_esc(job.get('title')) or 'Untitled role'}</h3>
        <p class="where">{where}
          {'<span class="stretch">stretch</span>' if job.get('stretch') else ''}
        </p>
        {f'<p class="fits">{_esc(job.get("angle"))}</p>' if job.get('angle') else ''}
        {f'<div class="dims">{dims}</div>' if dims else ''}
        <div class="against">
          <p class="label">Why it might not</p>
          <ul>{against_html}</ul>
        </div>
        {f'<a class="apply" href="{_href(job.get("url"))}" target="_blank" rel="noopener noreferrer">Open the listing</a>' if _href(job.get('url')) else ''}
      </div>
    </li>"""


def build_for_user(conn, user_id, display_name, limit=15, floor=4.0,
                   note=None):
    # 15 matches the board's daily cap. At 25 the page ran past 20,000
    # pixels, which is not a thing anyone scrolls through on a phone
    # before work.
    """Pull a user's board out of the database and render it."""
    from core import board

    profile = db.load_profile(conn, user_id) or {}
    jobs = [j for j in db.list_user_jobs(conn, user_id, min_score=floor,
                                         limit=limit)
            if j.get("status") != "ignored"]
    return render(display_name, profile, jobs,
                  board.distribution(conn, user_id, floor), note)


# ------------------------------------------------------------
# Cool slate neutrals with a single amber accent. Type comes entirely
# from stacks already installed on the reader's machine — the CSP blocks
# font CDNs, and a silent fallback would be worse than choosing well.
# ------------------------------------------------------------
_CSS = """
:root{
  --ground:#f6f7f9; --surface:#fff; --ink:#0f1319; --ink-2:#454e5c;
  --ink-3:#78828f; --rule:#e1e5ea; --rule-2:#c9d0d9;
  --accent:#9a6206; --accent-soft:#f3ead9; --flag:#9d3f47;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{
    --ground:#111419; --surface:#171b21; --ink:#e9ecf1; --ink-2:#a6aeba;
    --ink-3:#727b87; --rule:#252b33; --rule-2:#39414b;
    --accent:#e2a545; --accent-soft:#2a2318; --flag:#e08f96;
  }
}
:root[data-theme="dark"]{
  --ground:#111419; --surface:#171b21; --ink:#e9ecf1; --ink-2:#a6aeba;
  --ink-3:#727b87; --rule:#252b33; --rule-2:#39414b;
  --accent:#e2a545; --accent-soft:#2a2318; --flag:#e08f96;
}
:root[data-theme="light"]{
  --ground:#f6f7f9; --surface:#fff; --ink:#0f1319; --ink-2:#454e5c;
  --ink-3:#78828f; --rule:#e1e5ea; --rule-2:#c9d0d9;
  --accent:#9a6206; --accent-soft:#f3ead9; --flag:#9d3f47;
}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font-family:var(--serif);font-size:17px;line-height:1.55;
  -webkit-text-size-adjust:100%;font-variant-numeric:tabular-nums}
.page{max-width:44rem;margin:0 auto;padding:2.5rem 1.25rem 4rem}
h1,h2,h3{margin:0;font-weight:600;text-wrap:balance;letter-spacing:-.01em}

.masthead{padding-bottom:1.5rem;border-bottom:2px solid var(--ink)}
.eyebrow,.label,.stamp,.stretch,dt{font-family:var(--mono);
  font-size:.6875rem;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3)}
.masthead h1{font-size:2.1rem;margin:.35rem 0 .2rem}
.target{color:var(--ink-2);font-size:1.0625rem;margin:0}
.stamp{margin-top:.75rem}

.notice{font-family:var(--sans);font-size:.8125rem;line-height:1.5;
  border-left:3px solid var(--accent);background:var(--accent-soft);
  color:var(--ink-2);padding:.75rem .9rem;margin:1.5rem 0 0;border-radius:0 3px 3px 0}

.spread{display:flex;gap:1.5rem;align-items:flex-end;flex-wrap:wrap;
  padding:1.25rem 0;border-bottom:1px solid var(--rule)}
.bars{display:flex;gap:3px;align-items:flex-end;height:44px;flex:1;min-width:180px}
.bar{flex:1;height:var(--h);min-height:2px;background:var(--rule-2);border-radius:1px}
.stats{display:flex;gap:1.25rem;margin:0}
.stats div{display:flex;flex-direction:column;gap:.15rem}
dd{margin:0;font-family:var(--mono);font-size:1rem;color:var(--ink)}

.roles{list-style:none;margin:0;padding:0}
/* The score hangs in its own column so the numbers stack vertically and
   the page can be scanned by score alone on a phone. */
.role{display:grid;grid-template-columns:3.25rem 1fr;gap:.9rem;
  padding:1.6rem 0;border-bottom:1px solid var(--rule)}
.score{font-family:var(--mono);font-size:1.5rem;font-weight:600;
  margin:0;line-height:1.1;color:var(--ink)}
.role h3{font-size:1.1875rem;line-height:1.3}
.where{font-family:var(--sans);font-size:.875rem;color:var(--ink-2);
  margin:.3rem 0 0;display:flex;flex-wrap:wrap;gap:.5rem;align-items:center}
.stretch{color:var(--accent);border:1px solid var(--accent);
  border-radius:2px;padding:.05rem .35rem;font-size:.625rem}
.fits{margin:.7rem 0 0;color:var(--ink);font-size:1rem}

.dims{margin-top:.9rem;display:flex;flex-direction:column;gap:.3rem}
.dim{display:grid;grid-template-columns:8.5rem 1fr 1.5rem;gap:.6rem;
  align-items:center}
.dim-name{font-family:var(--sans);font-size:.75rem;color:var(--ink-2)}
.dim-bar{height:3px;background:var(--rule);border-radius:2px;overflow:hidden}
.dim-bar i{display:block;height:3px;width:var(--w);background:var(--ink-3)}
.dim-num{font-family:var(--mono);font-size:.75rem;color:var(--ink-2);
  text-align:right}
.is-weak .dim-name{color:var(--flag)}
.is-weak .dim-bar i{background:var(--flag)}

.against{margin-top:.9rem}
.against ul{margin:.35rem 0 0;padding-left:1.1rem}
.against li{font-size:.9375rem;color:var(--ink-2);margin-bottom:.2rem}
.against .muted{color:var(--ink-3)}

.apply{display:inline-block;margin-top:.9rem;font-family:var(--sans);
  font-size:.8125rem;color:var(--ink);text-decoration:none;
  border:1px solid var(--rule-2);border-radius:3px;padding:.4rem .8rem}
.apply:hover{border-color:var(--accent);color:var(--accent)}
.apply:focus-visible,a:focus-visible{outline:2px solid var(--accent);
  outline-offset:2px}

.empty{font-family:var(--sans);color:var(--ink-3);padding:2.5rem 0;
  border-bottom:1px solid var(--rule)}
.footer{margin-top:2.5rem;padding-top:1.25rem;border-top:1px solid var(--rule)}
.footer h2{font-family:var(--mono);font-size:.6875rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-3);margin-bottom:.6rem}
.footer p{font-size:.9375rem;color:var(--ink-2);margin:0 0 .6rem;
  max-width:34rem}

@media (max-width:520px){
  body{font-size:16px}
  .page{padding:1.75rem 1rem 3rem}
  .masthead h1{font-size:1.75rem}
  /* Wide enough for "10.0" — a narrower column let the score collide
     with the title on the one listing that scored a perfect ten. */
  .role{grid-template-columns:3.4rem 1fr;gap:.6rem}
  .score{font-size:1.3rem}
  .dim{grid-template-columns:6.5rem 1fr 1.4rem}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;
  transition:none!important}}
"""
