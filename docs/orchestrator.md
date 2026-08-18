# jetty-orc — the jetty orchestrator

`jetty.orchestrator` launches and supervises a co-located multi-process stack
— typically: an application, its backend processes, and a jetty sidecar — as
one named **instance**, from one TOML file, in the foreground. Linux only.

What it gives you that a shell script of backgrounded processes doesn't:

- **Whole-tree teardown.** Ctrl-C (or `jetty-orc kill`, or the supervisor
  dying) takes down every process an instance started — including
  double-forked grandchildren like `npx`-wrapped servers — because grouping
  is delegated to the kernel (cgroup v2 where available), not to pid lists.
- **Port brokering.** Named ports are allocated (or verified) at launch and
  injected into commands, environments and probes via `{ports.<name>}`.
  Instances get disjoint ports automatically, so several can run at once.
- **Restart policy with a budget.** Bounded restarts with backoff; exhaustion
  fails the whole instance loudly, with the crashing service's log tail.
- **Gates.** External conditions (credentials, VPN) probed by a command. A
  service that dies while its gate is failing parks in `blocked` — no restart
  budget spent — and revives when the gate passes again.
- **A fleet view.** `jetty-orc ls` shows every instance with health, ports,
  process count, memory and CPU; `status` breaks one down per service.

The package is deliberately standalone: stdlib + pydantic only, no
jetty-core imports, and every intra-package import is relative — so the
`src/jetty/orchestrator/` directory can be vendored into another build system
(bazel included) at any module path. Point a binary at
`jetty.orchestrator.cli:main` (or whatever path it lands on) and everything —
including the self-re-exec described below — keeps working.

## Quick start

```sh
jetty-orc doctor                              # what this host offers
jetty-orc check -c orchestrator.example.toml  # validate config, spawn nothing
jetty-orc up    -c orchestrator.example.toml  # run in the foreground; Ctrl-C stops all
jetty-orc ls                                  # every instance, from any terminal
jetty-orc status <name>                       # one instance, per service
jetty-orc ps <name>                           # full process tree, per service
jetty-orc logs <name> [-f] [-n 50]            # prefixed service logs, tail with -f
jetty-orc kill <name> [--force]               # stop it from outside
```

`up` runs `concurrently`-style: every service's output streams to the
console under a coloured `[service]` prefix (line-buffered, so lines never
interleave; colour only on a TTY, `NO_COLOR` respected; `--quiet` turns the
echo off). `logs` gives the same view from any other terminal — the last
`-n` lines per service, `-f` to keep tailing, and it works on a failed
instance's post-mortem logs too.

Exit codes match the jetty binary's contract: `0` clean stop, `1` runtime
failure (instance failed, port occupied, kill timed out), `2` config error —
a supervisor restarting an exit-2 would be retrying a config bug.

## Configuration

Annotated worked example: [`orchestrator.example.toml`](orchestrator.example.toml).
Unknown keys, dangling references, `after` cycles, duplicate fixed ports and
placeholder typos are all load-time errors — `check` catches everything that
can be caught without spawning.

