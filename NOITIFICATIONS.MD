# Gov Glance push notifications

How new government items reach users' phones, and what each file does. No
database: the server keeps a small cache file, and the app works out what's
new on its own.

## The flow

```
 server: cron, hourly                                  app
 poll_api_feeds.py                                     bell tapped -> FCM.subscribeTo("ExecutiveNews")
   1. asks the API for each feed's newest items
   2. compares with feed_state.json (cache)
   3. one push per topic with news  ------------>  "White House: Title ... and 3 other posts"
                                                        |
                                                        v tap
                                                   Notifications dashboard:
                                                   everything new in every followed topic
```

### Server: the cache

`feed_state.json` (next to the poller) keeps, per feed:

- `newest`: the newest item seen so far (id, title, created_at). It only moves forward.
- `seen`: ids of the most recent 100 items, for items that share a date.

An item is new when it isn't in `seen` and isn't older than `newest`, so a
late or re-sorted old item is never announced. New items go into the cache
only after their push sends, so a failed send is retried next run.
`--show-cache` prints each feed's cached newest item.

### App: what the dashboard calls new

Per topic, on this device: anything newer than the last visit to the
dashboard that hasn't been shown there before. The first visit for a topic
counts the last 48 hours. Leaving the page, or tapping "Mark all as read",
marks what was shown as read (`gg_notifications_seen` in localStorage). The
dashboard loads every feed of each followed topic straight from the feed API,
6 requests at a time, and caches responses for 5 minutes.

## Files

### Server (`gov_glance_notifications/src/`)

| File | What it does |
|---|---|
| `poll_api_feeds.py` | Hourly. Checks every feed, finds new items with the cache, pushes per FCM topic. |
| `notification-feeds.json` | The feed list: table, filters and topic for each feed. Generated, don't edit. |
| `send_notifications.py` | Message format (`build_message`) and the test topic name (`TEST_TOPIC`). |
| `notify.py` | Builds one FCM message (body length, image check). |
| `push_topics.py` | Maps a topic and country to its FCM topic name. |
| `feed_state.json` | Created by the poller: the cache. Delete it to start fresh (the next run only refills it). |

`export-notification-feeds.mjs`, at the `gov_glance_notifications/` root,
regenerates `src/notification-feeds.json` from the app's `feedsConfig.js`. Run
it on your Mac after changing feeds, then deploy the JSON.

### App

| File | Where | What it does |
|---|---|---|
| `NotificationButton.js` | `components/notifications/` | The bell. Subscribes to the topic's FCM topic. |
| `notificationTopics.js` | `components/notifications/` | Topic names and subscription state. Holds `TEST_TOPIC_OVERRIDE`. |
| `notificationRouting.js` | `components/notifications/` | Turns a tapped push into a dashboard URL. |
| `PushNotificationRouter.js` | `components/notifications/` | Listens for taps. Holds `DEBUG_TAPS`. |
| `NotificationsDashboard.js` / `.css` | `components/pages/` | The dashboard at `/notifications/dashboard`. |
| `App.js` | `src/` | Mounts the router and the dashboard route. |

## Setup

1. Server `.env` next to the poller: `REACT_APP_API_KEY=...` (the app's API key).
   Packages: `pip install firebase-admin python-dotenv certifi`.
2. First run fills the cache and sends nothing:
   ```
   python poll_api_feeds.py --live
   ```
3. Cron, hourly:
   ```
   0 * * * * cd /path/to/gov_glance_notifications/src && ../env/bin/python poll_api_feeds.py --live >> poll.log 2>&1
   ```

## Testing

With `TEST_TOPIC_OVERRIDE = '3.6.2_test'` in the app, every bell subscribes to
`3.6.2_test`. Without `--live`, the poller sends ONE combined push there:
"New in Executive, Justice and Legislative: 12 new items".

```
python poll_api_feeds.py --probe                        # one feed request, checks the key
python poll_api_feeds.py --dry-run --since-hours 24     # what would be sent; sends and saves nothing
python poll_api_feeds.py --since-hours 24               # send it to the test topic
python poll_api_feeds.py --since-hours 24 --only-topic Justice
python poll_api_feeds.py --show-cache                   # each feed's cached newest item
python poll_api_feeds.py --dry-run -v                   # explains every skipped item
```

While testing, a bell also records its real topic on the device, so the
dashboard lists the topics whose bells you tapped. (All bells still share the
one test subscription, so they show as on or off together.)

## Before shipping

- `notificationTopics.js`: `TEST_TOPIC_OVERRIDE = null`.
- `PushNotificationRouter.js`: `DEBUG_TAPS = false`.
- Rebuild, `npx cap sync ios`, test one tap from a closed app.

## Known limits

- Live, a user following several topics gets one push per topic with news.
  Combining them per person needs a server-side list of who follows what.
- Committee feeds and crawl feeds aren't checked (different endpoints).
- Two HUD department feeds return HTTP 500 from the API itself (Federal
  Housing Enterprise Oversight Office, Government National Mortgage
  Association). Both the poller and the dashboard skip them.