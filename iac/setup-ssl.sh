#!/bin/bash

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}🔒 Setting up SSL...${NC}"

# Load .env
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    DOMAIN=api-agent-dev.thaink2.fr
fi

DOMAIN=${DOMAIN:-api-agent-dev.thaink2.fr}
EMAIL=${SSL_EMAIL:-farid.azouaou@thaink2.fr}

echo -e "${GREEN}✅ Domain: ${DOMAIN}, Email: ${EMAIL}${NC}"

# Install certbot if needed
if ! command -v certbot &> /dev/null; then
    sudo apt-get update && sudo apt-get install -y certbot python3-certbot-nginx
fi

# Get or renew certificate for specific subdomain
if sudo certbot certificates 2>/dev/null | grep -A 2 "Certificate Name: $DOMAIN" | grep -q "Domains: $DOMAIN"; then
    echo -e "${YELLOW}Certificate exists for $DOMAIN, renewing...${NC}"
    sudo certbot renew --cert-name $DOMAIN --nginx --non-interactive
else
    echo -e "${YELLOW}Obtaining new certificate for subdomain: $DOMAIN${NC}"
    sudo certbot --nginx \
        -d $DOMAIN \
        --cert-name $DOMAIN \
        --non-interactive \
        --agree-tos \
        --email $EMAIL \
        --redirect
fi

# Test and reload
sudo nginx -t
sudo systemctl reload nginx

# Enable auto-renewal
sudo systemctl enable certbot.timer 2>/dev/null || true
sudo systemctl start certbot.timer 2>/dev/null || true


# Enable HTTP/2 on the certbot-generated 443 listener (idempotent + portable)
# Use the legacy "listen 443 ssl http2;" form — works on nginx 1.9.5+ through 1.26+.
# The newer "http2 on;" directive requires nginx >= 1.25 and breaks on Ubuntu 22.04
# default (nginx 1.18) and Ubuntu 24.04 (nginx 1.24) — see SCRUM ticket on CI failure.
NGINX_CONF="/etc/nginx/sites-available/$DOMAIN"
if [ -f "$NGINX_CONF" ]; then
    # Clean up any previous "http2 on;" insertion (broken on nginx <1.25)
    if sudo grep -qE '^\s*http2 on;\s*$' "$NGINX_CONF"; then
        echo -e "${YELLOW}🧹 Removing legacy http2 on; (nginx <1.25 compat)${NC}"
        sudo sed -i '/^\s*http2 on;\s*$/d' "$NGINX_CONF"
    fi
    # Ensure "listen 443 ssl;" carries the http2 flag
    if ! sudo grep -qE 'listen 443 ssl http2' "$NGINX_CONF"; then
        echo -e "${YELLOW}🚀 Enabling HTTP/2 on ${DOMAIN}...${NC}"
        sudo sed -i 's|listen 443 ssl;|listen 443 ssl http2;|' "$NGINX_CONF"
        if sudo nginx -t 2>&1 | grep -q 'successful'; then
            sudo systemctl reload nginx
            echo -e "${GREEN}✅ HTTP/2 enabled${NC}"
        else
            echo -e "${RED}❌ nginx config invalid after HTTP/2 patch — reverting${NC}"
            sudo sed -i 's|listen 443 ssl http2;|listen 443 ssl;|' "$NGINX_CONF"
            sudo nginx -t
        fi
    fi
fi

echo -e "${GREEN}✅ SSL configured for https://${DOMAIN}${NC}"
