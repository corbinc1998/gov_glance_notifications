"""Check each topic's feeds through the API and push new items to the FCM topics.

No database. Every run asks the API for the newest items in each feed the app
shows and compares them with a cache of what the last run saw
(feed_state.json, next to this script). For each feed the cache keeps:

    newest   the newest item seen so far: id, title, created_at
    seen     ids of the most recent items, for items that share a date

An item is new when it isn't in `seen` and isn't older than the cached newest
item. So an old item that shows up late (a backfill, a re-sorted feed) is never
announced, and the cache always says what the newest item in each feed is.

Each topic with new items gets one push on its FCM topic ("ExecutiveNews",
"CanadaJusticeNews", ...). In testing (no --live) everything goes as ONE
combined push to the test topic: "New in Executive, Justice and Legislative:
12 new items".

    python poll_api_feeds.py --probe                   # check one feed request
    python poll_api_feeds.py --dry-run                 # show what would be sent; change nothing
    python poll_api_feeds.py                           # testing: one combined push to the test topic
    python poll_api_feeds.py --only-topic Executive    # one topic only
    python poll_api_feeds.py --since-hours 24          # treat the last day's items as new (testing)
    python poll_api_feeds.py --live                    # real users
    python poll_api_feeds.py --show-cache              # print each feed's cached newest item

The first run for each feed only fills the cache, so existing items are never
announced. Run it hourly from cron once it works:

    0 * * * * cd /path/to/src && ../env/bin/python poll_api_feeds.py --live >> poll.log 2>&1

Feed list: notification-feeds.json, made by export-notification-feeds.mjs from
the app's feedsConfig.js.

Feed API: requests are built the way the app's src/hooks/useFeedData.js builds
them (table feeds from /posts/recent, department feeds from /departments/),
authenticated with "Authorization: Bearer <key>". The key is read from
FEED_API_KEY, else REACT_APP_API_KEY, else GOV_GLANCE_API_KEY.
"""
import argparse
import fcntl
import json
import logging
import os
import re
import ssl
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from notify import MAX_PAYLOAD_SIZE, InitFirebaseApp, SendFcmMessage
from push_topics import build_push_topic
from send_notifications import TEST_TOPIC, build_message, to_data_value

log = logging.getLogger("poll_api_feeds")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FEEDS_PATH = os.path.join(HERE, "notification-feeds.json")
STATE_PATH = os.path.join(HERE, "feed_state.json")

# ---------------------------------------------------------------------------
# Feed API, as src/hooks/useFeedData.js calls it.
# ---------------------------------------------------------------------------
FEED_API_BASE = os.environ.get("FEED_API_BASE", "https://api.govglance.org").rstrip("/")
FEED_API_KEY = (
    os.environ.get("FEED_API_KEY")
    or os.environ.get("REACT_APP_API_KEY")
    or os.environ.get("GOV_GLANCE_API_KEY")
)

ITEMS_PER_FEED = 10        # newest items requested from each feed
SEEN_PER_FEED = 100        # ids remembered per feed
DEFAULT_MAX_AGE_DAYS = 2   # new-to-us items older than this aren't announced
FUTURE_TOLERANCE = timedelta(hours=12)  # same rule as the app's dropFutureDated
REQUEST_TIMEOUT = 20       # seconds
WORKERS = 8                # feeds fetched at once

# Combined (digest) message, used for the test topic.
DIGEST_TITLE = "Gov Glance"
DIGEST_PREVIEW_COUNT = 3   # posts embedded for the app's notification bubbles
DIGEST_NAMED_TOPICS = 3    # topics named before "and N more"
PREVIEW_FIELDS = ("title", "table_id", "table_source_name", "country_schema", "collection_name")

# Envelope keys the app's extractPostsArray accepts, in its order.
ENVELOPE_KEYS = ("results", "posts", "data", "items", "recent", "documents")
OLDEST = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Feeds and the API
# ---------------------------------------------------------------------------

