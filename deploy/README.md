# Deploy notes — devhacks.navraj.me

Host: `homelab` (`ssh home`), path `/home/nav/devhacks3/track2-study-match`.

```text
Cloudflare ──► Caddy (devhacks.navraj.me) ──► 127.0.0.1:3278 (uvicorn, systemd "social-match")
```

`/etc/caddy/Caddyfile` already contains `devhacks.navraj.me { reverse_proxy localhost:3278 }`, so Caddy needs
no change. The app binds loopback only.

## Files that matter

| Path | Purpose |
| --- | --- |
| `socialmatch.conf` | All runtime config + secrets, mode 600. Loaded by systemd `EnvironmentFile=`. |
| `social_match.db` | SQLite database (WAL). Created on first start; back this up if you want the demo data. |
| `deploy/social-match.service` | The systemd unit. |

There is deliberately no `.env` — the app reads plain environment variables, and `EnvironmentFile=` does not care
about the filename. If a tool insists on `.env`, `cp socialmatch.conf .env && chmod 600 .env` is equivalent.

## Install (one-time, needs sudo)

```bash
sudo install -m 644 /home/nav/devhacks3/track2-study-match/deploy/social-match.service \
    /etc/systemd/system/social-match.service
sudo systemctl daemon-reload
sudo systemctl enable --now social-match
```

## SELinux (Fedora/RHEL hosts) — required, or the service cannot start

Two labels must be `bin_t`, because `unconfined_service_t` may execute `bin_t` but not `user_home_t`
(the code dir) or `container_file_t` (uv-managed interpreters). Symptoms if skipped:
`Failed to load environment files: Permission denied`, then `status=203/EXEC`.

```bash
sudo semanage fcontext -a -t bin_t "/home/nav/devhacks3/track2-study-match(/.*)?"
sudo semanage fcontext -a -t bin_t "/home/nav/.local/share/uv/python(/.*)?"
sudo restorecon -Rv /home/nav/devhacks3/track2-study-match /home/nav/.local/share/uv/python
sudo systemctl restart social-match
```

`restorecon` is idempotent, so re-running after a redeploy is safe. Quick temporary alternative
(reverts on the next full relabel): `sudo chcon -R -t bin_t <path>`.

Symptom reference: `ls -lZ <file>` shows the type as the third field of the context.

## Operate

```bash
systemctl status social-match --no-pager
journalctl -u social-match -f          # live logs
sudo systemctl restart social-match    # after a redeploy
```

Redeploy code (no sudo):

```bash
rsync -az --delete --exclude '.venv/' --exclude '__pycache__/' --exclude '*.db*' \
  --exclude 'socialmatch.conf' --exclude '.git/' \
  ~/active-code/devhacks3/track2-study-match/ home:/home/nav/devhacks3/track2-study-match/
ssh home 'cd /home/nav/devhacks3/track2-study-match && uv sync && sudo systemctl restart social-match'
```

## Invite code

`REGISTRATION_CODE` lives in `socialmatch.conf`. It is the signup gate — rotate it there and restart.

## Health

```bash
curl -s http://127.0.0.1:3278/health          # on the host
curl -s https://devhacks.navraj.me/health     # through Caddy + Cloudflare
```
