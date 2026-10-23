# 6.1.0

- Replace one-shot Nginx installer with root-only Python management menu and installed `sshbot-manager` command.
- Reuse Apache or Nginx; dedicated VirtualHost, localhost backend, port/domain/owner-path guards; preserve unrelated services.
- Eliminate automatic legacy-service shutdown and deleteWebhook behavior. Reject bot tokens with active webhooks.
- Add domain/SSL/menu updates, token and owner changes, access management, runtime/resource limits, logs, health, SSL renewal tools, backups, restore and optional legacy import.
- Isolate ACME account/certificates/work/logs and renewal timer from existing sites and shared Certbot configuration.
- Add isolated code releases, pre-change backups, file/symlink rollback and Telegram menu rollback on post-switch failure.
- Add CPU/RAM ceilings and per-user connection/upload/session settings.
- Close SSH transport before asynchronous teardown to avoid leaking connections after WebSocket cancellation.
- Expand tests and rewrite Persian installation guide for shared-server deployment.
