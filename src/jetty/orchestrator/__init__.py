"""Process orchestration for sidecar deployments (Linux only).

Launches a named *instance* — a group of services described in TOML — with:

  - kernel-backed containment (cgroup v2 where available, process groups as a
    fallback), so killing the instance kills the whole tree, including
    double-forked grandchildren;
  - a port broker that allocates free ports at startup and injects them into
    service commands, environments and readiness probes via `{ports.<name>}`
    placeholders;
  - per-service restart policy (bounded restarts with backoff; budget
    exhaustion tears the instance down with an aggregated error);
  - credential/condition *gates*: a service that dies while a gate is failing
    parks in `blocked` instead of burning its restart budget, and is revived
    when the gate passes again;
  - a registry of running instances for `jetty-orc ls` / `status` / `kill`.

This package is standalone by design: it imports nothing from jetty core and
depends only on the stdlib and pydantic, so it can be vendored into another
build system (e.g. bazel) at any module path — all intra-package imports are
relative.
"""

from .config import OrchestratorConfig
from .supervisor import Supervisor

__all__ = ["OrchestratorConfig", "Supervisor"]
