# Garage

Garage is a self-hosted, multi-user vehicle maintenance tracker. One shared garage keeps vehicle mileage, service history, maintenance reminders, and costs in sync across phones and computers. It runs as one Docker container with SQLite storage.

## Quick start

1. Copy `.env.example` to `.env` and replace `GARAGE_SECRET` with a long random value. One way to make one is `openssl rand -hex 32`.
2. Start Garage:

   ```sh
   docker compose up -d
   ```

3. Open `http://your-server:8917`.
4. The first visit shows setup. Create the first account, which becomes the administrator.

Fresh installs include four vehicles: 2021 Ford F-150, 2006 Mazda Miata, 2023 Hyundai Tucson, and Chevrolet Traverse. You can edit or remove them.

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
2. Add `GARAGE_SECRET` as a stack environment variable, or keep a `.env` file beside the Compose file.
3. Deploy the stack.
4. Open port `8917` on the CasaOS host and complete first-run setup.

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

## API

The browser uses a JSON REST API under `/api`. Authentication is cookie-based.

- `GET /api/status`, `POST /api/setup`
- `POST /api/login`, `POST /api/logout`, `GET /api/me`
- `GET/POST /api/vehicles`, `PUT/DELETE /api/vehicles/{id}`
- `GET/POST /api/services`, `PUT/DELETE /api/services/{id}`
- `GET/POST /api/reminders`, `PUT/DELETE /api/reminders/{id}`
- `GET /api/export`, `POST /api/import`
- `GET/POST /api/users`, `PUT /api/users/{id}` (administrator only)
