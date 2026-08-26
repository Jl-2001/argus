# Multi-host deployment: persistent services

This document covers running Argus in **multi-host mode** (see
README.md's "Multi-host monitoring" section for the feature itself)
as *persistent* services that survive logout and reboot on both
machines, using the SSH-reverse-tunnel topology this deployment
validated. It's operational documentation for one specific,
already-working setup -- it doesn't change how Argus behaves.

Templates referenced below live in `deploy/macos/` and `deploy/linux/`.
Every template is a sanitized `.example` file: copy it, fill in the
placeholders, and never commit the filled-in copy.

## Architecture

```text
   Mac (control plane)                    Ubuntu Dell (remote host)
  +----------------------+               +---------------------------+
  |  Argus API :8088     |               |                           |
  |  (127.0.0.1 only)    |   SSH reverse |  127.0.0.1:18088          |
  |  local collector      |<==tunnel=====>|  (forwarded from Mac:8088)|
  |  SQLite + incidents   |   -R 18088:  |          ^                |
  |  SSE: /api/v1/events  |   127.0.0.1: |          |                |
  +-----------^-----------+   8088       |   argus-agent -----> local Docker
              |                          |   (reads Docker locally   |
     POST /api/v1/agents/ingest          |    on the Dell only)      |
              +--------------------------+          |                |
                                          |   Argus web :5174         |
                                          |   (VITE_ARGUS_API_URL=    |
                                          |    http://127.0.0.1:18088)|
                                          +---------------------------+
```

Key properties, unchanged from README.md's own description of this
milestone:

- **The Mac never reaches into the Dell's Docker socket**, directly
  or through the tunnel. `argus-agent` is the only process that ever
  touches Docker on the Dell, and it only reads (list/inspect/logs).
- The tunnel carries exactly one thing: the Argus control-plane API
  (HTTP + SSE), forwarded from the Mac's `127.0.0.1:8088` to the
  Dell's `127.0.0.1:18088`. Nothing else rides on it.
- Data flows *up* -- `argus-agent` on the Dell POSTs sanitized
  snapshots to the control plane. The control plane never initiates a
  connection to the Dell.
- Both ends of the forward are loopback-only (`127.0.0.1`). The
  tunnel does not make the Argus API reachable from the Dell's LAN or
  the wider network, and the Argus API itself is never bound to
  `0.0.0.0` on either machine.

## Persistent services

### Mac (launchd)

Three `launchd` user agents, templated in `deploy/macos/`:

| Label | Template | Equivalent to |
|---|---|---|
| `com.argus.api` | `com.argus.api.plist.example` | `argus-api` (binds `127.0.0.1:8088`) |
| `com.argus.collector` | `com.argus.collector.plist.example` | `python -m argus.run_collector` |
| `com.argus.tunnel` | `com.argus.tunnel.plist.example` | the `ssh -R 18088:127.0.0.1:8088 ...` command below |

Install each one:

```bash
mkdir -p ~/Library/LaunchAgents
cp deploy/macos/com.argus.api.plist.example ~/Library/LaunchAgents/com.argus.api.plist
cp deploy/macos/com.argus.collector.plist.example ~/Library/LaunchAgents/com.argus.collector.plist
cp deploy/macos/com.argus.tunnel.plist.example ~/Library/LaunchAgents/com.argus.tunnel.plist

# Edit every <ARGUS_HOME>, <YOUR_USERNAME>, <REMOTE_USER>, <REMOTE_HOST>
# placeholder in the three copies above -- not the .example originals.

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.argus.api.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.argus.collector.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.argus.tunnel.plist
```

After editing an installed plist, reload it:

```bash
launchctl bootout gui/$(id -u)/com.argus.api
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.argus.api.plist
```

The tunnel requires SSH key authentication to the Dell to already
work non-interactively (`ssh <REMOTE_USER>@<REMOTE_HOST>` with no
password/passphrase prompt) -- `launchd` cannot answer an interactive
prompt.

### Dell (systemd --user)

Two `systemd --user` units, templated in `deploy/linux/`:

| Unit | Template | Equivalent to |
|---|---|---|
| `argus-agent.service` | `argus-agent.service.example` | `argus-agent`, configured via an env file |
| `argus-web.service` | `argus-web.service.example` | `npm run dev -- --port 5174` |

Install:

```bash
mkdir -p ~/.config/systemd/user ~/.config/argus
cp deploy/linux/argus-agent.service.example ~/.config/systemd/user/argus-agent.service
cp deploy/linux/argus-web.service.example ~/.config/systemd/user/argus-web.service
cp deploy/linux/argus-agent.env.example ~/.config/argus/argus-agent.env

# Edit every placeholder: <ARGUS_HOME>, <ARGUS_AGENT_ENV_FILE>,
# <ARGUS_WEB_HOME>, <NPM_PATH> in the two unit copies, and every
# <...> value in ~/.config/argus/argus-agent.env (agent id/token/host
# key come from `argus agents add` on the control plane -- see
# README.md's "Multi-host monitoring" section).

chmod 600 ~/.config/argus/argus-agent.env

systemctl --user daemon-reload
systemctl --user enable --now argus-agent.service
systemctl --user enable --now argus-web.service
```

The Dell-hosted dashboard also needs a local, git-ignored
`web/.env.local` (see "Realtime disconnected -- polling" below) --
this is separate from `argus-web.service` and is not something a
systemd unit sets.

### Lingering (Dell)

For `argus-agent.service` and `argus-web.service` to keep running
without an active login session (and to auto-start on reboot before
any login), the user needs lingering enabled once:

```bash
sudo loginctl enable-linger <user>
```

Verify:

```bash
loginctl show-user <user> -p Linger
# expect: Linger=yes
```

## Validation commands

### Mac

```bash
launchctl print gui/$(id -u)/com.argus.api
launchctl print gui/$(id -u)/com.argus.collector
launchctl print gui/$(id -u)/com.argus.tunnel

curl http://127.0.0.1:8088/api/v1/system/status
```

### Dell

```bash
systemctl --user status argus-agent.service
systemctl --user status argus-web.service

ss -ltnp | grep ':18088'

curl http://127.0.0.1:18088/api/v1/system/status
```

### SSE (cross-host realtime)

The dashboard's `Live` indicator (vs. `Realtime disconnected --
polling`) reflects `GET /api/v1/events` staying open (see README.md's
"Real-time dashboard" section). Verify it directly on each host:

Mac:

```bash
curl -v -N \
  -H "Accept: text/event-stream" \
  http://127.0.0.1:8088/api/v1/events
```

Dell (through the tunnel):

```bash
curl -v -N \
  -H "Accept: text/event-stream" \
  http://127.0.0.1:18088/api/v1/events
```

A working stream returns:

```text
HTTP/1.1 200 OK
content-type: text/event-stream
```

and then stays **open** -- it should keep producing events/heartbeats
rather than closing. `Ctrl-C` to stop watching; the connection closing
on its own, or the initial response never arriving, means something
between that host and the API is broken (start with the validation
commands above).

## Troubleshooting

These are the specific issues found during reboot testing on this
deployment -- not an exhaustive Docker/systemd/launchd troubleshooting
guide.

### Dell: `argus-agent` fails with `Docker unavailable` / `FileNotFoundError`

Seen after a reboot, even though Docker itself was healthy and
`/var/run/docker.sock` existed. Check:

```bash
systemctl status docker
ls -l /var/run/docker.sock
id
docker context show
docker context inspect "$(docker context show)"
```

On this Dell, the active Docker context (`docker context show`) had
drifted to `desktop-linux`, pointing at a stale Docker-Desktop-style
socket (`~/.docker/desktop/docker.sock`) that didn't exist on this
native-Docker-Engine machine. `argus-agent` (via the `docker` Python
SDK's `docker.from_env()`) follows whatever context is active, so it
failed the same way the CLI would have.

The fix, **on this machine**, was to point the context back at the
native socket:

```bash
docker context use default
```

Then confirm the SDK agrees:

```bash
python - <<'PY'
import docker
client = docker.from_env()
print(client.ping())
PY
# expect: True
```

This is **not a universal fix** -- which context is correct depends on
how Docker is installed on a given machine (native Docker Engine vs.
Docker Desktop for Linux vs. something else). The general check is
"does `docker context show` point at a context whose socket actually
exists and is reachable by this user," not "always run `docker
context use default`."

### Mac: local collector shows `STALE` immediately after login/reboot

The Mac collector reads Docker through Docker Desktop
(`desktop-linux` context, `~/.docker/run/docker.sock`), which doesn't
exist until Docker Desktop has finished starting. Check:

```bash
ls -l ~/.docker/run/docker.sock
```

If it's missing, start Docker Desktop -- the collector recovers on
its own (`HEALTHY`, `consecutive_failures=0`) once the socket appears;
no restart of `com.argus.collector` is needed.

To avoid this after every reboot, enable **Start Docker Desktop when
you sign in to your computer** in Docker Desktop's settings whenever
Argus is expected to monitor Mac-local containers continuously.

### Dashboard shows "Realtime disconnected -- polling"

Root cause on this deployment: the Dell-hosted dashboard defaulted to
`http://127.0.0.1:8088` (see `web/src/lib/env.ts`), which is only
reachable on the Mac itself -- the Dell reaches the control plane at
`http://127.0.0.1:18088` (the tunnel's forwarded port). SSE (and every
other API call) failed to connect until the dashboard was told the
right address.

Fix: create (on the Dell, never committed) `web/.env.local`:

```text
VITE_ARGUS_API_URL=http://127.0.0.1:18088
```

Then restart the Vite dev server (`argus-web.service` if running as a
systemd unit, or `npm run dev` directly) so it picks up the new env
file. The top bar should then read `Live`.

## Security notes

- **Never expose the Docker socket, or an unauthenticated Docker TCP
  API, to the network** -- on either machine. Every collector/agent in
  this deployment reads Docker over its own machine's local Unix
  socket only.
- The Mac control plane does not, and should not be made to, connect
  to the Dell's Docker socket -- remotely or otherwise. `argus-agent`
  is the sole reader of Docker on the Dell.
- The SSH reverse tunnel terminates on loopback (`127.0.0.1`) on both
  ends. Nothing forwarded through it is reachable from either
  machine's LAN interface.
- `ARGUS_AGENT_TOKEN` (and the SSH private key used for the tunnel)
  are secrets. The control plane persists only a **hash** of the
  agent token, never the raw value (see README.md's "Multi-host
  monitoring" section) -- the raw token exists only in the
  `argus agents add` output at issuance and in the agent's own
  environment file afterward.
- Never commit an SSH private key, a filled-in `*.env` file, or a
  filled-in `web/.env.local`. Never log a raw token or key.
- Argus monitoring remains **read-only** end to end, including across
  hosts -- `argus-agent` cannot start/stop/restart/exec/mutate
  anything on any host (see README.md and `argus/agent/`'s own
  docstrings). This deployment work does not add, and is not the
  place to add, any remote remediation or mutation capability.

## What's machine-specific (not templated)

The templates above are sanitized on purpose. You'll still need to,
per machine:

- Fill in every `<...>` placeholder (paths, usernames, remote
  host/user, `NPM_PATH`) with values specific to that machine.
- Generate/register the real `ARGUS_AGENT_TOKEN` via
  `argus agents add <host-key> --name "<display name>"` on the
  control plane, and place it only in the Dell's local, `chmod 600`,
  never-committed env file.
- Set up SSH key authentication from the Mac to the Dell (not
  Argus-specific, but required for `com.argus.tunnel`).
- Create the Dell's `web/.env.local` locally (git-ignored via
  `web/.gitignore`'s `*.local` pattern) -- it is never part of this
  repo's tracked content.
- Confirm which Docker context is correct for each machine (see
  Troubleshooting) -- this is environment-dependent, not something a
  template can encode.
