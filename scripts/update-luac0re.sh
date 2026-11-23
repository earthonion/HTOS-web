#!/usr/bin/env bash
# Checks for new Luac0re releases and updates prebuilt files + template version string.
# Intended to run as a cronjob, e.g.:
#   0 */6 * * * /opt/htos/scripts/update-luac0re.sh >> /var/log/luac0re-update.log 2>&1

set -euo pipefail

REPO="Gezine/Luac0re"
HTOS_DIR="${HTOS_DIR:-/opt/htos}"
PREBUILT_DIR="$HTOS_DIR/prebuilt/luac0re"
TEMPLATE="$HTOS_DIR/templates/luac0re.html"
VERSION_FILE="$PREBUILT_DIR/.current_version"
TMP_DIR=$(mktemp -d)

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# Fetch latest release info
RELEASE_JSON=$(curl -sf "https://api.github.com/repos/$REPO/releases/latest") || {
    echo "$(date): Failed to fetch release info"
    exit 1
}

LATEST_TAG=$(echo "$RELEASE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])")
RELEASE_NAME=$(echo "$RELEASE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")
ASSET_URL=$(echo "$RELEASE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['assets'][0]['browser_download_url'])")

# Check current version
CURRENT_TAG=""
if [ -f "$VERSION_FILE" ]; then
    CURRENT_TAG=$(cat "$VERSION_FILE")
fi

if [ "$LATEST_TAG" = "$CURRENT_TAG" ]; then
    echo "$(date): Already up to date ($CURRENT_TAG)"
    exit 0
fi

echo "$(date): Updating from '${CURRENT_TAG:-none}' to '$LATEST_TAG'"

# Download and extract
curl -sfL "$ASSET_URL" -o "$TMP_DIR/luac0re.zip" || {
    echo "$(date): Failed to download release"
    exit 1
}

unzip -qo "$TMP_DIR/luac0re.zip" -d "$TMP_DIR/extracted" || {
    echo "$(date): Failed to extract zip"
    exit 1
}

# Update prebuilt files
mkdir -p "$PREBUILT_DIR"
for cusa_dir in "$TMP_DIR"/extracted/CUSA*/; do
    cusa=$(basename "$cusa_dir")
    mkdir -p "$PREBUILT_DIR/$cusa"
    cp -f "$cusa_dir"* "$PREBUILT_DIR/$cusa/"
    echo "$(date): Updated $cusa"
done

# Update version string in template (only the <h1> tag)
sed -i "s|<h1>Luac0re [^<]*</h1>|<h1>${RELEASE_NAME}</h1>|" "$TEMPLATE"
echo "$(date): Updated template to '$RELEASE_NAME'"

# Save current version
echo "$LATEST_TAG" > "$VERSION_FILE"

# Restart service
systemctl restart htos 2>/dev/null && echo "$(date): Restarted htos service" || echo "$(date): Note: could not restart htos (run as root?)"

echo "$(date): Done"
