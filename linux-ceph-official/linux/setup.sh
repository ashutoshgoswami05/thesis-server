#!/usr/bin/env bash
# Prepares a fresh Ubuntu machine to run the evaluation framework.
#
# Usage:  bash linux/setup.sh
#
# Installs Docker from Docker's own repository rather than the distribution's,
# because Ubuntu's docker.io package lags and the compose plugin is packaged
# separately there. Creates the virtual environment and the shared network the
# compose files attach to.

set -eu
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Docker ==="
if ! command -v docker >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
                          docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER"
  echo "added $USER to the docker group -- log out and back in before running the campaign"
else
  echo "already installed: $(docker --version)"
fi

echo "=== Python ==="
sudo apt-get install -y python3 python3-venv python3-pip
if [ ! -x "$REPO/.venv/bin/python" ]; then
  python3 -m venv "$REPO/.venv"
fi
"$REPO/.venv/bin/python" -m pip install --upgrade pip
"$REPO/.venv/bin/python" -m pip install -r "$REPO/requirements.txt"
echo "venv ready: $("$REPO/.venv/bin/python" --version)"

echo "=== shared network ==="
docker network inspect thesis-net >/dev/null 2>&1 || docker network create thesis-net
echo "thesis-net present"

echo "=== disk ==="
df -h "$REPO" | tail -1
echo "the campaign needs roughly 40 GB of images and volumes; 100 GB is comfortable"

echo
echo "next:  bash linux/install.sh        # put the Linux scripts in place"
echo "then:  bash tests/run_fidelity_all.sh"
