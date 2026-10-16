"""Telegram texts (HTML parse mode) for a finished report and for a failed run."""
from html import escape as e


def report_caption(name, gen, label):
    return "📊 <b>%s</b> · %s\n📅 %s" % (e(name or "report"), e(gen.NAME.capitalize()), e(label))


def failure_text(name, gen, label, err):
    return "\n".join(["⚠️ <b>%s</b> · %s" % (e(name or "report"), e(gen.NAME.capitalize())),
                      "📅 %s" % e(label),
                      "",
                      "Report was not generated:",
                      "<pre>%s</pre>" % e(_cut(str(err), 800))])


def _cut(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"
