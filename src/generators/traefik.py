"""Traefik access logs, common/CLF or JSON format."""
import json
import re

from ..report import clf_minute, iso_minute

NAME = "traefik"
TITLE = "Traefik report"
DEFAULT_DIR = "/var/log/traefik"
DEFAULT_GLOB = "access.log*"
GROUP_LABEL = "Router"

CLF = re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) (\d+|-) "[^"]*" "([^"]*)" \d+ "([^"]*)" "[^"]*" (\d+)ms')


def parse(line):
    if line.startswith("{"):
        j = json.loads(line)
        ts = j.get("StartUTC") or j.get("time")
        return (iso_minute(ts), j.get("ClientHost", "-"), j.get("RequestMethod", "-"),
                j.get("RequestPath", "-"), int(j.get("DownstreamStatus", 0)),
                int(j.get("DownstreamContentSize") or 0), j.get("RouterName", "-"),
                int(j.get("Duration", 0)) // 1000000, j.get("request_User-Agent", "-"))
    m = CLF.match(line)
    if not m:
        return None
    ip, ts, method, path, st, size, ua, router, ms = m.groups()
    return (clf_minute(ts), ip, method, path, int(st), 0 if size == "-" else int(size),
            router, int(ms), ua)
