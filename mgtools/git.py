"""Git inspection shared by the commands. Inspection never fetches."""

from __future__ import annotations

import glob
import os
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TextIO


def decode(value: bytes) -> str:
    return os.fsdecode(value)


class GitError(RuntimeError):
    pass


class Git:
    def run(self, path: Path, *args: str, check: bool = True,
            output: TextIO | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["GIT_MERGE_AUTOEDIT"] = "no"
        # An inherited GIT_DIR must not redirect an operation into another repo.
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            env.pop(key, None)
        # Give interactive updates the real terminal so SSH/key-touch prompts and
        # progress remain visible. In-memory callers receive captured output.
        try:
            destination = output.fileno() if output is not None else subprocess.PIPE
        except (AttributeError, OSError, ValueError):
            destination = subprocess.PIPE
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(path), *args],
                stdout=destination,
                stderr=destination,
                env=env,
                check=False,
            )
        except OSError as exc:
            raise GitError(str(exc)) from exc
        result.stdout = result.stdout or b""
        result.stderr = result.stderr or b""
        if check and result.returncode:
            message = decode(result.stderr).strip() or decode(result.stdout).strip()
            raise GitError(message or f"git exited with status {result.returncode}")
        return result

    def text(self, path: Path, *args: str) -> str:
        return decode(self.run(path, *args).stdout).strip()

    def config(self, path: Path, key: str) -> str | None:
        result = self.run(path, "config", "--get", key, check=False)
        if result.returncode == 1:
            return None
        if result.returncode:
            raise GitError(decode(result.stderr).strip())
        return decode(result.stdout).strip()

    def has_commit(self, path: Path, ref: str) -> bool:
        return self.run(
            path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
        ).returncode == 0


@dataclass
class Repository:
    path: Path
    label: str


@dataclass
class Discovery:
    repositories: list[Repository] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def repository_root(git: Git, path: Path) -> bool:
    result = git.run(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode:
        if (path / ".git").exists():
            raise GitError(decode(result.stderr).strip())
        return False
    return Path(decode(result.stdout).removesuffix("\n")).resolve() == path.resolve()


def discover(git: Git, arguments: list[str]) -> Discovery:
    result = Discovery()
    seen: set[Path] = set()
    for argument in arguments:
        matches = [argument] if Path(argument).exists() else sorted(glob.glob(argument))
        if not matches:
            result.errors.append(f"{argument}: no matching path")
        for match in matches:
            path = Path(match).resolve()
            if not path.is_dir() or path in seen:
                continue
            seen.add(path)
            try:
                if repository_root(git, path):
                    result.repositories.append(Repository(path, match))
            except GitError as exc:
                result.errors.append(f"{match}: {exc}")
    return result


@dataclass
class BranchMatch:
    name: str
    source: str


def matching_branches(git: Git, path: Path, pattern: str, scope: str = "all") -> list[BranchMatch]:
    namespaces = {"local": ["refs/heads/"], "remotes": ["refs/remotes/"],
                  "all": ["refs/heads/", "refs/remotes/"]}
    data = git.run(path, "for-each-ref", "--sort=refname", "--format=%(refname)%00%(symref)",
                   *namespaces[scope]).stdout
    # Prefer the longest configured remote name, since remote names may contain '/'.
    remote_names = sorted(remotes(git, path), key=len, reverse=True) if scope != "local" else []
    matches = []
    for line in data.splitlines():
        ref, symbolic = map(decode, line.split(b"\0"))
        if ref.startswith("refs/heads/"):
            name = branch = ref.removeprefix("refs/heads/")
            source = "local"
        else:
            if symbolic:  # origin/HEAD and other aliases are not branches.
                continue
            name = ref.removeprefix("refs/remotes/")
            remote = next((remote for remote in remote_names if name.startswith(remote + "/")),
                          name.partition("/")[0])
            branch = name.removeprefix(remote + "/")
            source = "remote (cached)"
        if fnmatchcase(branch, pattern) or fnmatchcase(name, pattern):
            matches.append(BranchMatch(name, source))
    return matches


@dataclass
class State:
    branch: str | None = None
    oid: str | None = None
    staged: int = 0
    modified: int = 0
    untracked: int = 0
    conflicts: int = 0
    submodules: set[str] = field(default_factory=set)
    operations: list[tuple[str, str]] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.modified or self.untracked or self.conflicts or self.submodules)

    @property
    def branch_label(self) -> str:
        if self.branch is None:
            return f"(detached: {(self.oid or 'unknown')[:10]})"
        return self.branch if self.oid else f"{self.branch} (no commits)"

    @property
    def worktree(self) -> str:
        counts = [
            ("staged", self.staged),
            ("modified", self.modified),
            ("new", self.untracked),
            ("conflicts", self.conflicts),
            ("submodules", len(self.submodules)),
        ]
        return " ".join(f"{label}:{count}" for label, count in counts if count) or "clean"


