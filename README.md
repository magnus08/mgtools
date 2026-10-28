# mgtools

Two small commands for working with many Git repositories sequentially:

- **mgup** fetches and rebases the current branch, merges the default branch into
  feature branches, and updates submodules recursively.
- **mgstat** shows checked-out branches and working-tree changes, with optional
  default-branch filtering and outgoing commit counts.

Requires Python 3.10+ and Git 2.38+. There are no third-party runtime dependencies.
The previous scripts are preserved in `original/`.

## Running and installing

Run the executables directly from this checkout:

```sh
./mgstat --help
./mgup --help
./mgstat /path/to/projects/*
```

To put the commands on your `PATH`, run these commands from this checkout:

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/mgup" ~/.local/bin/mgup
ln -s "$PWD/mgstat" ~/.local/bin/mgstat
```

Ensure `~/.local/bin` is on your `PATH`. These launchers resolve symlinks back to
the checkout, so keep the `mgtools/` Python directory beside them. If you already
have commands at those destinations, choose different names or replace the old
links yourself. Alternatively, install the package with `pipx install .` or
`python3 -m pip install .` inside a virtual environment.

## Examples

These examples are also included in the corresponding command's `--help` output.

| Command | Behavior |
| --- | --- |
| `mgup *` | Update repositories sequentially, including recursive submodules |
| `mgup --dry-run *` | Show intended operations without fetching or changing anything |
| `mgup --no-merge-default *` | Update the current branch without merging the default branch |
| `mgup --no-submodules *` | Skip submodule updates |
| `mgstat *` | Show branch and working-tree status |
| `mgstat --non-default *` | Show repositories checked out on a non-default branch |
| `mgstat --unpushed *` | Include outgoing commit counts and comparison branch |
| `mgstat --fetch --unpushed *` | Refresh remote information before checking outgoing commits |

Options can be combined:

```sh
mgstat --non-default --unpushed *
mgup --no-merge-default --no-submodules service-*
mgup --default-branch develop project
mgstat --remote upstream --non-default *
```

Paths are repository **roots**. Both shell-expanded globs (`mgup *`) and quoted
patterns (`mgup 'service-*'`) work. Paths containing spaces and linked worktrees
are supported. Files and directories that are not repository roots are skipped;
the commands do not search directories recursively for repositories. Duplicate
paths and symlinks to the same worktree are processed once. Distinct worktrees
are processed separately. With no paths, the current directory is used. Put `--`
before paths whose names begin with a dash, for example `mgstat -- *`.

## Status

```text
REPOSITORY  BRANCH          WORKTREE                   PUSH STATUS (cached)
api         main            clean                      up to date (origin/main)
web         feature/search  staged:1 modified:2 new:1  ahead:3 behind:0 (origin/feature/search)
worker      fix/retry       clean                      target unknown
```

The worktree column distinguishes staged changes, unstaged modifications,
untracked files, conflicts, and changed submodules. A file with both staged and
unstaged changes contributes to both counts. Untracked files are counted
individually, including files inside untracked directories. Ignored files are
excluded. Submodules are also inspected recursively, including when Git's usual
status configuration hides submodule changes. File counts describe the root
repository; the submodule count identifies changed submodule checkouts.

`--non-default` hides known default-branch checkouts. Detached HEADs and
repositories whose default branch is unknown remain visible, with explanatory
labels. An empty result still prints the table header.

`--unpushed` **adds a column; it does not filter rows**. It compares the current
branch to Git's configured push destination (`@{push}`), which can differ from
the pull upstream. `ahead` counts commits present locally but absent from that
destination's cached ref; `behind` counts commits in that ref but absent locally.
A branch with no resolvable push target, a missing remote-tracking ref, a detached
HEAD, or no commits is reported as `target unknown`, never as fully pushed.

Ordinary status checks never fetch. Counts describe locally cached remote refs,
so they may be stale. `--fetch` first fetches the relevant default and upstream
remotes, plus the push remote when `--unpushed` is enabled. Each remote is fetched
once per repository. Authentication stays interactive, including hardware-key
prompts. If a fetch fails, available cached status is still shown with a warning
and a nonzero exit code. Fetch progress and diagnostics go to stderr; the table
goes to stdout.

## Updates

For each repository, `mgup`:

1. Checks the root repository and initialized submodules. Staged, unstaged,
   untracked, or submodule changes cause `SKIPPED: uncommitted changes` **before
   any fetch**. A different submodule checkout also counts as a change. Existing
   merge/rebase/cherry-pick/revert/bisect operations, detached root HEADs, and
   repositories without commits are skipped.
2. Fetches the current branch's upstream remote and, when needed, the remote
   supplying the default branch. Fetch does not recurse into submodules.
3. Rebases onto the configured upstream if it contains commits the current
   branch does not already include. Feature branches use `--rebase-merges` to
   preserve existing merge structure. Automatic stashing and updates to other
   local branch refs are disabled.
4. On a non-default branch, merges the remote default branch using `--no-edit`
   and a message such as `Merge origin/main into feature/search`. Fast-forwards
   are allowed. A feature branch tracking the default branch itself receives
   this merge instead of rebasing onto the default branch. A feature branch
   with no upstream can still receive the merge. A configured upstream that
   remains unavailable after fetching causes a skip.
5. Runs `git submodule update --init --recursive --checkout --jobs 1`. This
   initializes missing submodules and checks out the commits recorded in the
   updated repository, including nested submodules. It does not advance
   submodules to arbitrary remote tips. Submodule checkouts normally have a
   detached HEAD.

Rebase or merge conflicts stop that repository's update and leave the operation
in place for manual resolution. The output includes continue/abort guidance.
Other repositories are still processed. A later failure does not undo earlier
successful steps; for example, a submodule failure can follow a completed rebase.
Nothing is pushed automatically.

`--dry-run` performs local inspection and prints planned commands without
executing them. Because it cannot see future fetched refs, it cannot predict
conflicts or fully resolve missing default-branch information. Dirty and other
ineligible repositories are still reported as skipped.

## Default branches

Both commands use the same detection order:

1. `--default-branch BRANCH` for this invocation.
2. Per-repository Git configuration: `git config mgtools.defaultBranch develop`.
3. The selected remote's locally recorded `HEAD`, usually `origin/HEAD`.
4. An unambiguous existing remote-tracking `main` or `master` branch.

`--remote NAME` changes the remote used for this detection and for default-branch
merges; it defaults to `origin`. It does not change the current branch's upstream
or push configuration. Custom names such as `trunk` work through remote HEAD or
an explicit override. If both `main` and `master` exist without a usable remote
HEAD, the commands do not guess. `mgstat` marks the default as unknown; `mgup`
skips the branch update when it cannot establish the required merge target.
Use `--no-merge-default` to perform only the upstream update in that situation.

The detection uses local refs; if the server's default branch was renamed but
the recorded remote HEAD is stale, use an override or correct that local HEAD.
The default branch needs an upstream to be updated. A feature branch without an
upstream requires a known remote default branch unless it is being skipped.

## Exit codes and testing

- `0`: successful inspection/update, including expected skips and empty filters.
- `1`: a Git operation or path lookup failed; other repositories were processed.
- `2`: invalid arguments, or no repository roots were found.
- `130`: interrupted by Ctrl-C; inspect the last repository for unfinished work.

`mgup` ends with updated (or planned), skipped, and failed counts.

Run the test suite with:

```sh
python3 -m unittest discover -s tests -v
```

Tests create disposable repositories and exercise real local status, rebase,
merge, and worktree operations. Cached remote refs are seeded as fixture files.
Fetch and submodule-update operations are intercepted; the suite does not
contact remote servers or request hardware-key interaction.