```toml
[instance]
name = "dev"                # BASE name, [a-z0-9][a-z0-9_-]{0,63}: each `up`
                            # appends a short random suffix (dev-a3f1) so one
                            # config can run several instances concurrently.
                            # `up --name <exact>` pins the name instead — do
                            # that when you want a stable identity and a
                            # stable {state_dir} across restarts.
containment = "auto"        # auto | cgroup | scope | pgroup   (see Containment)
# workdir = "~"             # default runtime dir for services/gates/resolvers;
                            # unset = the config file's own directory

[ports]
api = "auto"                # kernel-picked free port
web = 5173                  # exactly 5173: refused if occupied
ui  = "5173+"               # prefer 5173; occupied -> 5174, 5175, ...
db  = "9000-9020"           # same, bounded: error if the whole range is taken

[gates.creds]
check = ["auth-tool", "status"]   # argv; exit 0 = satisfied
recheck_seconds = 15.0            # cache + poll cadence while blocked
timeout_seconds = 20.0            # unrunnable/overdue check = unsatisfied
continuous = false                # true = runtime invariant: a RUNNING
                                  # service is stopped (budget-free, into
                                  # `blocked`) while the gate is closed,
                                  # and revived when it reopens
close_after = 3                   # continuous only: consecutive failed
                                  # checks before "closed" — one flaky
                                  # probe never kills a healthy process;
                                  # reopening takes a single pass

[services.api]
cmd = ["python", "-m", "uvicorn", "app:app", "--port", "{ports.api}"]
cwd = "/path/to/server"
env = { DB = "{state_dir}/app.sqlite" }   # overlaid on the inherited env
after = ["backend"]         # start only once these are READY (probe passed)
requires = ["creds"]        # gates this service depends on

[services.api.ready]        # at most one probe; none = ready once spawned
http = "http://127.0.0.1:{ports.api}/healthz"   # GET, any status < 400
# tcp  = "127.0.0.1:{ports.api}"                # connect succeeds
# uds  = "{state_dir}/api.sock"                 # unix socket connect succeeds
# path = "{state_dir}/api.pid"                  # file exists
timeout_seconds = 30.0      # probe never passes -> incarnation killed + counted
interval_seconds = 0.25

[services.api.restart]
max_restarts = 3            # unexpected exits tolerated per window...
window_seconds = 60.0       # ...before the whole instance fails
backoff_initial_seconds = 0.5   # doubles per consecutive failure, capped
backoff_max_seconds = 15.0
no_restart_exit = [2]       # exit codes meaning "retrying cannot help"

[services.api.stop]
signal = "TERM"             # TERM | INT | HUP, sent to the whole group
grace_seconds = 10.0        # then SIGKILL to the whole group
```

### Inheritance

A config can extend one other config (single inheritance; chains like
dev → staging → prod are fine, cycles are errors):

```toml
# dev.toml
extends = "prod.toml"        # usual path rules: relative = this file's
                             # subtree, ~/absolute = anywhere

[instance]
name = "app-dev"

[ports]
api = "8000+"                # replaces prod's fixed port

[services.api.env]
APP_DEMO_MODE = "1"          # tables deep-merge: only this key changes

[services]
metrics = false              # `false` deletes an inherited service
```

Merge rules: tables merge recursively with the child winning; scalars,
arrays and argv strings replace wholesale (a child wanting a different
`cmd` states the whole cmd — positional list-splicing is a guessing game);
overriding an inherited table with `false` deletes it. `extends` resolves
before validation, so `check` and every load-time error apply to the merged
result. One anchoring note: relative paths *inside* the merged config
(cwd, scripts) resolve against the **entry** config's directory — keep
parent and child side by side (the normal layout), or use
absolute/`~`/`{env.*}` paths in the parent.

### Placeholders

`{ports.<name>}`, `{instance.name}`, `{home}`, `{state_dir}`, `{logs_dir}`
render into `cmd`, `env` values, `cwd`, readiness probes and gate/resolver
argvs after ports are allocated. `{{` / `}}` escape literal braces
(`str.format` parsing rules).

