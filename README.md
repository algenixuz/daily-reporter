# daily-report

Turns web-server access logs into one self-contained HTML report every day and sends it to a Telegram group.

- Generators: **traefik**, **nginx** (easy to add more)
- Python 3.7+, standard library only, nothing to `pip install`
- Reads plain and rotated `.gz` logs in one streaming pass
- Report: requests, errors (5xx / 499), latency p50/p95/p99, incidents, top/slowest/failing endpoints, top IPs, scanners, user agents

## Setup

```bash
git clone <repo-url> /opt/daily-report
cd /opt/daily-report
sudo python3 init.py
```

`init.py` asks:

| Question | Example |
|---|---|
| Name | `prod-1`, shown in the report title and the Telegram caption |
| Telegram bot token | `123456789:AA…`, from [@BotFather](https://t.me/BotFather) |
| Telegram chat id | `-1001234567890` |
| Generators | `traefik`, `nginx` or `traefik,nginx` |
| Log directory per generator | `/var/log/traefik` |
| UTC offset for report times | `5` (defaults to the server's offset) |

It then:

1. saves `config.json` with mode `600`, because the file holds the bot token,
2. can send a test message to the chat,
3. adds this line to the crontab of the user who ran it:

```
59 23 * * * cd /opt/daily-report && /usr/bin/python3 run.py >> logs/cron.log 2>&1 # daily-report:/opt/daily-report
```

You can run `init.py` again at any time. It shows the current values as defaults and replaces its own cron line, so you never get duplicates.

> Run `init.py` as a user that can read the log files. That is usually root, hence `sudo`. The cron job runs as the same user.

### Telegram bot and chat id

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy the token.
2. Add the bot to your group.
3. To find the chat id, send any message in the group, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"chat":{"id":-100…}`.
   If the result is empty, turn off the bot's privacy mode (BotFather → `/setprivacy` → Disable) or mention the bot in the group.

## Usage

Cron runs `run.py` at 23:59. It covers the whole current day (00:00 → now, in the configured UTC offset), writes `reports/<name>-<generator>-<date>.html` and sends each file to the chat with a short summary. If a generator fails, for example because there are no log files or nothing matched, the chat gets a ⚠️ message instead, so a missing report never goes unnoticed.

Manual runs:

```bash
python3 run.py                                  # same as cron: today, send to Telegram
python3 run.py --date 2026-10-14                # a past day
python3 run.py --no-send                        # only write reports/
python3 run.py -g nginx                         # one generator only
python3 run.py -g traefik --dir ./logs --since "2026-10-11 10:00" --until "2026-10-11 11:30" -o out.html --no-send
```

`run.py` needs no config with `-g ... --no-send`, which is handy for one-off reports on any machine.

Remove the cron job:

```bash
sudo python3 init.py --uninstall
```

## config.json

```json
{
  "name": "prod-1",
  "telegram": { "token": "123456789:AA...", "chat_id": "-1001234567890" },
  "generators": [
    { "type": "traefik", "dir": "/var/log/traefik", "glob": "access.log*" },
    { "type": "nginx",   "dir": "/var/log/nginx",   "glob": "access.log*" }
  ],
  "tz": 5.0,
  "top": 25,
  "keep_days": 30
}
```

- `glob`: which files inside `dir` to read. Rotated `access.log.1`, `access.log.2.gz` and so on are included.
- `top`: rows per table.
- `keep_days`: reports in `reports/` older than this are deleted. `0` keeps everything.

## Generators

### traefik

Traefik access logs in common (CLF) or JSON format. To turn them on in Traefik:

```yaml
accessLog:
  filePath: /var/log/traefik/access.log
  format: json        # or common
```

### nginx

Supports the stock `combined` format and JSON logs (`escape=json`). Requests are grouped by first path segment (`/api`, `/static`, …).

Stock `combined` has no timing, so the latency charts stay empty. To get latency, append `$request_time`:

```nginx
log_format timed '$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent '
                 '"$http_referer" "$http_user_agent" $request_time';
access_log /var/log/nginx/access.log timed;
```

JSON keys that are read: `time_iso8601`, `remote_addr`, `request_method`, `request_uri`, `status`, `body_bytes_sent`, `request_time`, `http_user_agent`.

### Adding a generator

Create `src/generators/<name>.py`:

```python
from ..report import clf_minute, iso_minute

NAME = "caddy"
TITLE = "Caddy report"
DEFAULT_DIR = "/var/log/caddy"
DEFAULT_GLOB = "access.log*"
GROUP_LABEL = "Host"            # the per-group table is grouped by this

def parse(line):
    # return (minute, ip, method, path, status, bytes, group, ms, user_agent), or None to skip the line
    # minute = minutes since epoch, UTC: use clf_minute("11/Oct/2026:05:28:01 +0000") or iso_minute("2026-10-11T05:28:01Z")
    ...
```

Then register it in `src/generators/__init__.py`:

```python
from . import caddy, nginx, traefik
GENERATORS = {m.NAME: m for m in (traefik, nginx, caddy)}
```

All aggregation and rendering happens in `src/report.py`, so a generator is only a parser.

## Layout

```
init.py                 interactive setup + crontab
run.py                  build reports, send to Telegram (cron entry point)
src/report.py           shared engine: reading, stats, incidents, HTML
src/telegram.py         Bot API client (sendDocument / sendMessage)
src/config.py           config.json load/save
src/generators/         one parser per log format
reports/                generated HTML (git-ignored)
logs/cron.log           cron output (git-ignored)
```

## Troubleshooting

- **Nothing arrives.** Check `logs/cron.log`. Then run `python3 run.py` by hand as the same user.
- **`no files match`**: wrong `dir` or `glob` in `config.json`.
- **`Forbidden: bot is not a member`** or **`chat not found`**: add the bot to the group and check the chat id. Supergroup ids start with `-100`.
- **Permission denied on logs**: re-run `sudo python3 init.py` so the cron job runs as root.
- **Times look shifted**: set `tz` in `config.json`. Cron itself fires at 23:59 in the server's timezone.
