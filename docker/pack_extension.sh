#!/usr/bin/env bash
# Pack the CDP extension as a CRX3 and write the Chrome force-install policy.
set -euo pipefail

EXT_DIR="${1:-/app/cf-turnstile-bypass/proxy-extensions/cdp}"
PEM="${2:-/opt/solver/extension.pem}"
OUT_CRX="${3:-/opt/solver/extension.crx}"
ID_SCRIPT="${4:-/opt/solver/docker/extension_id.py}"

mkdir -p "$(dirname "${PEM}")" "$(dirname "${OUT_CRX}")"

if [[ ! -f "${PEM}" ]]; then
    openssl genrsa 2048 | openssl pkcs8 -topk8 -nocrypt -out "${PEM}"
fi

EXT_ID="$(python3 "${ID_SCRIPT}" --id-only "${PEM}")"
EXT_KEY="$(python3 "${ID_SCRIPT}" --key-only "${PEM}")"

python3 - <<PY
import json
from pathlib import Path
manifest_path = Path("${EXT_DIR}") / "manifest.json"
manifest = json.loads(manifest_path.read_text())
manifest["key"] = """${EXT_KEY}"""
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
print(f"[pack] Wrote manifest key for extension id ${EXT_ID}")
PY

# Packing needs a display even though we never want the solver itself headless.
timeout 90 xvfb-run -a -s "-screen 0 800x600x24" \
    google-chrome-stable \
        --no-sandbox \
        --disable-gpu \
        --disable-dev-shm-usage \
        --pack-extension="${EXT_DIR}" \
        --pack-extension-key="${PEM}"

PACKED_CRX="${EXT_DIR}.crx"
if [[ ! -f "${PACKED_CRX}" ]]; then
    echo "[pack] Chrome did not write ${PACKED_CRX}" >&2
    exit 1
fi
mv "${PACKED_CRX}" "${OUT_CRX}"

cat > /opt/solver/updates.xml <<EOF
<?xml version='1.0' encoding='UTF-8'?>
<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>
  <app appid='${EXT_ID}'>
    <updatecheck codebase='http://127.0.0.1:9377/extension.crx' version='1.0' prodversionmin='1.0'/>
  </app>
</gupdate>
EOF

mkdir -p /etc/opt/chrome/policies/managed
# Do not force-install the extension. A force-installed debugger override
# replaces the real site and flags Cloudflare. PAGE_OVERRIDE=1 loads it
# via --load-extension at runtime instead.
cat > /etc/opt/chrome/policies/managed/solver.json <<EOF
{
  "CommandLineFlagSecurityWarningsEnabled": false,
  "DeveloperToolsAvailability": 1
}
EOF

echo "${EXT_ID}" > /opt/solver/extension_id.txt
echo "[pack] Extension id ${EXT_ID}"
echo "[pack] CRX ${OUT_CRX}"
echo "[pack] Skipped force-install (use PAGE_OVERRIDE=1 --load-extension)"
