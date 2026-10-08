"""Constants for the Ouderportaal integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ouderportaal"

CONF_PORTAL: Final = "portal"

# Fired once per new day-rhythm moment or new photo.
EVENT_NEW_ENTRY: Final = "ouderportaal_new_entry"

# Poll often while the daycare is open, rarely at night.
DAY_START_HOUR: Final = 6
DAY_END_HOUR: Final = 20
DAY_UPDATE_INTERVAL: Final = timedelta(minutes=5)
NIGHT_UPDATE_INTERVAL: Final = timedelta(hours=1)

STORAGE_VERSION: Final = 1

# Photos are saved under <media>/ouderportaal/<date>/.
ARCHIVE_DIR: Final = "ouderportaal"
MAX_PARALLEL_DOWNLOADS: Final = 3

# On first setup, import this much history by paging back through the timeline.
BACKFILL_DAYS: Final = 30
BACKFILL_MAX_PAGES: Final = 100
# Bump to run the import again on existing installs (2: fixed paging, which is by card offset).
BACKFILL_VERSION: Final = 2
# Pause between pages, to stay gentle on the portal.
BACKFILL_PAGE_DELAY_S: Final = 1.0
