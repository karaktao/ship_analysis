#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

app_root="/opt/ship_analysis"
app_dir="${app_root}/app"
service_user="shipanalysis"

if [[ ! -f "${app_dir}/pyproject.toml" ]]; then
  echo "Project source is missing from ${app_dir}." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git nginx python3 python3-venv xz-utils

if ! id "${service_user}" >/dev/null 2>&1; then
  useradd \
    --system \
    --home-dir "${app_root}" \
    --shell /usr/sbin/nologin \
    "${service_user}"
fi

node_version="$(
  python3 - <<'PY'
import json
from urllib.request import urlopen

with urlopen("https://nodejs.org/dist/index.json", timeout=30) as response:
    releases = json.load(response)
print(next(item["version"] for item in releases if item["version"].startswith("v22.")))
PY
)"

case "$(uname -m)" in
  x86_64) node_arch="x64" ;;
  aarch64|arm64) node_arch="arm64" ;;
  *)
    echo "Unsupported Node.js architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

node_dir="/opt/node-${node_version}-linux-${node_arch}"
if [[ ! -x "${node_dir}/bin/node" ]]; then
  archive="/tmp/node-${node_version}-linux-${node_arch}.tar.xz"
  curl --fail --location --silent --show-error \
    "https://nodejs.org/dist/${node_version}/node-${node_version}-linux-${node_arch}.tar.xz" \
    --output "${archive}"
  tar -xJf "${archive}" -C /opt
  rm -f "${archive}"
fi

for executable in node npm npx corepack; do
  ln -sfn "${node_dir}/bin/${executable}" "/usr/local/bin/${executable}"
done

install -d -m 0750 -o "${service_user}" -g "${service_user}" "${app_root}"
install -d -m 0750 -o "${service_user}" -g "${service_user}" "${app_dir}/data"
install -d -m 0750 -o root -g "${service_user}" /etc/ship-analysis
if [[ ! -f /etc/ship-analysis/collector.env ]]; then
  install -m 0640 -o root -g "${service_user}" /dev/null \
    /etc/ship-analysis/collector.env
fi

chown -R "${service_user}:${service_user}" "${app_dir}"

if [[ ! -x "${app_dir}/.venv/bin/python" ]]; then
  runuser -u "${service_user}" -- python3 -m venv "${app_dir}/.venv"
fi
runuser -u "${service_user}" -- \
  "${app_dir}/.venv/bin/python" -m pip install --disable-pip-version-check -e "${app_dir}"
runuser -u "${service_user}" -- \
  "${app_dir}/.venv/bin/ship-analysis" \
  --config "${app_dir}/config/regions.toml" init-db

runuser -u "${service_user}" -- \
  env PATH="/usr/local/bin:/usr/bin:/bin" \
  npm --prefix "${app_dir}/dashboard" ci
runuser -u "${service_user}" -- \
  env PATH="/usr/local/bin:/usr/bin:/bin" \
  npm --prefix "${app_dir}/dashboard" run build

install -m 0644 "${app_dir}/deploy/systemd/ship-analysis-collector.service" \
  /etc/systemd/system/ship-analysis-collector.service
install -m 0644 "${app_dir}/deploy/systemd/ship-analysis-maintenance.service" \
  /etc/systemd/system/ship-analysis-maintenance.service
install -m 0644 "${app_dir}/deploy/systemd/ship-analysis-retention.service" \
  /etc/systemd/system/ship-analysis-retention.service
install -m 0644 "${app_dir}/deploy/systemd/ship-analysis-retention.timer" \
  /etc/systemd/system/ship-analysis-retention.timer
install -m 0644 "${app_dir}/deploy/systemd/ship-analysis-dashboard-api.service" \
  /etc/systemd/system/ship-analysis-dashboard-api.service
install -m 0644 "${app_dir}/deploy/systemd/ship-analysis-dashboard-web.service" \
  /etc/systemd/system/ship-analysis-dashboard-web.service
install -m 0644 "${app_dir}/deploy/nginx/ship-analysis.conf" \
  /etc/nginx/sites-available/ship-analysis.conf
install -m 0700 "${app_dir}/deploy/set-euris-token.sh" \
  /usr/local/sbin/set-euris-token
ln -sfn /etc/nginx/sites-available/ship-analysis.conf \
  /etc/nginx/sites-enabled/ship-analysis.conf
rm -f /etc/nginx/sites-enabled/default

nginx -t
bash "${app_dir}/deploy/ensure-swap.sh"
systemctl daemon-reload
systemctl enable ship-analysis-collector.service
systemctl enable ship-analysis-maintenance.service
systemctl enable --now ship-analysis-retention.timer
systemctl enable ship-analysis-dashboard-api.service
systemctl enable ship-analysis-dashboard-web.service
systemctl restart ship-analysis-collector.service
systemctl restart ship-analysis-maintenance.service
systemctl restart ship-analysis-dashboard-api.service
systemctl restart ship-analysis-dashboard-web.service
systemctl enable nginx.service
systemctl restart nginx.service

echo "Ship Analysis deployment completed."
