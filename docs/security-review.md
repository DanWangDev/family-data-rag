# Credential and privacy review

**English** | [简体中文](security-review.zh-CN.md)

Review date: 2026-10-09 (Europe/London). The final recheck was based on primary workspace commit `fca6229` and its then-uncommitted changes. Newly added app/chat/retrieve, launcher, and backup code was included. This is a review of a code snapshot, not a guarantee against leaks in future commits or across the entire computer. Business code under development by Claude was not modified during this review.

## Scope and methods

- Checked code, documentation, configuration, CI, and uncommitted files in the primary workspace, including temporary debug scripts created during development.
- Scanned the working directory with Gitleaks 8.30.1, including local files ignored by Git; excluded `.git/`, the installed `.venv/`, and `__pycache__/`. No NAS data was imported, and the operating system credential store was not read. Scanner checks of binary data do not replace a review of document contents.
- Used `gitleaks git --log-opts="--all --full-history"` to inspect commits on all locally reachable refs. This scan does not cover remote refs that have not been fetched, GitHub Actions secrets, or the entire hosting account.
- Separately scanned local Git configuration, commit messages, and staged diffs; manually checked configuration, SDK endpoint/authentication-header inheritance, logging, and CI credential persistence.
- Downloaded the scanner from its official release and verified the publisher's SHA-256 checksum. Detection output used `--redact=100`; raw reports stayed in a temporary directory outside the repository and were neither committed nor uploaded. No real credentials were used to attempt service logins.
- [Gitleaks detection and commands](https://github.com/gitleaks/gitleaks) are rule-based; they cannot identify every custom secret or verify whether a token is valid.

## Results

| Check | Result |
| --- | --- |
| Working directory Gitleaks scan | No matches found |
| Locally reachable Git history scan | No matches found |
| Local Git configuration and commit messages | No matches found |
| Staged diff | No matches found; there were no staged changes at scan time |
| Current configuration examples and CI | No hardcoded credentials found |
| `python -m selftest` | Local synthetic checks passed |
| Scanner positive control | A synthetic token with no service authorization was generated outside the repository; rule detection succeeded and matched values were fully redacted in output/reports |
| SDK environment inheritance reproduction | Synthetic environment variables were used to inspect client configuration, confirming endpoint and authentication-header inheritance; no network requests were made |
| New CI workflow | YAML, minimal permissions, and configuration preventing credential persistence were checked; it had not yet run on a GitHub runner |

The model IDs, quantization settings, and hardware information in `bench.md` are not authentication credentials; values such as `deadbeef` and `cafebabe` in tests are synthetic model digests. No API key, password, or private key matches were found in committed or current code. The runtime paths below still need to be closed; these results do not justify a zero-egress claim.

## Runtime issues requiring developer fixes

### 1. Default Ollama client inherits endpoints and authentication headers (priority)

Locations: `embed_batch()`, `complete()`, and `model_digest()` in `models.py`.

These functions use `ollama.embed/chat/list`, the SDK's default client. The currently installed SDK reads `OLLAMA_HOST` during initialization and automatically adds Bearer Authorization when `OLLAMA_API_KEY` is present. Project code does not restrict the endpoint to loopback or clear this authentication header.

If the startup environment inherits a remote endpoint and API key, text and credentials may be sent there. HTTP proxy environments, redirects, and cloud model selection also need explicit controls. This is a code-path risk; the review found no actual tokens and observed no real data leaving the machine.

Recommended implementation: a shared local client that explicitly pins or strictly validates `http://127.0.0.1:11434`; disables environment proxies and redirects; removes Authorization before requests are sent, rather than changing the environment after constructing the client; rejects cloud model configuration; and verifies the service's cloud-disabled status. SDK client construction itself inherits API keys, so specifying only `host` is insufficient to remove the authentication header.

Acceptance criteria: use a synthetic key, remote `OLLAMA_HOST`, proxy environments, and mock transport in offline tests to assert that every call permits only the intended loopback endpoint, sends no Authorization, and rejects cloud configuration. Cover all embed/chat/list paths.

The dedicated-terminal configuration and removal of process-local `OLLAMA_API_KEY` described in the README are temporary operating measures, not substitutes for the code hardening above.

### 2. Raw exceptions may enter logs

Location: `_log(... ({e}))` in `build_index()` in `ingest.py`.

Exceptions from parsers, the SDK, or file reads may contain URLs, server responses, or input fragments. Normal logs containing only paths/counts do not guarantee that all failure paths exclude credentials and family text.

Recommended implementation: log run/file IDs and controlled error categories by default; keep raw exceptions in a protected local diagnostic channel; redact logs before sharing them.

Acceptance criteria: construct parser/SDK exceptions with synthetic secret strings, capture stderr, and assert that those strings, authentication headers, and source text do not appear.

### 3. The launcher does not yet verify actual listening interfaces

`run.bat` / `run.ps1` and the Web UI already exist. `Test-OllamaConfig` checks `cloud.disabled` at `/api/status`, but successful access through `127.0.0.1` does not prove that the service binds only to that interface: an existing service bound to all interfaces also passes this check. The launcher also does not remove inherited API keys or handle proxy environments.

Recommended implementation: inspect all actual TCP listening addresses and owning processes for port 11434, permitting only the agreed loopback addresses; test with synthetic listener lists to ensure wildcard interfaces are rejected. Outside the code, BitLocker, NAS permissions, cloud synchronization exclusions, and network behavior still require runtime verification. The target architecture must not be interpreted as deployed protection.

After client and error-log hardening is complete, verify local network behavior with representative data. Separate online model/dependency installation from runtime; during runtime, allow local Ollama and the specified NAS, and block unintended destinations.

## Preventive measures added in this review

- Extended `.gitignore` to cover `.env`, common private key/certificate containers, credential JSON files, and local scan reports; retained secret-free `.env.example` / `.env.sample` templates.
- Added `.github/workflows/secrets.yml`: scans all reachable history and the working tree on every push/PR; pins the scanner version; verifies the download checksum; grants read-only permissions; prevents checkout from persisting Git credentials; and does not upload reports.
- Clarified current implementations, planned capabilities, and unresolved security issues in the README, and provided local startup configuration.

Ignore rules only prevent ordinary accidental additions; they cannot undo history, prevent `git add -f`, or identify secrets in arbitrarily named files. CI detection happens after a push; scan locally before the first push. If real credentials are discovered in the future, revoke/rotate them first, then address Git history and related logs rather than only deleting the current file.
