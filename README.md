# Garage

Garage is a simple vehicle maintenance tracker. It is self-hosted and multi-user. One shared garage keeps vehicle mileage, service history, maintenance reminders, and costs in sync across phones and computers. It runs as one Docker container with SQLite storage.

## Quick start

1. Set `GARAGE_SECRET` to a long random value. In Dockhand, enter it directly in the stack Environment tab when you paste the Compose file. No `.env` file is required. Plain Docker Compose users can instead place `GARAGE_SECRET` in a `.env` file beside `docker-compose.yml`. One way to generate a value is `openssl rand -hex 32`.
2. Start Garage:

   ```sh
   docker compose up -d
   ```

3. Open `http://your-server:8917`.
4. The first visit shows setup. Create the first account, which becomes the administrator.

Fresh installs start with one example vehicle, a 1969 Mustang, that you can edit or delete.

## Vehicle photos

Each vehicle can have a photo (uploaded from the vehicle's **Edit** form, with camera capture on phones). Photos are stored under `/app/data/receipts` with receipt images. **Settings → Vehicle photos** controls whether photos replace the emoji icon on vehicle cards and pages; turning it off restores the emoji icons without deleting any photos.

## Settings

Administrators can open **Settings** to rename the garage. The name is stored in SQLite and appears in the header and on the garage home screen. New installs default to **Your Garage**.

Settings also has **Vehicle page sections** checkboxes to hide the Service log, Maintenance, Fuel, and Costs sections. Hidden sections disappear from every vehicle page for all users. The choices are stored in SQLite and apply to everyone, including non-administrators.

## Maintenance reminders and service logging

The **Maintenance** tab on each vehicle tracks recurring items by miles, months, or both. When logging a service you can pick **Marks maintenance done** to link it to one of those items; the item's last-done date and mileage reset to the service entry, so nothing has to be recorded twice. The same link is available in the REST API as `reminder_id` on the service-create endpoints.

## Estimated mileage

Garage learns each vehicle's driving pace from dated odometer readings in service entries and fill-ups. Vehicle cards and pages show an **est. current mileage** when the estimate runs ahead of the last recorded reading, and maintenance-due calculations (in the app and in `/api/v1/vehicles/{id}/maintenance`) use the estimate whenever it is fresher than the last manual reading. The REST vehicle list exposes both as `est_mileage` and `miles_per_day`.

## Fuel log

Each vehicle has a **Fuel** tab for fill-ups: date, odometer, gallons, and total cost. MPG is computed automatically between consecutive fill-ups, and the tab shows the running average MPG and fuel cost per mile. Logging a fill-up also raises the vehicle's recorded mileage when the odometer reading is higher. Fuel entries are included in JSON exports and imports.

## Receipt photos

Service entries and fill-ups can carry receipt photos. Use the receipt field when logging or editing an entry; on phones the field opens the camera. Photos are stored on disk under `/app/data/receipts` inside the existing data volume, so the same backup that covers `garage.db` covers them. Images are limited to 10 MB each (JPEG, PNG, WebP, GIF, HEIC), are served only to signed-in users, and deleting an entry deletes its photos. Receipt images are not part of JSON exports.

## Users

Administrators can open **Users** from the top bar to create users, change usernames, reset passwords, grant or remove administrator access, and deactivate accounts. All active users see the same garage. Every vehicle and service entry records the user who added or logged it. Non-administrators can manage vehicles, services, mileage, and reminders, but cannot manage users.

## Port

The included Compose file maps host port `8917` to container port `8000`. Change the left side of this line to use another host port:

```yaml
ports:
  - 8917:8000
```

## REST API tokens

Scripts and integrations can use token-authenticated REST endpoints under `/api/v1`. Administrators create named tokens in **Settings → API tokens**. The full token is shown once at creation; Garage stores only its SHA-256 hash. Revoking a token disables it immediately. Interactive OpenAPI docs are at `/api/docs` on your server.

Send the token as a bearer header:

```sh
curl -H "Authorization: Bearer gar_..." http://your-server:8917/api/v1/vehicles
```

Endpoints (all relative to `http://your-server:8917`):

- `GET /api/v1/vehicles` — list vehicles with recorded and estimated mileage
- `GET /api/v1/vehicles/{id}/services` — service history
- `GET /api/v1/vehicles/{id}/maintenance` — maintenance items with `status` (`ok`, `soon`, `overdue`) and a human-readable `label`
- `GET /api/v1/vehicles/{id}/fuel` — fill-up log with per-fill `mpg`
- `POST /api/v1/vehicles/{id}/services` — log a service entry (JSON)
- `POST /api/v1/vehicles/{id}/fuel` — log a fill-up (multipart form, optional receipt `file`)

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

## Backup and restore

Garage stores all app data in `/app/data/garage.db`. With the included bind mount, the host copy is:

```text
/DATA/AppData/garage/garage.db
```

For a consistent backup, stop the container, copy `garage.db` and the `receipts` folder beside it, then start it again. Restore by stopping Garage and replacing both with the backup. JSON export and import in the app are useful for moving garage records, but they do not include user accounts or login sessions.

## Dockhand / CasaOS

1. In Dockhand, create a stack and paste the contents of `docker-compose.yml`.
2. Add `GARAGE_SECRET` in Dockhand's Environment tab. No `.env` file is required. Plain Docker Compose users can use a `.env` file beside the Compose file instead.
3. Deploy the stack.
4. Open port `8917` on the CasaOS host and complete first-run setup.

## Synology reverse proxy

Garage can sit behind Synology DSM's reverse proxy. Create an HTTPS reverse-proxy rule whose destination is `http://<garage-host>:8917`, enable WebSocket forwarding, and forward the original `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto` headers. Set `GARAGE_COOKIE_SECURE=true` so session cookies are only sent over HTTPS.

Uvicorn honors forwarded headers only from trusted proxy addresses. Set `FORWARDED_ALLOW_IPS` to the Synology proxy's IP address (or a narrow trusted CIDR), for example:

```yaml
environment:
  - GARAGE_COOKIE_SECURE=true
  - FORWARDED_ALLOW_IPS=192.168.1.10
```

Do not use `FORWARDED_ALLOW_IPS=*` when port `8917` is reachable by untrusted clients. Keep the SQLite data directory outside any reverse-proxy static-file root.

The Compose file pulls `ghcr.io/dhrandy/garage:latest`. To build locally instead, run:

```sh
docker build -t garage:local .
docker run -d --name garage -p 8917:8000 \
  -e GARAGE_SECRET="$(openssl rand -hex 32)" \
  -v /DATA/AppData/garage:/app/data \
  garage:local
```

## Security notes

Passwords use PBKDF2-HMAC-SHA256 with a unique random salt and 260,000 iterations. Login state uses random, server-stored session tokens in an HTTP-only, SameSite cookie. Set `GARAGE_COOKIE_SECURE=true` when Garage is served through HTTPS. The setup route closes automatically after the first account is created.

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
- `GET /api/export`, `POST /api/import`
- `GET/PUT /api/settings` (`PUT` is administrator only)
- `GET/POST /api/users`, `PUT /api/users/{id}` (administrator only)
- `GET/POST /api/tokens`, `DELETE /api/tokens/{id}` (administrator only)
- `/api/v1/...` token endpoints (see **REST API tokens**)
