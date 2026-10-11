"""Constants for the Ouderportaal integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ouderportaal"

CONF_PORTAL: Final = "portal"

# Fired once per new day-rhythm moment, batch of photos or message.
EVENT_NEW_ENTRY: Final = "ouderportaal_new_entry"

# Poll often while the daycare is open, rarely at night.
DAY_START_HOUR: Final = 6
DAY_END_HOUR: Final = 20
DAY_UPDATE_INTERVAL: Final = timedelta(minutes=5)
NIGHT_UPDATE_INTERVAL: Final = timedelta(hours=1)

STORAGE_VERSION: Final = 1
# The history is written this long after it changes, so a burst of changes is written once.
HISTORY_SAVE_DELAY_S: Final = 10

# Each poll lists the Messages page items of this many recent days; a reply moves a conversation's date forward.
MESSAGES_POLL_DAYS: Final = 7

# The portal shows each part of its Messages page only to accounts with this system function.
AUTH_MESSAGES: Final = "WEB_PARENT_LOGBOOK"
AUTH_NEWS: Final = "WEB_HTML_NEWS_VIEW"
AUTH_NEWSLETTERS: Final = "WEB_ONLY_HTML_NEWSLETTER_ARCHIVE"
# The portal shows its Agenda, with the children's schedule, to accounts with any of these.
AUTH_AGENDA: Final = frozenset(
    {
        "WEB_PARENT_BOOKING_OVERVIEW",
        "WEB_PARENT_ACTIVITY_DATABASE",
        "WEB_PARENT_MEETINGS_VIEW",
        "WEB_ACTIVITY_PROGRAM_PARENT_OVERVIEW",
    }
)

# The schedule rarely changes, so it is read at most this often: from a month back to three months ahead, a month
# per request like the portal's Agenda.
SCHEDULE_UPDATE_INTERVAL: Final = timedelta(minutes=30)
SCHEDULE_PAST_DAYS: Final = 31
SCHEDULE_FUTURE_DAYS: Final = 92
SCHEDULE_REQUEST_DAYS: Final = 31

# Photos are saved under <media>/ouderportaal/<date>/.
ARCHIVE_DIR: Final = "ouderportaal"
MAX_PARALLEL_DOWNLOADS: Final = 3

# On first setup, import this much history by paging back through the timeline.
BACKFILL_DAYS: Final = 30
BACKFILL_MAX_PAGES: Final = 100
# Bump to run the import again on existing installs
# (2: fixed paging, which is by card offset; 3: messages; 4: news and newsletters).
BACKFILL_VERSION: Final = 4
# Pause between pages, to stay gentle on the portal.
BACKFILL_PAGE_DELAY_S: Final = 1.0
