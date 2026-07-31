"""The `hg` module — a read-only window onto one local Mercurial repository.

Spec: spec/hg-v1.md. The whole module is a thin, careful shell around the
`hg` binary:

- **Read-only by construction.** The only commands ever invoked are `root`,
  `log`, `status`, `diff`, `cat` and `branch`. There is no code path that
  could run a mutating command, so the guarantee does not rest on review of
  handler logic.
- **The user's Mercurial config is not part of the contract.** Every
  invocation runs with `HGPLAIN=1` and `HGRCPATH=` (empty), so aliases,
  extensions and hooks in a user's ~/.hgrc can neither change output shapes
  nor execute code on behalf of a request.
- **Requests cannot smuggle revset syntax.** Revisions are validated against
  a charset that excludes quotes, parentheses, whitespace and operators
  before they go anywhere near a revset (`_check_rev`); paths are always
  passed after `--` with an explicit `path:` (literal) pattern prefix.

Failure semantics follow SPEC.md §1.2: a repo that cannot be opened aborts
boot in `startup()` rather than serving errors forever, and an `hg` failure
at request time maps onto the closed error-code set (`_failure`), never onto
a raw stderr dump.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
from datetime import datetime, timedelta, timezone
from re import compile as _re
from typing import Any, Mapping

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict

from jetty.errors import ErrorCode, JettyError
from jetty.modules.base import Module


class HgSettings(BaseModel):
    """`[modules.hg]` — a typo'd key must fail boot, same as core config."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    #: Absolute path of the repository to serve. Required.
    repo: str
    #: The Mercurial executable. Overridable for hermetic test/CI installs.
    hg_bin: str = "hg"


_NULL_NODE = "0" * 40
_TIMEOUT_S = 10.0

# Deliberately narrower than what Mercurial allows in a name: everything
# excluded here (quotes, backslash, parens, whitespace, `:` `~` `^` `!` `+`)
# is revset or range syntax, and a revision that needs it has no business
# arriving over this API. Covers hashes, revnums, `tip`, `.`, and ordinary
# branch/bookmark names.
_REV_OK = _re(r"^[A-Za-z0-9._/-]{1,120}$")


def _check_rev(rev: str) -> str:
    if not _REV_OK.match(rev):
        raise JettyError(ErrorCode.INVALID_REQUEST, f"invalid revision {rev!r}")
    return rev


def _check_path(path: str) -> str:
    """Repo-relative, no traversal. `hg` would refuse `..` too (`path:` never
    escapes the root), but a malformed path is the client's bug and should
    say `invalid_request`, not surface as a confusing not_found."""
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(seg in ("", ".", "..") for seg in path.split("/"))
    ):
        raise JettyError(ErrorCode.INVALID_REQUEST, f"invalid repository path {path!r}")
    return path


def _iso(date: Any) -> str:
    """Mercurial's `[unixtime, offset]` (seconds west of UTC) → ISO 8601 in
    the author's own zone, which is what hg itself displays."""
    ts, offset = date
    tz = timezone(timedelta(seconds=-int(offset)))
    return datetime.fromtimestamp(ts, tz).isoformat()


def _changeset(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "node": entry["node"],
        # Local revnum: convenient for display, NOT stable across clones.
        "rev": entry["rev"],
        "branch": entry["branch"],
        "parents": [p for p in entry.get("parents", ()) if p != _NULL_NODE],
        "user": entry["user"],
        "date": _iso(entry["date"]),
        "desc": entry["desc"],
        "phase": entry.get("phase", "public"),
    }


def _status_files(raw: bytes) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for entry in json.loads(raw):
        item: dict[str, Any] = {"path": entry["path"], "status": entry["status"]}
        # `-C`: an add that is really a copy/rename names where it came from.
        if entry.get("source"):
            item["copy_source"] = entry["source"]
        files.append(item)
    return files


def _failure(stderr: bytes) -> JettyError:
    """One hg failure → one code from the closed set (SPEC.md §3.1)."""
    message = stderr.decode(errors="replace").strip() or "hg failed"
    line = message.splitlines()[0]
    lowered = line.lower()
    if "unknown revision" in lowered or "no such file in rev" in lowered:
        return JettyError(ErrorCode.NOT_FOUND, line)
    if "ambiguous" in lowered:
        return JettyError(ErrorCode.INVALID_REQUEST, line)
    return JettyError(ErrorCode.UPSTREAM_ERROR, line)


