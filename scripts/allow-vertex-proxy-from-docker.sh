#!/usr/bin/env bash
set -euo pipefail
NET_NAME="${1:-qqbot_astrbot_network}"
PORT="${2:-8877}"
NET_ID="$(docker network inspect "$NET_NAME" -f "{{.Id}}")"
BRIDGE="$(docker network inspect "$NET_NAME" -f "{{index .Options \"com.docker.network.bridge.name\"}}" 2>/dev/null || true)"
if [[ -z "$BRIDGE" || "$BRIDGE" == "<no value>" ]]; then
  BRIDGE="br-${NET_ID:0:12}"
fi
CMD="iptables -C INPUT -i '$BRIDGE' -p tcp --dport '$PORT' -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -i '$BRIDGE' -p tcp --dport '$PORT' -j ACCEPT"
docker run --rm --privileged --pid=host --net=host -v /:/host docker.m.daocloud.io/library/node:25-alpine \
  chroot /host sh -lc "$CMD"
echo "Allowed $NET_NAME ($BRIDGE) -> host TCP $PORT"
