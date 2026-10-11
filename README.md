# Ouderportaal (Konnect) for Home Assistant

Get your child's daycare day rhythm, photos and messages from a Konnect ouderportaal (`<name>.ouderportaal.nl`) into Home Assistant.
Get a notification when a bottle, nap or diaper is logged, new photos arrive or the daycare sends a message.

This is an unofficial integration built on the portal's undocumented web API.
It can break when Konnect changes the portal.

## What you get

One device per child, with:

| Entity | What it shows |
|---|---|
| `sensor.<child>_last_activity` | The latest day rhythm entry, e.g. `12:00 (Fles)voeding`, with details as attributes |
| `sensor.<child>_last_bottle` | Time of the last bottle (attribute `amount_ml`) |
| `sensor.<child>_last_diaper` | Time of the last diaper change |
| `sensor.<child>_last_sleep` | When the last nap ended, or started if still asleep (attribute `sleeping`) |
| `sensor.<child>_milk_today` | Total millilitres from today's bottles |
| `sensor.<child>_sleep_today` | Total minutes slept today |
| `image.<child>_latest_photo` | The newest photo, served from the saved copy |
| `calendar.<child>_day_rhythm` | The full day rhythm history as calendar events; "on" during an ongoing nap |
| `calendar.<child>_schedule` | When your child is planned at daycare, from the portal's Agenda; "on" while they are there |

### Schedule

The schedule calendar shows the times your child is planned at daycare, as on the portal's Agenda, from a month back to three months ahead.
Each time is called "Daycare", with what makes it different in brackets:

| Summary | What it is |
|---|---|
| `Daycare` | A usual day |
| `Daycare (extra day)` | An extra day |
| `Daycare (swap day from 7 October)` | A day swapped from another day |
| `Daycare (moved)` | A day moved to another group |
| `Daycare (option)` | An option, not certain yet |
| `Daycare (to confirm)` | A day the daycare asks you to confirm; `Daycare (confirmed)` once you have |
| `Daycare (extra day, pending approval)` | A request the daycare has not approved yet; also `on hold` and `pending parent approval` |

Absences (illness, vacation, a day swapped away) are left out, the way a calendar leaves out cancelled events, so the calendar is "on" only while your child is expected at daycare.
Requests that were rejected, cancelled or expired are left out too.
A morning and an afternoon on the same day are two events.
When the portal knows them, the group and the status of a request are in the event's description, and the daycare's location in its location.

The schedule is read every 30 minutes, day and night, so a change on the portal can take that long to show up.
If the Agenda cannot be read, the schedule calendar is unavailable until it can; the rest of the integration keeps working, and the other way around.
Only accounts that see the Agenda on the portal get this calendar.

### Messages

Everything on the portal's Messages page gets its own device, "Ouderportaal mailbox".
That page combines four kinds of item, each with a `message_type`:

| `message_type` | What it is |
|---|---|
| `message` | A conversation with the daycare, started by them or by you |
| `news` | A news item from the daycare |
| `newsletter` | A newsletter |
| `service_message` | A service message, such as new rates |

| Entity | What it shows |
|---|---|
| `sensor.ouderportaal_mailbox_unread_messages` | Unread items, the same count as the badge on the portal's Messages tab; attributes `messages`, `news` and `newsletters` split it up |
| `sensor.ouderportaal_mailbox_latest_message` | Subject of the newest item, with its text, `message_type` and `author` as attributes |

Only the lists are read, never an item itself: opening an item marks it as read on the portal, and then you would miss the unread marker in the app.
For a conversation, the list holds the whole latest message, its author and whether a parent wrote it (`from_parent`); open the portal for earlier messages.
News items and newsletters come with the first few hundred characters of their text.
Only the kinds of item the portal shows your account are read.

### Photos and history

