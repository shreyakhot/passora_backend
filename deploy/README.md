# Deployment (CI/CD)

The API runs on an AWS EC2 instance (Ubuntu 24.04, Python 3.12) as the systemd service `passora-backend`, from a git checkout at `/home/ubuntu/passora_backend`. nginx on port 80 proxies to uvicorn on `127.0.0.1:8000`.

## How a change goes live

1. **Push to `main`.**
2. **CI** (`.github/workflows/ci.yml`, job `checks`) runs on GitHub. It installs `requirements.txt` and runs `scripts/ci_check.py`, which:
   - validates every country config;
   - imports the app;
   - crops `test_face.jpg` in crop-only mode to the US passport size.
3. **CD** runs on the server. `passora-autodeploy.timer` runs `deploy/autodeploy.sh` every 2 minutes. When `origin/main` has a new commit whose `checks` job passed, it:
   1. checks the commit out;
   2. runs `pip install` if `requirements.txt` changed;
   3. refreshes the systemd units from `deploy/`;
   4. restarts `passora-backend`;
   5. waits up to 5 minutes for `/health`.
4. **Rollback.** If the API does not become healthy, the script rolls back to the previous commit and never retries the failed one. A commit whose CI failed is never deployed.

A change is usually live 5-10 minutes after the push: about 5 minutes for CI, up to 2 minutes for the next check, and about 20 seconds for the restart.

The server pulls from the public GitHub repo, so there are no deploy secrets, SSH keys or open SSH ports involved. A new public IP after an instance restart does not affect it.

## Watching and controlling deploys

On the server:

```bash
journalctl -u passora-autodeploy -f          # deploy log
sudo systemctl start passora-autodeploy      # check for a new commit now
systemctl list-timers passora-autodeploy.timer
sudo systemctl status passora-backend        # the API
sudo journalctl -u passora-backend -f        # API log
curl http://127.0.0.1:8000/health
```

To undo a bad change, revert it on `main` and push. The revert deploys like any other commit.

If a commit's CI was cancelled or failed and you want it deployed anyway, re-run the CI workflow for it from the Actions tab.

## One-time server setup

These steps are already done on the current server. They are only needed for a new one.

1. **Launch** Ubuntu 24.04 with at least 2 GB of RAM. Allow HTTP 80 and HTTPS 443. Allow SSH only from your own IP, when you need it. Attach an **Elastic IP**, so the address the frontend uses (`PHOTO_API_URL`) does not change when the instance restarts.
2. **Install packages and clone the repo** as `ubuntu`:

   ```bash
   sudo apt update && sudo apt install -y python3-venv python3-pip git curl nginx libgl1 libglib2.0-0
   git clone https://github.com/shreyakhot/passora_backend.git ~/passora_backend
   cd ~/passora_backend
   python3 -m venv venv && venv/bin/pip install -r requirements.txt
   nano .env
   ```

   `.env`:

   ```env
   PHOTO_DEBUG=false
   PHOTO_API_KEY=<long random key, the same value as the frontend's PHOTO_API_KEY>
   PHOTO_CLOUDINARY_CLOUD_NAME=<cloud name>
   PHOTO_CLOUDINARY_API_KEY=<api key>
   PHOTO_CLOUDINARY_API_SECRET=<api secret>
   PHOTO_CORS_ORIGINS=["https://photo-govt.com"]
   ```

3. **Install the services:** `bash deploy/autodeploy.sh install`. This:
   - adds a 2 GB swap file if there is none;
   - installs `passora-backend.service`, `passora-autodeploy.service` and `passora-autodeploy.timer` from `deploy/`, filling in the user and folder;
   - starts the timer.

   Then start the API: `sudo systemctl enable --now passora-backend`.

4. **Configure nginx** to proxy port 80 to `http://127.0.0.1:8000`, with `client_max_body_size 12M` (uploads are capped at 10 MB) and `proxy_read_timeout 120s`. For HTTPS, point a domain at the Elastic IP and run `certbot --nginx`.

The deploy user needs passwordless `sudo` to restart the service and install the units. The default `ubuntu` user already has it.
