#!/usr/bin/env python3
"""
Polls the Telegram Bot API for new messages and writes them into posts.json.
Run by .github/workflows/live-feed.yml.

Env:
  TELEGRAM_BOT_TOKEN  token from @BotFather (repository secret)
  ALLOWED_CHAT_IDS    comma-separated chat IDs allowed to post (repository variable)

Reporter commands (sent to the bot):
  any message          -> new post (text, formatting, links, photos with captions)
  edit a message       -> edits that post
  /delete  (or /undo)  -> removes that reporter's most recent post
"""
import html, json, os, pathlib, sys, time, urllib.parse, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
POSTS_FILE = ROOT / "posts.json"
STATE_FILE = ROOT / "state.json"
MEDIA_DIR = ROOT / "media"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = {s.strip() for s in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if s.strip()}
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"


def api(method, **params):
    body = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in params.items()}
    req = urllib.request.Request(f"{API}/{method}", data=urllib.parse.urlencode(body).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {res}")
    return res["result"]


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


TAGS = {
    "bold": ("<strong>", "</strong>"),
    "italic": ("<em>", "</em>"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"),
    "code": ("<code>", "</code>"),
    "pre": ("<pre>", "</pre>"),
    "blockquote": ("<blockquote>", "</blockquote>"),
    "expandable_blockquote": ("<blockquote>", "</blockquote>"),
}


def entities_to_html(text, entities):
    """Telegram text + entities -> HTML. Entity offsets are in UTF-16 code units."""
    units = text.encode("utf-16-le")
    n = len(units) // 2
    marks = []  # (position, kind, tiebreak, tag); kind 0 = close, 1 = open
    for e in entities or []:
        t, o, l = e.get("type"), e["offset"], e["length"]
        if t in TAGS:
            open_tag, close_tag = TAGS[t]
        elif t in ("text_link", "url"):
            url = e["url"] if t == "text_link" else units[2 * o:2 * (o + l)].decode("utf-16-le")
            if "://" not in url:
                url = "https://" + url
            open_tag, close_tag = f'<a href="{html.escape(url, quote=True)}">', "</a>"
        else:
            continue
        marks.append((o, 1, -l, open_tag))        # outer (longer) opens first
        marks.append((o + l, 0, -o, close_tag))   # inner (later start) closes first
    marks.sort(key=lambda m: m[:3])

    out, buf, mi = [], bytearray(), 0
    for i in range(n + 1):
        if mi < len(marks) and marks[mi][0] == i:
            out.append(html.escape(bytes(buf).decode("utf-16-le")))
            buf = bytearray()
            while mi < len(marks) and marks[mi][0] == i:
                out.append(marks[mi][3])
                mi += 1
        if i < n:
            buf += units[2 * i:2 * i + 2]
    out.append(html.escape(bytes(buf).decode("utf-16-le")))

    s = "".join(out).replace("\r\n", "\n").strip()
    if not s:
        return ""
    paragraphs = [p.strip().replace("\n", "<br>") for p in s.split("\n\n")]
    return "".join(f"<p>{p}</p>" for p in paragraphs if p)


def download_photo(photo_sizes):
    best = photo_sizes[-1]  # largest size
    info = api("getFile", file_id=best["file_id"])
    ext = pathlib.Path(info["file_path"]).suffix or ".jpg"
    name = f"{best['file_unique_id']}{ext}"
    dest = MEDIA_DIR / name
    if not dest.exists():
        MEDIA_DIR.mkdir(exist_ok=True)
        urllib.request.urlretrieve(f"{FILE_API}/{info['file_path']}", dest)
    return f"media/{name}"


def apply_message(m, is_edit, posts):
    chat_id, msg_id = m["chat"]["id"], m["message_id"]
    raw = m.get("text") or m.get("caption") or ""
    ents = m.get("entities") or m.get("caption_entities") or []

    if not is_edit and raw.startswith("/"):
        cmd = raw.split()[0].split("@")[0].lower()
        if cmd in ("/delete", "/undo"):
            for i, p in enumerate(posts):
                if p["chat_id"] == chat_id:
                    print(f"Deleted post {p['id']}")
                    del posts[i]
                    return True
        return False  # /start and unknown commands are ignored

    body = entities_to_html(raw, ents)
    photos = [download_photo(m["photo"])] if (m.get("photo") and not is_edit) else []
    pid = f"{chat_id}:{msg_id}"

    for p in posts:
        if p["id"] == pid:
            p["html"], p["edited"] = body, m.get("edit_date")
            print(f"Edited post {pid}")
            return True
    if is_edit:
        return False  # edit of a message that was never posted (or already deleted)

    group = m.get("media_group_id")
    if group:  # photo albums arrive as several messages; merge into one post
        for p in posts:
            if p.get("media_group_id") == group:
                p["photos"].extend(photos)
                if body and not p["html"]:
                    p["html"] = body
                print(f"Added photo to post {p['id']}")
                return True

    if not body and not photos:
        return False
    posts.insert(0, {
        "id": pid, "chat_id": chat_id, "message_id": msg_id,
        "date": m["date"], "edited": None,
        "from": (m.get("from") or {}).get("first_name", ""),
        "html": body, "photos": photos, "media_group_id": group,
    })
    print(f"New post {pid}")
    return True


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    state = load_json(STATE_FILE, {"last_update_id": 0})
    data = load_json(POSTS_FILE, {"updated": None, "posts": []})
    posts = data.setdefault("posts", [])
    offset = int(state.get("last_update_id", 0)) + 1
    changed, unknown = False, {}

    while True:
        updates = api("getUpdates", offset=offset, limit=100, timeout=0,
                      allowed_updates=["message", "edited_message"])
        for u in updates:
            offset = u["update_id"] + 1
            m = u.get("message") or u.get("edited_message")
            if not m:
                continue
            chat = m["chat"]
            cid = str(chat["id"])
            if cid not in ALLOWED:
                unknown[cid] = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
                continue
            if apply_message(m, "edited_message" in u, posts):
                changed = True
        if len(updates) < 100:
            break

    if unknown:
        print("Messages from chats NOT in ALLOWED_CHAT_IDS were ignored:")
        for cid, name in unknown.items():
            print(f"  {cid}   ({name})")

    if not ALLOWED:
        print("ALLOWED_CHAT_IDS is empty: nothing posted, nothing marked as read. "
              "Add the IDs above as a repository variable and run again.")
        write_output("changed", "false")
        return

    if changed:
        posts.sort(key=lambda p: p["date"], reverse=True)
        data["updated"] = int(time.time())
        save_json(POSTS_FILE, data)
    state["last_update_id"] = offset - 1
    save_json(STATE_FILE, state)
    write_output("changed", "true" if changed else "false")
    print(f"Done: {len(posts)} posts, changed={changed}")


if __name__ == "__main__":
    main()
