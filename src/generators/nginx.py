"""Nginx access logs: "combined" format, optionally followed by $request_time, or JSON (escape=json).

Stock "combined" has no timing, so latency charts stay empty. To get latency, append
$request_time to the log_format, e.g.

    log_format timed '$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent '
                     '"$http_referer" "$http_user_agent" $request_time';

"rt=0.123" / "request_time=0.123" key-value style is recognised too.
Requests are grouped by first path segment (/api, /static, ...).
"""
import json
import re

from ..report import clf_minute, iso_minute

NAME = "nginx"
TITLE = "Nginx report"
DEFAULT_DIR = "/var/log/nginx"
DEFAULT_GLOB = "access.log*"
GROUP_LABEL = "Path prefix"
GROUPS_TITLE = "Path prefixes"

COMBINED = re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) (\d+|-) "[^"]*" "([^"]*)"(.*)$')
RT = re.compile(r'(?:^|\s)(?:rt=|request_time=)?(\d+\.\d{3})(?=\s|$)')


def prefix(path):
    seg = path.split("?", 1)[0].split("/", 2)
    return ("/" + seg[1])[:60] if len(seg) > 1 and seg[1] else "/"


def seconds_to_ms(v):
    try:
        return int(float(v) * 1000)
    except (TypeError, ValueError):
        return 0


def parse(line):
    if line.startswith("{"):
        j = json.loads(line)
        ts = j.get("time_iso8601") or j.get("time") or j.get("@timestamp")
        minute = iso_minute(ts) if ts else clf_minute(j["time_local"])
        path = j.get("request_uri") or j.get("uri") or "-"
        return (minute, j.get("remote_addr", "-"), j.get("request_method", "-"), path,
                int(j.get("status", 0)), int(j.get("body_bytes_sent") or 0), prefix(path),
                seconds_to_ms(j.get("request_time")), j.get("http_user_agent", "-"))
    m = COMBINED.match(line)
    if not m:
        return None
    ip, ts, method, path, st, size, ua, rest = m.groups()
    rt = RT.search(rest)
    return (clf_minute(ts), ip, method, path, int(st), 0 if size == "-" else int(size),
            prefix(path), seconds_to_ms(rt.group(1)) if rt else 0, ua)
