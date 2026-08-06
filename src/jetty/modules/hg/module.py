"""The `hg` module — a read-only window onto Mercurial repositories under
one configured root.

Every request names its repository (`?repo=<relative path under root>`);
`_resolve` is the only door, and it containment-checks the resolved path
before any hg invocation. The root is the module's whole authority: nothing
outside it is reachable regardless of what a request asks for.

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
    #: Directory whose subdirectories are the servable repositories. Every
    #: request names one with `?repo=<relative path>`; nothing outside this
    #: root is ever reachable (spec/hg-v1.md §3). Required.
    root: str
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
        # Extension-specific metadata: evolve markers, etc.
        # A standard Mercurial concept; consumers pick the keys they need.
        "extras": entry.get("extras", {}),
        # Display name from whatever review system the repository is
        # attached to, already formatted for display. Clients print it
        # verbatim and fall back to the short node when null. The
        # open-source implementation has no review system; a
        # deployment-specific implementation may populate this.
        "label": None,
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

    async def startup(self) -> None:
        """Prove the root and the binary once; a misconfiguration must abort
        boot, not serve 503s forever (SPEC.md §1.2). Individual repos are
        checked per request — they come and go while the sidecar runs."""
        if not os.path.isdir(self.cfg.root):
            raise RuntimeError(
                f"hg module: root {self.cfg.root!r} is not a directory"
            )
        try:
            subprocess.run(
                [self.cfg.hg_bin, "--version"], check=True, timeout=_TIMEOUT_S,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=dict(os.environ, HGPLAIN="1", HGRCPATH=""),
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(
                f"hg module: cannot run {self.cfg.hg_bin!r}: {e}"
            ) from e

    def _resolve(self, repo: str) -> str:
        """`?repo=` → an absolute repository path, or a refusal.

        The param is a relative path under the configured root — same
        segment rules as file paths (no absolute, no `..`, no empties) —
        and the RESOLVED path must still sit under the resolved root, so a
        symlink inside the root cannot become a door out of it. Unknown or
        non-repository directories are `not_found`; a path that tries to
        leave the root is the client's bug, `invalid_request`.
        """
        if (
            not repo
            or repo.startswith("/")
            or "\\" in repo
            or any(seg in ("", ".", "..") for seg in repo.split("/"))
        ):
            raise JettyError(
                ErrorCode.INVALID_REQUEST, f"invalid repository name {repo!r}"
            )
        root = os.path.realpath(self.cfg.root)
        path = os.path.realpath(os.path.join(root, repo))
        if path != root and not path.startswith(root + os.sep):
            raise JettyError(
                ErrorCode.INVALID_REQUEST,
                f"repository {repo!r} resolves outside the configured root",
            )
        if not os.path.isdir(os.path.join(path, ".hg")):
            raise JettyError(
                ErrorCode.NOT_FOUND, f"unknown repository {repo!r}"
            )
        return path

    def _run(self, repo_path: str, *args: str) -> bytes:
        env = dict(os.environ, HGPLAIN="1", HGRCPATH="")
        try:
            proc = subprocess.run(
                [self.cfg.hg_bin, "-R", repo_path, "--noninteractive", *args],
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

    # The built-in ``-T json`` template does NOT include extras.  This custom
    # template outputs one JSON object per line with all the fields we need,
    # including the extras dict that carries extension metadata.
    # Uses p1node/p2node instead of parents because the dict() template
    # keyword does not properly serialize the parents list.
    _LOG_TEMPLATE = (
        "{dict(node, rev, branch, p1node, p2node, user, date, desc, phase,"
        " bookmarks, tags, extras)|json}\\n"
    )

    def _log_json(self, repo_path: str, *args: str) -> list[dict[str, Any]]:
        raw = self._run(repo_path, "log", "-T", self._LOG_TEMPLATE, *args)
        entries = [
            json.loads(line)
            for line in raw.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        # Reconstruct parents from p1node/p2node (matching -T json format)
        # and filter out the null node.
        for e in entries:
            parents = []
            for key in ("p1node", "p2node"):
                node = e.pop(key, _NULL_NODE)
                if node != _NULL_NODE:
                    parents.append(node)
            e["parents"] = parents
        # An empty repo materialises the null changeset for revsets like
        # `ancestors(tip)`; it is not a changeset a client should ever see.
        return [e for e in entries if e["node"] != _NULL_NODE]

    # Handlers are deliberately sync (`def`): FastAPI runs them in its
    # threadpool, so a slow `hg` never parks the event loop. Every handler
    # takes `repo` — which repository under the configured root — and
    # resolves it first, so no hg invocation can precede the containment
    # check.
    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/repo")
        def repo_summary(repo: str) -> dict[str, Any]:
            target = self._resolve(repo)
            tip = self._log_json(target, "-l", "1")
            parents = self._log_json(target, "-r", "parents()")
            branch = self._run(target, "branch").decode().strip()
            changed = json.loads(self._run(target, "status", "-T", "json"))
            return {
                "root": target,
                "tip": tip[0]["node"] if tip else None,
                "wdir_parents": [e["node"] for e in parents],
                "branch": branch,
                # hg's own definition (`hg id` `+`): untracked files do not
                # make a working directory dirty.
                "dirty": any(e["status"] in "MAR!" for e in changed),
            }

        @router.get("/changesets")
        def changesets(
            repo: str,
            start: str = "tip",
            path: str | None = None,
            user: str | None = None,
            limit: int = Query(default=50, ge=1, le=200),
        ) -> dict[str, Any]:
            target = self._resolve(repo)
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
            entries = self._log_json(target, *args)
            page = entries[:limit]
            return {
                "changesets": [_changeset(e) for e in page],
                # Cursor: pass back as `start` for the next page. Node-based,
                # so a commit landing between requests cannot shift the page.
                "next": entries[limit]["node"] if len(entries) > limit else None,
            }

        @router.get("/changesets/{rev}")
        def changeset(rev: str, repo: str) -> dict[str, Any]:
            target = self._resolve(repo)
            entries = self._log_json(target, "-r", _check_rev(rev))
            if not entries:
                raise JettyError(ErrorCode.NOT_FOUND, f"unknown revision {rev!r}")
            result = _changeset(entries[0])
            result["files"] = _status_files(self._run(
                target, "status", "--change", result["node"], "-C", "-T", "json"
            ))
            return result

        @router.get("/changesets/{rev}/diff")
        def changeset_diff(
            rev: str, repo: str, path: str | None = None
        ) -> Response:
            target = self._resolve(repo)
            args = ["diff", "-c", _check_rev(rev), "--git"]
            if path is not None:
                args += ["--", "path:" + _check_path(path)]
            return Response(
                content=self._run(target, *args), media_type="text/x-diff"
            )

        @router.get("/diff")
        def wdir_diff(repo: str, path: str | None = None) -> Response:
            """Uncommitted changes as a unified diff: working directory vs
            its parent. The counterpart of ``changeset_diff`` — same output
            format, but for work that has not been committed yet.

            Optional ``path`` narrows to one file (same rules as §2)."""
            target = self._resolve(repo)
            args = ["diff", "--git"]
            if path is not None:
                args += ["--", "path:" + _check_path(path)]
            return Response(
                content=self._run(target, *args), media_type="text/x-diff"
            )

        @router.get("/status")
        def status(
            repo: str,
            from_: str | None = Query(default=None, alias="from"),
            to: str | None = None,
        ) -> dict[str, Any]:
            """File states between two revisions, or against the working
            directory. Defaults answer the 90% question: what is uncommitted?
            """
            target = self._resolve(repo)
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
            return {"files": _status_files(self._run(target, *args))}

        @router.get("/files/{rev}/{file_path:path}")
        def file_content(rev: str, file_path: str, repo: str) -> Response:
            target = self._resolve(repo)
            content = self._run(
                target,
                "cat", "-r", _check_rev(rev), "--", "path:" + _check_path(file_path),
            )
            media = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            return Response(content=content, media_type=media)

        return router
