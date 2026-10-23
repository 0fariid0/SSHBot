# Validation report

Base: 891832e0c8cec3abafbc04bc0a98f29d095a393b.

Executed locally:
- Nine pytest cases, including real local AsyncSSH server connections with password and Ed25519 key, PTY input/output and SFTP upload.
- Reject changed host fingerprint; one-time WebSocket tickets; reject foreign Origin; reject logged-out tickets; terminate live session after logout.
- HMAC tampering, duplicate fields, stale/future Telegram assertions rejected.
- User isolation, allowlist and revocation checks, owner-only management, credential masking/encryption, referenced credential deletion blocked.
- Idempotent legacy Fernet migration and preservation of original source data.
- Chromium UI smoke checks at 1440×1000 and 390×844: search, host creation, password/key form modes, access screen, terminal opening/control keys/fullscreen and viewport overflow. These tests use explicit fixture API data, not real Telegram login.
- Actual Uvicorn HTTP health and WebSocket handshake/rejection check with websockets 15.0.1.
- Python compile and JavaScript syntax checks; Bash syntax validation.

Not executed here:
- Full apt/systemd/Nginx installer on a live Ubuntu VPS.
- Public DNS / ACME certificate issuance or renewals.
- Real Telegram token and Telegram iOS/Android/Desktop WebView integration.
- Long-running load/penetration testing, all SSH server implementations and all key formats.

No claim of production deployment or complete Termius feature parity is made.
