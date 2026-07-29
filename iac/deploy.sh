#!/bin/bash

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}🚀 Starting FastAPI deployment...${NC}"

# Load .env
if [ -f .env ]; then
    echo -e "${YELLOW}📝 Loading .env...${NC}"
    set -a
    source .env
    set +a
    echo -e "${GREEN}✅ Loaded: ${ENVIRONMENT:-dev} on port ${PORT:-8005}${NC}"
else
    PORT=8005
    ENVIRONMENT=development
fi

PORT=${PORT:-8005}
ENVIRONMENT=${ENVIRONMENT:-development}

# Install required Linux packages
if ! dpkg -l | grep -q libpq-dev || ! command -v curl &> /dev/null; then
    echo -e "${YELLOW}📦 Installing curl, libpq-dev and python3-dev...${NC}"
    sudo apt-get update && sudo apt-get install -y curl libpq-dev python3-dev
fi

# SCEI ARs module needs Microsoft ODBC Driver 18 for SQL Server.
# Skipped for tenants that do not enable the SCEI feature flag.
if [ "${SCEI_AR_FEATURES_ENABLED:-false}" = "true" ]; then
    if ! dpkg -l | grep -q msodbcsql18; then
        echo -e "${YELLOW}📦 Installing unixodbc + msodbcsql18 for SCEI SQL Server access...${NC}"
        UBUNTU_VER=$(lsb_release -rs)
        # Microsoft repo currently tops out at 24.04; fall back if the host runs newer.
        case "$UBUNTU_VER" in
            22.04|24.04) MS_REPO_VER="$UBUNTU_VER" ;;
            *) MS_REPO_VER="24.04" ;;
        esac
        curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft.gpg
        echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/ubuntu/${MS_REPO_VER}/prod $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/mssql-release.list > /dev/null
        sudo apt-get update
        sudo ACCEPT_EULA=Y DEBIAN_FRONTEND=noninteractive apt-get install -y unixodbc unixodbc-dev msodbcsql18
    fi
fi

# Install uv if needed
if ! command -v uv &> /dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

UV_PATH="$HOME/.local/bin/uv"
[ ! -f "$UV_PATH" ] && UV_PATH="$HOME/.cargo/bin/uv"

# Setup venv and install (--frozen: use existing lockfile, don't re-resolve)
[ ! -d ".venv" ] && $UV_PATH venv
$UV_PATH sync --frozen
$UV_PATH pip install . --no-deps

# Stop legacy nohup uvicorn processes left from previous deployments
docker stop fastapi-app 2>/dev/null || true
docker rm fastapi-app 2>/dev/null || true

# ── MCP Toolbox for Databases (managed as a dedicated systemd service) ──
# We deliberately do NOT spawn the toolbox as a child of this script: when
# the th2agent backend service is restarted via systemctl, control-group
# cleanup would kill any toolbox descendant. Running it as its own unit
# (th2agent-toolbox.service) keeps it stable across backend redeploys
# and lets the backend reload it via `sudo systemctl restart` when the
# UI saves a new MCP-DB config.
TOOLBOX_PORT=${TOOLBOX_PORT:-5000}
TOOLBOX_BIN="${WORKDIR:-$(pwd)}/toolbox"
TOOLBOX_DIR="${WORKDIR:-$(pwd)}"
TOOLBOX_BASE_CONFIG="${TOOLBOX_CONFIG:-tools.yaml}"
TOOLBOX_DYNAMIC_OVERLAY="tools.dynamic.yaml"
TOOLBOX_EFFECTIVE_CONFIG="$TOOLBOX_BASE_CONFIG"
if [ -f "$TOOLBOX_DYNAMIC_OVERLAY" ]; then
    echo -e "${BLUE}🧩 Merging $TOOLBOX_DYNAMIC_OVERLAY (backend-generated) on top of $TOOLBOX_BASE_CONFIG${NC}"
    python3 - "$TOOLBOX_BASE_CONFIG" "$TOOLBOX_DYNAMIC_OVERLAY" tools.merged.yaml <<'PYM'
import sys, yaml
base = yaml.safe_load(open(sys.argv[1])) or {}
overlay = yaml.safe_load(open(sys.argv[2])) or {}
for key in ('sources', 'tools', 'toolsets'):
    base.setdefault(key, {})
    base[key].update(overlay.get(key, {}) or {})
yaml.dump(base, open(sys.argv[3], 'w'), sort_keys=False)
PYM
    TOOLBOX_EFFECTIVE_CONFIG="tools.merged.yaml"
fi
TOOLBOX_CONFIG_FILE="$TOOLBOX_EFFECTIVE_CONFIG"

