# Agent instructions

## Branch workflow

- Use a branch-based workflow for all work.
- Never make changes or commits directly on `main`, except for the repository's initial commit.
- Before changing files, create or switch to a dedicated branch for the task, such as `feat/<task>`, `fix/<task>`, or `chore/<task>`.
- Keep related work on its task branch and merge it into `main` through a pull request after validation.

## Commit messages and frequency

- Use Conventional Commit messages compatible with Release Please: `<type>[optional scope][!]: <description>`.
- Examples: `feat(search): add document lookup`, `fix(ingest): handle empty files`, and `ci: add automated checks`.
- Use `!` or a `BREAKING CHANGE:` footer to identify breaking changes.
- Make frequent, small, coherent commits as meaningful pieces of work are completed and validated.
- Do not accumulate unrelated changes or save all work for one large, big-bang commit.

## Continuous integration

- Set up CI at the beginning of the project, before substantial feature development. If CI is missing in an existing project, establish it early in the task.
- Configure CI to validate pull requests and updates to `main`, using the repository's hosting platform.
- Automate as much relevant validation as practical, including linting, formatting checks, type checks, tests, and builds where applicable.
- Extend CI as new components and validation needs are introduced.
- Run appropriate checks locally during development and use CI to validate changes before merging.
- Resolve CI failures before merging. If a check cannot run, report the limitation rather than claiming validation passed.
