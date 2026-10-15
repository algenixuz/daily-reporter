"""Log-format generators. Each module defines:

    NAME          key used in config.json ("traefik")
    TITLE         report heading
    DEFAULT_DIR   where its logs usually live
    DEFAULT_GLOB  file pattern inside DEFAULT_DIR (rotated .gz files included)
    GROUP_LABEL   what the per-group table is grouped by ("Router", "Path prefix", ...)
    parse(line)   -> (minute, ip, method, path, status, bytes, group, ms, user_agent) or None

To add a format, drop a module in this folder and list it below.
"""
from . import nginx, traefik

GENERATORS = {m.NAME: m for m in (traefik, nginx)}
