# Notifications server runbook

Commands for running the push-notification poller on the DigitalOcean server.
Everything lives in its own folder, separate from `gov_glance_spiders`.

| | |
|---|---|
| Server | `root@64.23.155.236` (`gov-glance-foundation`) |
| Project folder | `/root/gov_glance_notifications` |
| Script | `/root/gov_glance_notifications/src/poll_api_feeds.py` |
| Python | `/root/gov_glance_notifications/env` (its own virtualenv) |
| Branch | `corbinTest` |
| Secrets (not in git) | `/root/gov_glance_notifications/.env`, `/root/gov_glance_notifications/src/firebase2/` |
| Cache (not in git) | `/root/gov_glance_notifications/src/feed_state.json` |
| Log | `/root/gov_glance_notifications/src/poll.log` |

Test topic: `3.6.2_test`. Without `--live`, every run sends one combined push there;
live, each topic gets its own push. Either way the newest headline is the title,
and the body reads "Also new: <next headline>" and "+N more updates".

---

## Connect

From your Mac:

```
ssh root@64.23.155.236
```

Then go to the script's folder. Every command below assumes you are here:

```
cd /root/gov_glance_notifications/src
```

---

## Run it now

```
../env/bin/python poll_api_feeds.py
```

Sends anything new since the last run, as one push to `3.6.2_test`.

### Clear the cache, then send the last 24 hours

```
rm feed_state.json
../env/bin/python poll_api_feeds.py --since-hours 24
```

A run on an empty cache without `--since-hours` only rebuilds the cache and
sends nothing.

### Other runs

| Command | What it does |
|---|---|
| `../env/bin/python poll_api_feeds.py --probe` | One feed request; checks the API key works |
| `../env/bin/python poll_api_feeds.py --dry-run --since-hours 24` | Shows what would be sent; sends and saves nothing |
| `../env/bin/python poll_api_feeds.py --since-hours 24 --only-topic Justice` | One topic only |
| `../env/bin/python poll_api_feeds.py --show-cache` | Each feed's cached newest item |
| `../env/bin/python poll_api_feeds.py --dry-run -v` | Explains why each item was skipped |

---

## Schedule (cron)

Open the schedule:

```
crontab -e
```

Every 30 minutes, test topic (current):

```
*/30 * * * * cd /root/gov_glance_notifications/src && ../env/bin/python poll_api_feeds.py >> poll.log 2>&1
```

Other intervals: change the first part.

| Interval | Start of the line |
|---|---|
| Every 30 minutes | `*/30 * * * *` |
| Every hour | `0 * * * *` |
| Every 3 hours | `0 */3 * * *` |

Keep only one `poll_api_feeds.py` line. Leave any spider lines alone.
Save and exit in nano: Ctrl+O, Enter, Ctrl+X.

Check what is scheduled:

```
crontab -l
```

Pause it: run `crontab -e` and put `#` at the start of the line.

Times are server time (`timedatectl` shows the zone; usually UTC).

---

## Check that it is running

```
tail -n 20 poll.log
```

Each run ends with a line like:

```
INFO 283 feed(s) checked: 2 topic(s) with new items, 1 push(es) sent, 0 feed(s) initialized, 2 failed, 0 too old
```

`2 failed` is normal: two HUD department feeds return HTTP 500 from the API.

Follow the log live (Ctrl+C to stop):

```
tail -f poll.log
```

---

## Update the code

After pushing to `corbinTest` from your Mac:

```
cd /root/gov_glance_notifications
git pull
```

If `requirements.txt` changed:

```
env/bin/pip install -r requirements.txt
```

Cron picks up the new code on its next run.

### Changing the feed list

On your Mac, after editing the app's `feedsConfig.js`:

```
cd ~/Desktop/GovGlanceNotifications/gov_glance_notifications
node export-notification-feeds.mjs
git add src/notification-feeds.json
git commit -m "Update notification feeds"
git push
```

Then `git pull` on the server.

---

## Secrets

Not in git. Copied once from the Mac (asks for the server's root password):

```
scp ~/Desktop/GovGlanceNotifications/.env root@64.23.155.236:/root/gov_glance_notifications/
scp -r ~/Desktop/GovGlanceNotifications/gov_glance_notifications/src/firebase2 root@64.23.155.236:/root/gov_glance_notifications/src/
```

`.env` needs `REACT_APP_API_KEY=...`.

---

## App update notifications

Run by hand after a release is live in the App Store. Reaches only devices
with notifications allowed; tapping it opens the App Store page.

```
cd /root/gov_glance_notifications/src
../env/bin/python send_update_notification.py --version 3.7.0 --dry-run   # preview
../env/bin/python send_update_notification.py --version 3.7.0             # test phone (3.6.2_test)
../env/bin/python send_update_notification.py --version 3.7.0 --live      # everyone (asks you to type SEND)
```

| Option | Who gets it |
|---|---|
| (default, with `--live`) | Every install from the release with update notifications onward (`AppUpdates` topic) |
| `--live --only-versions 3.6.1,3.6.2` | Only devices still on those versions |
| `--live --legacy` | Only installs from before this release, via the plain topic names their old onboarding gave them (`LegislativeNews`, ...). Updated installs leave those names on first launch |
| `--title "..." --body "..."` | Custom text (default: "Gov Glance 3.7.0 is available" / "Update now for the latest features and fixes.") |

---

## Going live (real users)

1. Ship the app with `TEST_TOPIC_OVERRIDE = null` (notificationTopics.js) and
   `DEBUG_TAPS = false` (PushNotificationRouter.js).
2. On the server, start a fresh cache in live mode:
   ```
   cd /root/gov_glance_notifications/src
   rm feed_state.json
   ../env/bin/python poll_api_feeds.py --live
   ```
   This only fills the cache; nothing is sent.
3. `crontab -e` and add `--live` to the line:
   ```
   */30 * * * * cd /root/gov_glance_notifications/src && ../env/bin/python poll_api_feeds.py --live >> poll.log 2>&1
   ```

Live, each topic with news gets one push on its own FCM topic
(`ExecutiveNews`, `JusticeNews`, ...).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Key: MISSING` on `--probe` | `.env` missing or has no `REACT_APP_API_KEY`; check `ls -a /root/gov_glance_notifications` |
| `HTTP Error 401/403` | Wrong API key in `.env` |
| Firebase file not found | `ls src/firebase2`; copy it again with `scp` |
| `Another run is in progress; exiting` | A previous run is still going; the next scheduled run continues normally |
| Nothing sent | Nothing new since the last run. Use `--since-hours 24` to test |
| `git pull` refuses | `git status`; if only stray local changes, `git reset --hard origin/corbinTest` |
| Password prompt from the Mac | Type the server's root password (characters don't show) |