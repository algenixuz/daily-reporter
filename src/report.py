"""Shared report engine: access-log records -> one self-contained HTML report.

Generators (src/generators/*) only know how to parse a log line into a record:

    (minute, ip, method, path, status, bytes, group, ms, user_agent)

minute is minutes since the Unix epoch (UTC). Everything else - streaming plain and .gz
files, aggregation, incident detection, charts and tables - lives here. Standard library only.
"""
import glob
import gzip
import html
import io
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

ID_SEG = re.compile(r'/(?:\d+|[0-9a-fA-F-]{16,}|[0-9a-fA-F]{24})(?=/|$)')
SCAN = re.compile(r'\.env|\.git|wp-|phpmyadmin|\.php|cgi-bin|actuator|\.aws|\.ssh|/etc/passwd|\.\./', re.I)
ISO_OFF = re.compile(r'([+-])(\d\d):?(\d\d)$')
CAP = 20000          # distinct endpoints / groups / user agents kept; the rest fold into "(other)"
BINS = 20            # latency histogram: 20 log bins per e-fold, ~5% resolution
EPOCH = datetime(1970, 1, 1)


class ReportError(Exception):
    pass


def lat_bin(ms):
    return int(math.log1p(ms) * BINS)


def bin_ms(b):
    return math.expm1((b + 0.5) / BINS)


def pct(hist, q):
    total = sum(hist.values())
    if not total:
        return 0
    cum, target = 0, q * total
    for b in sorted(hist):
        cum += hist[b]
        if cum >= target:
            return round(bin_ms(b))
    return 0


def new_row():
    return [0, 0, 0, 0, 0, Counter()]   # requests, 5xx, 499, sum ms, max ms, latency hist


def bump(d, key, st, ms, b, cap=None):
    r = d.get(key)
    if r is None:
        if cap and len(d) >= cap:
            key = "(other)"
            r = d.get(key)
        if r is None:
            r = d[key] = new_row()
    r[0] += 1
    if st >= 500:
        r[1] += 1
    elif st == 499:
        r[2] += 1
    r[3] += ms
    if ms > r[4]:
        r[4] = ms
    r[5][b] += 1


class Stats:
    def __init__(self):
        self.n = self.bytes = self.unparsed = 0
        self.status = Counter()
        self.hist = Counter()
        self.minute = {}
        self.endpoint = {}
        self.group = {}
        self.ip = {}            # ip -> [requests, 4xx, 5xx, bytes]
        self.scan = Counter()
        self.scan_paths = Counter()
        self.ua = Counter()
        self.norm_cache = {}

    def norm(self, path):
        p = self.norm_cache.get(path)
        if p is None:
            p = ID_SEG.sub("/:id", path.split("?", 1)[0])[:120]
            if len(self.norm_cache) > 200000:
                self.norm_cache.clear()
            self.norm_cache[path] = p
        return p

    def add(self, minute, ip, method, path, st, size, group, ms, ua):
        b = lat_bin(ms)
        self.n += 1
        self.bytes += size
        self.status[st] += 1
        self.hist[b] += 1
        bump(self.minute, minute, st, ms, b)
        bump(self.endpoint, method + " " + self.norm(path), st, ms, b, CAP)
        bump(self.group, group or "-", st, ms, b, CAP)
        r = self.ip.get(ip)
        if r is None:
            r = self.ip[ip] = [0, 0, 0, 0]
        r[0] += 1
        if 400 <= st < 500:
            r[1] += 1
        elif st >= 500:
            r[2] += 1
        r[3] += size
        if st >= 400 and SCAN.search(path):
            self.scan[ip] += 1
            self.scan_paths[path[:120]] += 1
        ua = ua[:160]
        if ua in self.ua or len(self.ua) < CAP:
            self.ua[ua] += 1
        else:
            self.ua["(other)"] += 1


# ---------------------------------------------------------------- time helpers for parsers

_day_cache = {}


def clf_minute(ts):
    # "11/Oct/2026:05:28:01 +0000" -> minutes since epoch (UTC)
    key = ts[:11] + ts[20:26]
    base = _day_cache.get(key)
    if base is None:
        d = datetime.strptime(ts[:11] + ts[20:26], "%d/%b/%Y %z")
        base = _day_cache[key] = int(d.timestamp()) // 60
    return base + int(ts[12:14]) * 60 + int(ts[15:17])


