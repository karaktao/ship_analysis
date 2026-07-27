#!/usr/bin/env bash
set -eu

read -r -s -p "Paste EuRIS API token: " token
printf "\n"

if [[ -z "${token}" ]]; then
  echo "Token was empty; no change made." >&2
  exit 2
fi

token_length=${#token}
if (( token_length % 2 == 0 )); then
  half_length=$((token_length / 2))
  if [[ "${token:0:half_length}" == "${token:half_length}" ]]; then
    echo "The token was pasted twice; no change made." >&2
    exit 4
  fi
fi

TOKEN_TO_TEST="${token}" python3 -c '
import json
import os
from urllib.request import Request, urlopen

url = (
    "https://www.eurisportal.eu/visuris/api/TracksV2/GetTracksByBBoxV2"
    "?minLon=4.85&minLat=51.85&maxLon=4.90&maxLat=51.90"
)
request = Request(
    url,
    headers={
        "Accept": "application/json",
        "Authorization": "Bearer " + os.environ["TOKEN_TO_TEST"],
        "User-Agent": "ship-analysis-token-check",
    },
)
with urlopen(request, timeout=20) as response:
    json.load(response)
    print("Token validated: HTTP", response.status)
'

case "${token}" in
  *[!A-Za-z0-9._~+/=-]*)
    echo "Token contains unsupported environment-file characters; no change made." >&2
    exit 3
    ;;
esac

umask 027
temporary="/etc/ship-analysis/collector.env.new"
printf 'EURIS_API_TOKEN=%s\n' "${token}" > "${temporary}"
chown root:shipanalysis "${temporary}"
chmod 640 "${temporary}"
mv "${temporary}" /etc/ship-analysis/collector.env
unset token

systemctl restart ship-analysis-collector.service
sleep 2
systemctl is-active --quiet ship-analysis-collector.service
echo "Token installed and collector restarted successfully."