if [ -f "$TOOLBOX_BIN" ] && [ -f "$TOOLBOX_CONFIG_FILE" ]; then
    TOOLBOX_UNIT="th2agent-toolbox.service"
    TOOLBOX_UNIT_FILE="/etc/systemd/system/${TOOLBOX_UNIT}"
    TOOLBOX_SUDOERS="/etc/sudoers.d/th2agent-toolbox"

    echo -e "${YELLOW}📦 Installing/updating ${TOOLBOX_UNIT}...${NC}"
    sudo tee "${TOOLBOX_UNIT_FILE}" > /dev/null <<UNIT
[Unit]
Description=MCP Toolbox for Databases (th2agent)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=${TOOLBOX_DIR}
EnvironmentFile=${TOOLBOX_DIR}/.env
ExecStart=${TOOLBOX_BIN} --tools-file ${TOOLBOX_DIR}/${TOOLBOX_CONFIG_FILE} --address 0.0.0.0 --port ${TOOLBOX_PORT}
Restart=on-failure
RestartSec=2
KillMode=process
StandardOutput=append:${TOOLBOX_DIR}/toolbox-systemd.log
StandardError=append:${TOOLBOX_DIR}/toolbox-systemd.log

[Install]
WantedBy=multi-user.target
UNIT

    # Allow the backend (running as user 'ubuntu') to restart the toolbox
    # without a password — single command, single unit, nothing else.
    sudo tee "${TOOLBOX_SUDOERS}" > /dev/null <<'SUDO'
ubuntu ALL=(root) NOPASSWD: /bin/systemctl restart th2agent-toolbox.service, /bin/systemctl reload-or-restart th2agent-toolbox.service
SUDO
    sudo chmod 0440 "${TOOLBOX_SUDOERS}"

    sudo systemctl daemon-reload
    sudo systemctl enable "${TOOLBOX_UNIT}" >/dev/null 2>&1 || true
    sudo systemctl restart "${TOOLBOX_UNIT}"

    for i in 1 2 3 4 5 6; do
        if curl -sf "http://localhost:${TOOLBOX_PORT}/" > /dev/null 2>&1; then
            echo -e "${GREEN}✅ MCP Toolbox ready ($(systemctl show -p MainPID --value ${TOOLBOX_UNIT}))${NC}"
            break
        fi
        sleep 2
    done
    if ! curl -sf "http://localhost:${TOOLBOX_PORT}/" > /dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  Toolbox not responding yet. Check: journalctl -u ${TOOLBOX_UNIT} -n 50${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  MCP Toolbox skipped (missing ${TOOLBOX_BIN} or ${TOOLBOX_CONFIG_FILE})${NC}"
fi

# ── FastAPI as systemd service (auto-restart on crash) ────
SERVICE_NAME="th2agent"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
WORKDIR="$(pwd)"
UVICORN_BIN="${WORKDIR}/.venv/bin/uvicorn"

# Clean up legacy nohup pid file (no longer used)
LEGACY_PID_FILE="fastapi-${ENVIRONMENT}.pid"
if [ -f "$LEGACY_PID_FILE" ]; then
    LEGACY_PID=$(cat "$LEGACY_PID_FILE" 2>/dev/null)
    [ -n "$LEGACY_PID" ] && kill -0 "$LEGACY_PID" 2>/dev/null && kill "$LEGACY_PID" 2>/dev/null || true
    rm -f "$LEGACY_PID_FILE"
fi
# Free the port if any orphan listener remains
lsof -ti:${PORT} 2>/dev/null | xargs -r kill -9 2>/dev/null || true

echo -e "${YELLOW}🛠  Writing systemd unit ${SERVICE_NAME}.service...${NC}"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=th2agent FastAPI backend
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=${WORKDIR}
ExecStart=/bin/bash -c 'set -a && source ${WORKDIR}/.env && set +a && exec ${UVICORN_BIN} th2agent.main:app --host 0.0.0.0 --port "\${PORT:-${PORT}}"'
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" 2>/dev/null || true
sudo systemctl restart "$SERVICE_NAME"

echo -e "${GREEN}✅ ${SERVICE_NAME}.service started${NC}"

# Wait for health check
for i in {1..10}; do
    if curl -f http://localhost:${PORT}/health 2>/dev/null; then
        echo -e "${GREEN}🎉 Deployed! http://localhost:${PORT}/docs${NC}"
        echo -e "${YELLOW}💡 Logs: sudo journalctl -u ${SERVICE_NAME} -f${NC}"
        exit 0
    fi
    sleep 3
done

echo -e "${RED}❌ Failed to start. Check: sudo journalctl -u ${SERVICE_NAME} -n 100${NC}"
sudo systemctl status "$SERVICE_NAME" --no-pager | head -15 || true
exit 1