def iso_minute(ts):
    # "2026-10-11T05:32:00.123Z" or "2026-10-11T10:32:00+05:00" -> minutes since epoch (UTC)
    key = ts[:10]
    base = _day_cache.get(key)
    if base is None:
        d = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        base = _day_cache[key] = int(d.timestamp()) // 60
    m = base + int(ts[11:13]) * 60 + int(ts[14:16])
    o = ISO_OFF.search(ts[19:])
    if o:
        off = int(o.group(2)) * 60 + int(o.group(3))
        m += -off if o.group(1) == "+" else off
    return m


def to_minute(s, tz=0):
    """'YYYY-MM-DD HH:MM' in UTC+tz -> minutes since epoch (UTC)."""
    return int((datetime.strptime(s, "%Y-%m-%d %H:%M") - EPOCH).total_seconds()) // 60 - int(tz * 60)


# ---------------------------------------------------------------- reading

def find_files(log_dir, pattern, since=None):
    files = sorted(glob.glob(os.path.join(log_dir, pattern)), key=os.path.getmtime)
    if since is not None:
        # a rotated file's mtime is its last write: if that is before since, nothing in it matches
        files = [f for f in files if os.path.getmtime(f) / 60 >= since - 60]
    return files


def read_files(files, parse, stats, since, until):
    for path in files:
        raw = open(path, "rb")
        size = os.path.getsize(path) or 1
        stream = gzip.GzipFile(fileobj=raw) if path.endswith(".gz") else raw
        text = io.TextIOWrapper(stream, encoding="utf-8", errors="replace")
        t0, lines = time.time(), 0
        for line in text:
            lines += 1
            if not lines % 1000000:
                done = raw.tell() / size
                rate = lines / (time.time() - t0)
                eta = (time.time() - t0) * (1 - done) / max(done, 1e-9)
                print("  %s  %3.0f%%  %dk lines/s  ~%ds left" % (os.path.basename(path), 100 * done, rate / 1000, eta),
                      file=sys.stderr, flush=True)
            try:
                rec = parse(line)
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                rec = None
            if rec is None:
                stats.unparsed += 1
                continue
            if (since is not None and rec[0] < since) or (until is not None and rec[0] >= until):
                continue
            stats.add(*rec)
        raw.close()
        print("  %s done: %d lines in %.0fs" % (os.path.basename(path), lines, time.time() - t0), file=sys.stderr, flush=True)


# ---------------------------------------------------------------- analysis

