"""Command-line interfaces and the sequential update workflow."""

from __future__ import annotations

import argparse
import shlex
import sys
from typing import TextIO

from .git import (
    Git, GitError, Repository, decode, default_branch, discover, inspect,
    push_status, remotes, tracking,
)


def examples(entries: list[tuple[str, str]]) -> str:
    width = max(len(command) for command, _ in entries)
    return "examples:\n" + "\n".join(
        f"  {command:<{width}}  {description}" for command, description in entries
    )


UP_EXAMPLES = examples([
    ("mgup *", "Update repositories and recursive submodules"),
    ("mgup --dry-run *", "Preview without fetching or changing anything"),
    ("mgup --no-merge-default *", "Update without merging the default branch"),
    ("mgup --no-submodules *", "Skip submodule updates"),
])

STAT_EXAMPLES = examples([
    ("mgstat *", "Show branches and working-tree status"),
    ("mgstat --non-default *", "Show non-default checkouts"),
    ("mgstat --unpushed *", "Include outgoing commit counts"),
    ("mgstat --fetch --unpushed *", "Refresh remote information first"),
    ("mgstat --non-default --unpushed *", "Combine filtering and commit counts"),
])


def parser(command: str) -> argparse.ArgumentParser:
    updating = command == "mgup"
    description = (
        "Fetch and rebase repositories sequentially, merge the default branch into\n"
        "feature branches, then update submodules. Dirty repositories are skipped\n"
        "before fetching."
        if updating else
        "Show repository branches and working-tree changes. Uses local information\n"
        "unless --fetch is requested. --unpushed adds counts; it does not filter rows."
    )
    result = argparse.ArgumentParser(
        prog=command, description=description,
        epilog=UP_EXAMPLES if updating else STAT_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    result.add_argument("paths", nargs="*", default=["."], metavar="PATH",
                        help="repository roots or glob patterns (default: current directory)")
    result.add_argument("--remote", default="origin", metavar="NAME",
                        help="remote used to detect/merge the default branch (default: origin)")
    result.add_argument("--default-branch", metavar="BRANCH",
                        help="override default-branch detection and mgtools.defaultBranch config")
    if updating:
        result.add_argument("--dry-run", action="store_true", help="preview using cached refs; do not fetch or change anything")
        result.add_argument("--no-merge-default", action="store_true", help="only update the current branch's upstream")
        result.add_argument("--no-submodules", action="store_true", help="skip recursive submodule updates")
    else:
        result.add_argument("--non-default", action="store_true", help="hide known default checkouts; retain detached and unknown cases")
        result.add_argument("--unpushed", action="store_true", help="show ahead/behind counts against the configured push destination")
        result.add_argument("--fetch", action="store_true", help="fetch relevant remotes before inspection (may require authentication)")
    return result


def display(value: str) -> str:
    """Keep pathnames and Git diagnostics on their intended terminal lines."""
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in value)


def action(git: Git, repo: Repository, args: list[str], out: TextIO, dry_run: bool = False) -> None:
    print(f"  {'would run: ' if dry_run else ''}{display(shlex.join(['git', *args]))}", file=out, flush=True)
    if dry_run:
        return
    result = git.run(repo.path, *args, check=False, output=out)
    for data in (result.stdout, result.stderr):
        for line in decode(data).splitlines():
            print(f"    {display(line)}", file=out)
    if result.returncode:
        raise GitError(f"command failed (exit {result.returncode})")


def fetch(git: Git, repo: Repository, names: list[str], out: TextIO, dry_run: bool = False) -> None:
    for name in dict.fromkeys(names):
        if name != ".":
            action(git, repo, ["fetch", "--no-recurse-submodules", "--", name], out, dry_run)


