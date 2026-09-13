"""Publishing builds a Pages branch without disturbing the working tree."""
import os
import subprocess

import pytest

from cardealfindr import publish


def git(*args, cwd):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway git repo with one commit and a dirty working tree."""
    d = tmp_path / "repo"
    d.mkdir()
    git("init", "-b", "main", cwd=d)
    git("config", "user.email", "t@example.com", cwd=d)
    git("config", "user.name", "Test", cwd=d)
    git("remote", "add", "origin", "https://github.com/someone/SomeRepo.git", cwd=d)
    (d / "code.py").write_text("print(1)\n")
    git("add", "code.py", cwd=d)
    git("commit", "-m", "initial", cwd=d)
    (d / "code.py").write_text("print(2)\n")          # uncommitted change, must survive
    (d / "report.html").write_text("<html>run one</html>")
    monkeypatch.chdir(d)
    return d


def test_publish_builds_branch_and_leaves_worktree_alone(repo):
    res = publish.publish_report("report.html", run_id=1, push=False)
    assert res["pushed"] is False and res["branch"] == "gh-pages"
    assert res["url"] == "https://someone.github.io/SomeRepo/"
    assert res["settings"] == "https://github.com/someone/SomeRepo/settings/pages"

    files = set(git("ls-tree", "-r", "--name-only", "gh-pages", cwd=repo).splitlines())
    assert files == {"index.html", "run-0001.html", ".nojekyll"}
    assert git("show", "gh-pages:index.html", cwd=repo) == "<html>run one</html>"

    # the branch we are on, the checked-out files and the dirty edit are all intact
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"
    assert (repo / "code.py").read_text() == "print(2)\n"
    assert "code.py" in git("status", "--short", cwd=repo)


def test_second_publish_keeps_earlier_runs(repo):
    publish.publish_report("report.html", run_id=1, push=False)
    (repo / "report.html").write_text("<html>run two</html>")
    publish.publish_report("report.html", run_id=2, push=False)

    files = set(git("ls-tree", "-r", "--name-only", "gh-pages", cwd=repo).splitlines())
    assert files == {"index.html", "run-0001.html", "run-0002.html", ".nojekyll"}
    assert git("show", "gh-pages:index.html", cwd=repo) == "<html>run two</html>"
    assert git("show", "gh-pages:run-0001.html", cwd=repo) == "<html>run one</html>"
    # history is linear: the second publish builds on the first
    assert len(git("rev-list", "gh-pages", cwd=repo).splitlines()) == 2


def test_missing_report_is_a_clear_error(repo):
    with pytest.raises(publish.PublishError, match="no report at"):
        publish.publish_report("does-not-exist.html", run_id=1, push=False)
