"""
Publish the HTML report to a git branch that GitHub Pages serves.

Why a separate branch: the report is a build artifact, not source. Keeping it
on `gh-pages` means the code branch stays clean, and GitHub serves the page at
a permanent URL that you can bookmark:

    https://<user>.github.io/<repo>/

How it works: the branch is built with git plumbing commands (hash-object,
write-tree, commit-tree) using a TEMPORARY index file, so your working tree
and your normal `git status` are never touched. You can run `publish` with
uncommitted changes in progress and nothing moves.

What lands on the branch:
    index.html      the newest report (what the Pages URL shows)
    run-<id>.html   one archived copy per run, so old reports stay reachable
    .nojekyll       tells GitHub Pages to serve the files as-is
Previous files are preserved: each publish reads the existing branch tree
first, then adds to it.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from typing import Optional

log = logging.getLogger(__name__)

NOJEKYLL = ".nojekyll"


class PublishError(RuntimeError):
    """Raised when git is unavailable, the repo has no remote, or a push fails."""


def _git(*args: str, env: Optional[dict] = None, check: bool = True) -> str:
    """Run a git command and return stdout, stripped."""
    proc = subprocess.run(["git", *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
    if check and proc.returncode != 0:
        raise PublishError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _remote_slug(remote: str = "origin") -> Optional[tuple[str, str]]:
    """(owner, repo) parsed from the remote URL, or None if it is not GitHub."""
    try:
        url = _git("remote", "get-url", remote)
    except PublishError:
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/([^/.]+)", url)
    return (m.group(1), m.group(2)) if m else None


def pages_url(branch: str, remote: str = "origin") -> Optional[str]:
    slug = _remote_slug(remote)
    return f"https://{slug[0]}.github.io/{slug[1]}/" if slug else None


def settings_url(remote: str = "origin") -> Optional[str]:
    slug = _remote_slug(remote)
    return f"https://github.com/{slug[0]}/{slug[1]}/settings/pages" if slug else None


def publish_report(html_path: str, run_id: int, branch: str = "gh-pages",
                   remote: str = "origin", push: bool = True) -> dict:
    """
    Copy `html_path` onto `branch` as index.html (plus an archived run-<id>.html)
    and push. Returns a summary dict for the CLI to print.
    """
    if not os.path.isfile(html_path):
        raise PublishError(f"no report at {html_path} - run the pipeline first")
    _git("rev-parse", "--git-dir")            # fails loudly if we are not in a repo

    with open(html_path, "rb") as fh:
        html = fh.read()

    with tempfile.TemporaryDirectory() as tmp:
        index_file = os.path.join(tmp, "index")
        env = {"GIT_INDEX_FILE": index_file}

        # Start from the existing branch so earlier run-<id>.html files survive.
        parent = _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        if not parent:
            # Branch may exist only on the remote (e.g. a fresh clone).
            _git("fetch", remote, f"{branch}:{branch}", check=False)
            parent = _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        if parent:
            _git("read-tree", parent, env=env)
        else:
            _git("read-tree", "--empty", env=env)

        # Write the report as a blob and stage it under both names.
        blob_src = os.path.join(tmp, "report.html")
        with open(blob_src, "wb") as fh:
            fh.write(html)
        blob = _git("hash-object", "-w", blob_src)
        for name in ("index.html", f"run-{run_id:04d}.html"):
            _git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env=env)

        marker = os.path.join(tmp, "nojekyll")
        open(marker, "w").close()
        _git("update-index", "--add", "--cacheinfo",
             f"100644,{_git('hash-object', '-w', marker)},{NOJEKYLL}", env=env)

        tree = _git("write-tree", env=env)
        message = f"Publish CarDealFindr report for run {run_id}"
        args = ["commit-tree", tree, "-m", message]
        if parent:
            args += ["-p", parent]
        commit = _git(*args)
        _git("update-ref", f"refs/heads/{branch}", commit)

    result = {"branch": branch, "commit": commit[:8], "pushed": False,
              "url": pages_url(branch, remote), "settings": settings_url(remote)}
    if push:
        delay = 2
        for attempt in range(1, 6):
            proc = subprocess.run(["git", "push", remote, f"{branch}:{branch}"],
                                  capture_output=True, text=True)
            if proc.returncode == 0:
                result["pushed"] = True
                break
            log.warning("push attempt %d failed: %s", attempt, proc.stderr.strip()[:200])
            if attempt < 5:
                import time
                time.sleep(delay)
                delay *= 2
        else:
            raise PublishError(f"could not push {branch} after 5 attempts; "
                               f"the commit is local, retry with: git push {remote} {branch}")
    return result
