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
jetty-orc kill <name> [--force]               # stop it from outside
```

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
name = "dev"                # [a-z0-9][a-z0-9_-]{0,63}; namespaces everything
containment = "auto"        # auto | cgroup | scope | pgroup   (see Containment)

[ports]
api = "auto"                # kernel-picked free port
web = 5173                  # exactly 5173: refused if occupied
ui  = "5173+"               # prefer 5173; occupied -> 5174, 5175, ...
db  = "9000-9020"           # same, bounded: error if the whole range is taken

[gates.creds]
check = ["auth-tool", "status"]   # argv; exit 0 = satisfied
recheck_seconds = 15.0            # cache + poll cadence while blocked
timeout_seconds = 20.0            # unrunnable/overdue check = unsatisfied

[services.api]
cmd = ["python", "-m", "uvicorn", "app:app", "--port", "{ports.api}"]
cwd = "/path/to/server"
env = { DB = "{state_dir}/app.sqlite" }   # overlaid on the inherited env
after = ["backend"]         # start only once these are READY (probe passed)
requires = ["creds"]        # gates this service depends on

[services.api.ready]        # at most one probe; none = ready once spawned
http = "http://127.0.0.1:{ports.api}/healthz"   # GET, any status < 400
# tcp  = "127.0.0.1:{ports.api}"                # connect succeeds
# path = "{state_dir}/api.sock"                 # file exists (UDS listeners)
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

### Placeholders

`{ports.<name>}`, `{instance.name}`, `{state_dir}`, `{logs_dir}` render into
`cmd`, `env` values, `cwd`, readiness probes and gate argvs after ports are
allocated. `{{` / `}}` escape literal braces (`str.format` parsing rules).

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
travels with failure reports.

## Dynamic binaries (resolvers)

When "which binary" changes between releases, don't hardcode paths — declare
a resolver: a script whose stdout names the current path(s), run by the
supervisor before a spawn that needs it.

```toml
[resolvers.release]
cmd = ["infra/latest-release.sh"]        # runs in the supervisor's cwd
provides = ["control_plane", "harness"]  # names bound by ONE invocation
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
  resolution *result changing*, not the crash itself: a service
  crash-looping on an unchanged release never touches its healthy siblings.
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

A record names its supervisor by pid **and** kernel start-ticks, so a
recycled pid can't impersonate a live instance; zombies count as dead.
`up` refuses a name whose record is still live. `ls` reads records plus live
cgroup//proc stats (CPU% is sampled over ~300 ms); `kill --force` performs a
kernel-level kill from outside (`cgroup.kill` on the instance root, or a
session sweep under pgroup) for wedged instances.

## Troubleshooting

- *Which containment did I get?* — first line of `up` output
  (`containment=…`), or `jetty-orc status <name>`, or `doctor` before launch.
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