def parse_status(data: bytes) -> State:
    state = State()
    records = iter(data.split(b"\0"))
    for record in records:
        if not record:
            continue
        if record.startswith(b"# branch.head "):
            branch = decode(record[len(b"# branch.head "):])
            state.branch = None if branch == "(detached)" else branch
        elif record.startswith(b"# branch.oid "):
            oid = decode(record[len(b"# branch.oid "):])
            state.oid = None if oid == "(initial)" else oid
        elif record.startswith(b"? "):
            state.untracked += 1
        elif record.startswith(b"u "):
            state.conflicts += 1
        elif record[:2] in (b"1 ", b"2 "):
            renamed = record.startswith(b"2 ")
            fields = record.split(b" ", 9 if renamed else 8)
            xy, sub = fields[1:3]
            state.staged += xy[:1] != b"."
            state.modified += xy[1:2] != b"."
            if sub.startswith(b"S") and sub != b"S...":
                state.submodules.add(decode(fields[-1]))
            if renamed:
                next(records)  # The original pathname is a second NUL-terminated field.
    return state


def operation(git: Git, path: Path) -> str | None:
    git_dir = Path(decode(git.run(path, "rev-parse", "--absolute-git-dir").stdout).removesuffix("\n"))
    markers = (
        ("rebase-merge", "rebase"),
        ("rebase-apply", "am" if (git_dir / "rebase-apply/applying").exists() else "rebase"),
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("sequencer", "sequencer"),
        ("BISECT_LOG", "bisect"),
    )
    return next((name for marker, name in markers if (git_dir / marker).exists()), None)


def read_status(git: Git, path: Path) -> State:
    return parse_status(git.run(
        path, "status", "--porcelain=v2", "-z", "--branch", "--no-ahead-behind",
        "--untracked-files=all", "--ignore-submodules=none",
    ).stdout)


def inspect(git: Git, path: Path) -> State:
    state = read_status(git, path)
    active = operation(git, path)
    if active:
        state.operations.append((".", active))
    visited = {path.resolve()}

    def inspect_submodules(parent: Path) -> None:
        entries = git.run(parent, "ls-files", "--stage", "-z").stdout.split(b"\0")
        for entry in entries:
            if not entry:
                continue
            metadata, filename = entry.split(b"\t", 1)
            mode, expected, stage = metadata.split()
            if mode != b"160000" or stage != b"0":
                continue
            child = parent / decode(filename)
            if not child.is_dir() or child.resolve() in visited:
                continue
            # An uninitialized submodule resolves to its parent repository.
            if not repository_root(git, child):
                continue
            visited.add(child.resolve())
            relative = str(child.relative_to(path))
            child_state = read_status(git, child)
            if child_state.dirty or child_state.oid != decode(expected):
                state.submodules.add(relative)
            child_operation = operation(git, child)
            if child_operation:
                state.operations.append((relative, child_operation))
            inspect_submodules(child)

    inspect_submodules(path)
    return state


@dataclass
class DefaultBranch:
    name: str | None
    ref: str | None
    reason: str = ""


def default_branch(git: Git, path: Path, remote: str, override: str | None) -> DefaultBranch:
    configured = override or git.config(path, "mgtools.defaultBranch")
    prefix = f"refs/remotes/{remote}/"
    if configured:
        if git.run(path, "check-ref-format", f"refs/heads/{configured}", check=False).returncode:
            raise GitError(f"invalid default branch: {configured}")
        return DefaultBranch(configured, prefix + configured)
    head = git.run(path, "symbolic-ref", "--quiet", prefix + "HEAD", check=False)
    target = decode(head.stdout).strip()
    if head.returncode == 0 and target.startswith(prefix) and git.has_commit(path, target):
        return DefaultBranch(target[len(prefix):], target)
    candidates = [name for name in ("main", "master") if git.has_commit(path, prefix + name)]
    if len(candidates) == 1:
        return DefaultBranch(candidates[0], prefix + candidates[0])
    reason = "both main and master exist" if candidates else "no default-branch ref found"
    return DefaultBranch(None, None, reason)


@dataclass
class Tracking:
    upstream: str | None = None
    upstream_remote: str | None = None
    push_remote: str | None = None


def tracking(git: Git, path: Path, branch: str | None) -> Tracking:
    if branch is None:
        return Tracking()
    ref = f"refs/heads/{branch}"
    data = git.run(
        path, "for-each-ref",
        "--format=%(refname)%00%(upstream)%00%(upstream:remotename)%00%(push:remotename)",
        ref,
    ).stdout
    for line in data.splitlines():
        name, upstream, upstream_remote, push_remote = map(decode, line.split(b"\0"))
        if name == ref:
            return Tracking(upstream or None, upstream_remote or None, push_remote or None)
    return Tracking()


def remotes(git: Git, path: Path) -> list[str]:
    return git.text(path, "remote").splitlines()


def push_status(git: Git, path: Path, state: State) -> str:
    if not state.branch or not state.oid:
        return "target unknown"
    result = git.run(path, "rev-parse", "--symbolic-full-name", "--verify", "@{push}", check=False)
    ref = decode(result.stdout).strip()
    if result.returncode or not ref or not git.has_commit(path, ref):
        return "target unknown"
    counts = git.text(path, "rev-list", "--left-right", "--count", f"HEAD...{ref}").split()
    ahead, behind = map(int, counts)
    target = ref.removeprefix("refs/remotes/").removeprefix("refs/heads/")
    status = "up to date" if not ahead and not behind else f"ahead:{ahead} behind:{behind}"
    return f"{status} ({target})"
