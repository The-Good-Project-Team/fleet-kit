#!/usr/bin/env python3
"""status_page.py -- render the fleet status page (incident.io-style) from status_data.

Server-rendered, no JS, no build step: this page's whole job is to be readable when things
are broken, and a client-rendered dashboard that needs a working runtime to tell you the
runtime is down is a status page that lies exactly when it matters.
"""
from __future__ import annotations

from html import escape

import status_data

CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#6b7280;--line:#e5e7eb;
--ok:#30a46c;--down:#e5484d;--unknown:#e8e8ea;--warn:#ffb224;--warnbg:#fffbeb;--card:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#0c0c0d;--fg:#ededef;--muted:#8b8b8f;--line:#232326;--unknown:#232326;
--warnbg:#2a2213;--card:#141416}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:820px;margin:0 auto;padding:40px 20px 64px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px}
h1{font-size:22px;font-weight:700;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.banner{border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:24px}
.banner.bad{border-color:var(--warn)}
.banner-head{padding:14px 18px;font-weight:600;display:flex;gap:9px;align-items:center}
.banner.bad .banner-head{background:var(--warnbg)}
.banner.good .banner-head{background:transparent}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.dot.ok{background:var(--ok)}.dot.down{background:var(--down)}.dot.unknown{background:var(--muted)}
.card{border:1px solid var(--line);border-radius:10px;background:var(--card);padding:20px 22px}
.card h2{font-size:15px;font-weight:600;margin:0 0 18px}
.comp{padding:16px 0;border-top:1px solid var(--line)}
.comp:first-of-type{border-top:0;padding-top:0}
.comp-top{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.comp-name{display:flex;align-items:center;gap:8px;font-weight:500}
.comp-desc{color:var(--muted);font-size:12px;margin-top:2px}
.uptime{color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.bars{display:flex;gap:2px;margin-top:11px;overflow-x:auto;padding-bottom:2px}
.bars i{flex:1 0 4px;min-width:4px;height:30px;border-radius:2px;background:var(--unknown)}
.bars i.ok{background:var(--ok)}
.bars i.down{background:var(--down)}
.axis{display:flex;justify-content:space-between;color:var(--muted);font-size:11px;margin-top:7px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:26px;line-height:1.7}
"""

_LABEL = {"ok": "Operational", "down": "Degraded", "unknown": "No data"}


def render(hours: int = 72) -> str:
    d = status_data.snapshot(hours)
    overall = d["overall"]
    bad = overall == "down"

    rows = []
    for c in d["components"]:
        pct = "%.2f%% uptime" % c["uptime_pct"] if c["uptime_pct"] is not None else "no data"
        bars = "".join(
            '<i class="%s"></i>' % (x if x != "unknown" else "") for x in c["cells"]
        )
        rows.append(
            '<div class="comp">'
            '<div class="comp-top"><div>'
            '<div class="comp-name"><span class="dot %s"></span>%s</div>'
            '<div class="comp-desc">%s</div></div>'
            '<div class="uptime">%s</div></div>'
            '<div class="bars">%s</div>'
            '</div>' % (c["current"], escape(c["label"]), escape(c["description"]), pct, bars)
        )

    headline = ("Some components are degraded" if bad
                else "All systems operational" if overall == "ok"
                else "Status partially unknown")

    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Fleet status</title><style>%s</style></head><body><div class=wrap>"
        "<header><div><h1>Fleet status</h1>"
        "<div class=sub>philanthropy &middot; dino</div></div></header>"
        "<div class='banner %s'><div class=banner-head>"
        "<span class='dot %s'></span>%s</div></div>"
        "<div class=card><h2>System status &mdash; last %d hours</h2>%s"
        "<div class=axis><span>%dh ago</span><span>now</span></div></div>"
        "<footer>Rolled up from the health checks that run every 5 minutes.<br>"
        "Grey means no check ran in that hour, never &ldquo;healthy&rdquo;. "
        "Generated %s.</footer>"
        "</div></body></html>"
        % (CSS, "bad" if bad else "good", overall, escape(headline),
           d["hours"], "".join(rows), d["hours"], d["generated_at"])
    )


if __name__ == "__main__":
    print(render())
