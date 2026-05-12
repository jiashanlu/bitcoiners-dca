# Deployment guide

Two recommended ways to run bitcoiners-dca: Docker Compose (easiest) or
a system service (for users running their own Umbrel/Raspberry Pi). Pick one.

---

## Option A — Docker Compose (recommended)

Works on any system with Docker. Both the scheduler daemon AND the web
dashboard run as separate containers sharing the same data volume.

```bash
# 1. Clone the repo
git clone https://github.com/jiashanlu/bitcoiners-dca.git
cd bitcoiners-dca

# 2. Copy + edit config
cp config.example.yaml config.yaml
$EDITOR config.yaml             # set strategy + enable your exchanges

# 3. Copy + edit secrets
cp .env.example .env
$EDITOR .env                    # paste your exchange API keys

# 4. Start
docker compose up -d

# 5. Verify
docker compose logs -f dca      # follow daemon logs
open http://localhost:8000      # open dashboard
```

**The data lives in `./data/dca.db` on your host filesystem.** Back this up.

**Updating to a new version:**
```bash
git pull
docker compose down
docker compose up -d --build
```

---

## Option B — systemd service (Linux / Umbrel)

For users running the bot on their own Umbrel or RPi without Docker.

### 1. Install

```bash
python3.11 -m venv /opt/bitcoiners-dca/venv
source /opt/bitcoiners-dca/venv/bin/activate
pip install bitcoiners-dca       # or pip install -e /path/to/source

cp config.example.yaml /etc/bitcoiners-dca/config.yaml
$EDITOR /etc/bitcoiners-dca/config.yaml
```

### 2. Environment file

`/etc/bitcoiners-dca/dca.env`:
```
OKX_API_KEY=...
OKX_API_SECRET=...
OKX_API_PASSPHRASE=...
TG_BOT_TOKEN=...
```

Set permissions:
```bash
chmod 600 /etc/bitcoiners-dca/dca.env
```

### 3. systemd unit

`/etc/systemd/system/bitcoiners-dca.service`:
```ini
[Unit]
Description=bitcoiners-dca DCA scheduler daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dca
Group=dca
WorkingDirectory=/opt/bitcoiners-dca
EnvironmentFile=/etc/bitcoiners-dca/dca.env
ExecStart=/opt/bitcoiners-dca/venv/bin/bitcoiners-dca run --config /etc/bitcoiners-dca/config.yaml
Restart=always
RestartSec=10

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/bitcoiners-dca/data /opt/bitcoiners-dca/reports
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bitcoiners-dca
sudo systemctl status bitcoiners-dca
journalctl -u bitcoiners-dca -f
```

### 4. Optionally run dashboard as a second service

`/etc/systemd/system/bitcoiners-dca-dashboard.service`:
```ini
[Unit]
Description=bitcoiners-dca read-only dashboard
After=bitcoiners-dca.service

[Service]
Type=simple
User=dca
Group=dca
WorkingDirectory=/opt/bitcoiners-dca
EnvironmentFile=/etc/bitcoiners-dca/dca.env
ExecStart=/opt/bitcoiners-dca/venv/bin/bitcoiners-dca dashboard \
    --config /etc/bitcoiners-dca/config.yaml \
    --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## Option C — Umbrel community app (TODO)

When the project hits stable, we'll package it as an Umbrel community app
for one-click install on home Bitcoin nodes. PRs welcome.

---

## Pre-launch checklist

Before going live:

- [ ] Set `dry_run: true` and run for 24 hours — verify no errors, balances poll, drafts make sense
- [ ] Verify auto-withdraw address is YOUR hardware wallet (echo it from config and confirm)
- [ ] Test Telegram bot — send a manual test from your account to verify chat_id
- [ ] Confirm trade-only API key permissions on each exchange (no withdraw scope unless you want auto-withdraw)
- [ ] Whitelist the hardware-wallet address in each exchange's withdrawal settings
- [ ] Back up `data/dca.db` to your home node / NAS / cloud (encrypted)
- [ ] Set `dry_run: false` in config.yaml
- [ ] Restart the service: `docker compose restart dca` or `sudo systemctl restart bitcoiners-dca`
- [ ] Watch the first real cycle live (cron time + 1 min)

---

## Monitoring

```bash
# Live logs
docker compose logs -f dca                # Docker
journalctl -u bitcoiners-dca -f           # systemd

# CLI status
bitcoiners-dca status --config /path/to/config.yaml

# Dashboard
open http://localhost:8000
```

---

## Backups

The entire state of the bot is in `data/dca.db`. Back this up daily:

```bash
# Simple cron — copy DB to home node Bitcoin storage
0 3 * * * sqlite3 /opt/bitcoiners-dca/data/dca.db ".backup /backups/dca-$(date +\%F).db"
```

---

## Security model

- Run the daemon as a dedicated unprivileged user (`dca`)
- Mount config + secrets read-only into Docker
- API keys should have **trade-only** scope on each exchange. Only enable
  withdrawal scope if you're using auto-withdraw AND have whitelisted
  destination addresses.
- The dashboard binds to 127.0.0.1 by default — only accessible from the
  host. Don't expose to the network without auth.
- For remote monitoring: use SSH port-forwarding (`ssh -L 8000:localhost:8000 user@host`) rather than opening the dashboard port.
