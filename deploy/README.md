# Deploying to AWS EC2

`.github/workflows/deploy-aws.yml` deploys on every push to `main` (or manually from the Actions tab):

1. Opens SSH (port 22) in the EC2 security group for the GitHub runner's IP only.
2. Copies the code to `~/passportphotoapi` on the server with rsync. The server's `.env`, `venv/`, `uploads/` and `results/` are left alone.
3. Runs `deploy/deploy.sh` on the server: `pip install -r requirements.txt`, restarts the `passportphotoapi` systemd service, and waits for `/health`.
4. Closes port 22 again, even when the deploy fails.

The steps below only need doing once.

## 1. Launch the EC2 instance

- **AMI:** Ubuntu Server 24.04 LTS (ships Python 3.12, which `requirements.txt` needs) or Amazon Linux 2023 (install `python3.12`, see step 2).
- **Instance type:** `t3.medium` (4 GB) recommended. MediaPipe, MODNet and rembg/u2net are all loaded into memory. `t3.small` (2 GB) works only with swap.
- **Storage:** 20 GB gp3.
- **Security group:** allow **HTTP 80** and **HTTPS 443** from anywhere. Do **not** add an SSH rule for 0.0.0.0/0. The workflow adds and removes its own rule. Add a rule for your own IP if you want to SSH in manually.
- **Key pair:** create one and keep the `.pem`. It goes into the `EC2_SSH_KEY` secret.
- Allocate an **Elastic IP** and associate it with the instance so the address doesn't change.

## 2. Prepare the server

SSH in as the deploy user (`EC2_USER`), then install the system packages.

**Ubuntu 24.04:**

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip rsync curl nginx libgl1 libglib2.0-0
```

**Amazon Linux 2023.** The default `python3` here is 3.9, which is too old; `deploy.sh` uses `python3.12` when it's installed:

```bash
sudo dnf install -y python3.12 python3.12-pip rsync nginx mesa-libGL glib2
sudo systemctl enable --now nginx
```

Then, on either OS:

```bash
# Swap. Needed on t3.small, harmless on bigger instances.
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab

mkdir -p ~/passportphotoapi        # as the deploy user
nano ~/passportphotoapi/.env    # see below
```

`~/passportphotoapi/.env`:

```env
PHOTO_DEBUG=false
PHOTO_API_KEY=<long random key, same value as the frontend's PIX_PASSPORT_API_KEY>
PHOTO_CLOUDINARY_CLOUD_NAME=<cloud name>
PHOTO_CLOUDINARY_API_KEY=<api key>
PHOTO_CLOUDINARY_API_SECRET=<api secret>
PHOTO_CORS_ORIGINS=["https://photo-govt.com"]
```

You don't need to install the systemd service yourself. `deploy.sh` fills in `deploy/passportphotoapi.service` with the deploy user and folder and installs it on every deploy.

The deploy user must be able to run `sudo` **without a password**, because the deploy installs and restarts the service. The default `ubuntu` and `ec2-user` accounts already can. For a user you created yourself, for example `passora`:

```bash
sudo useradd -m -s /bin/bash passora
sudo mkdir -p /home/passora/.ssh
sudo cp ~/.ssh/authorized_keys /home/passora/.ssh/   # same key pair as the default user
sudo chown -R passora:passora /home/passora/.ssh && sudo chmod 700 /home/passora/.ssh
echo 'passora ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/passora
```

The app folder and `.env` then go in `/home/passora/passportphotoapi/`.

## 3. nginx + HTTPS

The API listens on `127.0.0.1:8000`. nginx exposes it at `/api/` and strips the prefix, so `https://<domain>/api/health` reaches `/health`. Create `/etc/nginx/sites-available/passportphotoapi`:

```nginx
server {
    listen 80;
    server_name api.example.com;          # your domain

    client_max_body_size 12M;             # uploads are capped at 10 MB by the app

    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;          # photo processing can be slow
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/passportphotoapi /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# Point the domain's DNS A record at the Elastic IP first, then:
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.example.com
```

## 4. IAM user for GitHub Actions

The workflow's AWS keys only need to add and remove SSH rules on that one security group. **IAM → Users → Create user** `github-deploy-passportphotoapi`, then attach this inline policy. Replace the region, account ID and security group ID:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupIngress"
      ],
      "Resource": "arn:aws:ec2:<region>:<account-id>:security-group/<sg-id>"
    }
  ]
}
```

Then go to **Security credentials → Create access key** (use case: *Application running outside AWS*).

## 5. GitHub secrets

Go to **Repo → Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | access key of the IAM user above |
| `AWS_SECRET_ACCESS_KEY` | its secret key |
| `AWS_REGION` | e.g. `ap-south-1` |
| `EC2_SECURITY_GROUP_ID` | e.g. `sg-0123456789abcdef0` |
| `EC2_HOST` | the Elastic IP |
| `EC2_USER` | the SSH user: `ubuntu` (Ubuntu), `ec2-user` (Amazon Linux), or your own user such as `passora` |
| `EC2_SSH_KEY` | full contents of the `.pem` file, including the `BEGIN`/`END` lines |

## 6. Deploy

Push to `main`, or go to **Actions → Deploy to AWS EC2 → Run workflow**. The first deploy can take 10+ minutes. It installs every dependency, downloads the u2net model and compiles numba kernels. Later deploys take about a minute.

Useful commands on the server:

```bash
sudo systemctl status passportphotoapi
sudo journalctl -u passportphotoapi -f      # live logs
curl http://127.0.0.1:8000/health
```
