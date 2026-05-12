# bitcoiners-dca · Umbrel community app

Files in this directory package the bot for one-click install on Umbrel home
Bitcoin nodes.

## Files

- `umbrel-app.yml` — app manifest (id, version, description, gallery refs)
- `docker-compose.yml` — Umbrel-flavored compose: 2 services (daemon + web)
  sharing a single APP_DATA_DIR
- `exports.sh` — environment shim (currently a no-op)
- `gallery/` — screenshots referenced by `gallery:` in the manifest

## Pre-publish checklist

1. **Push a public Docker image** matching the version in `umbrel-app.yml`.
   The compose file references `ghcr.io/jiashanlu/bitcoiners-dca:0.3.0` — build
   and push it from the project root:

   ```bash
   docker buildx build \
     --platform linux/amd64,linux/arm64 \
     -t ghcr.io/jiashanlu/bitcoiners-dca:0.3.0 \
     -t ghcr.io/jiashanlu/bitcoiners-dca:latest \
     --push .
   ```

   Umbrel's most common architectures are arm64 (RPi 4/5) and amd64 (Intel
   NUC, Beelink, etc.) — build both.

2. **Add screenshots** to `gallery/` (1.png, 2.png, 3.png). Recommended:
   dashboard overview, trades log, backtest result.

3. **Test locally** by symlinking this directory into a local Umbrel community
   apps repo and running `umbrel-cli app install bitcoiners-dca`.

4. **Submit a PR** to https://github.com/getumbrel/umbrel-apps with this
   directory added under `bitcoiners-dca/`. Update `submission:` in the
   manifest to point at the PR URL.

## First-run flow (for end-users)

On first install the app starts with no `config.yaml`. The user needs to:

1. Open a terminal on the Umbrel and copy the example config:
   ```bash
   cd ~/umbrel/app-data/bitcoiners-dca/data
   mkdir -p ../config
   docker compose exec daemon cat /app/config.example.yaml > ../config/config.yaml
   ```
2. Edit `~/umbrel/app-data/bitcoiners-dca/.env` with their exchange API keys
   (token for BitOasis; key+secret+passphrase for OKX; key+secret for Binance).
3. Restart the app from the Umbrel UI.
4. Open the web dashboard from Umbrel; everything runs from there.

We can streamline this with a first-run wizard in the dashboard later — for v0.3
it's CLI-friendly admin only.
