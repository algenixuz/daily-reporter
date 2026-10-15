#!/usr/bin/env python3
"""Interactive setup: asks a few questions, writes config.json, registers the daily cron job.

  python3 init.py              # setup (re-run any time; current values are the defaults)
  python3 init.py --uninstall  # remove the cron job

Run it as the user that can read the log files (often root: sudo python3 init.py) -
the cron job is installed in that user's crontab.
"""
import argparse
import fnmatch
import os
import shlex
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from src import config, telegram          # noqa: E402
from src.generators import GENERATORS     # noqa: E402

SCHEDULE = "59 23 * * *"
MARK = "# daily-report:" + ROOT           # one entry per clone, so re-running replaces it


def ask(prompt, default=None, shown=None, required=True):
    hint = shown if shown is not None else default
    while True:
        v = input("%s%s: " % (prompt, " [%s]" % hint if hint not in (None, "") else "")).strip()
        if v:
            return v
        if default not in (None, ""):
            return default
        if not required:
            return ""
        print("  required")


def yes(prompt, default=True):
    v = input("%s [%s]: " % (prompt, "Y/n" if default else "y/N")).strip().lower()
    return default if not v else v.startswith("y")


def mask(token):
    return token[:6] + "…" + token[-4:] if token and len(token) > 12 else token


def ask_token(current):
    while True:
        token = ask("Telegram bot token (from @BotFather)", current, mask(current))
        try:
            bot = telegram.get_me(token)
            print("  ok: @%s" % bot.get("username"))
            return token
        except telegram.TelegramError as e:
            print("  %s" % e)
            if not yes("  token check failed, try again?"):
                return token


def ask_generators(current):
    by_type = {g["type"]: g for g in current}
    default = ",".join(by_type) or "traefik"
    while True:
        names = [n.strip() for n in ask("Generators (%s)" % ", ".join(sorted(GENERATORS)), default).split(",") if n.strip()]
        unknown = [n for n in names if n not in GENERATORS]
        if not unknown:
            break
        print("  unknown: %s" % ", ".join(unknown))
    out = []
    for n in names:
        gen, old = GENERATORS[n], by_type.get(n, {})
        d = ask("  %s log directory" % n, old.get("dir") or gen.DEFAULT_DIR)
        g = old.get("glob") or gen.DEFAULT_GLOB
        if not os.path.isdir(d):
            print("  warning: %s does not exist" % d)
        else:
            files = [f for f in os.listdir(d) if fnmatch.fnmatch(f, g)]
            if not files:
                print("  warning: no %s files in %s" % (g, d))
            elif not all(os.access(os.path.join(d, f), os.R_OK) for f in files):
                print("  warning: some files in %s are not readable by this user - run init.py as a user that can read them" % d)
        out.append({"type": n, "dir": d, "glob": g})
    return out


# ---------------------------------------------------------------- crontab

def read_crontab():
    r = subprocess.run(["crontab", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return r.stdout.splitlines() if r.returncode == 0 else []   # non-zero = no crontab yet


def write_crontab(lines):
    text = "\n".join(lines) + "\n" if lines else ""
    subprocess.run(["crontab", "-"], input=text, universal_newlines=True, check=True)


def install_cron():
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    line = "%s cd %s && %s run.py >> logs/cron.log 2>&1 %s" % (
        SCHEDULE, shlex.quote(ROOT), shlex.quote(sys.executable), MARK)
    write_crontab([l for l in read_crontab() if not l.endswith(MARK)] + [line])
    return line


def uninstall_cron():
    lines = read_crontab()
    kept = [l for l in lines if not l.endswith(MARK)]
    write_crontab(kept)
    return len(lines) - len(kept)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true", help="remove the cron job and exit")
    ap.add_argument("--no-cron", action="store_true", help="write config.json only")
    args = ap.parse_args()

    if (args.uninstall or not args.no_cron) and not shutil.which("crontab"):
        sys.exit("crontab not found - install cron first (apt install cron / dnf install cronie)")
    if args.uninstall:
        print("removed %d cron entry(ies)" % uninstall_cron())
        return

    cfg = config.load()
    tg = cfg.get("telegram") or {}
    print("daily-report setup (Enter keeps the value in [brackets])\n")

    cfg["name"] = ask("Name (shown in report title and Telegram caption, e.g. prod-1)", cfg.get("name"))
    token = ask_token(tg.get("token"))
    chat_id = ask("Telegram chat id (group ids look like -100…)", tg.get("chat_id"))
    cfg["telegram"] = {"token": token, "chat_id": chat_id}
    cfg["generators"] = ask_generators(cfg.get("generators") or [])
    tz = ask("UTC offset in hours for report times", "%g" % cfg.get("tz", config.local_tz()))
    cfg["tz"] = float(tz)
    cfg.setdefault("top", 25)
    cfg.setdefault("keep_days", 30)

    config.save(cfg)
    print("\nsaved %s" % config.PATH)

    if yes("Send a test message to the chat?"):
        try:
            telegram.send_message(token, chat_id, "✅ <b>%s</b>: daily-report is set up. Reports arrive daily at 23:59." % cfg["name"])
            print("  sent")
        except telegram.TelegramError as e:
            print("  %s\n  check the chat id and that the bot is a member of the group" % e)

    if not args.no_cron:
        print("\ncron installed:\n  %s" % install_cron())
        print("logs: %s" % os.path.join(ROOT, "logs", "cron.log"))
    print("\nrun now:  %s %s" % (sys.executable, os.path.join(ROOT, "run.py")))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(1)
