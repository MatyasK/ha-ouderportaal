# Ouderportaal (Konnect) for Home Assistant

Get your child's daycare day rhythm and photos from a Konnect ouderportaal (`<name>.ouderportaal.nl`) into Home Assistant.
Get a notification when a bottle, nap or diaper is logged or new photos arrive.

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

### Photos and history

Every photo is saved once into Home Assistant's media folder, as `media/ouderportaal/<YYYY-MM-DD>/<photo id>.jpeg`.
You can browse them under Media > My media, they are included in backups, and they stay even if the daycare removes them from the portal.

On first setup, the integration imports the last 30 days of day rhythm and photos in the background, one timeline page per second.
After that it keeps everything new.

For a timeline card, the websocket command `ouderportaal/timeline` returns everything per child, newest day first:

```json
{"type": "ouderportaal/timeline", "days": 30, "child_id": "optional"}
```

```json
{"children": [{"id": "...", "name": "Sam", "days": [
  {"date": "2026-10-07",
   "moments": [{"title": "09:00 (Fles)voeding", "category": "bottle", "amount_ml": 130, "start": "...", "end": null, "remarks": "20cc over", "...": "..."}],
   "photos": [{"id": "...", "url": "/media/local/ouderportaal/2026-10-07/....jpeg", "saved": true, "description": "...", "media_type": "photo", "media_content_id": "media-source://..."}]}
]}]}
```

Photo `url`s need a logged-in request; in a custom card, sign them first with `hass.callWS({type: "auth/sign_path", path: url})`.

And an event, `ouderportaal_new_entry`, fired for:

- every new day rhythm entry (`entry_type: moment`, `change: new`), and when an entry changes, such as a nap getting its end time (`change: updated`);
- each batch of new photos (`entry_type: photos`), once per batch, not per photo.

The first time the integration runs it only records what is already there, so you don't get notifications for old entries.

## Install

1. In HACS, add `https://github.com/MatyasK/ha-ouderportaal` as a custom repository (type: Integration) and install it.
   Or copy `custom_components/ouderportaal` into your Home Assistant `config/custom_components` folder.
2. Restart Home Assistant.
3. Go to Settings > Devices & services > Add integration > Ouderportaal.
4. Enter your portal name (e.g. `mijnopvang`, or paste the website address), email address and password.

If the password changes, Home Assistant asks you to log in again.

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

`media_url` points at the saved copy, which the companion app loads with your Home Assistant login.
`image_url` is the portal's own signed URL; it works anywhere but expires after about an hour.

### Event data

Moments: `child_id`, `child_name`, `change`, `category` (`bottle`, `sleep`, `diaper`, `toilet`, `food`, `drink`, `medication`, `temperature`, `sunscreen`, `note`, `activity`), `icon`, `title`, `text`, `label`, `description`, `remarks`, `date`, `start`, `end`, `duration_minutes`, `amount_ml`.

Photos: `child_id`, `child_name`, `count`, `media_url`, `image_url`, `photos` (each with `id`, `date`, `description`, `media_type`, `media_url`, `media_content_id`, `full_url`, `medium_url`, `thumb_url`).

## How it works

- Polls the first page of the timeline every 5 minutes between 06:00 and 20:00, and hourly at night.
- Logs in with `PUT /auth-api/login`.
  The token lasts 15 minutes and is renewed with `PUT /auth-api/token` and the `__Host-refresh_token` cookie (valid for 1 hour).
  If renewing fails, it logs in again.
- The day rhythm comes as HTML inside the day's journal card and is parsed into entries.
  The category comes from the icon the daycare picked (`dagritme-flesje`, `dagritme-slapen-bedje`, ...).
- What was already announced, and the history, are stored in `.storage/`, so a restart does not repeat notifications.

Limitations:

- History before installing goes back 30 days.
- Messages, newsletters and documents are not included yet.

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

---