Every photo is saved once into Home Assistant's media folder, as `media/ouderportaal/<YYYY-MM-DD>/<photo id>.jpeg`.
You can browse them under Media > My media, they are included in backups, and they stay even if the daycare removes them from the portal.
If the media folder cannot be written to, photos are not saved and a warning is logged once; everything else keeps working.

On first setup, the integration imports the last 30 days of day rhythm, photos and messages in the background, one timeline page per second.
After that it keeps everything new.
When the daycare corrects or removes a day rhythm entry, the history follows, for as long as that day is on the first page of the portal's timeline.

For a timeline card, the websocket command `ouderportaal/timeline` returns everything per child, newest day first:

```json
{"type": "ouderportaal/timeline", "days": 30, "child_id": "optional"}
```

```json
{"children": [{"id": "...", "name": "Sam", "days": [
  {"date": "2026-10-07",
   "moments": [{"title": "09:00 (Fles)voeding", "category": "bottle", "amount_ml": 130, "start": "...", "end": null, "remarks": "20cc over", "...": "..."}],
   "photos": [{"id": "...", "url": "/media/local/ouderportaal/2026-10-07/....jpeg", "saved": true, "description": "...", "media_type": "photo", "media_content_id": "media-source://..."}],
   "messages": [{"key": "message-...", "id": "...", "message_type": "message", "subject": "...", "preview": "...", "author": "Tommie", "from_parent": false, "has_attachment": false, "sent": "2026-10-07T14:30:00+02:00", "unread": true, "child_names": ["Sam"], "url": "https://<portal>.ouderportaal.nl/parent/#/tabs/mailbox"}]}
]}]}
```

Photo `url`s need a logged-in request; in a custom card, sign them first with `hass.callWS({type: "auth/sign_path", path: url})`.
News and newsletters are for the whole family, so they show up for every child.

And an event, `ouderportaal_new_entry`, fired for:

- every new day rhythm entry (`entry_type: moment`, `change: new`), and when an entry changes, such as a nap getting its end time or the day's note getting longer (`change: updated`);
- each batch of new photos (`entry_type: photos`), once per batch, not per photo;
- every new item on the Messages page (`entry_type: message`, `change: new`), and a reply in an existing conversation (`change: updated`).
  This includes messages and replies a parent writes, with `from_parent: true`, so you can leave out your own.
  Reading a message does not trigger anything.

The first time the integration runs it only records what is already there, so you don't get notifications for old entries.

## Install

1. In HACS, add `https://github.com/MatyasK/ha-ouderportaal` as a custom repository (type: Integration) and install it.
   Or copy `custom_components/ouderportaal` into your Home Assistant `config/custom_components` folder.
2. Restart Home Assistant.
3. Go to Settings > Devices & services > Add integration > Ouderportaal.
4. Enter your portal name (e.g. `mijnopvang`, or paste the website address), email address and password.

If the password changes, Home Assistant asks you to log in again.

Both parents can add their own account, also for the same child.
Each account gets its own devices; the second one's entity ids end in `_2`.

## Notification examples

New day rhythm entry:

```yaml
automation:
  - alias: Daycare update
    triggers:
      - trigger: event
        event_type: ouderportaal_new_entry
        event_data:
          entry_type: moment
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: "{{ trigger.event.data.child_name }}"
          message: "{{ trigger.event.data.text }}"
```

New photos, with a preview in the notification:

```yaml
automation:
  - alias: Daycare photos
    triggers:
      - trigger: event
        event_type: ouderportaal_new_entry
        event_data:
          entry_type: photos
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: "{{ trigger.event.data.child_name }}"
          message: "{{ trigger.event.data.count }} new photo(s)"
          data:
            image: "{{ trigger.event.data.media_url }}"
```

New message from the daycare (not the ones you write yourself), with a button that opens the Messages page:

```yaml
automation:
  - alias: Daycare message
    triggers:
      - trigger: event
        event_type: ouderportaal_new_entry
        event_data:
          entry_type: message
          from_parent: false
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: "{{ trigger.event.data.author or 'Daycare' }}: {{ trigger.event.data.subject }}"
          message: "{{ trigger.event.data.preview }}"
          data:
            clickAction: "{{ trigger.event.data.url }}"
            url: "{{ trigger.event.data.url }}"
```