def incidents(stats):
    """Windows of consecutive bad minutes: traffic collapse, error bursts, or very slow requests."""
    if not stats.minute:
        return []
    lo, hi = min(stats.minute), max(stats.minute)
    empty = [0, 0, 0, 0, 0, None]
    bad = []
    for m in range(lo, hi + 1):
        r = stats.minute.get(m, empty)
        n = r[0]
        window = sorted(stats.minute.get(x, empty)[0] for x in range(m - 60, m, 5))
        base = window[len(window) // 2]
        why = set()
        if base >= 10 and n < 0.1 * base:
            why.add("traffic drop")
        if n >= 5 and (r[1] + r[2]) / n >= 0.2:
            why.add("errors")
        if n and r[3] / n >= 5000:
            why.add("slow")
        if why:
            bad.append((m, why))
    windows = []
    for m, why in bad:
        if windows and m - windows[-1]["end"] <= 3:
            w = windows[-1]
            w["end"] = m
            w["why"] |= why
        else:
            windows.append({"start": m, "end": m, "why": set(why)})
    for w in windows:
        rows = [stats.minute.get(x, empty) for x in range(w["start"], w["end"] + 1)]
        w["n"] = sum(r[0] for r in rows)
        w["e5"] = sum(r[1] for r in rows)
        w["c499"] = sum(r[2] for r in rows)
        w["max"] = max(r[4] for r in rows)
    return windows


def buckets(stats, max_buckets=240):
    lo, hi = min(stats.minute), max(stats.minute)
    for step in (1, 5, 10, 15, 30, 60, 180, 360, 720, 1440):
        if (hi - lo) / step <= max_buckets:
            break
    lo -= lo % step
    out = []
    for start in range(lo, hi + 1, step):
        n = e5 = c499 = s = mx = 0
        h = Counter()
        for m in range(start, start + step):
            r = stats.minute.get(m)
            if r:
                n += r[0]; e5 += r[1]; c499 += r[2]; s += r[3]
                mx = max(mx, r[4]); h.update(r[5])
        out.append({"t": start, "n": n, "e5": e5, "c499": c499, "avg": s / n if n else 0,
                    "p95": pct(h, .95), "max": mx})
    return step, out


def summary(stats):
    """Headline numbers, used for the Telegram caption."""
    return {"requests": stats.n, "ips": len(stats.ip), "bytes": stats.bytes,
            "5xx": sum(c for st, c in stats.status.items() if st >= 500), "499": stats.status[499],
            "p50": pct(stats.hist, .5), "p95": pct(stats.hist, .95), "p99": pct(stats.hist, .99),
            "incidents": len(incidents(stats)), "unparsed": stats.unparsed}


# ---------------------------------------------------------------- rendering

def esc(s):
    return html.escape(str(s), quote=True)


def fnum(v):
    v = float(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            return ("%.1f" % (v / div)).rstrip("0").rstrip(".") + suf
    return "%d" % v if v == int(v) else "%.1f" % v


def fms(ms):
    ms = float(ms)
    if ms >= 60000:
        return "%.1f min" % (ms / 60000)
    if ms >= 1000:
        return "%.1f s" % (ms / 1000)
    return "%d ms" % ms


def nice(v):
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for f in (1, 2, 2.5, 5, 10):
        if f * e >= v:
            return f * e
    return 10 * e


W, H, PL, PR, PT, PB = 1000, 220, 52, 8, 8, 24


def x_labels(labels, xs):
    idx = range(0, len(labels), max(1, len(labels) // 8))
    return "".join('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>' % (xs(i), H - 6, esc(labels[i])) for i in idx)


def bar_chart(labels, tips, series):
    """Stacked bars. series = [(name, css class, values)]."""
    n = len(labels)
    totals = [sum(s[2][i] for s in series) for i in range(n)]
    ymax = nice(max(totals) if totals else 1)
    if ymax < 20:
        ymax = max(4, math.ceil(ymax / 4) * 4)   # whole-number ticks for small counts
    pw, ph = W - PL - PR, H - PT - PB
    bw = pw / max(n, 1)
    gap = 2 if bw > 6 else (1 if bw > 3 else 0)
    y = lambda v: PT + ph - v / ymax * ph
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (W, H)]
    for k in range(5):
        v = ymax * k / 4
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/><text x="%d" y="%.1f" class="ax" text-anchor="end">%s</text>'
                   % (PL, W - PR, y(v), y(v), PL - 6, y(v) + 4, fnum(v)))
    for i in range(n):
        x = PL + i * bw
        out.append('<g class="col"><rect class="hit" x="%.1f" y="%d" width="%.1f" height="%d"/>' % (x, PT, bw, ph))
        base = 0
        for name, cls, vals in series:
            v = vals[i]
            if v:
                out.append('<rect class="%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>'
                           % (cls, x + gap / 2, y(base + v), max(bw - gap, 0.6), max(y(base) - y(base + v) - (gap if base else 0), 0.6)))
            base += v
        out.append("<title>%s</title></g>" % esc(tips[i]))
    out.append('<line x1="%d" x2="%d" y1="%d" y2="%d" class="base"/>' % (PL, W - PR, PT + ph, PT + ph))
    out.append(x_labels(labels, lambda i: PL + (i + .5) * bw))
    out.append("</svg>")
    return "".join(out)


def line_chart(labels, tips, series):
    """Log-scale latency lines. series = [(name, css class, values in ms)]."""
    n = len(labels)
    vmax = max([max(s[2]) for s in series if s[2]] + [10])
    top = 10 ** math.ceil(math.log10(vmax))
    pw, ph = W - PL - PR, H - PT - PB
    step = pw / max(n, 1)
    xs = lambda i: PL + (i + .5) * step
    y = lambda v: PT + ph - math.log10(max(v, 1)) / math.log10(top) * ph
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (W, H)]
    t = 1
    while t <= top:
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/><text x="%d" y="%.1f" class="ax" text-anchor="end">%s</text>'
                   % (PL, W - PR, y(t), y(t), PL - 6, y(t) + 4, fms(t)))
        t *= 10
    for name, cls, vals in series:
        run = []
        for i, v in enumerate(vals + [None]):
            if v:                       # empty buckets break the line instead of plunging to zero
                run.append("%.1f,%.1f" % (xs(i), y(v)))
            elif run:
                out.append('<polyline class="%s" points="%s"/>' % (cls, " ".join(run)))
                run = []
    for i in range(n):
        out.append('<g class="col"><rect class="hit" x="%.1f" y="%d" width="%.1f" height="%d"/><title>%s</title></g>'
                   % (PL + i * step, PT, step, ph, esc(tips[i])))
    out.append(x_labels(labels, xs))
    out.append("</svg>")
    return "".join(out)


