"""Integration tests use local Git histories; all network operations are intercepted."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mgtools.cli import main, parser
from mgtools.git import Git, GitError, default_branch, discover, inspect, matching_branches, push_status


class OfflineGit(Git):
    """Exercise real inspection/rebase/merge, with a hard boundary at network commands."""

    def __init__(self):
        self.commands = []
        self.fetch_hook = None
        self.submodule_hook = None

    def run(self, path, *args, check=True, output=None):
        self.commands.append((path, args))
        arguments = list(args)
        while arguments and arguments[0] == "-c":
            arguments = arguments[2:]
        command = arguments[0]
        if command == "fetch":
            if self.fetch_hook:
                return self.fetch_hook(path, arguments)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if command == "submodule":
            if self.submodule_hook:
                return self.submodule_hook(path, arguments)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if command in {"push", "pull", "clone", "ls-remote"}:
            raise AssertionError(f"network operation attempted: {args}")
        if command == "remote" and len(arguments) > 1:
            raise AssertionError(f"unexpected remote operation: {args}")
        return super().run(path, *args, check=check, output=output)

    def actions(self, command):
        return [(path, args) for path, args in self.commands if command in args]


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mgtools-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "",  # A second barrier against accidental network access.
        })
        env_patch = patch.dict(os.environ, environment, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.git = OfflineGit()

    def raw(self, path, *args, check=True):
        result = subprocess.run(
            ["git", "-C", str(path), *args], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        if check and result.returncode:
            self.fail(f"git {args}: {os.fsdecode(result.stderr)}")
        return os.fsdecode(result.stdout).strip()

    def config(self, path, key, value):
        self.raw(path, "config", key, value)

    def repo(self, name="repo", *, branch="main", empty=False, remote=True):
        path = self.root / name
        path.mkdir(parents=True)
        self.raw(path, "init", "-q", "-b", branch)
        for key, value in {
            "user.name": "MG Tools Tests", "user.email": "tests@example.invalid",
            "commit.gpgsign": "false", "core.hooksPath": os.devnull,
            "core.editor": "false", "push.default": "simple",
        }.items():
            self.config(path, key, value)
        if not empty:
            self.commit(path, "base.txt", "base\n")
        if remote:
            self.remote(path, "origin")
            if not empty:
                self.remote_ref(path, "origin", branch, self.raw(path, "rev-parse", "HEAD"))
                self.remote_head(path, "origin", branch)
                self.upstream(path, branch, "origin", branch)
        return path

    def remote(self, path, name):
        self.config(path, f"remote.{name}.url", "ssh://unreachable.invalid/never-connect")
        self.config(path, f"remote.{name}.fetch", f"+refs/heads/*:refs/remotes/{name}/*")

    def remote_ref(self, path, remote, branch, oid):
        # Seed cached refs as fixture files: no fetch/push/clone or Git ref-update command.
        git_dir = Path(self.raw(path, "rev-parse", "--git-common-dir"))
        if not git_dir.is_absolute():
            git_dir = path / git_dir
        target = git_dir / "refs" / "remotes" / remote / branch
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(oid + "\n")

    def remote_head(self, path, remote, branch):
        self.remote_ref(path, remote, "HEAD", f"ref: refs/remotes/{remote}/{branch}")

    def upstream(self, path, branch, remote, target):
        self.config(path, f"branch.{branch}.remote", remote)
        self.config(path, f"branch.{branch}.merge", f"refs/heads/{target}")

    def commit(self, path, name, content):
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        self.raw(path, "add", "--", name)
        self.raw(path, "commit", "-qm", f"Change {name}")
        return self.raw(path, "rev-parse", "HEAD")

    def feature(self, path, name="feature/example", *, upstream=True):
        self.raw(path, "switch", "-qc", name)
        oid = self.commit(path, "feature.txt", "feature\n")
        if upstream:
            self.remote_ref(path, "origin", name, oid)
            self.upstream(path, name, "origin", name)
        return oid

    def advance(self, path, source, remote, target, filename="remote.txt", content="remote\n"):
        previous = self.raw(path, "symbolic-ref", "--short", "HEAD")
        self.raw(path, "switch", "-qc", f"incoming-{len(self.git.commands)}-{target.replace('/', '-')}", source)
        oid = self.commit(path, filename, content)
        self.remote_ref(path, remote, target, oid)
        self.raw(path, "switch", "-q", previous)
        return oid

    def invoke(self, command, *args):
        out, err = io.StringIO(), io.StringIO()
        code = main(command, list(map(str, args)), git=self.git, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def attach(self, parent, child):
        name = str(child.relative_to(parent))
        oid = self.raw(child, "rev-parse", "HEAD")
        with (parent / ".gitmodules").open("a") as stream:
            stream.write(f'[submodule "{name}"]\n\tpath = {name}\n\turl = ssh://unreachable.invalid/submodule\n')
        self.config(parent, f"submodule.{name}.url", "ssh://unreachable.invalid/submodule")
        self.raw(parent, "add", ".gitmodules")
        self.raw(parent, "update-index", "--add", "--cacheinfo", f"160000,{oid},{name}")
        self.raw(parent, "commit", "-qm", f"Add submodule {name}")

    def test_discovery_handles_spaces_globs_worktrees_and_duplicate_paths(self):
        path = self.repo("repo with spaces")
        (path / "ordinary-directory").mkdir()
        link = self.root / "alias"
        link.symlink_to(path, target_is_directory=True)
        worktree = self.root / "second worktree"
        self.raw(path, "worktree", "add", "-qb", "other", str(worktree))
        (self.root / "ordinary-file").write_text("not a repo")
        found = discover(self.git, [str(self.root / "*"), str(path), str(path / "ordinary-directory")])
        self.assertEqual({repo.path for repo in found.repositories}, {path, worktree})
        self.assertEqual(found.errors, [])

    def test_unmatched_path_is_reported(self):
        code, _, err = self.invoke("mgstat", self.root / "missing-*")
        self.assertEqual(code, 2)
        self.assertIn("no matching path", err)

    def test_repository_paths_with_trailing_whitespace_are_supported(self):
        for name in ("trailing space ", "trailing newline\n"):
            with self.subTest(name=name):
                path = self.repo(name)
                found = discover(self.git, [str(path)])
                self.assertEqual(len(found.repositories), 1)
                self.assertFalse(inspect(self.git, path).dirty)
                code, out, err = self.invoke("mgstat", path)
                self.assertEqual(code, 0, out + err)
                self.assertEqual(len(out.splitlines()), 2)

    def test_find_exact_branch_includes_other_checkouts_and_cached_remotes(self):
        local = self.repo("local repo")
        remote = self.repo("remote repo")
        unrelated = self.repo("unrelated")
        self.raw(local, "branch", "feature/search")
        self.remote_ref(remote, "origin", "feature/search", self.raw(remote, "rev-parse", "HEAD"))
        self.raw(unrelated, "branch", "feature/search-more")
        self.raw(unrelated, "tag", "feature/search")
        (local / "base.txt").write_text("uncommitted work\n")
        before = self.raw(local, "status", "--porcelain=v2", "--branch")

        code, out, err = self.invoke("mgfind", "feature/search", self.root / "*")

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 3)
        self.assertIn(str(local), out)
        self.assertIn(str(remote), out)
        self.assertNotIn(str(unrelated), out)
        self.assertIn("origin/feature/search", out)
        self.assertIn("remote (cached)", out)
        self.assertEqual(self.raw(local, "status", "--porcelain=v2", "--branch"), before)
        self.assertTrue(all(args[0] in {"rev-parse", "for-each-ref", "remote"}
                            for _, args in self.git.commands))

    def test_find_globs_match_whole_names_case_sensitively_and_cross_slashes(self):
        path = self.repo()
        names = ["feature/ABC-123", "feature/nested/ABC-124", "fix/ABC-125", "fix/abc-123"]
        for name in names:
            self.raw(path, "branch", name)
        cases = {
            "*ABC-12[34]": names[:2],
            "feature/*": names[:2],
            "fix/ABC-12?": [names[2]],
            "*abc*": [names[3]],
            "ABC-123": [],
        }
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                matches = matching_branches(self.git, path, pattern)
                self.assertEqual([match.name for match in matches], expected)

    def test_find_scopes_and_remote_prefixes_omit_symbolic_aliases(self):
        path = self.repo()
        oid = self.raw(path, "rev-parse", "HEAD")
        self.remote(path, "team/review")
        self.remote_ref(path, "team/review", "main", oid)
        self.remote_head(path, "team/review", "main")
        self.assertEqual(
            [(match.name, match.source) for match in matching_branches(self.git, path, "main")],
            [("main", "local"), ("origin/main", "remote (cached)"), ("team/review/main", "remote (cached)")],
        )
        cases = [
            (["--local", "*"], ["main"]),
            (["--remotes", "*"], ["origin/main", "team/review/main"]),
            (["--remotes", "origin/*"], ["origin/main"]),
            (["--remotes", "team/review/*"], ["team/review/main"]),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                code, out, err = self.invoke("mgfind", *arguments, path)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual([row.split()[1] for row in out.splitlines()[1:]], expected)

    def test_find_handles_detached_worktrees_and_duplicate_paths(self):
        path = self.repo("repo with spaces")
        self.raw(path, "branch", "feature/search")
        worktree = self.root / "detached worktree"
        self.raw(path, "worktree", "add", "-q", "--detach", str(worktree))
        code, out, err = self.invoke("mgfind", "feature/*", path, worktree, path)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 3)
        self.assertIn(str(path), out)
        self.assertIn(str(worktree), out)

    def test_find_empty_repositories_and_no_matches_print_only_header(self):
        normal = self.repo()
        empty = self.repo("empty", empty=True)
        code, out, err = self.invoke("mgfind", "missing-*", normal, empty)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), ["REPOSITORY  BRANCH  SOURCE"])

    def test_find_defaults_to_current_directory(self):
        path = self.repo()
        previous = Path.cwd()
        try:
            os.chdir(path)
            code, out, err = self.invoke("mgfind", "--local", "main")
        finally:
            os.chdir(previous)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[1].split(), [".", "main", "local"])

    def test_find_reports_path_errors_and_continues_search(self):
        path = self.repo()
        code, out, err = self.invoke("mgfind", "main", self.root / "missing-*", path)
        self.assertEqual(code, 1)
        self.assertIn(str(path), out)
        self.assertIn("no matching path", err)
        code, _, err = self.invoke("mgfind", "main", self.root / "missing-*")
        self.assertEqual(code, 2)
        self.assertIn("No repository roots found", err)

    def test_find_reports_git_failure_and_continues_search(self):
        bad, good = self.repo("bad"), self.repo("good")
        run = self.git.run

        def failing_run(path, *args, **kwargs):
            if path == bad and args[0] == "for-each-ref":
                raise GitError("cannot read refs")
            return run(path, *args, **kwargs)

        with patch.object(self.git, "run", side_effect=failing_run):
            code, out, err = self.invoke("mgfind", "main", bad, good)
        self.assertEqual(code, 1)
        self.assertNotIn(str(bad), out)
        self.assertIn(str(good), out)
        self.assertIn(f"{bad}: cannot read refs", err)

    def test_find_requires_pattern_and_rejects_conflicting_scopes(self):
        for arguments in ([], ["--local", "--remotes", "*"]):
            with self.subTest(arguments=arguments), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parser("mgfind").parse_args(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_dirty_counts_include_staged_modified_untracked_and_renames(self):
        path = self.repo()
        self.commit(path, "old\tname\n.txt", "rename me\n")
        self.raw(path, "mv", "old\tname\n.txt", "new\nname\t.txt")
        (path / "base.txt").write_text("staged\n")
        self.raw(path, "add", "base.txt")
        (path / "base.txt").write_text("also unstaged\n")
        (path / "new directory").mkdir()
        (path / "new directory/a").write_text("a")
        (path / "new directory/b").write_text("b")
        state = inspect(self.git, path)
        self.assertEqual((state.staged, state.modified, state.untracked), (2, 1, 2))

    def test_ignored_files_do_not_make_repository_dirty(self):
        path = self.repo()
        self.commit(path, ".gitignore", "ignored\n")
        (path / "ignored").write_text("ignored")
        self.assertFalse(inspect(self.git, path).dirty)

    def test_staged_unstaged_and_untracked_each_skip_before_fetch(self):
        for kind in ("staged", "modified", "untracked"):
            with self.subTest(kind=kind):
                path = self.repo(kind)
                target = "new.txt" if kind == "untracked" else "base.txt"
                (path / target).write_text("local work\n")
                if kind == "staged":
                    self.raw(path, "add", target)
                before = self.raw(path, "rev-parse", "HEAD")
                self.git.commands.clear()
                code, out, _ = self.invoke("mgup", path)
                self.assertEqual(code, 0)
                self.assertIn("SKIPPED: uncommitted changes", out)
                self.assertEqual(self.git.actions("fetch"), [])
                self.assertEqual(self.git.actions("rebase"), [])
                self.assertEqual(self.git.actions("submodule"), [])
                self.assertEqual(self.raw(path, "rev-parse", "HEAD"), before)
                self.assertEqual((path / target).read_text(), "local work\n")

    def test_default_branch_prefers_remote_head_and_supports_overrides(self):
        path = self.repo(branch="trunk")
        oid = self.raw(path, "rev-parse", "HEAD")
        self.remote_ref(path, "origin", "main", oid)
        self.remote_ref(path, "origin", "master", oid)
        self.assertEqual(default_branch(self.git, path, "origin", None).name, "trunk")
        self.config(path, "mgtools.defaultBranch", "develop")
        self.assertEqual(default_branch(self.git, path, "origin", None).name, "develop")
        self.assertEqual(default_branch(self.git, path, "origin", "release").name, "release")

    def test_default_branch_fallback_does_not_guess_between_main_and_master(self):
        path = self.repo()
        (path / ".git/refs/remotes/origin/HEAD").unlink()
        self.assertEqual(default_branch(self.git, path, "origin", None).name, "main")
        self.remote_ref(path, "origin", "master", self.raw(path, "rev-parse", "HEAD"))
        self.assertIsNone(default_branch(self.git, path, "origin", None).name)

    def test_non_default_filter_retains_unknown_and_detached_cases(self):
        normal = self.repo("normal")
        feature = self.repo("feature")
        self.feature(feature)
        unknown = self.repo("unknown", remote=False)
        detached = self.repo("detached")
        self.raw(detached, "switch", "-q", "--detach")
        code, out, err = self.invoke("mgstat", "--non-default", normal, feature, unknown, detached)
        self.assertEqual(code, 0, err)
        self.assertNotIn(str(normal), out)
        for path in (feature, unknown, detached):
            self.assertIn(str(path), out)
        self.assertIn("default unknown", out)
        self.assertIn("(detached:", out)

    def test_push_counts_use_push_destination_in_triangular_workflow(self):
        path = self.repo()
        base = self.raw(path, "rev-parse", "HEAD")
        self.feature(path)
        self.remote(path, "publish")
        self.remote_ref(path, "publish", "feature/example", base)
        self.config(path, "remote.pushDefault", "publish")
        self.config(path, "push.default", "current")
        self.assertEqual(push_status(self.git, path, inspect(self.git, path)),
                         "ahead:1 behind:0 (publish/feature/example)")
        code, out, _ = self.invoke("mgstat", "--unpushed", path)
        self.assertEqual(code, 0)
        self.assertIn("ahead:1", out)
        self.assertEqual(self.git.actions("fetch"), [])

    def test_push_status_reports_divergence_and_unknown_target(self):
        path = self.repo()
        base = self.raw(path, "rev-parse", "HEAD")
        self.commit(path, "local.txt", "local")
        self.advance(path, base, "origin", "main")
        self.assertEqual(push_status(self.git, path, inspect(self.git, path)), "ahead:1 behind:1 (origin/main)")
        self.raw(path, "switch", "-qc", "unpublished")
        self.assertEqual(push_status(self.git, path, inspect(self.git, path)), "target unknown")

    def test_status_fetches_upstream_and_push_remotes_once(self):
        path = self.repo()
        self.remote(path, "publish")
        self.config(path, "remote.pushDefault", "publish")
        self.config(path, "push.default", "current")
        code, _, _ = self.invoke("mgstat", "--fetch", "--unpushed", path)
        self.assertEqual(code, 0)
        self.assertEqual([args[-1] for _, args in self.git.actions("fetch")], ["origin", "publish"])

    def test_status_fetch_failure_is_visible_and_returns_failure(self):
        path = self.repo()
        self.git.fetch_hook = lambda *_: subprocess.CompletedProcess([], 1, b"", b"offline")
        code, out, err = self.invoke("mgstat", "--fetch", "--unpushed", path)
        self.assertEqual(code, 1)
        self.assertIn("fetch failed; cached refs", out)
        self.assertIn("offline", err)

    def test_default_update_rebases_local_commit_onto_upstream(self):
        path = self.repo()
        base = self.raw(path, "rev-parse", "HEAD")
        old_local = self.commit(path, "local.txt", "local")
        remote = self.advance(path, base, "origin", "main")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertNotEqual(self.raw(path, "rev-parse", "HEAD"), old_local)
        self.assertEqual(self.raw(path, "rev-parse", "HEAD^"), remote)
        self.assertEqual((path / "local.txt").read_text(), "local")
        self.assertEqual(len(self.git.actions("submodule")), 1)

    def test_update_can_stream_git_output_to_a_real_file_descriptor(self):
        path = self.repo()
        self.advance(path, "main", "origin", "main")
        log = self.root / "update.log"
        with log.open("w") as output:
            code = main("mgup", [str(path)], git=self.git, out=output, err=io.StringIO())
        self.assertEqual(code, 0, log.read_text())
        self.assertIn("1 updated", log.read_text())
        self.assertTrue((path / "remote.txt").exists())

    def test_feature_merge_is_noninteractive_and_repeatable(self):
        path = self.repo()
        self.feature(path)
        remote = self.advance(path, "main", "origin", "main")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        merged = self.raw(path, "rev-parse", "HEAD")
        self.assertEqual(self.raw(path, "rev-parse", "HEAD^2"), remote)
        self.assertEqual(self.raw(path, "log", "-1", "--format=%s"), "Merge origin/main into feature/example")
        self.assertEqual(self.raw(path, "symbolic-ref", "--short", "HEAD"), "feature/example")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.raw(path, "rev-parse", "HEAD"), merged)

    def test_feature_rebases_upstream_then_merges_default(self):
        path = self.repo()
        feature = self.feature(path)
        self.commit(path, "local.txt", "local")
        feature_remote = self.advance(path, feature, "origin", "feature/example", "shared.txt")
        main_remote = self.advance(path, "main", "origin", "main", "default.txt")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.raw(path, "rev-parse", "HEAD^2"), main_remote)
        self.assertEqual(self.raw(path, "rev-parse", "HEAD^1^"), feature_remote)
        commands = [args for _, args in self.git.commands]
        rebase_index = next(i for i, args in enumerate(commands) if "rebase" in args)
        merge_index = next(i for i, args in enumerate(commands) if "merge" in args)
        self.assertLess(rebase_index, merge_index)
        self.assertIn("--rebase-merges", commands[rebase_index])

    def test_later_rebase_preserves_previous_default_merge(self):
        path = self.repo()
        feature = self.feature(path)
        remote = self.advance(path, "main", "origin", "main", "default.txt")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.advance(path, feature, "origin", "feature/example", "shared.txt")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.raw(path, "merge-base", "HEAD", remote), remote)
        self.assertGreater(int(self.raw(path, "rev-list", "--count", "--merges", "HEAD")), 0)
        self.assertTrue((path / "shared.txt").exists())

    def test_feature_without_upstream_can_merge_default(self):
        path = self.repo()
        self.feature(path, upstream=False)
        self.advance(path, "main", "origin", "main")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertIn("No upstream configured", out)
        self.assertEqual(self.git.actions("rebase"), [])
        self.assertTrue((path / "remote.txt").exists())

    def test_feature_tracking_default_merges_instead_of_rebasing_onto_default(self):
        path = self.repo()
        feature = self.feature(path, upstream=False)
        self.upstream(path, "feature/example", "origin", "main")
        self.advance(path, "main", "origin", "main")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.git.actions("rebase"), [])
        self.assertEqual(self.raw(path, "rev-parse", "HEAD^1"), feature)

    def test_upstream_on_another_remote_is_fetched(self):
        path = self.repo()
        oid = self.feature(path)
        self.remote(path, "team")
        self.remote_ref(path, "team", "topic", oid)
        self.upstream(path, "feature/example", "team", "topic")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([args[-1] for _, args in self.git.actions("fetch")], ["team", "origin"])

    def test_no_merge_and_no_submodule_flags(self):
        path = self.repo()
        feature = self.feature(path)
        self.advance(path, "main", "origin", "main")
        code, out, err = self.invoke("mgup", "--no-merge-default", "--no-submodules", path)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.raw(path, "rev-parse", "HEAD"), feature)
        self.assertEqual(self.git.actions("merge"), [])
        self.assertEqual(self.git.actions("submodule"), [])

    def test_dry_run_does_not_execute_mutations(self):
        path = self.repo()
        before = self.feature(path)
        self.advance(path, "main", "origin", "main")
        code, out, err = self.invoke("mgup", "--dry-run", path)
        self.assertEqual(code, 0, out + err)
        self.assertIn("would run: git fetch", out)
        self.assertIn("1 planned", out)
        for command in ("fetch", "rebase", "merge", "submodule"):
            self.assertEqual(self.git.actions(command), [])
        self.assertEqual(self.raw(path, "rev-parse", "HEAD"), before)

    def test_detached_unborn_and_in_progress_repositories_are_skipped(self):
        detached = self.repo("detached")
        self.raw(detached, "switch", "-q", "--detach")
        unborn = self.repo("unborn", empty=True)
        active = self.repo("active")
        (active / ".git/rebase-merge").mkdir()
        code, out, err = self.invoke("mgup", detached, unborn, active)
        self.assertEqual(code, 0, out + err)
        self.assertIn("detached HEAD", out)
        self.assertIn("repository has no commits", out)
        self.assertIn("operation in progress", out)
        self.assertIn("3 skipped", out)
        self.assertEqual(self.git.actions("fetch"), [])

    def test_unknown_default_does_not_modify_branch(self):
        path = self.repo()
        before = self.feature(path)
        (path / ".git/refs/remotes/origin/HEAD").unlink()
        self.remote_ref(path, "origin", "master", before)
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertIn("default branch unknown", out)
        self.assertEqual(self.git.actions("rebase"), [])
        self.assertEqual(self.raw(path, "rev-parse", "HEAD"), before)

    def test_default_can_be_discovered_after_fetch(self):
        path = self.repo()
        self.feature(path)
        default_oid = self.raw(path, "rev-parse", "main")
        (path / ".git/refs/remotes/origin/main").unlink()

        def fetched(repo, _):
            self.remote_ref(repo, "origin", "main", default_oid)
            return subprocess.CompletedProcess([], 0, b"", b"")

        self.git.fetch_hook = fetched
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 updated", out)

    def test_missing_upstream_is_reported_without_rebase(self):
        path = self.repo()
        self.feature(path)
        (path / ".git/refs/remotes/origin/feature/example").unlink()
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 0, out + err)
        self.assertIn("upstream unavailable", out)
        self.assertEqual(self.git.actions("rebase"), [])
        self.assertEqual(self.git.actions("merge"), [])

    def test_fetch_failure_stops_repository_but_continues_batch(self):
        bad, good = self.repo("bad"), self.repo("good")
        self.git.fetch_hook = lambda repo, _: subprocess.CompletedProcess([], int(repo == bad), b"", b"offline" if repo == bad else b"")
        code, out, err = self.invoke("mgup", bad, good)
        self.assertEqual(code, 1, out + err)
        self.assertIn("1 updated, 0 skipped, 1 failed", out)
        self.assertFalse(any(path == bad for path, _ in self.git.actions("submodule")))

    def test_merge_conflict_is_left_for_resolution_and_batch_continues(self):
        bad, good = self.repo("bad"), self.repo("good")
        self.feature(bad)
        self.commit(bad, "base.txt", "feature version\n")
        self.advance(bad, "main", "origin", "main", "base.txt", "remote version\n")
        code, out, err = self.invoke("mgup", bad, good)
        self.assertEqual(code, 1, out + err)
        self.assertTrue((bad / ".git/MERGE_HEAD").exists())
        self.assertIn("git merge --abort", out)
        self.assertIn("1 updated, 0 skipped, 1 failed", out)
        self.assertFalse(any(path == bad for path, _ in self.git.actions("submodule")))

    def test_rebase_conflict_is_left_for_resolution(self):
        path = self.repo()
        base = self.raw(path, "rev-parse", "HEAD")
        self.commit(path, "base.txt", "local version\n")
        self.advance(path, base, "origin", "main", "base.txt", "remote version\n")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 1, out + err)
        self.assertTrue((path / ".git/rebase-merge").exists())
        self.assertIn("git rebase --abort", out)
        self.assertEqual(self.git.actions("submodule"), [])

    def test_nested_dirty_submodule_is_detected_despite_ignore_config(self):
        parent = self.repo("parent")
        child = self.repo("parent/child", remote=False)
        nested = self.repo("parent/child/nested", remote=False)
        self.attach(child, nested)
        self.attach(parent, child)
        self.config(parent, "submodule.child.ignore", "all")
        self.config(child, "submodule.nested.ignore", "all")
        (nested / "untracked.txt").write_text("work")
        state = inspect(self.git, parent)
        self.assertIn("child/nested", state.submodules)
        code, out, err = self.invoke("mgup", parent)
        self.assertEqual(code, 0, out + err)
        self.assertIn("uncommitted changes", out)
        self.assertEqual(self.git.actions("fetch"), [])

    def test_clean_submodule_with_operation_in_progress_is_skipped(self):
        parent = self.repo("parent")
        child = self.repo("parent/child", remote=False)
        self.attach(parent, child)
        (child / ".git/rebase-merge").mkdir()
        code, out, err = self.invoke("mgup", parent)
        self.assertEqual(code, 0, out + err)
        self.assertIn("rebase in child", out)
        self.assertEqual(self.git.actions("fetch"), [])

    def test_submodule_failure_is_a_failed_update(self):
        path = self.repo()
        self.git.submodule_hook = lambda *_: subprocess.CompletedProcess([], 1, b"", b"missing submodule commit")
        code, out, err = self.invoke("mgup", path)
        self.assertEqual(code, 1, out + err)
        self.assertIn("missing submodule commit", out)
        self.assertIn("1 failed", out)

    def test_uninitialized_submodule_does_not_count_as_dirty(self):
        parent = self.repo("parent")
        source = self.repo("source", remote=False)
        oid = self.raw(source, "rev-parse", "HEAD")
        (parent / ".gitmodules").write_text('[submodule "missing"]\n\tpath = missing\n\turl = ssh://unreachable.invalid/missing\n')
        self.raw(parent, "add", ".gitmodules")
        self.raw(parent, "update-index", "--add", "--cacheinfo", f"160000,{oid},missing")
        self.raw(parent, "commit", "-qm", "Add uninitialized submodule")
        (parent / "missing").mkdir()
        self.assertFalse(inspect(self.git, parent).dirty)
        code, out, err = self.invoke("mgup", parent)
        self.assertEqual(code, 0, out + err)
        args = self.git.actions("submodule")[0][1]
        self.assertIn("--init", args)
        self.assertIn("--recursive", args)
        self.assertIn("--checkout", args)

    def test_help_contains_requested_examples(self):
        up_help = parser("mgup").format_help()
        stat_help = parser("mgstat").format_help()
        for example in ("mgup *", "mgup --dry-run *", "mgup --no-merge-default *", "mgup --no-submodules *"):
            self.assertIn(example, up_help)
        for example in ("mgstat *", "mgstat --non-default *", "mgstat --unpushed *", "mgstat --fetch --unpushed *"):
            self.assertIn(example, stat_help)
        find_help = parser("mgfind").format_help()
        for example in ("mgfind feature/search *", "mgfind '*ticket-123*' *",
                        "mgfind 'feature/*' '/path/to/projects/*'", "mgfind --local 'fix/*' *",
                        "mgfind --remotes 'origin/feature/*' *"):
            self.assertIn(example, find_help)


if __name__ == "__main__":
    unittest.main()
