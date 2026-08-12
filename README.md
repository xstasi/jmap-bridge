# JMAP Bridge

A [JMAP](https://jmap.io/) server that translates to existing mail/calendar/
contacts protocols instead of storing anything itself:

| JMAP capability                          | Backend protocol      |
|-------------------------------------------|------------------------|
| `urn:ietf:params:jmap:mail`, `:submission` | IMAP + SMTP           |
| `urn:ietf:params:jmap:calendars`           | CalDAV                |
| `urn:ietf:params:jmap:contacts`            | CardDAV                |

This lets any JMAP client (webmail, aerc, etc.) talk to an ordinary IMAP/
CalDAV/CardDAV provider that never implemented JMAP itself.

## Design

- **Zero local storage.** Every request is served live from the backend
  server. There's no database, no cache of message bodies, no persisted
  state of any kind — JMAP `state` strings are computed on the fly from
  backend metadata (IMAP `UIDVALIDITY`/`HIGHESTMODSEQ`, CalDAV/CardDAV sync
  tokens).
- **Credential passthrough.** Clients authenticate with their real IMAP/
  CalDAV username and password via HTTP Basic auth on every request; the
  bridge never stores a credential and never mediates OAuth. One JMAP
  session always maps to exactly one backend account.
- **Multi-tenant via config.** `config/domains.yaml` maps each email
  domain to its own IMAP/SMTP/CalDAV/CardDAV servers, so one bridge
  deployment can serve several unrelated domains/providers.
- **Async Python**, built on Starlette/uvicorn, with a bounded per-user
  IMAP connection pool and stateless HTTP calls to CalDAV/CardDAV.

See [`GAPS.md`](GAPS.md) for what's deliberately out of scope or deferred,
and the module docstring at the top of most `src/jmap_bridge/**/*.py` files
for the reasoning behind specific design decisions.

## Status

All three phases are implemented and have been live-tested against real
backends (Dovecot, Nextcloud, Radicale) and real JMAP clients (aerc,
Bulwark webmail): Mail↔IMAP, Calendars↔CalDAV, Contacts↔CardDAV.

## Requirements

- Python ≥ 3.11
- An IMAP + SMTP server per domain you want to serve (required)
- A CalDAV and/or CardDAV server per domain (optional — omitting them just
  means that domain's JMAP session won't advertise the calendars/contacts
  capability)

## Quick start (local)

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp config/domains.example.yaml config/domains.yaml
# edit config/domains.yaml with your real IMAP/SMTP/CalDAV/CardDAV hosts

JMAP_BRIDGE_CONFIG=config/domains.yaml \
JMAP_BRIDGE_BASE_URL=http://localhost:8080 \
.venv/bin/python -m jmap_bridge.app
```

Point a JMAP client at `http://localhost:8080/.well-known/jmap` (or
`/session` directly), authenticating with an IMAP username/password for one
of the domains in `domains.yaml`.

## Configuration

All configuration is one YAML file, `config/domains.yaml` (see
[`config/domains.example.yaml`](config/domains.example.yaml) for the full
format and comments). Each top-level key is an email domain; `imap` and
`smtp` are required, `caldav`/`carddav` are optional per domain.

Environment variables (all read in `src/jmap_bridge/app.py`):

| Variable                    | Default                  | Purpose                                                   |
|------------------------------|---------------------------|-------------------------------------------------------------|
| `JMAP_BRIDGE_CONFIG`         | `config/domains.yaml`    | Path to the domains config file                             |
| `JMAP_BRIDGE_BASE_URL`       | `http://localhost:8080`  | Public URL this bridge is reachable at (used in JMAP Session URLs) |
| `JMAP_BRIDGE_LOG_LEVEL`      | `INFO`                   | Log level for the bridge's own loggers (third-party libraries stay at WARNING regardless — see `configure_logging` in `app.py`) |
| `PORT`                       | `8080`                   | Port to listen on                                            |
| `JMAP_BRIDGE_SSL_KEYFILE` / `JMAP_BRIDGE_SSL_CERTFILE` | unset | Enable direct TLS termination (see deployment below) |

## Deployment

`Containerfile` + `compose.yml` build and run a production image. Your
`config/domains.yaml` is never baked into the image — it's mounted
read-only at container start.

```bash
cp config/domains.example.yaml config/domains.yaml   # edit with real values
JMAP_BRIDGE_BASE_URL=https://jmap.example.com docker compose up -d --build
# (or: podman compose up -d --build)
```

Two ways to run it (documented in detail in `compose.yml`):

1. **Reverse-proxy-fronted (default).** Run the bridge as plain HTTP behind
   nginx/Caddy/Traefik, which does real ACME/TLS. Nothing extra to
   configure beyond `domains.yaml` and `JMAP_BRIDGE_BASE_URL`.
2. **Direct TLS termination.** For a deployment with no separate proxy,
   uncomment the `certs` volume and `JMAP_BRIDGE_SSL_*` variables in
   `compose.yml` and mount a real certificate pair.

The image ships a `HEALTHCHECK` (`docker/healthcheck.py`) that understands
JMAP's auth semantics (every real route requires auth or redirects, so a
plain "is it 2xx" check would be wrong).

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/unit -q
```

- `tests/unit/` — fast, no network, fakes every backend connection.
- `tests/integration/` + `tests/fixtures/docker-compose.test.yml` — spins
  up real Dovecot/Radicale containers and drives the bridge against them.
- Source lives under `src/jmap_bridge/`: `types/` holds one module per
  JMAP type (`Email`, `Mailbox`, `Calendar`, `ContactCard`, ...), each
  registering its methods via `@method(...)`; `backends/` holds the
  IMAP/CalDAV/CardDAV/SMTP client code and the id-encoding/state-diffing
  logic that maps each protocol onto JMAP's data model.

## License

Not yet specified.