def load_feeds(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return [
        f for f in data.get("feeds", [])
        # Crawl feeds live on a different service (crawl.govglance.org); not handled here.
        if f.get("kind") in ("table", "agency") and "crawl=" not in f.get("params", "")
    ]


def feed_url(feed, limit=ITEMS_PER_FEED):
    """The request useFeedData makes for this feed."""
    params = feed["params"]
    if feed["kind"] == "agency":
        # Department feeds (table_name & agency).
        return f"{FEED_API_BASE}/departments/?schema_name=united_states_of_america&{params}&limit={limit}"
    return f"{FEED_API_BASE}/posts/recent?limit={limit}&skip=0&{params}"


def _ssl_context():
    """Verify HTTPS with certifi's CA bundle when it's installed.

    The python.org installer on macOS ships without root certificates, so the
    default context fails with CERTIFICATE_VERIFY_FAILED. certifi comes with
    firebase-admin, so it's normally present.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = _ssl_context()


def fetch_json(url):
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": "govglance-notifier"}
    if FEED_API_KEY:
        headers["Authorization"] = f"Bearer {FEED_API_KEY}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT) as response:
        return json.loads(response.read().decode("utf-8"))


def items_from(body):
    """A feed response as a flat list, like the app's extractPostsArray.

    Accepts a bare list, an envelope ({"results": [...]} and similar), or a map
    grouped by collection ({"senate_news": [...], ...}). None if it's none of these.
    """
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ENVELOPE_KEYS:
            if isinstance(body.get(key), list):
                return body[key]
        values = list(body.values())
        if values and all(isinstance(v, list) for v in values):
            return [item for group in values for item in group]
    return None


def fetch_feed(feed):
    """(feed, items) or (feed, None) when the request fails."""
    url = feed_url(feed)
    try:
        items = items_from(fetch_json(url))
        if items is None:
            log.warning("Unexpected response shape for %s (%s)", feed["key"], url)
        return feed, items
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        log.warning("Request failed for %s: %s (%s)", feed["key"], error, url)
        return feed, None


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def item_id(item):
    value = item.get("id")
    return None if value is None else str(value)


def item_title(item):
    for key in ("status_title", "title", "name"):
        if item.get(key):
            return str(item[key]).strip()
    return None


DATE_ONLY_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")      # 2026-11-10
DATE_ONLY_US = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")  # 11-10-2026, as the app displays dates


def parse_time(value):
    """(start, end) of a created_at value, both aware UTC, or (None, None).

    A full timestamp gives start == end. A date with no time ("2026-11-10")
    covers that whole day: start is midnight, end is the last moment of the day.
    """
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()

    day = None
    if DATE_ONLY_ISO.match(text):
        day = datetime.strptime(text, "%Y-%m-%d")
    else:
        us = DATE_ONLY_US.match(text)
        if us:
            month, day_of_month, year = (int(part) for part in us.groups())
            try:
                day = datetime(year, month, day_of_month)
            except ValueError:
                return None, None
    if day is not None:
        start = day.replace(tzinfo=timezone.utc)
        return start, start + timedelta(days=1) - timedelta(microseconds=1)

    parsed = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)  # RFC 2822, e.g. "Mon, 10 Nov 2026 14:00:00 GMT"
        except (TypeError, ValueError, IndexError):
            return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if (parsed.hour, parsed.minute, parsed.second, parsed.microsecond) == (0, 0, 0, 0):
        # Exactly midnight is almost always a date saved as a timestamp
        # ("2026-11-10T00:00:00+00:00"), so it covers the whole day too.
        return parsed, parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed, parsed


def item_time(item):
    return parse_time(item.get("created_at"))


def image_of(item):
    value = item.get("image_url")
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None


# ---------------------------------------------------------------------------
# Cache (feed_state.json)
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return {"feeds": {}}
    state.setdefault("feeds", {})
    return state


def save_state(state):
    tmp = f"{STATE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1)
    os.replace(tmp, STATE_PATH)


def cached_newest_start(entry):
    """When the cached newest item was created (start of its day for a date), or None."""
    newest = (entry or {}).get("newest") or {}
    return parse_time(newest.get("created_at"))[0]


def remember(state, feed_key, items):
    """Add items to a feed's cache: their ids to `seen`, and the newest one to `newest`.

    `newest` only moves forward, so it always names the newest item ever seen
    in the feed, even if that item later drops out of the API's response.
    """
    entry = state["feeds"].setdefault(feed_key, {"seen": []})
    ids = [i for i in (item_id(item) for item in items) if i]
    entry["seen"] = ([i for i in ids if i not in entry["seen"]] + entry["seen"])[:SEEN_PER_FEED]

    # The API lists newest first, so walk the list backwards: on a tie (items
    # sharing a date) the one listed first wins.
    current = cached_newest_start(entry)
    for item in reversed(items):
        start = item_time(item)[0]
        if item_id(item) and start and (current is None or start >= current):
            current = start
            entry["newest"] = {
                "id": item_id(item),
                "title": item_title(item),
                "created_at": item.get("created_at"),
            }
    entry["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def show_cache(state, feeds):
    for feed in feeds:
        newest = (state["feeds"].get(feed["key"]) or {}).get("newest")
        if newest:
            print(f"{feed['key']:<60} {str(newest.get('created_at'))[:25]:<25} {str(newest.get('title'))[:70]}")
        else:
            print(f"{feed['key']:<60} (nothing cached)")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def join_names(names):
    """['A', 'B', 'C'] -> 'A, B and C'; long lists end with 'and N more'."""
    if len(names) > DIGEST_NAMED_TOPICS:
        shown = names[:DIGEST_NAMED_TOPICS]
        return f"{', '.join(shown)} and {len(names) - len(shown)} more"
    if len(names) <= 2:
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def build_topic_message(entry, fcm_topic):
    """The usual one-topic message ("Title ... and 4 other posts")."""
    return build_message({"topic": entry["label"], "results": entry["rows"]}, fcm_topic)


def build_combined_message(news, fcm_topic):
    """One message covering every topic with news.

    One topic: the usual message. Several: "New in Legislative, Executive and
    Justice: 28 new items", with data type "digest" so a tap opens the
    dashboard without pinning a single topic.
    """
    if len(news) == 1:
        (entry,) = news.values()
        return build_topic_message(entry, fcm_topic)

    # Busiest topics first, so the names shown are the ones with the most news.
    ordered = sorted(news.values(), key=lambda entry: (-len(entry["rows"]), entry["label"]))
    labels = [entry["label"] for entry in ordered]
    total = sum(len(entry["rows"]) for entry in ordered)
    body = f"New in {join_names(labels)}: {total} new item{'s' if total != 1 else ''}"

    rows = [row for entry in ordered for row in entry["rows"]]
    rows.sort(key=lambda row: row.get("_time") or OLDEST, reverse=True)
    preview = [{key: to_data_value(row.get(key)) for key in PREVIEW_FIELDS}
               for row in rows[:DIGEST_PREVIEW_COUNT]]

    while True:
        data = {
            "type": "digest",
            "topics": ",".join(labels),
            "schema": rows[0].get("country_schema") if rows else "",
            "results": json.dumps(preview),
        }
        message = SendFcmMessage(notification_title=DIGEST_TITLE, body=body, data=data, topic=fcm_topic)
        if message.payload_size() <= MAX_PAYLOAD_SIZE or not preview:
            return message
        preview.pop()


def describe(message):
    return f"{message.notification_title}: {message.body}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def probe(feeds):
    feed = next((f for f in feeds if f["kind"] == "table"), feeds[0])
    url = feed_url(feed, limit=2)
    print(f"Feed:  {feed['key']} ({feed['name']})\nURL:   {url}")
    print(f"Key:   {'set' if FEED_API_KEY else 'MISSING (add REACT_APP_API_KEY to .env)'}")
    try:
        body = fetch_json(url)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        print(f"Request failed: {error}")
        if isinstance(error, HTTPError) and error.code in (401, 403):
            print("The API rejected the key. Use the app's REACT_APP_API_KEY value.")
        return 1
    items = items_from(body)
    print(f"Items: {'none found' if items is None else len(items)}")
    print(json.dumps(items[0] if items else body, indent=2, default=str)[:1500])
    return 0 if items else 1


def find_new_items(feed, items, entry, args, now):
    """The items in one feed response that should be announced, and why others weren't."""
    since = now - timedelta(hours=args.since_hours) if args.since_hours is not None else None
    cutoff = now - timedelta(days=args.max_age_days)
    future_limit = now + FUTURE_TOLERANCE
    seen = set(entry["seen"]) if entry else set()
    newest_start = cached_newest_start(entry)

    new, too_old = [], 0
    for item in items:
        identifier = item_id(item)
        title = item_title(item)
        raw_date = item.get("created_at")
        if not identifier or not title:
            log.debug("skip %s: no %s", feed["key"], "id" if not identifier else "title")
            continue
        start, end = item_time(item)
        if start and start > future_limit:
            # Bad future-dated record; the app hides these, so don't announce them.
            log.debug("skip %s #%s: future-dated (%s)", feed["key"], identifier, raw_date)
            continue

        if since is not None:
            # Testing: anything inside the window counts, seen or not.
            if not end or end < since:
                log.debug("skip %s #%s: %r is outside the window", feed["key"], identifier, raw_date)
                continue
        else:
            if identifier in seen:
                continue
            if newest_start and end and end < newest_start:
                # Older than the newest item already cached: a late or re-sorted item.
                log.debug("skip %s #%s: older than cached newest (%s)", feed["key"], identifier, raw_date)
                continue
            if end and end < cutoff:
                too_old += 1
                continue
        new.append(item)
    return new, too_old


def main():
    parser = argparse.ArgumentParser(description="Push new feed items to the FCM topics that follow them.")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--topic", default=TEST_TOPIC,
                        help=f"testing: send one combined push to this topic (default: {TEST_TOPIC})")
    target.add_argument("--live", action="store_true", help="send to the real per-topic FCM topics")
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent; send and save nothing")
    parser.add_argument("--probe", action="store_true", help="fetch one feed and print the response, then stop")
    parser.add_argument("--show-cache", action="store_true", help="print each feed's cached newest item, then stop")
    parser.add_argument("--only-topic", default=None, help="only check this topic (e.g. Executive)")
    parser.add_argument("--since-hours", type=float, default=None,
                        help="treat items created in the last N hours as new, even if seen (testing)")
    parser.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS,
                        help=f"don't announce items older than this (default {DEFAULT_MAX_AGE_DAYS})")
    parser.add_argument("--feeds", default=DEFAULT_FEEDS_PATH, help="path to notification-feeds.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    feeds = load_feeds(args.feeds)
    if args.only_topic:
        wanted = args.only_topic.strip().lower()
        feeds = [f for f in feeds if f["topic"].lower() == wanted]
    if not feeds:
        raise SystemExit("No feeds to check. Check --feeds / --only-topic.")
    if args.probe:
        return probe(feeds)
    if args.show_cache:
        show_cache(load_state(), feeds)
        return 0

    # One run at a time: an overlapping run would announce the same items twice.
    lock = open(f"{STATE_PATH}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("Another run is in progress; exiting")
        return 0

    state = load_state()
    now = datetime.now(timezone.utc)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fetched = list(pool.map(fetch_feed, feeds))

    # topic -> {(table, id): (feed, item)}; new items per feed, cached once their topic sends.
    pending = defaultdict(dict)
    pending_cache = defaultdict(lambda: defaultdict(list))
    counts = {"failed": 0, "initialized": 0, "too_old": 0}

    for feed, items in fetched:
        if items is None:
            counts["failed"] += 1
            continue
        entry = state["feeds"].get(feed["key"])

        if entry is None and args.since_hours is None:
            # First time this feed is seen: fill the cache, announce nothing.
            if not args.dry_run:
                remember(state, feed["key"], items)
            counts["initialized"] += 1
            continue

        new, too_old = find_new_items(feed, items, entry, args, now)
        counts["too_old"] += too_old
        new_ids = {item_id(item) for item in new}

        # Items that aren't new go in the cache now; new ones after their push sends,
        # so a failed send is retried next run.
        if not args.dry_run:
            remember(state, feed["key"], [item for item in items if item_id(item) not in new_ids])
        for item in new:
            pending_cache[feed["topic"]][feed["key"]].append(item)
            key = (feed["table"], item_id(item))
            current = pending[feed["topic"]].get(key)
            # The most specific feed (most filters) names the item.
            if current is None or len(feed.get("filters", [])) > len(current[0].get("filters", [])):
                pending[feed["topic"]][key] = (feed, item)

    # Rows per topic, newest first, keyed by the FCM topic the topic maps to.
    news = {}
    for topic, entries in sorted(pending.items()):
        rows = []
        for (table, identifier), (feed, item) in entries.items():
            rows.append({
                "id": identifier,
                "topic": topic,
                "title": item_title(item),
                "table_id": identifier,
                "table_source_name": table,
                "country_schema": feed.get("schema") or "united_states_of_america",
                "collection_name": feed.get("name") or table.replace("_", " ").title(),
                "image_url": image_of(item),
                "_time": item_time(item)[0] or OLDEST,
                "_date": item.get("created_at"),
            })
        rows.sort(key=lambda row: row["_time"], reverse=True)
        fcm_topic = build_push_topic(topic, rows[0]["country_schema"])
        if not fcm_topic:
            log.warning("No FCM topic for %r; skipping", topic)
            continue
        news[fcm_topic] = {"label": topic, "rows": rows}

    if args.dry_run:
        for fcm_topic, entry in news.items():
            print(f"{entry['label']} ({fcm_topic}): {len(entry['rows'])} new")
            for row in entry["rows"][:10]:
                print(f"    {str(row['_date'])[:25]:<25} [{row['collection_name']}] {row['title'][:80]}")

    # Build the pushes: one per FCM topic live, one combined push when testing.
    sends = []  # (message, FCM topics it covers)
    if news and args.live:
        for fcm_topic, entry in news.items():
            sends.append((build_topic_message(entry, fcm_topic), {fcm_topic}))
    elif news:
        sends.append((build_combined_message(news, args.topic), set(news)))

    delivered = set()
    if sends and not args.dry_run:
        InitFirebaseApp()
    for message, covers in sends:
        if args.dry_run:
            print(f"  {message.topic} <- {describe(message)}")
        elif message.send_notification(raise_on_error=False):
            log.info("Sent to %s: %s", message.topic, describe(message))
            delivered |= covers
        else:
            log.error("Send to %s failed; its items will be retried next run", message.topic)

    # New items go in the cache once their push went out.
    if not args.dry_run:
        for fcm_topic, entry in news.items():
            if fcm_topic in delivered:
                for feed_key, items in pending_cache[entry["label"]].items():
                    remember(state, feed_key, items)
        save_state(state)

    log.info(
        "%d feed(s) checked: %d topic(s) with new items%s, %d push(es) sent, "
        "%d feed(s) initialized, %d failed, %d too old",
        len(feeds), len(news), " (dry run)" if args.dry_run else "",
        0 if args.dry_run else len([1 for _, covers in sends if covers <= delivered]),
        counts["initialized"], counts["failed"], counts["too_old"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())