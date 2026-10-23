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
