# SSHBot Web 6.1 validation

Base upstream commit: 891832e0c8cec3abafbc04bc0a98f29d095a393b.

## Executed for 6.1

- 29 pytest tests passed.
- Real local AsyncSSH password/key connections, terminal input/output, SFTP upload and overwrite refusal.
- HMAC tampering, duplicate fields, stale/future auth, user isolation, revoked users, credential masking/encryption, fingerprint mismatch, ticket reuse and logout invalidation.
- Session duration and API/UI limit settings; legacy migration preservation.
- Installer template checks for Apache WebSocket routing order, loopback backend, separate domain and systemd resource limits.
- Mocked installation/change scenarios: Apache detection, unknown listener refusal, occupied port refusal, domain/wildcard collision, existing webhook rejection without deletion.
- Transaction rollback preserves file bytes, modes and symlinks; failed health/ACME restores prior domain/configuration; successful domain change retains prior route until certificate phase and updates Telegram menu.
- Assertions that manager never stops/restarts/disables Apache, Nginx, legacy sshbot or MariaDB during the tested change flows. Apache is reloaded, SSHBot alone restarted.
- Release file permission test ensures the service user can execute venv entry points even when installation uses umask 077.
- Python compilation, Bash and JavaScript syntax checks.
- SSH connection teardown now closes transport before cancellable awaits; test server fixture explicitly cleans its connections/tasks.

## Prior v6.0 validation retained

- Chromium desktop/mobile UI smoke checks at 1440×1000 and 390×844, including search, forms, users, terminal and fullscreen.
- Real Uvicorn HTTP and WebSocket transport smoke check.

## Not verified in this environment

- Full apt/systemd installation or a live Apache/Nginx configtest/reload. The environment cannot run the required package/service setup. Manager integration tests mock host commands; they are not a live-server installation test.
- Real DNS and Let's Encrypt issuance/renewals, or Telegram token/WebView operation.
- Repeated Chromium run for 6.1: browser binaries were no longer available. The visual layout is unchanged; UI limit wiring received syntax and backend tests.
- Unknown hosting-panel, Docker, multi-layer proxy or custom webserver layouts; these may require manual review and are conservatively rejected where detected.

Package/module installation is not transactional. File rollback concerns registered SSHBot paths, not arbitrary package changes or external Telegram/network failures. Review the installation guide before applying to a production server.

## GitHub bootstrap update

34 tests pass after adding five bootstrap tests: piped invocation with a complete fixture archive, incomplete package rejection, traversal rejection, download failure, and local checkout installation path. Temporary download cleanup is verified. `bash -n install.sh` passes. Downloads are mocked; no live GitHub deployment, interactive remote SSH session, systemd installation, or SSL issuance was performed by these tests.

## Finglish menu update

All 34 tests pass. The complete manager source is ASCII-only. An AST comparison against the previous packaged manager, with string constants normalized, confirms that only text changed. Python compilation passes.

## Workspace layout update — 2026-10-27

34 backend/bootstrap/manager tests pass. Node JavaScript syntax validation passes. Playwright Chromium checks pass at 1440×1000, 500×760 (compact Telegram-sized window), 390×844 with mocked system/content safe insets, and 844×390 landscape. Checks include host search/create, credential forms, owner users, fullscreen API request, legacy-client/unsupported fullscreen fallback, separate terminal page, retained WebSocket connections while navigating all other sections, switching two session tabs, closing a background session without navigating, closing the last session, and absence of document overflow. Telegram viewport height changes are simulated on mobile. Screenshots were inspected for desktop, compact/mobile and terminal layouts.

These tests use explicit fixture APIs, a fake SSH WebSocket and mocked Telegram APIs. They verify interface behavior, not fullscreen availability in every real Telegram client. No new deployment or live-device Telegram fullscreen test was performed. Existing backend integration tests still cover real local AsyncSSH connections.

## Clipboard and shortcuts update — 2026-10-27

34 backend/bootstrap/manager tests pass. Node syntax validation passes for app.js and terminal-controls.js. Existing UI smoke tests pass at four desktop/mobile/compact/landscape sizes. Additional Playwright checks use real xterm, mocked SSH transport and a mocked clipboard: selected Ctrl+C copies with no SSH input, unselected Ctrl+C emits exactly one SIGINT, Ctrl+X/Ctrl+D and toolbar buttons emit their expected bytes, smart right-click copies/pastes, keyboard paste has no duplicate send, native clipboard events and bracketed-paste framing are honored, multiline paste can be cancelled, large pastes are rejected, clipboard denial opens working manual dialogs, duplicate bindings are rejected, custom bindings and font size survive reload and remain per-user, delayed paste is blocked after leaving the terminal, and tab navigation/focus/close hotkeys work. Settings dialogs were visually inspected at desktop and mobile widths.

No real Telegram clipboard-permission behavior or OS-reserved key interception was verified. Those depend on the client; the application exposes a manual clipboard fallback and editable bindings.
