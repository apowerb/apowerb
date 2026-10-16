#!/bin/sh
# Installe dans /opt/venv les trois dépendances natives qui décident du choix
# de base : pyodbc (unixODBC), psycopg2-binary (libpq embarquée) et torch CPU
# (th2forecast). torch peut manquer pour une version de Python sans roue :
# on le note au lieu d'échouer, la mesure Trivy reste utile.
set -e
PY="$1"
uv venv --python "$PY" /opt/venv
uv pip install --python /opt/venv/bin/python pyodbc psycopg2-binary
uv pip install --python /opt/venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch==2.8.0 \
  || echo "torch==2.8.0 : aucune roue pour $("$PY" -V 2>&1)" > /opt/venv/TORCH_UNAVAILABLE