The evening before a daycare day:

```yaml
automation:
  - alias: Daycare tomorrow
    triggers:
      - trigger: time
        at: "19:00:00"
    actions:
      - action: calendar.get_events
        target:
          entity_id: calendar.sam_schedule
        data:
          start_date_time: "{{ today_at() + timedelta(days=1) }}"
          duration:
            days: 1
        response_variable: tomorrow
      - variables:
          events: "{{ tomorrow['calendar.sam_schedule'].events }}"
      - condition: template
        value_template: "{{ events | count > 0 }}"
      - action: notify.mobile_app_your_phone
        data:
          title: "Daycare tomorrow"
          message: "{{ events[0].summary }} from {{ events[0].start[11:16] }}"
```

`media_url` points at the saved copy, which the companion app loads with your Home Assistant login.
`image_url` is the portal's own signed URL; it works anywhere but expires after about an hour.

### Event data

Moments: `child_id`, `child_name`, `change`, `category` (`bottle`, `sleep`, `diaper`, `toilet`, `food`, `drink`, `medication`, `temperature`, `sunscreen`, `note`, `activity`), `icon`, `title`, `text`, `label`, `description`, `remarks`, `date`, `start`, `end`, `duration_minutes`, `amount_ml`.

Messages: `message_type` (`message`, `news`, `newsletter`, `service_message`), `message_id`, `change`, `child_ids`, `child_names`, `subject`, `preview`, `author`, `from_parent`, `has_attachment`, `sent`, `unread`, `url`.
To be notified only of conversations, add `message_type: message` to the trigger's `event_data`.

Photos: `child_id`, `child_name`, `count`, `media_url`, `image_url`, `photos` (each with `id`, `date`, `description`, `media_type`, `media_url`, `media_content_id`, `full_url`, `medium_url`, `thumb_url`).

## How it works

- Polls the first page of the timeline every 5 minutes between 06:00 and 20:00, and hourly at night.
- Logs in with `PUT /auth-api/login`.
  The token lasts 15 minutes and is renewed with `PUT /auth-api/token` and the `__Host-refresh_token` cookie (valid for 1 hour).
  If renewing fails, it logs in again.
- Each poll also reads the Messages page items of the last 7 days: `GET /logbook/overview`, `/htmlnews/view` and `/newsletter`, the same lists the portal reads, gated by the same account permissions (`/parent/authorizations`).
- Every 30 minutes it also reads the Agenda, `GET /calendar/<from>/until/<to>`, a month per request from a month back to three months ahead, if the account may see the Agenda (`/parent/authorizations`).
- The day rhythm comes as HTML inside the day's journal card and is parsed into entries.
  The category comes from the icon the daycare picked (`dagritme-flesje`, `dagritme-slapen-bedje`, ...).
- What was already announced, and the history, are stored in `.storage/`, so a restart does not repeat notifications.

Limitations:

- History before installing goes back 30 days.
- Documents are not included yet.
- The Agenda's activities, parent meetings and activity programs are not included yet, only the schedule.
- The schedule's summaries are in English.

## Development

Home Assistant does not run on Windows, so the tests run in Docker:

```bash
docker build -f Dockerfile.test -t ouderportaal-test .
docker run --rm -v "$PWD:/app" ouderportaal-test
```

To check the client against your real portal without Home Assistant (asks for your password, stores nothing):

```bash
uv run --no-project --with aiohttp python scripts/try_portal.py <portal> <email>
```

Never commit HAR files or tokens: they contain your password and session.

## Credits

The API was mapped with help from [konnect-cli](https://github.com/anneschuth/konnect-cli) and [Ouderportaal-Foto-Downloader](https://github.com/tangenent/Ouderportaal-Foto-Downloader) (both MIT).
