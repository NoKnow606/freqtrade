#!/usr/bin/env bash
# One-shot Debian host preparation for the regime-driven freqtrade deployment.
# Tested target: Debian 12 (bookworm) / 13 (trixie), x86_64 or arm64, run as root
# or via sudo. Safe to re-run.
#
#   curl -fsSL <raw url>/server-setup.sh | sudo bash
#   # or: sudo bash server-setup.sh
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-freqtrade}"
REPO_URL="${REPO_URL:-}"            # optional: git repo containing user_data/
INSTALL_DIR="${INSTALL_DIR:-/opt/freqtrade}"

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo bash $0)"; exit 1; }

log "1/7 base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl gnupg git openssl ufw fail2ban unattended-upgrades \
  chrony jq sqlite3 rsync

log "2/7 time sync (exchange signatures reject clock drift > 30s)"
systemctl enable --now chrony
chronyc makestep >/dev/null 2>&1 || true

log "3/7 docker engine + compose plugin (official apt repo)"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
# json-file logs would grow unbounded otherwise
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" }
}
EOF
systemctl restart docker

log "4/7 deploy user (non-root, in docker group)"
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

log "5/7 firewall: ssh only; freqtrade API stays on 127.0.0.1"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

log "6/7 fail2ban for sshd + unattended security upgrades"
cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

log "7/7 project directory"
mkdir -p "$INSTALL_DIR"
if [ -n "$REPO_URL" ] && [ ! -d "$INSTALL_DIR/.git" ]; then
  git clone "$REPO_URL" "$INSTALL_DIR"
fi
mkdir -p "$INSTALL_DIR/user_data"/{signals,logs,data,backtest_results}
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$INSTALL_DIR"

# pre-pull so first `compose up` is fast
sudo -u "$DEPLOY_USER" docker pull freqtradeorg/freqtrade:stable >/dev/null

cat <<EOF

Done. Next, as user '$DEPLOY_USER':

  sudo -iu $DEPLOY_USER
  cd $INSTALL_DIR/user_data/deploy
  cp .env.example .env
  sed -i "s|^FREQTRADE__API_SERVER__JWT_SECRET_KEY=.*|FREQTRADE__API_SERVER__JWT_SECRET_KEY=\$(openssl rand -hex 32)|" .env
  sed -i "s|^FREQTRADE__API_SERVER__WS_TOKEN=.*|FREQTRADE__API_SERVER__WS_TOKEN=\$(openssl rand -hex 32)|" .env
  \$EDITOR .env          # set API_SERVER password; exchange keys can stay empty for dry-run
  docker compose up -d
  docker compose logs -f

Access FreqUI from your laptop:  ssh -L 8080:127.0.0.1:8080 $DEPLOY_USER@<server>
EOF
