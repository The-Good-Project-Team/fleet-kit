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
--ok:#30a46c;--down:#e5484d;--unknown:#e8e8ea;--warn:#ffb224;--warnbg:#fffbeb;
--unknownbd:#9ca3af;--unknownbg:#f4f4f5;--card:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#0c0c0d;--fg:#ededef;--muted:#8b8b8f;--line:#232326;--unknown:#232326;
--warnbg:#2a2213;--unknownbd:#6b7280;--unknownbg:#1c1c1f;--card:#141416}}
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
.banner.unknown{border-color:var(--unknownbd)}
.banner-head{padding:14px 18px;font-weight:600;display:flex;gap:9px;align-items:center}
.banner.bad .banner-head{background:var(--warnbg)}
.banner.good .banner-head{background:transparent}
.banner.unknown .banner-head{background:var(--unknownbg)}
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
.mem{display:flex;align-items:center;gap:12px;padding:11px 0;border-top:1px solid var(--line)}
.mem:first-of-type{border-top:0}
.mem-name{flex:1;font-weight:500;display:flex;align-items:center;gap:8px}
.mem-runs{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
.mem-bar{display:flex;gap:1px;width:130px;flex:none}
.mem-bar i{height:16px;border-radius:1px}
.mem-bar i.ok{background:var(--ok)}
.mem-bar i.spare{background:#c9d4dd}
.mem-bar i.down{background:var(--down)}
.mem-ago{color:var(--muted);font-size:12px;width:74px;text-align:right;
font-variant-numeric:tabular-nums;white-space:nowrap}
.mem-cost{color:var(--muted);font-size:12px;width:66px;text-align:right;
font-variant-numeric:tabular-nums}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .mem-bar i.spare{background:#39424b}}
@media(max-width:640px){.mem-cost,.mem-bar{display:none}}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:26px;line-height:1.7}
/* gh#399: the live alert_store.py feed (/api/alerts), separate from the .banner above --
that banner is the OLD log-derived per-component uptime rollup (status_data's own `overall`),
this is the live, deduped severity state. Kept visually distinct (own icon, own border color,
a bullet list of the actual open conditions) so the two are never mistaken for one signal. */
.live-alert{border:1px solid var(--warn);border-radius:10px;padding:14px 18px;
margin-bottom:24px;background:var(--warnbg)}
.live-alert.critical{border-color:var(--down);background:rgba(229,72,77,.08)}
.live-alert-head{font-weight:600;display:flex;align-items:center;gap:8px}
.live-alert ul{margin:10px 0 0;padding-left:20px;font-size:13px;color:var(--muted)}
"""

_LABEL = {"ok": "Operational", "down": "Degraded", "unknown": "No data"}

# gh#358: the banner box itself must never render "unknown" with the same class as "good" --
# a status page that grays out only the headline text while the surrounding box still reads
# green is the exact lie its own module docstring warns against.
_BANNER_CLASS = {"down": "bad", "ok": "good", "unknown": "unknown"}


def _live_alert_html(live_alerts: dict) -> str:
    """Render the live /api/alerts feed as its own block, or "" when there is nothing open --
    AC8 requires a `worst: "ok"` response to show neither this nor the fleet_view.html banner."""
    worst = live_alerts.get("worst", "unknown")
    if worst == "ok":
        return ""
    counts = live_alerts.get("counts") or {}
    head = []
    if counts.get("critical"):
        head.append("%d critical" % counts["critical"])
    if counts.get("degraded"):
        head.append("%d degraded" % counts["degraded"])
    if not head:
        head.append("alerts feed unreachable" if worst == "unknown" else worst)
    items = "".join(
        "<li>%s: %s%s</li>" % (
            escape(a.get("check", "")), escape(a.get("problem", "")),
            " (%s)" % escape(",".join(a.get("handles", []))) if a.get("handles") else "",
        )
        for a in live_alerts.get("open", [])
    )
    cls = "critical" if worst == "critical" else ""
    return (
        "<div class='live-alert %s'><div class=live-alert-head>&#9888; %s alert(s) open</div>"
        "%s</div>" % (cls, escape(", ".join(head)), ("<ul>%s</ul>" % items if items else ""))
    )


def render(hours: int = 72) -> str:
    d = status_data.snapshot(hours)
    overall = d["overall"]
    bad = overall == "down"
    banner_class = _BANNER_CLASS.get(overall, "unknown")

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

    mem_rows = []
    for m in d.get("members", []):
        total = max(m["runs"], 1)
        # Proportional strip, not one cell per run: members range from 15 to 301 runs
        # over the same window, so per-run cells would make a busy member unreadable
        # and a quiet one invisible.
        seg = []
        for key, cls in (("ok", "ok"), ("spare", "spare"), ("bad", "down")):
            w = m[key] / total * 100.0
            if w > 0:
                seg.append('<i class="%s" style="width:%.1f%%"></i>' % (cls, w))
        mem_rows.append(
            '<div class="mem">'
            '<div class="mem-name"><span class="dot %s"></span>%s</div>'
            '<div class="mem-runs">%d runs</div>'
            '<div class="mem-bar">%s</div>'
            '<div class="mem-cost">%s</div>'
            '<div class="mem-ago">%s</div>'
            '</div>' % (m["state"], escape(m["name"]), m["runs"],
                        "".join(seg), m["cost_str"], escape(m["ago"]))
        )
    members_card = (
        '<div class=card style="margin-top:20px"><h2>Fleet members &mdash; last %d hours</h2>%s'
        '<div class=axis style="margin-top:12px"><span>'
        '<span style="display:inline-block;width:8px;height:8px;background:var(--ok);'
        'border-radius:1px;margin-right:5px"></span>worked'
        '<span style="display:inline-block;width:8px;height:8px;background:#c9d4dd;'
        'border-radius:1px;margin:0 5px 0 14px"></span>declined to spend / cut short'
        '</span><span>last run</span></div></div>'
        % (d["hours"], "".join(mem_rows))
    ) if mem_rows else ""

    headline = ("Some components are degraded" if bad
                else "All systems operational" if overall == "ok"
                else "Status partially unknown")
    live_alert_html = _live_alert_html(d.get("live_alerts") or {})

    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Fleet status</title><style>%s</style></head><body><div class=wrap>"
        "<header><div><h1>Fleet status</h1>"
        "<div class=sub>philanthropy &middot; dino</div></div></header>"
        "<div class='banner %s'><div class=banner-head>"
        "<span class='dot %s'></span>%s</div></div>%s"
        "<div class=card><h2>System status &mdash; last %d hours</h2>%s"
        "<div class=axis><span>%dh ago</span><span>now</span></div></div>%s"
        "<footer>Rolled up from the health checks that run every 5 minutes.<br>"
        "Grey means no check ran in that hour, never &ldquo;healthy&rdquo;. "
        "Generated %s.</footer>"
        "</div></body></html>"
        % (CSS, banner_class, overall, escape(headline), live_alert_html,
           d["hours"], "".join(rows), d["hours"], members_card, d["generated_at"])
    )


if __name__ == "__main__":
    print(render())
