# Agent Guidelines

## Git commits

- Do not make commits or push to upstream unless explicitly instructed by the user.
- Keep commit messages to a single line (no multi-line messages).
- Do not add "Generated with" lines, "Co-Authored-By" lines, or any AI tool attribution to commit messages.

## Before pushing to GitHub

- Test all affected configurations locally (if possible) before pushing to GitHub.
- Cancel all old/running jobs on GitHub Actions before pushing new changes.
- Monitor jobs after pushing until they complete and confirm they pass.
- Do not wait for all jobs to finish before starting fixes — use `gh run view --log-failed` to fetch failure logs as soon as a job fails, diagnose immediately, and push fixes as soon as you have a clear picture of all failing cases.

## Documentation

- Keep `README.md` up to date with any user facing changes made to the code.

## Changelog

- `docs/changelog.md` is only for user-facing functionality changes (new features, bug fixes, improvements).
- Do not add CI, build system, tooling, test infrastructure, or other internal changes to the changelog.
- Keep entries concise and focused on what changed for users of the application.

## Scope discipline

- Do not remove any capability or support unless explicitly asked by the user.
- Do not weaken or delete tests without explicit direction.

## Test coverage

- Make sure test cases test all features and configurations of the project.
- When adding new features or fixing bugs, add or update tests to cover the new behaviour.
