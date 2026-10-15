"""Telegram texts (HTML parse mode) for a finished report and for a failed run."""
from datetime import timedelta
from html import escape as e

from .report import EPOCH, fms, fnum

CAPTION_MAX = 1024     # Telegram caption limit; counted on visible text, so raw HTML length is a safe bound


def _fmt(minute, tz, f):
    return (EPOCH + timedelta(minutes=minute + int(tz * 60))).strftime(f)


def _tzname(tz):
    return "UTC" if not tz else "UTC%+g" % tz


def _share(n, total):
    return "%.2f%%" % (100 * n / total) if total else "0%"


def status(s):
    rate = (s["5xx"] + s["499"]) / s["requests"]
    if rate >= 0.05:
        return "🔴 <b>Unhealthy</b>: %s of requests failed" % _share(s["5xx"] + s["499"], s["requests"])
    if s["incidents"] or rate >= 0.01:
        parts = []
        if s["incidents"]:
            parts.append("%d incident%s" % (len(s["incidents"]), "" if len(s["incidents"]) == 1 else "s"))
        if rate >= 0.01:
            parts.append("%s failed" % _share(s["5xx"] + s["499"], s["requests"]))
        return "🟡 <b>Degraded</b>: " + ", ".join(parts)
    return "🟢 <b>Healthy</b>"


def report_caption(name, gen, label, s, tz):
    n = s["requests"]
    head = ["📊 <b>%s</b> · %s" % (e(name or "report"), e(gen.NAME.capitalize())),
            "📅 %s · %s → %s %s" % (e(label), _fmt(s["start"], tz, "%H:%M"), _fmt(s["end"] - 1, tz, "%H:%M"), _tzname(tz)),
            "",
            status(s)]
    traffic = ["",
               "<b>Traffic</b>",
               "├ Requests: <b>%s</b>" % fnum(n),
               "├ Unique IPs: <b>%s</b>" % fnum(s["ips"]),
               "└ Volume: <b>%s MB</b>" % fnum(s["bytes"] / 1048576)]
    errors = ["",
              "<b>Errors</b>",
              "├ 5xx: <b>%s</b> (%s)" % (fnum(s["5xx"]), _share(s["5xx"], n)),
              "├ 499: <b>%s</b> (%s)" % (fnum(s["499"]), _share(s["499"], n)),
              "└ 4xx: <b>%s</b> (%s)" % (fnum(s["4xx"]), _share(s["4xx"], n))]
    latency = ["",
               "<b>Latency</b>",
               "└ p50 <b>%s</b> · p95 <b>%s</b> · p99 <b>%s</b>" % (fms(s["p50"]), fms(s["p95"]), fms(s["p99"]))]
    sections = [head, traffic, errors, latency]

    # optional sections, dropped from the end if the caption gets too long
    extra = []
    if s["incidents"]:
        rows = ["%s %s–%s %s" % ("└" if i == min(len(s["incidents"]), 3) - 1 else "├",
                                 _fmt(a, tz, "%H:%M"), _fmt(b, tz, "%H:%M"), e(", ".join(why)))
                for i, (a, b, why) in enumerate(s["incidents"][:3])]
        more = " (+%d more)" % (len(s["incidents"]) - 3) if len(s["incidents"]) > 3 else ""
        extra.append(["", "<b>Incidents</b>%s" % more] + rows)
    if s["failing"]:
        rows = ["%s <code>%s</code> · %s" % ("└" if i == len(s["failing"]) - 1 else "├", e(_cut(k, 48)), fnum(c))
                for i, (k, c) in enumerate(s["failing"])]
        extra.append(["", "<b>Top failing endpoints</b>"] + rows)
    if s["scanners"]:
        extra.append(["", "🕵️ %s IP%s probing .env, .git, wp-…" % (fnum(s["scanners"]), "" if s["scanners"] == 1 else "s")])
    extra.append(["", "<i>Open the file for charts and full tables.</i>"])

    text = "\n".join(sum(sections, []))
    for block in extra:
        candidate = text + "\n" + "\n".join(block)
        if len(candidate) <= CAPTION_MAX:
            text = candidate
    return text


def failure_text(name, gen, label, err):
    return "\n".join(["⚠️ <b>%s</b> · %s" % (e(name or "report"), e(gen.NAME.capitalize())),
                      "📅 %s" % e(label),
                      "",
                      "Report was not generated:",
                      "<pre>%s</pre>" % e(_cut(str(err), 800))])


def _cut(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"