**Environment substitution** is how a config stays overridable without
editing it: `{env.NAME}` substitutes an environment variable (a clear error
at `check`/`up` if unset — that's how you mark a variable required), and
`{env.NAME:-default}` falls back to the default when the variable is unset
or empty (the docker-compose `:-` convention). In argv positions (`cmd`,
gate `check`, resolver `cmd`) an element that is *nothing but* an env
placeholder shell-splits after substitution:

```toml
[services.api]
cmd = ["python", "-m", "uvicorn", "app:app",
       "--log-level", "{env.API_LOG_LEVEL:-info}",   # scalar with default
       "{env.API_FLAGS:-}"]                          # optional flags: zero
                                                     # args unset, several set
```

```sh
API_LOG_LEVEL=debug API_FLAGS="--reload --workers 2" jetty-orc up -c orc.toml
```

An env placeholder embedded in a larger element (`"--x={env.Y:-}"`)
substitutes as plain text and stays one argument. Env vars are read at
render time, i.e. per spawn.

Port specs render too: `http = "{env.HTTP_PORT:-8080+}"` lets the
environment pick the spec (a bare number like `HTTP_PORT=9000` means that
fixed port; `auto` and scan forms work as values as well). Ports allocate
before the rest of the context exists, so a `{ports.*}` reference inside a
port spec is a self-reference and fails at `check`. The rendered spec is
validated at `check` and again at launch.

Paths compose with this: substitution happens before path resolution, so
`cwd = "{env.MY_PROJECT_DIR:-~/projects}"` uses the variable when set and
falls back to `~/projects` — and a `~` inside the variable's value expands
too. The usual rules then apply (absolute/`~` anywhere, relative confined
to the config's subtree).

Argv fields (`cmd`, gate `check`, resolver `cmd`) also accept a single
string instead of a list — `cmd = "./run --flag"` is shell-*split*, not a
shell: `&&` and pipes stay literal words; use `["bash", "-c", "..."]` for
shell semantics.

### Paths

Relative paths in a config anchor to **the TOML file's directory**, not to
wherever the supervisor was launched — a config means the same thing from
any cwd. They may reach siblings and anything below the config's directory,
but not outside it: `../sibling/thing` fails with a clear error at `check`
time, because it silently depends on where the config is checked out.
Anything outside the tree must be an absolute path — visible and deliberate.
This applies to command paths (`cmd[0]`, gate `check[0]`, resolver `cmd[0]`
— a bare name like `python` is a PATH lookup and passes through), to
`ready.path`, `ready.uds`, and to `cwd`.

`~` and `~user` expand and count as absolute — like an absolute path, they
name a location deliberately. `{home}` is also available as a placeholder
wherever placeholders render (env values included, which get no `~`
treatment).

Each service's runtime directory is its `cwd`: relative → config-relative
and confined as above; absolute (including `~`, e.g. `cwd = "~"`) →
anywhere; unset → `instance.workdir`, which itself defaults to the config
file's own directory. Gate checks and resolver scripts also run from
`instance.workdir` — so by default a resolver that reads a sibling manifest
means the same manifest from any launch cwd, and setting `workdir`
explicitly moves that default for the whole instance in one place.

### Environment

Services also receive in their environment: `JETTY_ORC_INSTANCE`,
`JETTY_ORC_SERVICE`, and — under cgroup containment — `JETTY_ORC_CGROUP_ROOT`,
the instance's cgroup directory. A service that reports resource usage (a
dashboard, a health endpoint) should read the instance's totals and process
list from that directory (`memory.current`, `cgroup.procs` in its subtree)
rather than from `/proc/self/cgroup`, which sees only the service's own
subgroup and undercounts once sibling services hold the interesting
processes.

A jetty sidecar reads no environment variables, so to orchestrate one,
generate its TOML (e.g. into `{state_dir}`) and pass `--config`.

## Lifecycle

Service states: `pending → starting → running`, plus `backoff`, `blocked`,
`stopping`, `stopped`, `failed`.

Services start concurrently except where `after` orders them, and `after`
means *ready*, not merely spawned. When a running service exits without being
asked to, the exit is classified, in order:

1. **Code in `no_restart_exit`** → instance fails immediately.
2. **A required gate is failing** → park in `blocked`; restart-budget history
   is cleared (the crash is the environment's fault, not the service's);
   re-check on the gate's cadence; restart when all gates pass. Gates are
   also consulted before the first spawn, so a service whose credentials are
   already missing starts life `blocked` instead of crash-looping.
3. **Otherwise** → restart after exponential backoff. More than
   `max_restarts` such exits within `window_seconds` fails the instance.

By default a gate never touches a *running* process — it is a start
precondition, and a live service rides out an expired credential until it
crashes on its own. A gate with `continuous = true` is stronger: a runtime
invariant. The supervisor polls it for every running service that requires
it (directly or via a resolver), and once it has failed `close_after`
consecutive checks — real runs, one per `recheck_seconds`, so a single
flaky probe is survived — the service is gracefully stopped (stop signal →
grace → SIGKILL to the group) and parked in `blocked`: no exit
classification, no restart budget. One passing check reopens the gate and
revives the service. Closure latency ≈ `close_after × recheck_seconds`;
reopening is immediate by design — slow to kill, quick to recover.

An instance failure stops every other service in reverse dependency order
(graceful signal → grace period → SIGKILL), prints the failing service's log
tail, exits 1, and leaves its final registry record as a post-mortem
(`ls` shows it until `kill <name>` clears it).

After **every** exit, crash or stop, the service's whole containment group is
swept with SIGKILL before any restart — a lingering grandchild can never hold
the port its successor needs.

Signals to the supervisor: first SIGINT/SIGTERM = clean stop of the whole
instance (exit 0); a second = immediate hard kill of every service group.

Logs: one file per service per run, under
`~/.jetty/logs/<instance>-<timestamp>/` — the timestamp is the `up`
invocation's, so separate runs of the same instance never interleave
(`$JETTY_ORC_LOG_ROOT` overrides the location; old run directories are never
deleted — prune them yourself). Append-only, stdout+stderr merged, with
orchestrator annotations (spawn headers, restart/gate notes) inline;
restarts within a run append under a fresh spawn header. The last ~8 KiB
travels with failure reports. Resolver invocations get the same treatment —
`resolver-<name>.log` beside the service logs (header, the script's stderr
**streamed line by line as it runs** — a slow release fetch's progress is
visible live, not replayed after exit — and the resolved result or
failure), included in `logs` and the `up` console; cache hits run nothing
and log nothing.

## Dynamic binaries (resolvers)

When "which binary" changes between releases, don't hardcode paths — declare
a resolver: a script whose stdout names the current path(s), run by the
supervisor before a spawn that needs it.

```toml
[resolvers.release]
cmd = ["infra/latest-release.sh"]        # config-relative, like all paths
provides = ["control_plane", "harness"]  # names bound by ONE invocation
requires = ["creds"]                     # optional: gates the feed depends on
timeout_seconds = 30.0
refresh = "spawn"        # default: re-run before each (re)spawn
                         # "instance": resolve once, pinned for the whole run
cache_seconds = 5.0      # one resolution shared across a spawn wave

[services.api]
cmd = ["{bin.control_plane}", "--port", "{ports.api}"]
env = { HARNESS_BIN = "{bin.harness}" }
```

Output contract: a resolver providing **one** name (`provides` defaults to
the resolver's own name) may print just the path; one providing **several**
prints `name=path` lines, any order, `#` comments and blank lines ignored.
Unknown, repeated or missing names, relative paths, and paths that don't
exist are all errors — order never carries meaning, so a reordered echo
can't silently swap two binaries.

Semantics worth relying on:

- **Pinning**: every name a resolver provides comes from one invocation,
  atomically; services spawning within `cache_seconds` of each other share
  that invocation. A manifest read mid-release cannot hand the control plane
  one version and its harness another.
- **Pinned groups restart together**: when a service (re)spawns and its
  multi-binary resolver has moved to a new release, every sibling still
  running the previous release is bounced too — a budget-free restart, so
  the group can never run split across versions. The trigger is the
  resolution *result changing* — where "result" includes each binary's
  content fingerprint (size + mtime), so an in-place rebuild at the same
  path counts as a new release — never the crash itself: a service
  crash-looping on an unchanged release never touches its healthy siblings.
- **Resolvers can be gated**: `requires = ["creds"]` on a resolver makes
  every service using its binaries inherit that gate — while it fails, they
  park in `blocked` (the resolver is not even run) instead of crash-looping
  into resolution failures, and revive when it passes.
- **Releases apply on respawn, never mid-flight**: the resolver moving does
  not, by itself, touch any running process; the new release lands when a
  service next crashes, is killed, or restarts (dragging its pinned
  siblings along per the rule above). Killing one service's pid is
  therefore a deploy.
- **Resolver failure = spawn failure** of the service that asked: the
  ordinary restart budget and backoff apply, the script's stderr lands in
  the service log and any instance-failure report. A release that's
  mid-publish looks like a briefly crashing service, not a wedged one.

`jetty-orc status <name>` shows each resolved binary and which resolver
produced it.

### Copying off unreliable mounts

If the resolved path lives somewhere that can disappear (a network mount, a
release dir that gets rotated), set `copy = true` on the resolver: binaries
are copied into `~/.jetty/bin/` (`$JETTY_ORC_BIN_ROOT` overrides) and
services run the copy.

```toml
[resolvers.release]
cmd = ["infra/latest-release.sh"]
provides = ["control_plane", "harness"]
copy = true
copy_keep_days = 7.0
```

- **Cached by source path**: the copy's name is the source's basename plus a
  hash of its path, and a `.src` sidecar records the source's size + mtime —
  an unchanged source is never re-copied; a changed one is re-copied
  atomically (tmp + rename, so a running process's binary is never written
  into).
- **Vanish-resilient**: if the source can't even be stat'd but a copy
  exists, the copy is used (with a warning) — a mount disappears precisely
  when the respawn needs the binary. No copy either → the usual
  resolver-failure path.
- **Cleanup**: every use touches the copy, and copies (plus sidecars, plus
  orphaned partial copies) unused for `copy_keep_days` (default 7) are
  deleted on the next resolution — binaries in active rotation never
  expire; last month's releases do.

`status` shows both the local path and the source it was copied from.

## Watching binaries (`watch`)

Resolvers cover releases: a *new path* is picked up at the next respawn. They
cannot see an **in-place rebuild** — same path, new bytes — because nothing
respawns and no resolver generation moves. `watch` covers that half, per
service:

```toml
[services.backend]
cmd = ["dist/backend", "--port", "{ports.backend}"]
watch = ["dist/backend"]          # any paths; {bin.<name>} and {state_dir} work
```

While the service runs, the orchestrator polls each watched path (1s cadence,
stat only). When one changes, the service is relaunched — **budget-free**: a
rebuild is nobody's crash, so it spends no `max_restarts`, triggers no
backoff, and cannot fail the instance. Everything else in the instance stays
up; readiness ordering applies only at instance start, so dependents are not
bounced.

Two guards keep a half-written binary from being launched:

- a change fires only once the file's signature (inode, size, mtime) is
  **identical on two consecutive polls** — a file still being written keeps
  moving;
- nothing fires while any watched path is **missing** — an `rm` followed by a
  rebuild reads as "build in progress", and the relaunch happens when the new
  file lands and settles.

So the worst-case latency from "build finished" to "new process" is about two
polls, and an atomic `mv` over the path (the inode changes; write-then-rename
build systems) is detected like any other change.

`watch` composes with resolvers: `watch = ["{bin.backend}"]` polls whatever
path the resolver last handed this incarnation, and the relaunch re-resolves —
so it follows a release *and* notices the current release being rebuilt in
place. Relative paths anchor to the config file's directory, confined to its
subtree, like every other config path.

Watching is per service and deliberately not a global mode: the service whose
binary you are iterating on gets `watch`; the database next to it does not.

## Distribution

The orchestrator package is **stdlib-only** (enforced by a test — a
dependency creeping in fails CI, not a deploy). That makes the deployment
story a copied directory:

```sh
scp -r src/jetty/orchestrator box:jetty_orc   # any Linux with Python 3.11+
python3 jetty_orc doctor                      # no pip, no venv, no installs
python3 jetty_orc up -c orc.toml
```

The directory can live anywhere and be named anything that is a valid
Python module name (`jetty_orc`, `orchestrator`, …). It is the whole tool —
`up`, `logs`, `ps`, gates, resolvers, containment included. Vendoring into
another build system (bazel) is the same move: copy it to any package path
(all intra-package imports are relative) and point a binary at `cli:main`.

## Containment

The guarantee sought: every process a service starts can be enumerated,
measured and killed — even after double-forks. Three levels, strongest first:

| Level | How | Escape hatch? |
|---|---|---|
| `cgroup` | Supervisor owns a delegated cgroup-v2 dir; one subgroup per service; children enter pre-exec (no race); teardown = `cgroup.kill`; stats = `memory.current`/`cpu.stat` | None — `setsid()` doesn't escape a cgroup |
| `scope` | No owned cgroup, but a systemd **user** manager is reachable: `up` re-execs itself under `systemd-run --user --scope -p Delegate=yes`, landing in the `cgroup` case | (becomes `cgroup`) |
| `pgroup` | Each service child is a session leader; enumeration walks `/proc` by session id; teardown = `killpg` + per-pid sweep | A descendant calling `setsid()` escapes |

`auto` picks the strongest available (warning on fallback); asking for a
level the host can't provide is an error. All levels set `PR_SET_PDEATHSIG`
on direct children, so even a SIGKILLed supervisor takes its immediate
children with it. The orchestrator never adopts a cgroup containing other
processes and never reclaims an occupied port — by construction it cannot
kill a process it didn't start.

Host prerequisites, best to acceptable (run `jetty-orc doctor` to see which
tier a box gets):

1. Run `jetty-orc up` inside a systemd unit/scope with `Delegate=yes` (no
   user session needed), or in a container that owns its cgroup namespace →
   `cgroup` directly.
2. A reachable systemd user manager (`systemd-run` on PATH, `XDG_RUNTIME_DIR`
   set; `loginctl enable-linger` to survive logout) → `scope`.
3. Neither → `pgroup`. Everything works; teardown is best-effort against
   deliberate daemonizers.

Beyond that: cgroup v2 mounted (any mainstream distro since ~2021), kernel
≥ 5.14 for `cgroup.kill` (older kernels degrade to per-pid kill), Python
≥ 3.11, pydantic v2. No root anywhere.

## Ports

Four forms: `"auto"` (kernel picks), `5173` (exact or refuse), `"5173+"`
(prefer 5173, scan upward to the first free port), `"9000-9020"` (bounded
scan; an exhausted range is an error). Probe sockets are held for the whole
batch, so one launch can't hand two names the same port — and two instances
sharing a config template with `"8000+"` naturally land on 8000 and 8001. A
fixed port that's occupied stops `up` with exit 1 — the orchestrator never
frees a port by killing its holder. The close-then-rebind race is accepted:
losing it looks like a service bind failure, which the restart policy
retries.

## Registry

Root: `--root` flag > `$JETTY_ORC_ROOT` > `$XDG_STATE_HOME/jetty-orc`
(default `~/.local/state/jetty-orc`). Layout:

```
<root>/registry/<name>.json           # atomic-rename writes; one per instance
<root>/instances/<name>/              # {state_dir}: databases, generated configs, ...
~/.jetty/logs/<name>-<timestamp>/     # {logs_dir}: <service>.log, one dir per run
```

`{state_dir}` is created (and re-pinned on every `up`) with mode `0700`, so
it is the natural home for coordination sockets: services on one instance
rendezvous over `"{state_dir}/<name>.sock"` and the directory mode is the
ACL — unlike a loopback TCP port, which any local user can connect to. Probe
such a service with `ready.uds`, which `connect()`s and therefore isn't
fooled by a stale socket file left behind by a crashed incarnation (a bare
`ready.path` is). Two things are the service's own job: unlink a pre-existing
socket path before binding, and keep paths short — `sun_path` caps unix
socket paths at ~107 bytes, which a deep `--root` plus a long instance name
can exceed (a `ready.uds` probe over the cap is a render-time error).

A record names its supervisor by pid **and** kernel start-ticks, so a
recycled pid can't impersonate a live instance; zombies count as dead.
`up` refuses an exact name whose record is still live (with the default
random suffix that only happens under `--name`). `status`/`logs`/`kill`
accept the base name when it matches exactly one instance (`logs sf-dev`
finds `sf-dev-a3f1`); an ambiguous prefix is refused with the candidates
listed. `ls` reads records plus live
cgroup//proc stats (CPU% is sampled over ~300 ms); `kill --force` performs a
kernel-level kill from outside (`cgroup.kill` on the instance root, or a
session sweep under pgroup) for wedged instances.

## Troubleshooting

- *Which containment did I get?* — first line of `up` output
  (`containment=…`), or `jetty-orc status <name>`, or `doctor` before launch.
- *Process tree* — `jetty-orc ps <name>`. Under cgroup containment the
  standard tools work on the scope too (its path is in `status`/`ps`
  output): `systemd-cgls <path>` for the kernel's own view of the tree,
  `systemd-cgtop` for live per-cgroup usage. `ps` exists rather than
  delegating to them because it also works under pgroup containment, adds
  per-pid RSS and the service attribution, and needs no systemd on the box.
- *A service crash-loops at startup* — read its log under
  `~/.jetty/logs/<instance>-<timestamp>/` (`jetty-orc status` prints the run's
  directory); the spawn header shows the exact rendered argv. `check` catches
  template and reference mistakes without spawning.
- *Instance failed; what happened?* — the `up` stderr ends with the
  aggregated reason and log tail, and `ls` keeps showing the failed record
  until you `kill <name>` it.
- *`up` says the port is in use* — something else holds a fixed port; the
  orchestrator won't reclaim it. Free it yourself or switch to `"auto"`.
- *Blocked forever on a gate* — run the gate's `check` argv by hand; any
  nonzero exit (or a hang past `timeout_seconds`) is "unsatisfied".
