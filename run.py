#!/usr/bin/env python3
"""Build reports for every generator in config.json and send them to Telegram. Cron runs this daily.

  python3 run.py                                  # today (00:00 -> now, in config tz), send to Telegram
  python3 run.py --date 2026-10-14                # a past day
  python3 run.py --no-send                        # only write reports/
  python3 run.py -g nginx                         # one generator (config dir, or its default)
  python3 run.py -g traefik --dir ./logs --since "2026-10-11 10:00" --until "2026-10-11 11:30" -o out.html --no-send

--since/--until and every time shown in the report are in UTC + tz hours (config "tz", else server offset).
"""
import argparse
import glob
import os
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from src import config, message, telegram             # noqa: E402
from src.generators import GENERATORS                 # noqa: E402
from src.report import ReportError, build, to_minute  # noqa: E402

REPORTS = os.path.join(ROOT, "reports")


def cleanup(keep_days):
    if not keep_days:
        return
    cutoff = time.time() - keep_days * 86400
    for f in glob.glob(os.path.join(REPORTS, "*.html")):
        if os.path.getmtime(f) < cutoff:
            os.remove(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-g", "--generator", choices=sorted(GENERATORS), help="run only this generator")
    ap.add_argument("--dir", help="log directory (with -g)")
    ap.add_argument("--glob", help="file pattern inside --dir (with -g)")
    ap.add_argument("--date", help="YYYY-MM-DD, whole day in tz (default today)")
    ap.add_argument("--since", help='"YYYY-MM-DD HH:MM" in tz, overrides --date')
    ap.add_argument("--until", help='"YYYY-MM-DD HH:MM" in tz')
    ap.add_argument("--tz", type=float, help="hours from UTC, e.g. 5 (default config tz or server offset)")
    ap.add_argument("--top", type=int, help="rows per table (default 25)")
    ap.add_argument("-o", "--out", help="output file (with -g)")
    ap.add_argument("--no-send", action="store_true", help="do not send to Telegram")
    args = ap.parse_args()

    cfg = config.load()
    tz = args.tz if args.tz is not None else cfg.get("tz", config.local_tz())
    top = args.top or cfg.get("top", 25)
    name = cfg.get("name", "")
    tg = cfg.get("telegram") or {}
    send = not args.no_send
    if send and not (tg.get("token") and tg.get("chat_id")):
        sys.exit("no Telegram settings in config.json: run python3 init.py, or pass --no-send")

    if args.since or args.until:
        since = to_minute(args.since, tz) if args.since else None
        until = to_minute(args.until, tz) if args.until else None
        label = "%s → %s" % (args.since or "start", args.until or "end")
        stamp = (args.since or args.until or "custom")[:16].replace(" ", "_").replace(":", "")
    else:
        if args.date:
            day = datetime.strptime(args.date, "%Y-%m-%d").date()
        else:
            day = (datetime.now(timezone.utc) + timedelta(hours=tz)).date()
        since = to_minute("%s 00:00" % day, tz)
        until = to_minute("%s 00:00" % (day + timedelta(days=1)), tz)
        label = day.strftime("%a, %d %b %Y")
        stamp = day.isoformat()

    if args.generator:
        conf = next((g for g in cfg.get("generators", []) if g["type"] == args.generator), {})
        targets = [dict(conf, type=args.generator, dir=args.dir or conf.get("dir"), glob=args.glob or conf.get("glob"))]
    else:
        targets = cfg.get("generators") or []
        if not targets:
            sys.exit("no generators in config.json: run python3 init.py, or pass -g")

    os.makedirs(REPORTS, exist_ok=True)
    failed = 0
    for t in targets:
        gen = GENERATORS[t["type"]]
        out = args.out if args.out and len(targets) == 1 else os.path.join(
            REPORTS, "%s%s-%s.html" % (name + "-" if name else "", gen.NAME, stamp))
        title = "%s · %s" % (gen.TITLE, name) if name else gen.TITLE
        print("[%s] %s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), gen.NAME, label), file=sys.stderr)
        try:
            page, s = build(gen, t.get("dir"), t.get("glob"), since, until, tz, top, title)
            with open(out, "w", encoding="utf-8") as f:
                f.write(page)
            print("wrote %s (%d requests)" % (out, s["requests"]), file=sys.stderr)
            if send:
                telegram.send_document(tg["token"], tg["chat_id"], out, message.report_caption(name, gen, label))
                print("sent to Telegram", file=sys.stderr)
        except (ReportError, telegram.TelegramError, OSError) as e:
            failed += 1
            print("error: %s" % e, file=sys.stderr)
            if send:
                try:
                    telegram.send_message(tg["token"], tg["chat_id"], message.failure_text(name, gen, label, e))
                except telegram.TelegramError as e2:
                    print("error: %s" % e2, file=sys.stderr)

    cleanup(cfg.get("keep_days", 30))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
