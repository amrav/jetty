"""One supervised service: spawn → readiness → run → classify the exit.

The exit classification is the heart of the restart policy:

  - exit code in `no_restart_exit`  → fail the instance (retrying a config
    bug just burns CPU — jetty core's own exit-2 contract);
  - a required gate is failing      → park in `blocked`; the crash is the
    environment's fault, so it does NOT spend restart budget, and the service
    revives when the gate passes;
  - otherwise                       → restart with exponential backoff, up to
    `max_restarts` within `window_seconds`; past that, fail the instance with
    the log tail attached.

After every exit the service's whole group is swept with SIGKILL before a
restart, so a lingering grandchild can never squat on the port its successor
needs.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from .config import ServiceConfig
from .containment import Containment
from .gates import GateSet
from .render import RenderedService

_TAIL_BYTES = 8192
_PROBE_ATTEMPT_TIMEOUT = 2.0


class Service:
    def __init__(
        self,
        name: str,
        cfg: ServiceConfig,
        rendered: RenderedService,
        containment: Containment,
        gates: GateSet,
        log_path: Path,
        dep_events: list[asyncio.Event],
        extra_env: dict[str, str],
        notify: Callable[["Service"], None],
        fail: Callable[[str, "Service"], None],
    ):
        self.name = name
        self.log_path = log_path
        self.state = "pending"
        self.pid: int | None = None
        self.last_exit: int | None = None
        self.restarts = 0
        self.blocked_on: list[str] = []
        self.ready_event = asyncio.Event()

        self._cfg = cfg
        self._rendered = rendered
        self._containment = containment
        self._gates = gates
        self._dep_events = dep_events
        self._extra_env = extra_env
        self._notify = notify
        self._fail_cb = fail

        self._stop_event = asyncio.Event()
        self._fail_times: list[float] = []
        self._tail = bytearray()
        self._logf = None
        self._reader_task: asyncio.Task | None = None
        self.proc: asyncio.subprocess.Process | None = None

    # -- public --

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    def status(self) -> dict:
        return {
            "state": self.state,
            "pid": self.pid,
            "restarts": self.restarts,
            "last_exit": self.last_exit,
            "blocked_on": self.blocked_on,
        }

    async def run(self) -> None:
        for ev in self._dep_events:
            await self._race(ev.wait())
            if self.stopping:
                self._set_state("stopped")
                return
        restart_cfg = self._cfg.restart
        while not self.stopping:
            if self._cfg.requires:
                ok, failing = await self._gates.satisfied(self._cfg.requires)
                if not ok:
                    await self._block(failing)
                    continue
            code = await self._run_once()
            if self.stopping:
                break
            self.last_exit = code

            if code in restart_cfg.no_restart_exit:
                self._fail(
                    f"service '{self.name}' exited with code {code}, which "
                    "restart.no_restart_exit marks as not worth retrying"
                )
                return
            if self._cfg.requires:
                ok, failing = await self._gates.satisfied(
                    self._cfg.requires, refresh=True
                )
                if not ok:
                    self._log_note(
                        f"exited {code} while gate(s) {', '.join(failing)} are "
                        "failing; blocking without spending restart budget"
                    )
                    continue

            now = time.monotonic()
            self._fail_times = [
                t for t in self._fail_times if now - t < restart_cfg.window_seconds
            ] + [now]
            self.restarts += 1
            if len(self._fail_times) > restart_cfg.max_restarts:
                span = now - self._fail_times[0]
                self._fail(
                    f"service '{self.name}' exited {len(self._fail_times)} times "
                    f"in {span:.1f}s (limit: {restart_cfg.max_restarts} restarts "
                    f"per {restart_cfg.window_seconds:.0f}s); last exit code {code}"
                )
                return
            delay = min(
                restart_cfg.backoff_initial_seconds * 2 ** (len(self._fail_times) - 1),
                restart_cfg.backoff_max_seconds,
            )
            self._log_note(f"exited {code}; restarting in {delay:.1f}s")
            self._set_state("backoff")
            await self._race(asyncio.sleep(delay))
        self._set_state("stopped")

    async def stop(self) -> None:
        """Graceful signal to the whole group, grace period, then SIGKILL."""
        self._stop_event.set()
        if self.proc is not None and self.proc.returncode is None:
            self._set_state("stopping")
            sig = getattr(signal, "SIG" + self._cfg.stop.signal)
            self._containment.sweep(self.name, sig)
            try:
                await asyncio.wait_for(self.proc.wait(), self._cfg.stop.grace_seconds)
            except TimeoutError:
                self._log_note(
                    f"no exit {self._cfg.stop.grace_seconds}s after "
                    f"SIG{self._cfg.stop.signal}; escalating to SIGKILL"
                )
        self._containment.sweep(self.name, signal.SIGKILL)

    # -- internals --

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._notify(self)

    async def _race(self, coro) -> None:
        """Await `coro`, abandoning it if stop is requested first."""
        main = asyncio.ensure_future(coro)
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({main, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (main, stop):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def _run_once(self) -> int:
        """One incarnation: spawn, probe, wait; returns the exit code (127 if
        the spawn itself failed)."""
        err = await self._spawn()
        if err is not None:
            return 127
        assert self.proc is not None
        if await self._wait_ready():
            self._set_state("running")
            self.ready_event.set()
        code = await self.proc.wait()
        if self._reader_task is not None:
            await self._reader_task
            self._reader_task = None
        if self._logf is not None:
            self._logf.close()
            self._logf = None
        self.ready_event.clear()
        self.pid = None
        # Sweep survivors of this incarnation before any restart.
        self._containment.sweep(self.name, signal.SIGKILL)
        return code

    async def _spawn(self) -> str | None:
        self._set_state("starting")
        self._logf = open(self.log_path, "ab", buffering=0)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._append_output(
            f"=== jetty-orc spawn {stamp} :: {' '.join(self._rendered.cmd)}\n".encode()
        )
        env = {
            **os.environ,
            **self._extra_env,
            **self._rendered.env,
            "JETTY_ORC_SERVICE": self.name,
        }
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self._rendered.cmd,
                cwd=self._rendered.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=self._containment.preexec(self.name),
            )
        except Exception as e:  # noqa: BLE001 - a spawn failure is a service failure
            msg = f"spawn failed: {e}"
            self._log_note(msg)
            self._logf.close()
            self._logf = None
            return msg
        self.pid = self.proc.pid
        self._containment.register(self.name, self.proc.pid)
        self._reader_task = asyncio.create_task(self._pump(self.proc.stdout))
        self._notify(self)
        return None

    async def _pump(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            self._append_output(chunk)

    def _append_output(self, chunk: bytes) -> None:
        if self._logf is not None:
            try:
                self._logf.write(chunk)
            except ValueError:
                pass  # closed under us during teardown
        self._tail.extend(chunk)
        del self._tail[:-_TAIL_BYTES]

    def _log_note(self, msg: str) -> None:
        data = f"jetty-orc: {msg}\n".encode()
        if self._logf is None:
            # Notes between incarnations (exit classification, backoff,
            # gate transitions) still belong in the log file.
            try:
                with open(self.log_path, "ab") as f:
                    f.write(data)
            except OSError:
                pass
            self._tail.extend(data)
            del self._tail[:-_TAIL_BYTES]
            return
        self._append_output(data)

    async def _wait_ready(self) -> bool:
        r = self._rendered
        if r.ready_http is None and r.ready_tcp is None and r.ready_path is None:
            return True
        assert self.proc is not None
        ready_cfg = self._cfg.ready
        deadline = time.monotonic() + ready_cfg.timeout_seconds
        while time.monotonic() < deadline and not self.stopping:
            if self.proc.returncode is not None:
                return False
            if await self._probe_once():
                return True
            await self._race(asyncio.sleep(ready_cfg.interval_seconds))
        if self.stopping:
            return False
        self._log_note(
            f"readiness probe did not pass within {ready_cfg.timeout_seconds}s; killing"
        )
        self._containment.sweep(self.name, signal.SIGKILL)
        return False

    async def _probe_once(self) -> bool:
        r = self._rendered
        try:
            if r.ready_path is not None:
                return os.path.exists(r.ready_path)
            if r.ready_tcp is not None:
                host, _, port = r.ready_tcp.rpartition(":")
                return await self._connect_ok(host.strip("[]"), int(port), None)
            assert r.ready_http is not None
            u = urllib.parse.urlsplit(r.ready_http)
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            return await self._connect_ok(u.hostname, u.port or 80, path)
        except (OSError, ValueError):
            return False

    @staticmethod
    async def _connect_ok(host: str, port: int, http_path: str | None) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), _PROBE_ATTEMPT_TIMEOUT
            )
        except (OSError, TimeoutError):
            return False
        try:
            if http_path is None:
                return True
            writer.write(
                f"GET {http_path} HTTP/1.1\r\nHost: {host}\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), _PROBE_ATTEMPT_TIMEOUT)
            parts = line.split()
            return len(parts) >= 2 and 200 <= int(parts[1]) < 400
        except (OSError, TimeoutError, ValueError):
            return False
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _block(self, failing: list[str]) -> None:
        """Park until every required gate passes (or stop). Entering blocked
        forgives past crashes: they were the environment's fault."""
        self.blocked_on = failing
        self._fail_times.clear()
        self._set_state("blocked")
        self._notify(self)
        while not self.stopping:
            await self._race(
                asyncio.sleep(self._gates.min_recheck(self._cfg.requires))
            )
            if self.stopping:
                return
            ok, failing = await self._gates.satisfied(self._cfg.requires)
            if ok:
                self.blocked_on = []
                self._log_note("gates satisfied again; restarting")
                return
            if failing != self.blocked_on:
                self.blocked_on = failing
                self._notify(self)

    def _fail(self, headline: str) -> None:
        self._set_state("failed")
        tail = self._tail.decode(errors="replace").strip()
        tail = "\n".join(tail.splitlines()[-30:])
        detail = headline
        if tail:
            detail += (
                f"\n--- last output of '{self.name}' ({self.log_path}) ---\n"
                f"{tail}\n--- end ---"
            )
        else:
            detail += f" (no output captured; log: {self.log_path})"
        self._fail_cb(detail, self)
