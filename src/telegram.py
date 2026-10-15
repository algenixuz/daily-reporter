"""Minimal Telegram Bot API client (urllib, multipart/form-data)."""
import json
import os
import urllib.error
import urllib.request
import uuid


class TelegramError(Exception):
    pass


def call(token, method, fields=None, files=None, timeout=120):
    """files: {field: (filename, bytes, content type)}"""
    boundary = uuid.uuid4().hex
    body = []
    for k, v in (fields or {}).items():
        if v is not None:
            body.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode())
    for k, (name, data, ctype) in (files or {}).items():
        body.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                     % (boundary, k, name, ctype)).encode() + data + b"\r\n")
    body.append(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (token, method), data=b"".join(body),
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            res = json.load(e)
        except ValueError:
            raise TelegramError("%s: HTTP %d %s" % (method, e.code, e.reason))
    except urllib.error.URLError as e:
        raise TelegramError("%s: %s" % (method, e.reason))
    if not res.get("ok"):
        raise TelegramError("%s: %s" % (method, res.get("description", res)))
    return res["result"]


def get_me(token):
    return call(token, "getMe", timeout=20)


def send_message(token, chat_id, text):
    return call(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}, timeout=30)


def send_document(token, chat_id, path, caption=""):
    with open(path, "rb") as f:
        data = f.read()
    return call(token, "sendDocument", {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                {"document": (os.path.basename(path), data, "text/html")})
