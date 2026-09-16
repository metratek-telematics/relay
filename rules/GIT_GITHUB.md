# Git & GitHub Rules

## Local safety
Never use destructive commands against user work:
- git reset --hard;
- git clean -fd;
- git checkout -- .;
- force checkout over changes;
- destructive rebase.

Do not discard pre-existing staged, unstaged, or untracked files.

## Task branch
Work on the assigned isolated branch/worktree.
Keep branch changes scoped to the task.

## Commit quality
Before committing:
- inspect git status;
- inspect diff;
- remove temporary/debug/reference files;
- verify no secrets;
- ensure only intended files changed.

Commit message should describe the task/result, not "updates" or "fix stuff".

## Pull request
PR should:
- have a clear task-oriented title;
- summarize behavior changed;
- mention meaningful architecture decisions;
- list verification performed;
- call out known limitations/pre-existing failures;
- link/close source issue when appropriate.

Do not claim verification not performed.

## Never
- push default/protected branch directly;
- force push unless explicitly authorized;
- merge automatically;
- close issues manually when the PR closing keyword can handle it;
- modify unrelated GitHub issues/PRs.
