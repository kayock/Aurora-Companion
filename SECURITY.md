# Security policy

Aurora is experimental, self-hosted software. Its privacy controls and token-based LAN
access are designed for a **single trusted user**, not as a multi-user security boundary.

## Reporting a vulnerability

Please **do not post exploitable vulnerabilities, private credentials, access tokens,
or personal conversation databases in public issues**. Use GitHub's private
vulnerability reporting feature if enabled for this repository; otherwise contact
the maintainer privately before sharing details.

## Safe deployment defaults

- Keep Aurora on localhost unless you need LAN access. When enabling LAN access,
  use Aurora's token authentication and trusted HTTPS certificates.
- Never expose the dashboard or API directly to the public Internet.
- Keep real `config.yaml`, `.env`, certificates, database files and backups out
  of Git; `.gitignore` excludes common local runtime files.
- Treat microphone, camera and screen access as optional, explicit permissions.
- Review [docs/PORTABLE_DEPLOY.md](docs/PORTABLE_DEPLOY.md) before enabling systemd
  deployment or network listeners.

This project does not promise enterprise-grade hardening or a production security audit.