class HgModule(Module):
    name = "hg"
    api_version = "v1"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.cfg = HgSettings.model_validate(dict(settings))
        self._root: str | None = None

    async def startup(self) -> None:
        """Open the repo once; an unreadable repo must abort boot, not serve
        503s forever (SPEC.md §1.2)."""
        try:
            self._root = self._run("root").decode().strip()
        except JettyError as e:
            raise RuntimeError(
                f"hg module: cannot open repository {self.cfg.repo!r}: {e.message}"
            ) from e

    def _run(self, *args: str) -> bytes:
        env = dict(os.environ, HGPLAIN="1", HGRCPATH="")
        try:
            proc = subprocess.run(
                [self.cfg.hg_bin, "-R", self.cfg.repo, "--noninteractive", *args],
                capture_output=True,
                timeout=_TIMEOUT_S,
                env=env,
            )
        except FileNotFoundError:
            raise JettyError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"hg executable {self.cfg.hg_bin!r} not found",
            ) from None
        except subprocess.TimeoutExpired:
            raise JettyError(
                ErrorCode.UPSTREAM_UNAVAILABLE, f"hg timed out after {_TIMEOUT_S:g}s"
            ) from None
        if proc.returncode != 0:
            raise _failure(proc.stderr)
        return proc.stdout

    def _log_json(self, *args: str) -> list[dict[str, Any]]:
        entries = json.loads(self._run("log", "-T", "json", *args))
        # An empty repo materialises the null changeset for revsets like
        # `ancestors(tip)`; it is not a changeset a client should ever see.
        return [e for e in entries if e["node"] != _NULL_NODE]

    # Handlers are deliberately sync (`def`): FastAPI runs them in its
    # threadpool, so a slow `hg` never parks the event loop.
    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/repo")
        def repo() -> dict[str, Any]:
            tip = self._log_json("-l", "1")
            parents = self._log_json("-r", "parents()")
            branch = self._run("branch").decode().strip()
            changed = json.loads(self._run("status", "-T", "json"))
            return {
                "root": self._root or self.cfg.repo,
                "tip": tip[0]["node"] if tip else None,
                "wdir_parents": [e["node"] for e in parents],
                "branch": branch,
                # hg's own definition (`hg id` `+`): untracked files do not
                # make a working directory dirty.
                "dirty": any(e["status"] in "MAR!" for e in changed),
            }

        @router.get("/changesets")
        def changesets(
            start: str = "tip",
            path: str | None = None,
            user: str | None = None,
            limit: int = Query(default=50, ge=1, le=200),
        ) -> dict[str, Any]:
            _check_rev(start)
            args = [
                "-r",
                f"reverse(ancestors('{start}'))",
                "-l",
                str(limit + 1),
            ]
            if user is not None:
                args += ["-u", user]
            if path is not None:
                args += ["--", "path:" + _check_path(path)]
            entries = self._log_json(*args)
            page = entries[:limit]
            return {
                "changesets": [_changeset(e) for e in page],
                # Cursor: pass back as `start` for the next page. Node-based,
                # so a commit landing between requests cannot shift the page.
                "next": entries[limit]["node"] if len(entries) > limit else None,
            }

        @router.get("/changesets/{rev}")
        def changeset(rev: str) -> dict[str, Any]:
            entries = self._log_json("-r", _check_rev(rev))
            if not entries:
                raise JettyError(ErrorCode.NOT_FOUND, f"unknown revision {rev!r}")
            result = _changeset(entries[0])
            result["files"] = _status_files(
                self._run("status", "--change", result["node"], "-C", "-T", "json")
            )
            return result

        @router.get("/changesets/{rev}/diff")
        def changeset_diff(rev: str, path: str | None = None) -> Response:
            args = ["diff", "-c", _check_rev(rev), "--git"]
            if path is not None:
                args += ["--", "path:" + _check_path(path)]
            return Response(content=self._run(*args), media_type="text/x-diff")

        @router.get("/status")
        def status(
            from_: str | None = Query(default=None, alias="from"),
            to: str | None = None,
        ) -> dict[str, Any]:
            """File states between two revisions, or against the working
            directory. Defaults answer the 90% question: what is uncommitted?
            """
            args = ["status", "-C", "-T", "json"]
            wdir = to is None or to == "wdir"
            if from_ is not None:
                args += ["--rev", _check_rev(from_)]
                if not wdir:
                    args += ["--rev", _check_rev(to)]
            elif not wdir:
                # No `from`: default to the target's parent, i.e. "what did
                # this revision change" — same answer as /changesets/{rev}.
                args += ["--change", _check_rev(to)]
            return {"files": _status_files(self._run(*args))}

        @router.get("/files/{rev}/{file_path:path}")
        def file_content(rev: str, file_path: str) -> Response:
            content = self._run(
                "cat", "-r", _check_rev(rev), "--", "path:" + _check_path(file_path)
            )
            media = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            return Response(content=content, media_type=media)

        return router
