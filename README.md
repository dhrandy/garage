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

## Settings

Administrators can open **Settings** to rename the garage. The name is stored in SQLite and appears in the header and on the garage home screen. New installs default to **Your Garage**.

Settings also has **Vehicle page sections** checkboxes to hide the Service log, Maintenance, Fuel, and Costs sections. Hidden sections disappear from every vehicle page for all users. The choices are stored in SQLite and apply to everyone, including non-administrators.

## Users

Administrators can open **Users** from the top bar to create users, change usernames, reset passwords, grant or remove administrator access, and deactivate accounts. All active users see the same garage. Every vehicle and service entry records the user who added or logged it. Non-administrators can manage vehicles, services, mileage, and reminders, but cannot manage users.

## Port

The included Compose file maps host port `8917` to container port `8000`. Change the left side of this line to use another host port:

```yaml
ports:
  - 8917:8000
```

## Backup and restore

Garage stores all app data in `/app/data/garage.db`. With the included bind mount, the host copy is:

```text
/DATA/AppData/garage/garage.db
```

For a consistent backup, stop the container, copy `garage.db`, then start it again. Restore by stopping Garage and replacing that file with the backup. JSON export and import in the app are useful for moving garage records, but they do not include user accounts or login sessions.

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
- `GET/POST /api/vehicles`, `PUT/DELETE /api/vehicles/{id}`
- `GET/POST /api/services`, `PUT/DELETE /api/services/{id}`
- `GET/POST /api/reminders`, `PUT/DELETE /api/reminders/{id}`
- `GET /api/export`, `POST /api/import`
- `GET/PUT /api/settings` (`PUT` is administrator only)
- `GET/POST /api/users`, `PUT /api/users/{id}` (administrator only)
