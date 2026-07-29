#!/bin/bash

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}🌐 Configuring Nginx...${NC}"

# Load .env
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    DOMAIN=api-agent-dev.thaink2.fr
    PORT=8005
fi

DOMAIN=${DOMAIN:-api-agent-dev.thaink2.fr}
PORT=${PORT:-8005}

echo -e "${GREEN}✅ Domain: ${DOMAIN}, Port: ${PORT}${NC}"

NGINX_CONF="/etc/nginx/sites-available/$DOMAIN"
NGINX_ENABLED="/etc/nginx/sites-enabled/$DOMAIN"
ACCESS_LOG="/var/log/nginx/${DOMAIN}-access.log"
ERROR_LOG="/var/log/nginx/${DOMAIN}-error.log"

# Install nginx if needed
if ! command -v nginx &> /dev/null; then
    sudo apt-get update && sudo apt-get install -y nginx
fi

# Backup existing config
[ -f "$NGINX_CONF" ] && sudo cp "$NGINX_CONF" "${NGINX_CONF}.backup.$(date +%Y%m%d-%H%M%S)"

# Create nginx config
sudo tee $NGINX_CONF > /dev/null <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    access_log $ACCESS_LOG;
    error_log $ERROR_LOG;
    client_max_body_size 50M;
    location / {
        proxy_pass http://localhost:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;

        proxy_connect_timeout 60s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
    }

    # ADK /run (synchronous, used by webhook background tasks)
    # Needs longer timeout because the agent processes the full session history.
    location = /run {
        proxy_pass http://localhost:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        proxy_connect_timeout 60s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;
    }

    # SSE streaming: covers both passes through nginx
    #   1st pass: browser  -> /api/adk/run_sse (custom router)
    #   2nd pass: aiohttp  -> /run_sse          (ADK native endpoint)
    location ~ ^/(api/adk/)?run_sse {
        proxy_pass http://localhost:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        proxy_connect_timeout 60s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;

        # Disable buffering for SSE
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
    }
}
EOF

# Create log files
sudo touch $ACCESS_LOG $ERROR_LOG
sudo chown www-data:adm $ACCESS_LOG $ERROR_LOG
sudo chmod 640 $ACCESS_LOG $ERROR_LOG

# Enable site
[ ! -L "$NGINX_ENABLED" ] && sudo ln -s $NGINX_CONF $NGINX_ENABLED

# Test and reload
sudo nginx -t
sudo systemctl reload nginx

echo -e "${GREEN}✅ Nginx configured for ${DOMAIN}:${PORT}${NC}"
echo -e "${YELLOW}💡 View logs: sudo tail -f ${ACCESS_LOG}${NC}"
