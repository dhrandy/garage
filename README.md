# Garage

[![Beta](https://img.shields.io/badge/status-beta-orange)](https://github.com/dhrandy/garage)

Garage is in beta. Things may change between versions - features, data formats, and APIs can shift until a stable release.

Garage is a simple vehicle maintenance tracker. It is self-hosted and multi-user. Each person gets their own garage view with vehicle mileage, service history, maintenance reminders, and costs in sync across phones and computers. It runs as one Docker container with SQLite storage.

## Quick start

1. Run Garage with Docker, keeping its data in a folder on the host:

   ```sh
   docker run -d --name garage -p 8917:8000 -v ./data:/app/data ghcr.io/dhrandy/garage:latest
   ```

   Or with Docker Compose. Save this as `docker-compose.yml` (the repo includes the same file):

   ```yaml
   services:
     garage:
       image: ghcr.io/dhrandy/garage:latest
       container_name: garage
       restart: unless-stopped
       working_dir: /app
       volumes:
         - ./data:/app/data
       environment:
         - TZ=${TZ:-UTC}
       ports:
         - 8917:8000
   ```

   `GARAGE_SECRET` from older examples is optional and currently unused by the app. Then start it:

   ```sh
   docker compose up -d
   ```

2. Open `http://your-server:8917`.
3. The first visit shows setup. Create the first account, which becomes the administrator.

Fresh installs start with one example vehicle, a 1969 Mustang, that you can edit or delete.

## Vehicle photos

Each vehicle can have a photo (uploaded from the vehicle's **Edit** form, with camera capture on phones). Photos are stored under `/app/data/receipts` with receipt images. **Settings → Vehicle photos** controls whether photos replace the emoji icon on vehicle cards and pages; turning it off restores the emoji icons without deleting any photos.

## Settings

Administrators can open **Settings** to rename the garage. The name is stored in SQLite and appears in the header and on the garage home screen. New installs default to **Your Garage**.

Settings also has **Vehicle page sections** checkboxes to hide the Service log, Maintenance, Fuel, Costs, and Notes sections. Hidden sections disappear from every vehicle page for all users. The choices are stored in SQLite and apply to everyone, including non-administrators.

## Vehicle specs

On phones, vehicle metadata stays in compact chips and all vehicle tabs remain visible in a two-row navigation grid.

The **Specs** tab groups detailed vehicle information into compact cards for powertrain, body and dimensions, wheels and tires, fuel economy, capability, and maintenance. On wide screens the cards pack into two columns without reserving empty row space; on phones they stack into one column.

## Maintenance reminders and service logging

The **Maintenance** tab on each vehicle tracks recurring items by miles, months, or both. A service date is optional; undated entries display **Date not set**. When a date is supplied, you can pick **Marks maintenance done** to link the service to one of those items; the item's last-done date and mileage reset to the service entry, so nothing has to be recorded twice. Undated services do not reset or link a maintenance reminder. The same rules apply to `date` and `reminder_id` on the service-create API endpoints.

## Estimated mileage

Garage learns each vehicle's driving pace from dated odometer readings in service entries and fill-ups. Vehicle cards and pages show an **est. current mileage** when the estimate runs ahead of the last recorded reading, and maintenance-due calculations (in the app and in `/api/v1/vehicles/{id}/maintenance`) use the estimate whenever it is fresher than the last manual reading. The REST vehicle list exposes both as `est_mileage` and `miles_per_day`.

## Fuel log

Each vehicle has a **Fuel** tab for fill-ups: date, odometer, gallons, and total cost. MPG is computed automatically between consecutive fill-ups, and the tab shows the running average MPG and fuel cost per mile. Logging a fill-up also raises the vehicle's recorded mileage when the odometer reading is higher. Fuel entries are included in JSON exports and imports.

## Notes

Each vehicle has a **Notes** tab for freeform dated notes: things like tire pressures, part numbers, or reminders to yourself. Notes are a simple dated list that the vehicle's owner and administrators can add to, edit, and delete, and each note shows who wrote it. The tab can be hidden in Settings, and notes are available over the REST API (`GET`/`POST /api/v1/vehicles/{id}/notes`) so scripts and AI assistants can read and write them.

## Receipt photos

Service entries and fill-ups can carry receipt photos. Use the receipt field when logging or editing an entry; on phones the field opens the camera. Photos are stored on disk under `/app/data/receipts` inside the existing data volume, so the same backup that covers `garage.db` covers them. Images are limited to 10 MB each (JPEG, PNG, WebP, GIF, HEIC), are served only to signed-in users, and deleting an entry deletes its photos. Receipt images are not part of JSON exports.

## Users

Administrators can open **Users** from the top bar to create users, change usernames, reset passwords, grant or remove administrator access, and deactivate accounts. Non-administrators cannot manage users.

Visibility is per person: a non-administrator sees only their own vehicles - in the vehicle list, on the dashboard, and in every vehicle-scoped view (services, fuel, costs, reminders, notes, mods, receipts). Other people's vehicles are invisible to them, and the API answers `404` for them as if they did not exist. Administrators see every vehicle, subject to the **Private** flag below, and can turn on **Show all vehicles** in the top-bar menu to reveal private ones.

Every vehicle has an owner, chosen when the vehicle is added; administrators can reassign ownership from the vehicle's **Edit** form. Administrators can edit every vehicle and everything on it. A non-administrator can edit only their own vehicles — specs, services, fill-ups, maintenance items, modifications, notes, receipts, mileage, and photo — and has no access to anyone else's.

A vehicle marked **Private** is hidden even from administrators unless they turn on **Show all vehicles**. The flag works as an extra layer within the per-person visibility above; for non-administrators it only affects their own vehicles.

## Port

The included Compose file maps host port `8917` to container port `8000`. Change the left side of this line to use another host port:

```yaml
ports:
  - 8917:8000
```

## Notifications

Garage can send maintenance alerts through [Apprise](https://github.com/caronc/apprise), which routes one URL to Telegram, Discord, email, and 80+ other services. Administrators configure it in **Settings → Notifications**: add one or more Apprise URLs, one per line, and use **Send test notification** to verify delivery. Once a day, Garage checks every maintenance item against current or estimated mileage and dates, and sends a single notification when an item newly becomes due soon or overdue. Fixing the item re-arms the alert; a state that does not change is never re-sent.

Example URLs:

```text
discord://webhook_id/webhook_token
tgram://bot_token/chat_id
mailto://user:pass@smtp.example.com?to=you@example.com
```

A plain `http(s)://` URL receives a JSON webhook POST instead (`{"title": ..., "body": ...}`), which also works if the apprise package is unavailable. Treat these URLs like passwords: they carry service tokens, so only administrators can read or change them. The URLs and the sent-state live in SQLite.

## REST API tokens

Scripts and integrations can use token-authenticated REST endpoints under `/api/v1`. Administrators create named tokens in **Settings → API tokens**. The full token is shown once at creation; Garage stores only its SHA-256 hash. Revoking a token disables it immediately. Interactive OpenAPI docs are at `/api/docs` on your server.

Token requests act as the user who created the token, so the visibility and ownership rules apply to scripts too: a token sees exactly the vehicles its creator can see, and write endpoints reject vehicles the creator may not edit.

Send the token as a bearer header:

```sh
curl -H "Authorization: Bearer gar_..." http://your-server:8917/api/v1/vehicles
```

Endpoints (all relative to `http://your-server:8917`):

- `GET /api/v1/vehicles` — list vehicles with recorded and estimated mileage
- `GET /api/v1/vehicles/{id}/services` — service history
- `GET /api/v1/vehicles/{id}/specs` — detailed vehicle specs
- `GET /api/v1/vehicles/{id}/maintenance` — maintenance items with `status` (`ok`, `soon`, `overdue`) and a human-readable `label`
- `GET /api/v1/vehicles/{id}/fuel` — fill-up log with per-fill `mpg`
- `GET /api/v1/vehicles/{id}/notes` — dated freeform notes
- `GET /api/v1/vehicles/{id}/mods` — modification list
- `PUT /api/v1/vehicles/{id}/specs` — create or replace detailed specs (engine, transmission, drivetrain, dimensions, capacities, `wheel_size`, and related fields). Wheel size is separate from the vehicle's `tire_size`, so values such as `17 × 7 in` and `205/45R17` are stored independently.
- `PUT /api/v1/vehicles/{id}/mileage` — update the odometer directly (JSON: `mileage`, optional `date`), same as the app's Update mileage button
- `POST /api/v1/vehicles/{id}/services` — log a service entry (JSON; `date` is optional, and an undated service ignores `reminder_id`)
- `POST /api/v1/vehicles/{id}/fuel` — log a fill-up (multipart form, optional receipt `file`)
- `POST /api/v1/vehicles/{id}/notes` — add a note (JSON: `date`, `body`)
- `POST /api/v1/vehicles/{id}/mods` — add a modification (JSON: `name`, optional `date`, `price`, `torque_specs`, `gotchas`, `youtube_url`)

Examples:

```sh
# Log a service entry
curl -X POST -H "Authorization: Bearer gar_..." -H "Content-Type: application/json" \
  -d '{"date":"2026-09-21","mileage":24310,"type":"Oil change","cost":54.99,"provider":"DIY"}' \
  http://your-server:8917/api/v1/vehicles/1/services

# Log a fill-up with a receipt photo
curl -X POST -H "Authorization: Bearer gar_..." \
  -F date=2026-09-21 -F odometer=24310 -F gallons=10.2 -F cost=36.50 \
  -F file=@receipt.jpg \
  http://your-server:8917/api/v1/vehicles/1/fuel
```

API requests are rate limited: 100 requests per minute per token, and repeated invalid tokens from one address are blocked for 15 minutes, mirroring the login protection. `429` responses carry a `Retry-After` header.

## Use with an AI assistant

The REST API works well with AI assistants that can make HTTP requests.

1. Create a token in **Settings → API tokens** (administrator account).
2. Give your assistant your Garage server URL and the token, and tell it what you want. For example:
   - "Add a fuel fill-up to my truck: date 2026-09-21, odometer 24310, gallons 10.2, cost $36.50, receipt attached"
   - "What maintenance is due on my vehicles?"
3. Point it at the interactive OpenAPI docs at `/api/docs` on your server so it can learn the exact endpoints and payloads.

A token is a password: do not paste it in public chats or repositories, prefer HTTPS so it is not sent in plain text, and revoke it in Settings if it is ever exposed.

## Backup and restore

Garage stores all app data in `/app/data` inside the container: the SQLite database `garage.db` plus uploaded receipt and vehicle photos under `receipts/`. With the included Compose file the host copy is the `./data` folder beside `docker-compose.yml`; with `docker run -v` it is whatever host folder you mount at `/app/data`.

For a consistent backup, stop the container, copy `garage.db` and the `receipts` folder beside it, then start it again. Restore by stopping Garage and replacing both with the backup. JSON export and import in the app are useful for moving garage records, but they do not include user accounts, login sessions, or photos.

## Reverse proxy

Garage works behind any HTTPS reverse proxy. The proxy must forward the original `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto` headers. Set `GARAGE_COOKIE_SECURE=true` so session cookies are only sent over HTTPS.

Uvicorn honors forwarded headers only from trusted proxy addresses, so set `FORWARDED_ALLOW_IPS` to the proxy's IP address (or a narrow trusted CIDR). Do not use `FORWARDED_ALLOW_IPS=*` when Garage's port is reachable by untrusted clients. Example environment:

```yaml
environment:
  - GARAGE_COOKIE_SECURE=true
  - FORWARDED_ALLOW_IPS=192.168.1.10
```

### nginx

```nginx
location / {
    proxy_pass http://127.0.0.1:8917;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### Caddy

```caddy
garage.example.com {
    reverse_proxy 127.0.0.1:8917
}
```

Caddy forwards `Host` and `X-Forwarded-*` headers automatically.

### Traefik

```yaml
labels:
  - traefik.http.routers.garage.rule=Host(`garage.example.com`)
  - traefik.http.services.garage.loadbalancer.server.port=8917
```

### Synology DSM

Create an HTTPS reverse-proxy rule whose destination is `http://<garage-host>:8917` and forward the original `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto` headers.

## Dockhand, CasaOS, and other Docker GUIs

Any Docker GUI that can run a Compose file works. In Dockhand, for example: create a stack, paste the contents of `docker-compose.yml`, adjust the volume path to a folder your host exposes, deploy, then open port `8917` and complete first-run setup.

## Building locally

The Compose file pulls `ghcr.io/dhrandy/garage:latest`. To build locally instead, run:

```sh
docker build -t garage:local .
docker run -d --name garage -p 8917:8000 -v ./data:/app/data garage:local
```

## Security notes

Passwords use PBKDF2-HMAC-SHA256 with a unique random salt and 260,000 iterations. Login state uses random, server-stored session tokens in an HTTP-only, SameSite cookie, so there is no signing secret to configure; `GARAGE_SECRET`, accepted by older deployment examples, is not read by the app. Set `GARAGE_COOKIE_SECURE=true` when Garage is served through HTTPS. The setup route closes automatically after the first account is created. API tokens are stored as SHA-256 hashes. Notification URLs contain service credentials and are only visible to administrators.

## License

Garage is available under the [MIT License](LICENSE).

## API

The browser uses a JSON REST API under `/api`. Authentication is cookie-based.

- `GET /api/status`, `POST /api/setup`
- `POST /api/login`, `POST /api/logout`, `GET /api/me`
- `GET/POST /api/vehicles`, `PUT/DELETE /api/vehicles/{id}`, `POST /api/vehicles/{id}/photo` (multipart upload)
- `GET/POST /api/services`, `PUT/DELETE /api/services/{id}`
- `GET/POST /api/fuel`, `PUT/DELETE /api/fuel/{id}`
- `GET/POST /api/receipts`, `GET/DELETE /api/receipts/{id}` (multipart upload)
- `GET/POST /api/reminders`, `PUT/DELETE /api/reminders/{id}`
- `GET/POST /api/notes`, `PUT/DELETE /api/notes/{id}`
- `GET /api/export`, `POST /api/import`
- `GET/PUT /api/settings` (`PUT` is administrator only)
- `GET/POST /api/users`, `PUT /api/users/{id}` (administrator only)
- `GET/POST /api/tokens`, `DELETE /api/tokens/{id}` (administrator only)
- `GET/PUT /api/notifications`, `POST /api/notifications/test` (administrator only)
- `/api/v1/...` token endpoints (see **REST API tokens**)

Writes on a vehicle and its entries (`PUT/DELETE /api/vehicles/{id}`, `POST /api/vehicles/{id}/photo`, and the `POST`/`PUT`/`DELETE` routes for specs, services, fuel, receipts, reminders, notes, and mods) require the vehicle's owner or an administrator. Reads are limited the same way: a non-administrator only ever sees their own vehicles, and requests for any other vehicle return `404`.