def legend(items):
    return '<div class="legend">' + "".join('<span><i class="sw %s"></i>%s</span>' % (c, esc(n)) for n, c in items) + "</div>"


def table(headers, rows, numeric=()):
    """rows: lists of (display, sort value) tuples or plain strings."""
    out = ['<div class="tw"><table class="sort"><thead><tr>']
    for i, h in enumerate(headers):
        out.append('<th class="%s">%s</th>' % ("num" if i in numeric else "", esc(h)))
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for i, c in enumerate(row):
            disp, val = c if isinstance(c, tuple) else (c, None)
            out.append('<td class="%s"%s>%s</td>' % ("num" if i in numeric else "",
                       ' data-v="%s"' % esc(val) if val is not None else "", esc(disp)))
        out.append("</tr>")
    if not rows:
        out.append('<tr><td colspan="%d" class="muted">nothing</td></tr>' % len(headers))
    out.append("</tbody></table></div>")
    return "".join(out)


def n_(v):
    return (fnum(v), v)


def ms_(v):
    return (fms(v), v)


def render(stats, files, title, group_label="Router", groups_title=None, tz=0, top=25):
    off = int(tz * 60)
    tzname = "UTC" if not off else "UTC%+g" % tz
    fmt = lambda m, f="%Y-%m-%d %H:%M": (EPOCH + timedelta(minutes=m + off)).strftime(f)

    step, bk = buckets(stats)
    labels = [fmt(b["t"], "%d %H:%M" if step < 1440 else "%m-%d") for b in bk]
    tips = ["%s (%d min)\nrequests %s\n5xx %s\n499 %s\navg %s · p95 %s · max %s"
            % (fmt(b["t"]), step, fnum(b["n"]), fnum(b["e5"]), fnum(b["c499"]), fms(b["avg"]), fms(b["p95"]), fms(b["max"]))
            for b in bk]

    n = stats.n
    cls = Counter()
    for st, c in stats.status.items():
        cls["%dxx" % (st // 100)] += c
    e5 = cls["5xx"]
    c499 = stats.status[499]
    tiles = [("Requests", fnum(n)), ("Unique IPs", fnum(len(stats.ip))), ("Traffic", fnum(stats.bytes / 1048576) + " MB"),
             ("5xx", "%s · %.2f%%" % (fnum(e5), 100 * e5 / n)), ("499 (client gave up)", "%s · %.2f%%" % (fnum(c499), 100 * c499 / n)),
             ("p50 / p95 / p99", "%s / %s / %s" % (fms(pct(stats.hist, .5)), fms(pct(stats.hist, .95)), fms(pct(stats.hist, .99))))]

    inc = incidents(stats)
    inc_rows = [[(fmt(w["start"]), w["start"]), (fmt(w["end"] + 1), w["end"]), n_(w["end"] - w["start"] + 1),
                 ", ".join(sorted(w["why"])), n_(w["n"]), n_(w["e5"]), n_(w["c499"]), ms_(w["max"])] for w in inc]

    def ep_rows(items):
        return [[k, n_(r[0]), n_(r[1]), n_(r[2]), ms_(r[3] / r[0]), ms_(pct(r[5], .95)), ms_(r[4])] for k, r in items]

    eps = stats.endpoint.items()
    top_ep = sorted(eps, key=lambda kv: -kv[1][0])[:top]
    slow_ep = sorted((kv for kv in eps if kv[1][0] >= 5), key=lambda kv: -pct(kv[1][5], .95))[:top]
    err_ep = sorted((kv for kv in eps if kv[1][1] + kv[1][2]), key=lambda kv: -(kv[1][1] + kv[1][2]))[:top]
    ep_head = ["Endpoint", "Requests", "5xx", "499", "Avg", "p95", "Max"]
    ep_num = (1, 2, 3, 4, 5, 6)

    groups = ep_rows(sorted(stats.group.items(), key=lambda kv: -kv[1][0])[:top])
    ips = [[ip, n_(r[0]), n_(r[1]), n_(r[2]), (fnum(r[3] / 1048576) + " MB", r[3]), n_(stats.scan.get(ip, 0))]
           for ip, r in sorted(stats.ip.items(), key=lambda kv: -kv[1][0])[:top]]
    scanners = [[ip, n_(c)] for ip, c in stats.scan.most_common(top)]
    scan_paths = [[p, n_(c)] for p, c in stats.scan_paths.most_common(top)]
    statuses = [[(str(st), st), n_(c), ("%.2f%%" % (100 * c / n), c)] for st, c in sorted(stats.status.items())]
    uas = [[ua or "-", n_(c)] for ua, c in stats.ua.most_common(top)]

    span = "%s → %s %s" % (fmt(min(stats.minute)), fmt(max(stats.minute) + 1), tzname)
    sec = []
    sec.append('<section><h2>Incidents</h2><p class="muted">Minutes where traffic fell below 10%% of the previous hour, '
               '≥20%% of requests were 5xx/499, or average latency was ≥5 s. Gaps of up to 3 minutes are merged.</p>%s</section>'
               % table(["From", "To", "Minutes", "Signal", "Requests", "5xx", "499", "Max latency"], inc_rows, (2, 4, 5, 6, 7)))
    sec.append('<section><h2>Requests</h2><p class="muted">per %d min · hover a bar for details</p>%s</section>'
               % (step, bar_chart(labels, tips, [("Requests", "s1", [b["n"] for b in bk])])))
    sec.append('<section><h2>Errors</h2>%s%s</section>'
               % (legend([("5xx server error", "crit"), ("499 client gave up", "seri")]),
                  bar_chart(labels, tips, [("5xx", "crit", [b["e5"] for b in bk]), ("499", "seri", [b["c499"] for b in bk])])))
    sec.append('<section><h2>Latency</h2><p class="muted">log scale</p>%s%s</section>'
               % (legend([("p95", "l1"), ("max", "l2")]),
                  line_chart(labels, tips, [("p95", "l1", [b["p95"] for b in bk]), ("max", "l2", [b["max"] for b in bk])])))
    sec.append("<section><h2>Top endpoints</h2>%s</section>" % table(ep_head, ep_rows(top_ep), ep_num))
    sec.append('<section><h2>Slowest endpoints</h2><p class="muted">by p95, at least 5 requests</p>%s</section>' % table(ep_head, ep_rows(slow_ep), ep_num))
    sec.append("<section><h2>Endpoints with errors</h2>%s</section>" % table(ep_head, ep_rows(err_ep), ep_num))
    sec.append("<section><h2>%s</h2>%s</section>" % (esc(groups_title or group_label + "s"), table([group_label] + ep_head[1:], groups, ep_num)))
    sec.append('<div class="two"><section><h2>Status codes</h2>%s</section><section><h2>Top IPs</h2>%s</section></div>'
               % (table(["Status", "Requests", "Share"], statuses, (0, 1, 2)),
                  table(["IP", "Requests", "4xx", "5xx", "Traffic", "Probes"], ips, (1, 2, 3, 4, 5))))
    sec.append('<div class="two"><section><h2>Scanners</h2><p class="muted">IPs probing .env, .git, wp-, .php and similar</p>%s</section>'
               '<section><h2>Probed paths</h2><p class="muted">&nbsp;</p>%s</section></div>'
               % (table(["IP", "Probes"], scanners, (1,)), table(["Path", "Hits"], scan_paths, (1,))))
    sec.append("<section><h2>User agents</h2>%s</section>" % table(["User agent", "Requests"], uas, (1,)))

    return PAGE.format(
        title=esc(title),
        span=esc(span),
        meta=esc("%d file(s) · %s unparsed lines · generated %s" % (len(files), fnum(stats.unparsed), datetime.now().strftime("%Y-%m-%d %H:%M"))),
        tiles="".join('<div class="tile"><div class="k">%s</div><div class="v">%s</div></div>' % (esc(k), esc(v)) for k, v in tiles),
        body="".join(sec))


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{color-scheme:light;--bg:#f7f7f5;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8984;--line:#e6e5e1;--grid:#efeeea;
--s1:#2a78d6;--l2:#eb6834;--crit:#d03b3b;--seri:#ec835a;--hover:rgba(0,0,0,.05)}}
@media (prefers-color-scheme:dark){{:root{{color-scheme:dark;--bg:#121211;--card:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#8d8c85;
--line:#2c2c2a;--grid:#262624;--s1:#3987e5;--l2:#d95926;--hover:rgba(255,255,255,.06)}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px 16px 48px}}
main{{max-width:1180px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 2px}} h2{{font-size:15px;margin:0 0 8px}}
.muted{{color:var(--muted);font-size:12.5px;margin:0 0 10px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:18px 0}}
.tile,section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.tile .k{{color:var(--ink2);font-size:12px}} .tile .v{{font-size:19px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}}
section{{margin-bottom:14px;min-width:0}}
.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:14px}} .two section{{margin:0}} .two{{margin-bottom:14px}}
.chart{{width:100%;height:auto;display:block}}
.chart .grid{{stroke:var(--grid)}} .chart .base{{stroke:var(--line)}} .chart .ax{{fill:var(--muted);font-size:11px}}
.chart .hit{{fill:transparent}} .chart .col:hover .hit{{fill:var(--hover)}}
.s1{{fill:var(--s1)}} .crit{{fill:var(--crit)}} .seri{{fill:var(--seri)}}
polyline{{fill:none;stroke-width:2;stroke-linejoin:round}} polyline.l1{{stroke:var(--s1)}} polyline.l2{{stroke:var(--l2)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--ink2);margin-bottom:6px}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}}
.sw.crit{{background:var(--crit)}} .sw.seri{{background:var(--seri)}} .sw.l1{{background:var(--s1)}} .sw.l2{{background:var(--l2)}}
.tw{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
td:first-child{{white-space:normal;word-break:break-all;max-width:520px}}
th{{color:var(--ink2);font-weight:600;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--card)}}
th:hover{{color:var(--ink)}} th[data-dir=asc]::after{{content:" ▲"}} th[data-dir=desc]::after{{content:" ▼"}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
tbody tr:hover{{background:var(--hover)}}
</style></head><body><main>
<h1>{title}</h1><div class="muted">{span} · {meta}</div>
<div class="tiles">{tiles}</div>
{body}
</main><script>
document.querySelectorAll("table.sort th").forEach(function(th,i){{th.addEventListener("click",function(){{
var tb=th.closest("table").tBodies[0],rows=[].slice.call(tb.rows),d=th.dataset.dir==="desc"?"asc":"desc";
th.parentNode.querySelectorAll("th").forEach(function(h){{delete h.dataset.dir}});th.dataset.dir=d;
var v=function(r){{var c=r.cells[i];return c?(c.dataset.v!==undefined?c.dataset.v:c.textContent):""}};
rows.sort(function(a,b){{var x=v(a),y=v(b),nx=parseFloat(x),ny=parseFloat(y);
var c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:x.localeCompare(y);return d==="asc"?c:-c}});
rows.forEach(function(r){{tb.appendChild(r)}})}})}});
</script></body></html>"""


# ---------------------------------------------------------------- entry point for generators

def build(gen, log_dir=None, pattern=None, since=None, until=None, tz=0, top=25, title=None):
    """Run one generator module over its log files. Returns (html, summary dict)."""
    log_dir = log_dir or gen.DEFAULT_DIR
    pattern = pattern or gen.DEFAULT_GLOB
    files = find_files(log_dir, pattern, since)
    if not files:
        raise ReportError("no files match %s" % os.path.join(log_dir, pattern))
    print("reading %d file(s): %s" % (len(files), ", ".join(os.path.basename(f) for f in files)), file=sys.stderr)
    stats = Stats()
    read_files(files, gen.parse, stats, since, until)
    if not stats.n:
        raise ReportError("no requests matched in %s (%d unparsed lines)" % (os.path.join(log_dir, pattern), stats.unparsed))
    page = render(stats, files, title or gen.TITLE, gen.GROUP_LABEL, getattr(gen, "GROUPS_TITLE", None), tz, top)
    return page, summary(stats)
