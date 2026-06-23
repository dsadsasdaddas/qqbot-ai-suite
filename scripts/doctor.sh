#!/usr/bin/env bash
set -euo pipefail
docker ps --filter name=astrbot --filter name=napcat --filter name=dora-vertex-proxy --filter name=glm-code-runner --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
echo
echo 'Vertex proxy health:'
curl -fsS http://127.0.0.1:8877/health || true
echo
