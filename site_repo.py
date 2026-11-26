"""
Keeping the site repo (GITHUB_REPO_PATH) usable for the jobs that push generated
data into it: export_companies.py (src/data/companies/, the index) and
rns_digest.py (announcements-today.json).

Both pull --rebase and both write under src/data/, so a push from elsewhere can
leave the clone mid-rebase with a conflict, after which every later run fails.
This module:

  recover()          Run first. If the clone is mid-rebase / merge / cherry-pick or
                     has unmerged paths, and every conflicted path is a generated
                     file, abort the operation so the job can start clean. A
                     conflict on anything else (briefings, the hand-edited slug
                     map, site code) is left alone and raised for a human.
  sync()             pull --rebase --autostash; on a conflict confined to
                     generated files, keep this run's version (it is the newest
                     output of the job) and continue the rebase.
  commit_and_push()  recover, commit only the given paths, sync, push (once more
                     after a sync if the push loses a race).

Usage:
  python3 site_repo.py --check     Report the clone's state and how old the last
                                   export/digest commits on origin are; alert if
                                   broken or stale. Exit 1 if anything is wrong.
  python3 site_repo.py --repair    recover() only (e.g. before publish.py).
"""
import os
import sys
from datetime import datetime, timedelta

from config import log

MAX_REBASE_STEPS = 50


class SiteRepoError(RuntimeError):
    """The site repo needs a human: a conflict outside the generated files."""


def generated_paths(companies_rel):
    """Paths the jobs regenerate on every run (safe to overwrite on a conflict).
    Directories end with '/'. The slug map is excluded: it can be hand-edited."""
    from export_companies import INDEX_FILENAME
    from rns_digest import DIGEST_FILENAME
    companies_rel = companies_rel.strip("/\\").replace("\\", "/")
    data_rel = os.path.dirname(companies_rel)
    return [companies_rel + "/", f"{data_rel}/{INDEX_FILENAME}", f"{data_rel}/{DIGEST_FILENAME}"]


def is_generated(path, generated):
    return any(path == g or (g.endswith("/") and path.startswith(g)) for g in generated)


def _default_git():
    from export_companies import _git
    return _git


def _try(git, repo, *args):
    try:
        return git(repo, *args), None
    except RuntimeError as e:
        return None, str(e)


def state(repo, git=None):
    """What the clone is in the middle of, and which paths are unmerged."""
    git = git or _default_git()
    git_dir = git(repo, "rev-parse", "--git-dir")
    if not isinstance(git_dir, str) or not git_dir.strip():
        git_dir = ".git"
    git_dir = git_dir.strip()
    git_dir = git_dir if os.path.isabs(git_dir) else os.path.join(repo, git_dir)
    in_progress = None
    if os.path.isdir(os.path.join(git_dir, "rebase-merge")) or \
       os.path.isdir(os.path.join(git_dir, "rebase-apply")):
        in_progress = "rebase"
    elif os.path.exists(os.path.join(git_dir, "MERGE_HEAD")):
        in_progress = "merge"
    elif os.path.exists(os.path.join(git_dir, "CHERRY_PICK_HEAD")):
        in_progress = "cherry-pick"
    out = git(repo, "diff", "--name-only", "--diff-filter=U")
    out = out if isinstance(out, str) else ""
    unmerged = sorted(set(p for p in out.splitlines() if p.strip()))
    return {"in_progress": in_progress, "unmerged": unmerged}


def recover(repo, generated, git=None):
    """Return a description of what was repaired, or None if the clone was fine.
    Raises SiteRepoError if a non-generated path is conflicted."""
    git = git or _default_git()
    st = state(repo, git)
    if not st["in_progress"] and not st["unmerged"]:
        return None
    foreign = [p for p in st["unmerged"] if not is_generated(p, generated)]
    what = f"{st['in_progress'] or 'unmerged paths'} ({', '.join(st['unmerged']) or 'no conflicted files'})"
    if foreign:
        raise SiteRepoError(f"site repo stuck mid-{what}; conflicts outside generated data "
                            f"need a human: {', '.join(foreign)}")
    op = st["in_progress"]
    if op:
        _, err = _try(git, repo, op, "--abort")
        if err:
            # Abort can itself fail on a half-written state; fall back to the
            # lower-level cleanups, which also only touch the operation's files.
            if op == "rebase":
                _try(git, repo, "rebase", "--quit")
            _try(git, repo, "reset", "--merge")
    else:
        _try(git, repo, "reset", "--merge")
    after = state(repo, git)
    if after["in_progress"] or after["unmerged"]:
        raise SiteRepoError(f"site repo stuck mid-{what} and automatic abort did not clear it")
    msg = f"aborted a stuck {what}"
    log(f"  🔧 Site repo: {msg}")
    return msg


