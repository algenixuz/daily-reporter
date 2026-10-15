"""config.json next to init.py / run.py. Written by init.py, mode 600 (it holds the bot token)."""
import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "config.json")


def local_tz():
    """Server's current UTC offset in hours, e.g. 5.0."""
    return datetime.now().astimezone().utcoffset().total_seconds() / 3600


def load():
    if not os.path.exists(PATH):
        return {}
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)


def save(cfg):
    fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(PATH, 0o600)
