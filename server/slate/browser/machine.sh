#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl tzdata xvfb x11-utils x11-xserver-utils \
  openbox libpulse0 libgl1-mesa-dri libglx-mesa0 libegl-mesa0 fonts-noto-color-emoji fonts-dejavu-core
curl -fsSLo /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y -q /tmp/chrome.deb
rm /tmp/chrome.deb
google-chrome-stable --version
install -d -m 755 /etc/opt/chrome/policies/managed
echo '{ "CommandLineFlagSecurityWarningsEnabled": false, "RestoreOnStartup": 1, "PromotionsEnabled": false }' \
  >/etc/opt/chrome/policies/managed/slate.json
ln -sfn /usr/share/zoneinfo/America/Los_Angeles /etc/localtime
echo America/Los_Angeles >/etc/timezone
bash "$here/voxl/build.sh" /usr/local/lib/voxl
rm -rf /var/lib/apt/lists/*
