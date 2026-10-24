# portal

<p align="center"><img src="docs/hero.svg" alt="portal: point a dockerized project at a hostname, get a public tunnel" width="720"></p>

portal is a small Python library for putting a dockerized project on a public
hostname through Cloudflare Tunnel. You give it a project directory and a
hostname. It builds and starts the project without publishing any host ports,
creates a tunnel just for that project, runs a `cloudflared` sidecar next to
it, points DNS at the tunnel, and then keeps requesting the hostname until
something answers.

Each project gets its own tunnel and its own `cloudflared` container. Projects
share nothing, so tearing one down can't take another with it.

## Install

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

You need Python 3.11+, the `docker` CLI on PATH with Compose 2.24 or newer (the
reason is under [What deploy() does](#what-deploy-does)), and a Cloudflare API
token.

## Configuration

All configuration lives in one `.env` file:

```bash
cp .env.example .env
```

```dotenv
CLOUDFLARE_API_TOKEN=...     # required
CLOUDFLARE_ACCOUNT_ID=...    # optional; looked up from the token if missing
```

The token needs three permissions:

- Account → Cloudflare Tunnel → Edit
- Zone → DNS → Edit
- Zone → Zone → Read

Real environment variables win over the file. portal finds the file by walking
up from the current directory. To use a different file, set `PORTAL_ENV_FILE`
or pass `env_file=` to `deploy()` or `destroy()`. `status()` and `inspect()`
don't need the token at all.

A few optional environment variables change portal's own behavior:

| Variable | Default | Effect |
| --- | --- | --- |
| `PORTAL_ENV_FILE` | nearest `.env` | Which file to read the token from |
| `PORTAL_HOME` | `~/.portal` | Where state and generated compose files live |
| `PORTAL_CLOUDFLARED_IMAGE` | a pinned `cloudflare/cloudflared` tag | Sidecar image to run |

This `.env` belongs to portal. It has nothing to do with the `.env` of the
project you're deploying. portal never reads that file's values: it passes the
path to `docker compose --env-file`, and it only looks at which key names from
the project's `.env.example` are missing.

## Usage

```python
from portal.api import deploy, destroy, status, inspect

inspect("~/code/myapp")                 # detected plan, no side effects

r = deploy("~/code/myapp", host="app.example.com")
r.live_url, r.verified, r.probe         # 'https://app.example.com', True, 'HTTP 200'

status()                                # every deployment: containers + a live probe
destroy("myapp")                        # DNS record, tunnel, containers, state entry
```

`deploy()` also accepts `port=` and `service=` for cases where detection
guesses wrong or the compose file has several candidate services. The
deployment's name (its slug) comes from the directory name. Pass `name=` to
pick another one. portal refuses to deploy a second directory under a name
that's already taken by a different path.

`host` has to be a bare hostname such as `app.example.com` or `example.com`,
with no scheme, port, or path.

None of these calls ask for confirmation. `deploy()` creates real Cloudflare
resources and `destroy()` deletes them, so if a human needs to approve that
first, the caller has to handle it. Failures raise `PortalError` with a message
you can show to a person as-is.

portal logs progress to the `portal` logger. Call
`logging.basicConfig(level=logging.INFO)` to watch a deploy step by step.
Warnings, such as keys from the project's `.env.example` that are missing from
its `.env`, show up even without that.

### From the shell

The same four calls are available as a thin command-line wrapper:

```bash
python -m portal -v deploy ~/code/myapp --host app.example.com
python -m portal inspect ~/code/myapp
python -m portal status
python -m portal destroy myapp
```

`deploy` and `inspect` take `--port`, `--service`, and `--name`. `deploy` also
takes `--docker-host`. `destroy` exits with status 1 if cleanup was only
partial.

<p align="center"><img src="docs/flow.svg" alt="deploy() flow: detect, docker up, wait, tunnel, ingress, DNS, curl-verify" width="720"></p>

## What deploy() does

1. **Detect.** Finds the compose file, or a bare Dockerfile, and picks the
   service to expose. A service using `network_mode` is rejected, because
   compose can't attach it to portal's network.
2. **Start the app.** Runs `docker compose -p portal-<slug> -f <yours> -f
   <override> up -d --build`. portal never edits your compose file. It adds an
   override on top that sets a fixed `container_name`, joins the service to a
   per-project network (keeping any networks it already had), adds `portal.*`
   labels, and sets `ports: !override []`.

   The `!override` tag is required. Compose normally merges lists, so a plain
   `ports: []` would leave your existing port mappings in place. This merge tag
   needs Compose 2.24 or newer.

   For a project with only a Dockerfile, portal generates a minimal compose
   file under `~/.portal/projects/<slug>/`, not inside your project.
3. **Wait for the container.** `compose up -d` returns as soon as the
   container is created, even if the container crashes a second later. portal
   waits up to 30 seconds for `docker ps` to report the container as `Up`, and
   raises an error otherwise. Cloudflare isn't touched until the container is
   running.
4. **Create the tunnel.** portal creates a tunnel named `portal-<slug>`, or
   reuses it if it already exists. The tunnel uses `config_src: cloudflare`, so
   its ingress rules are managed through the API instead of a local config file.
5. **Start the sidecar.** Runs a pinned `cloudflare/cloudflared` image as
   `portal-<slug>-cloudflared` on the same network. Set
   `PORTAL_CLOUDFLARED_IMAGE` to use a different one. The tunnel token is passed
   to it through the environment, so it never appears in the `docker run`
   arguments.
6. **Set ingress.** One rule for the hostname, followed by the required
   `http_status:404` catch-all.
7. **Point DNS.** portal finds the zone by trying the hostname and then each
   parent domain, longest first, so a zone apex works too. It then creates or
   updates a proxied CNAME to `<tunnel-id>.cfargotunnel.com` with the comment
   `managed by portal:<slug>`. If a record already exists for that name without this exact
   comment, portal refuses to change it. `destroy()` follows the same rule
   before deleting a record. If you redeploy a project under a new hostname,
   portal deletes the old record.
8. **Verify.** portal requests `https://<host>` every 3 seconds for up to a
   minute. A new tunnel and DNS record often take a few seconds before they
   route traffic, so a single check right after setup would usually fail even
   when everything is fine. Any response from the app counts as success, error
   statuses included. The statuses Cloudflare's edge produces when it can't
   reach the app (502, 504, 520–530) don't count, and portal keeps polling.
   If the minute runs out, `deploy()` still returns normally, with
   `verified=False`. `probe` has the last status.

The deployment is recorded in `~/.portal/state.json` as each resource is
created, not only at the end. A deploy that fails halfway can still be cleaned
up with `destroy()`. The file contains IDs and paths, never secrets.

`destroy()` runs these steps in reverse. It stops the `cloudflared` sidecar
before deleting the tunnel, because Cloudflare won't delete a tunnel that
still has connections. If deleting the DNS record or the tunnel fails, it logs a
warning and still removes the containers. It also keeps the state entry
(`state_removed=False`), so calling `destroy()` again finishes the cleanup.

## Remote hosts over SSH

The docker CLI supports `DOCKER_HOST=ssh://user@host`, which runs each command
against the Docker daemon on another machine over SSH. portal exposes this as
`docker_host=`:

```python
deploy(
    "~/code/myapp",
    host="app.example.com",
    docker_host="ssh://deploy@203.0.113.9",
)
```

The remote machine only needs `docker` and `sshd`. portal doesn't need to be
installed there. The project directory stays on your machine, and
`docker compose build` sends the build context over the SSH connection.

Requirements:

- `ssh` on your local PATH.
- **Key-based** SSH access to the remote account.
- Docker running on the remote machine, with that user in the `docker` group.

Pass `docker_host` only to `deploy()`. It's saved in `state.json`, and
`status()` and `destroy()` read it from there, so local and remote deployments
work together without extra arguments. You can override the saved value by
passing `docker_host` to `destroy()` explicitly.

### Password-only SSH won't work

When `DOCKER_HOST` is set, docker runs `ssh` itself as a subprocess with no
terminal attached, so a password prompt can't be answered. The deploy either
hangs or fails, and there's no reliable way to supply the password. Set up a
key first:

```bash
# dedicated keypair (or reuse one you already have)
ssh-keygen -t ed25519 -f ~/.ssh/portal_deploy -C "portal-deploy"

# install it; this is the last time you'll type the password
ssh-copy-id -i ~/.ssh/portal_deploy.pub deploy@203.0.113.9

# no ssh-copy-id (plain Windows)? From PowerShell:
type $env:USERPROFILE\.ssh\portal_deploy.pub | ssh deploy@203.0.113.9 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"

# should print "ok" without asking for anything
ssh -i ~/.ssh/portal_deploy deploy@203.0.113.9 echo ok
```

Then add the host to `~/.ssh/config` so plain `ssh://deploy@203.0.113.9`
picks up the key:

```
Host 203.0.113.9
    User deploy
    IdentityFile ~/.ssh/portal_deploy
```

If you can't add a key to the remote account, `docker_host` won't work for
that machine. Run portal on the remote machine itself and leave out
`docker_host`.

## Scope

portal deploys one project per call. It has no drift detection, no automatic
rollback, no background daemon, and no shared tunnels. A failed deploy isn't
undone, but it is recorded, so `destroy()` can clean it up. `status()` only
reports problems and never fixes anything.

portal doesn't write Dockerfiles. The project has to already have a Dockerfile
or a compose file. Working out how to containerize a project is a separate
problem, and keeping it out is what keeps portal small.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The tests cover project detection, ingress rule building, the state store and
its lock, the compose override and synthesized compose files, zone lookup and
the DNS ownership checks, the docker environment handling for `docker_host`,
`.env` loading, and the full `deploy()`/`status()`/`destroy()` orchestration
with docker and Cloudflare stubbed out. The orchestration tests include
half-finished deploys, redeploys to a new hostname, name collisions, and
partial teardowns. None of this touches a real Docker daemon or the
Cloudflare API, so `pytest` needs no credentials.

To actually verify a token against the live Cloudflare API — creates a
throwaway tunnel, confirms it, deletes it, confirms it's gone — run:

```bash
PORTAL_RUN_LIVE_TESTS=1 pytest -m live -s -v tests/test_live_cloudflare.py
```

This is opt-in and double-gated (it's excluded from a plain `pytest` run and
still skips without the env var even if you ask for `-m live`), because it
creates and deletes a real resource under your account. Add
`PORTAL_LIVE_TEST_ZONE=example.com` to also check zone visibility, and
`PORTAL_LIVE_TEST_DNS=1` on top of that to exercise a real CNAME create/delete
as well — that one briefly writes a public DNS record, so it's a separate
opt-in from the tunnel check.
