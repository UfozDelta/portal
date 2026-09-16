# portal

Point portal at a dockerized project and a hostname. It builds and starts the
project with no host ports published, gives it its own Cloudflare Tunnel, runs a
dedicated `cloudflared` sidecar beside it, and points DNS at the tunnel. The
machine ends up with nothing listening on a public port.

One tunnel per project. One `cloudflared` container per project. Nothing shared.

## Install

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

Needs Python 3.11+, `docker` on PATH, and a Cloudflare API token.

## Cloudflare token

Create a scoped API token with:

- Account → Cloudflare Tunnel → Edit
- Zone → DNS → Edit
- Zone → Zone → Read

Give it to portal either way:

```bash
export CLOUDFLARE_API_TOKEN=...      # checked first
portal init --token ...              # or write ~/.portal/credentials.toml
```

The token is read from the environment first and falls back to
`~/.portal/credentials.toml`. It is never written to `state.json` and never
printed, including inside error messages. On POSIX the credentials file is
chmod 0600; **on Windows portal does not set an ACL on it yet** — treat the file
as a secret and lock it down yourself.

Your project's own `.env` is never read or copied. portal only checks which
*key names* from `.env.example` are missing from it.

## Commands

| command | what it does |
| --- | --- |
| `portal init [--token T]` | Verifies the token, caches the account id, creates `~/.portal/`. |
| `portal inspect PATH [--port N] [--service S] [--json]` | Reports what would be deployed. Changes nothing. |
| `portal deploy PATH --host FQDN [--port N] [--service S] [--yes]` | Deploys and publishes. Prompts before creating cloud resources unless `--yes`. |
| `portal status [SLUG] [--no-probe]` | Container state plus an HTTPS probe of the hostname. |
| `portal destroy SLUG [--yes] [--volumes]` | Removes DNS, tunnel, containers, state entry. Preserves volumes unless `--volumes`. |

## Example

```bash
export CLOUDFLARE_API_TOKEN=...
portal init
portal inspect ~/code/myapp
portal deploy ~/code/myapp --host app.example.com
portal status
portal destroy myapp
```

## What deploy actually does

1. Creates a docker network `portal-<slug>`.
2. `docker compose -p portal-<slug> -f <your compose> -f <portal override> up -d --build`.
   Your compose file is never modified — the override layers on top of it, adding
   a deterministic `container_name`, the portal network, `portal.*` labels, and
   `ports: []` so nothing is published to the host. A Dockerfile-only project gets
   a minimal compose file synthesized under `~/.portal/projects/<slug>/`.
3. Creates a Cloudflare Tunnel named `portal-<slug>` with `config_src: cloudflare`,
   so its ingress is managed over the API rather than by a local config file.
4. Runs `cloudflare/cloudflared:latest` as `portal-<slug>-cloudflared` on the same
   network, with the tunnel token in its environment.
5. Sets the tunnel ingress to one hostname rule plus the mandatory
   `http_status:404` catch-all, in that order.
6. Resolves the zone for the hostname and upserts a proxied CNAME to
   `<tunnel-id>.cfargotunnel.com`, stamped `comment: managed by portal:<slug>`.
   A pre-existing record without that stamp is refused, not overwritten.
7. Scaffolds `deploy.sh` and `deploy.bat` into your project **only if absent**.
8. Records the deployment in `~/.portal/state.json` (atomic write, no secrets).

## Scope

Phase 1: the five commands above, one project at a time, happy path. No drift
detection, no rollback, no daemon, no shared tunnels.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Covers detection, ingress rule building, the state store, the override builder
and the scaffold/DNS-ownership rules. Live Cloudflare calls and real
`docker compose` runs are not automated — they need credentials and a daemon.