def _resolve_rebase(repo, generated, git):
    """Finish a stopped rebase whose conflicts are all generated files, keeping the
    version from the commit being replayed (this job's newer output)."""
    for _ in range(MAX_REBASE_STEPS):
        st = state(repo, git)
        if st["in_progress"] != "rebase":
            return
        foreign = [p for p in st["unmerged"] if not is_generated(p, generated)]
        if foreign:
            _try(git, repo, "rebase", "--abort")
            raise SiteRepoError("pull --rebase conflicted outside generated data; rebase aborted, "
                                f"needs a human: {', '.join(foreign)}")
        for p in st["unmerged"]:
            # During a rebase "--theirs" is the commit being replayed: ours.
            _, err = _try(git, repo, "checkout", "--theirs", "--", p)
            if err:                                   # deleted on our side
                git(repo, "rm", "-q", "--", p)
            else:
                git(repo, "add", "--", p)
        _, err = _try(git, repo, "-c", "core.editor=true", "rebase", "--continue")
        if err and ("nothing to commit" in err.lower() or "no changes" in err.lower()):
            _try(git, repo, "rebase", "--skip")
    _try(git, repo, "rebase", "--abort")
    raise SiteRepoError(f"rebase did not finish after {MAX_REBASE_STEPS} steps; aborted")


def sync(repo, branch, generated, git=None):
    """pull --rebase --autostash, resolving conflicts on generated files."""
    git = git or _default_git()
    _, err = _try(git, repo, "pull", "--rebase", "--autostash", "origin", branch)
    if err is None:
        return
    if state(repo, git)["in_progress"] != "rebase":
        raise RuntimeError(err)
    log("  🔧 Site repo: pull --rebase conflicted on generated data; keeping this run's version")
    _resolve_rebase(repo, generated, git)


def commit_and_push(repo, branch, rel_paths, message, generated, git=None):
    """Recover, stage and commit only rel_paths, sync, push. Returns False if there
    was nothing to commit."""
    git = git or _default_git()
    recover(repo, generated, git)
    git(repo, "add", "-A", "--", *rel_paths)
    _, err = _try(git, repo, "commit", "-m", message, "--", *rel_paths)
    if err:
        if "nothing to commit" in err.lower() or "nothing added" in err.lower() \
           or "no changes added" in err.lower():
            log("  ℹ GitHub: no changes to commit")
            return False
        raise RuntimeError(err)
    sync(repo, branch, generated, git)
    _, err = _try(git, repo, "push", "origin", branch)
    if err:                       # someone pushed between our pull and push
        sync(repo, branch, generated, git)
        git(repo, "push", "origin", branch)
    return True


# ── health check ────────────────────────────────────────────

STALE_AFTER = {"companies:": timedelta(hours=30), "announcements:": timedelta(hours=30)}


def last_commit_age(repo, branch, prefix, git, now):
    out = git(repo, "log", f"origin/{branch}", "-1", f"--grep=^{prefix}", "--format=%ct")
    if not isinstance(out, str) or not out.strip():
        return None
    return now - datetime.fromtimestamp(int(out.strip()))     # local time, like `now`


def check(repo, branch, generated, git=None, now=None):
    """Problems with the clone or with how recently the jobs reached origin."""
    git = git or _default_git()
    now = now or datetime.now()
    problems = []
    st = state(repo, git)
    if st["in_progress"] or st["unmerged"]:
        problems.append(f"clone is mid-{st['in_progress'] or 'merge'}; unmerged: "
                        f"{', '.join(st['unmerged']) or 'none'}")
    _, err = _try(git, repo, "fetch", "-q", "origin", branch)
    if err:
        problems.append(f"git fetch failed: {err}")
    # Weekends have no RNS flow; only judge staleness on weekdays after 18:00.
    if now.weekday() < 5 and now.hour >= 18:
        for prefix, limit in STALE_AFTER.items():
            age = last_commit_age(repo, branch, prefix, git, now)
            if age is None or age > limit + timedelta(days=2 if now.weekday() == 0 else 0):
                problems.append(f"no '{prefix}' commit on origin/{branch} for "
                                + (f"{age.days}d {age.seconds // 3600}h" if age else "ever"))
    return problems


def main(argv):
    from config import log as _log  # noqa: F401  (loads .env)
    import alerts
    repo = os.environ.get("GITHUB_REPO_PATH", "")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    companies_rel = os.environ.get("GITHUB_COMPANIES_DIR", "src/data/companies")
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        log("❌ GITHUB_REPO_PATH must point at the site repo")
        return 1
    generated = generated_paths(companies_rel)
    if "--repair" in argv:
        try:
            msg = recover(repo, generated)
        except SiteRepoError as e:
            alerts.failure("site-repo", str(e))
            return 1
        if msg:
            alerts.notice("site-repo", f"Recovered automatically: {msg}")
        alerts.resolved("site-repo")
        return 0
    if "--check" in argv:
        problems = check(repo, branch, generated)
        for p in problems:
            log(f"❌ {p}")
        if problems:
            alerts.failure("site-check", "\n".join(problems))
            return 1
        log("✅ Site repo clean; export and digest commits are recent")
        alerts.resolved("site-check")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
