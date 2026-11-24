"""Tell users a new version of the app is out. Run by hand when a release ships.

Only devices that allowed notifications get it: the app subscribes every such
device to the "AppUpdates" topic, plus one topic for the version it runs
("app-version-3.7.0"), each time it launches (pushRegistration.js). Tapping the
notification opens the App Store page.

    python send_update_notification.py --version 3.7.0 --dry-run    # show it; send nothing
    python send_update_notification.py --version 3.7.0              # testing: to 3.6.2_test only
    python send_update_notification.py --version 3.7.0 --live       # everyone with notifications on
    python send_update_notification.py --version 3.7.0 --live --only-versions 3.6.1,3.6.2
    python send_update_notification.py --version 3.7.0 --live --legacy

Audiences (with --live):

    default          "AppUpdates": every install from this release on.
    --only-versions  just devices running these versions, for example
                     everyone not yet on the new one.
    --legacy         installs from before this release, which never joined
                     "AppUpdates". They are reached through the plain topic
                     names the old onboarding subscribed them to
                     ("LegislativeNews", "ExecutiveNews", "JudicialNews", per
                     country). New versions leave those names on first launch
                     (they use "v2-..." topics), so only old installs get it.

--live asks you to type SEND before anything goes out (skip with --yes).
"""
import argparse
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from notify import InitFirebaseApp, SendFcmMessage
from send_notifications import TEST_TOPIC

# Must match pushRegistration.js in the app.
UPDATE_TOPIC = "AppUpdates"
VERSION_TOPIC_PREFIX = "app-version-"

APP_STORE_URL = "https://apps.apple.com/app/id6448072618"

# FCM allows at most 5 topics in one condition.
MAX_CONDITION_TOPICS = 5

# What the old onboarding subscribed people to, per country.
LEGACY_TOPICS = {
    "us": ["LegislativeNews", "ExecutiveNews", "JudicialNews"],
    "ca": ["CanadaLegislativeNews", "CanadaExecutiveNews", "CanadaJudicialNews"],
    "mx": ["MexicoLegislativeNews", "MexicoExecutiveNews", "MexicoJudicialNews"],
    "uk": ["UKLegislativeNews", "UKExecutiveNews", "UKJudicialNews"],
}


def version_topic(version):
    return f"{VERSION_TOPIC_PREFIX}{version.strip()}"


def condition_for(topics):
    return " || ".join(f"'{topic}' in topics" for topic in topics)


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def build(args, topic=None, condition=None):
    data = {"type": "update", "version": args.version, "url": args.url}
    return SendFcmMessage(
        notification_title=args.title or f"Gov Glance {args.version} is available",
        body=args.body,
        data=data,
        topic=topic,
        condition=condition,
    )


def plan(args):
    """The messages to send: one per topic or condition."""
    if not args.live:
        return [build(args, topic=args.topic)]
    if args.only_versions:
        versions = [v for v in args.only_versions.split(",") if v.strip()]
        topics = [version_topic(v) for v in versions]
        # A device is on one version topic, so no one gets two of these.
        return [build(args, condition=condition_for(group)) for group in chunks(topics, MAX_CONDITION_TOPICS)]
    if args.legacy:
        # One per country, to the plain pre-v2 names only old installs are still
        # on. Someone following topics in two countries gets two.
        return [build(args, condition=condition_for(topics)) for topics in LEGACY_TOPICS.values()]
    return [build(args, topic=UPDATE_TOPIC)]


def main():
    parser = argparse.ArgumentParser(description="Send an app update notification.")
    parser.add_argument("--version", required=True, help="the new version, e.g. 3.7.0")
    parser.add_argument("--title", default=None, help='default: "Gov Glance <version> is available"')
    parser.add_argument("--body", default="Update now for the latest features and fixes.",
                        help="notification text")
    parser.add_argument("--url", default=APP_STORE_URL, help="opened when the notification is tapped")
    parser.add_argument("--topic", default=TEST_TOPIC, help=f"testing: send here (default {TEST_TOPIC})")
    parser.add_argument("--live", action="store_true", help="send to real users")
    audience = parser.add_mutually_exclusive_group()
    audience.add_argument("--only-versions", default=None,
                          help="with --live: only devices on these versions, comma-separated")
    audience.add_argument("--legacy", action="store_true",
                          help="with --live: installs from before update notifications existed")
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent; send nothing")
    parser.add_argument("--yes", action="store_true", help="with --live: skip the confirmation")
    args = parser.parse_args()

    if (args.only_versions or args.legacy) and not args.live:
        parser.error("--only-versions and --legacy need --live")

    messages = plan(args)
    for message in messages:
        print(f"  {message.topic or message.condition}")
        print(f"    {message.notification_title}: {message.body}")
    if args.dry_run:
        print("Dry run; nothing sent.")
        return 0

    if args.live and not args.yes:
        answer = input(f"Send to real users ({len(messages)} message(s))? Type SEND to confirm: ")
        if answer.strip() != "SEND":
            print("Cancelled.")
            return 1

    InitFirebaseApp()
    failed = sum(not message.send_notification(raise_on_error=False) for message in messages)
    print(f"{len(messages) - failed} sent, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())