def update(git: Git, repo: Repository, options: argparse.Namespace, out: TextIO) -> str:
    state = inspect(git, repo.path)
    print(f"=== {display(repo.label)} [{display(state.branch_label)}] ===", file=out, flush=True)

    def skip(reason: str) -> str:
        print(f"  SKIPPED: {display(reason)}", file=out)
        return "skipped"

    if state.operations:
        return skip("operation in progress: " + ", ".join(f"{name} in {path}" for path, name in state.operations))
    if state.dirty:
        return skip(f"uncommitted changes ({state.worktree})")
    if not state.branch:
        return skip("detached HEAD")
    if not state.oid:
        return skip("repository has no commits")

    default = default_branch(git, repo.path, options.remote, options.default_branch)
    branch_tracking = tracking(git, repo.path, state.branch)
    is_default = state.branch == default.name
    merge_default = not options.no_merge_default and not is_default
    available = remotes(git, repo.path)
    required = []
    if branch_tracking.upstream_remote:
        required.append(branch_tracking.upstream_remote)
    if merge_default:
        required.append(options.remote)
    missing = [name for name in required if name != "." and name not in available]
    if missing:
        return skip(f"remote not configured: {', '.join(dict.fromkeys(missing))}")
    if not branch_tracking.upstream and not merge_default:
        return skip("no upstream configured")

    fetch(git, repo, required, out, options.dry_run)
    # Fetch may have supplied a previously missing default branch or upstream.
    default = default_branch(git, repo.path, options.remote, options.default_branch)
    is_default = state.branch == default.name
    merge_default = not options.no_merge_default and not is_default
    if merge_default and not default.name:
        if options.dry_run:
            print("  Default branch unknown in cached refs; merge decision requires a fetch.", file=out)
        else:
            return skip(f"default branch unknown ({default.reason}); use --default-branch or mgtools.defaultBranch")
    if merge_default and default.ref and not git.has_commit(repo.path, default.ref) and not options.dry_run:
        return skip(f"default branch unavailable: {options.remote}/{default.name}")

    upstream = branch_tracking.upstream
    # A feature branch tracking origin/main must merge main, not rebase onto it.
    rebase_upstream = upstream and not (merge_default and upstream == default.ref)
    if not upstream:
        if is_default or options.no_merge_default:
            return skip("no upstream configured")
        print("  No upstream configured; only merging the default branch.", file=out)
    if rebase_upstream:
        if not options.dry_run and not git.has_commit(repo.path, upstream):
            return skip(f"upstream unavailable after fetch: {upstream}")
        ancestor = git.run(repo.path, "merge-base", "--is-ancestor", upstream, "HEAD", check=False)
        if ancestor.returncode not in (0, 1) and not options.dry_run:
            raise GitError(decode(ancestor.stderr).strip() or "cannot compare upstream history")
        # Avoid replaying previously integrated default-branch commits on repeated runs.
        if options.dry_run or ancestor.returncode != 0:
            args = [
                "-c", "submodule.recurse=false", "-c", "rebase.updateRefs=false",
                "-c", "rebase.autoSquash=false", "rebase", "--no-autostash",
            ]
            if not is_default:
                args.append("--rebase-merges")
            args.append(upstream)
            try:
                action(git, repo, args, out, options.dry_run)
            except GitError:
                print("  If a rebase is in progress, resolve it and run git rebase --continue, or git rebase --abort.", file=out)
                raise
        else:
            print("  Current branch already contains its upstream.", file=out)

    if merge_default and default.ref:
        message = f"Merge {options.remote}/{default.name} into {state.branch}"
        try:
            action(git, repo, [
                "-c", "submodule.recurse=false", "merge", "--no-autostash", "--ff",
                "--no-edit", "-m", message, default.ref,
            ], out, options.dry_run)
        except GitError:
            print("  If a merge is in progress, resolve it and run git merge --continue, or git merge --abort.", file=out)
            raise
    if not options.no_submodules:
        action(git, repo, ["submodule", "update", "--init", "--recursive", "--checkout", "--jobs", "1"], out, options.dry_run)
    return "planned" if options.dry_run else "updated"


def show_status(git: Git, repositories: list[Repository], options: argparse.Namespace, out: TextIO, err: TextIO) -> bool:
    headers = ["REPOSITORY", "BRANCH", "WORKTREE"]
    if options.unpushed:
        headers.append("PUSH STATUS (cached)")
    rows: list[list[str]] = []
    notes: list[str] = []
    failed = False
    for repo in repositories:
        try:
            state = inspect(git, repo.path)
            repo_notes = []
            if options.fetch:
                info = tracking(git, repo.path, state.branch)
                available = remotes(git, repo.path)
                required = [name for name in (options.remote, info.upstream_remote, info.push_remote if options.unpushed else None)
                            if name and name != "." and name in available]
                try:
                    if not required:
                        raise GitError("no relevant remotes configured")
                    print(f"=== {display(repo.label)} ===", file=err)
                    fetch(git, repo, required, err)
                except GitError as exc:
                    failed = True
                    repo_notes.append("fetch failed; cached refs")
                    print(f"{display(repo.label)}: {display(str(exc))}", file=err)
            default = default_branch(git, repo.path, options.remote, options.default_branch)
            if options.non_default and default.name is not None and state.branch == default.name:
                continue
            if default.name is None:
                repo_notes.append("default unknown")
            repo_notes.extend(f"{name} in progress ({path})" for path, name in state.operations)
            row = [repo.label, state.branch_label, state.worktree]
            if options.unpushed:
                row.append(push_status(git, repo.path, state))
            rows.append([display(cell) for cell in row])
            notes.append(display("; ".join(repo_notes)))
        except GitError as exc:
            failed = True
            print(f"{display(repo.label)}: {display(str(exc))}", file=err)
    if any(notes):
        headers.append("NOTES")
        for row, note in zip(rows, notes):
            row.append(note)
    widths = [max(len(row[column]) for row in [headers, *rows]) for column in range(len(headers))]
    for row in [headers, *rows]:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip(), file=out)
    return failed


def main(command: str, argv: list[str] | None = None, *, git: Git | None = None,
         out: TextIO | None = None, err: TextIO | None = None) -> int:
    options = parser(command).parse_args(argv)
    git = git or Git()
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        found = discover(git, options.paths)
        for error in found.errors:
            print(display(error), file=err)
        if not found.repositories:
            print("No repository roots found in the supplied paths.", file=err)
            return 2
        if command == "mgstat":
            failed = show_status(git, found.repositories, options, out, err)
            return int(failed or bool(found.errors))
        counts = {"updated": 0, "planned": 0, "skipped": 0, "failed": len(found.errors)}
        for repo in found.repositories:
            try:
                counts[update(git, repo, options, out)] += 1
            except GitError as exc:
                counts["failed"] += 1
                print(f"  FAILED {display(repo.label)}: {display(str(exc))}", file=out)
        label = "planned" if options.dry_run else "updated"
        print(f"\nSummary: {counts[label]} {label}, {counts['skipped']} skipped, {counts['failed']} failed", file=out)
        return int(bool(counts["failed"]))
    except GitError as exc:
        print(display(str(exc)), file=err)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted; check the last repository for an operation in progress.", file=err)
        return 130
    except BrokenPipeError:
        return 0


def mgup() -> int:
    return main("mgup")


def mgstat() -> int:
    return main("mgstat")
