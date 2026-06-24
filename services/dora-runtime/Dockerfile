FROM soulter/astrbot:latest

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai

ARG APT_PROXY=""
ARG DEBIAN_MIRROR="https://mirrors.aliyun.com/debian"
ARG DEBIAN_SECURITY_MIRROR="https://mirrors.aliyun.com/debian-security"
RUN if [ -n "$APT_PROXY" ]; then \
      echo "Acquire::http::Proxy \"$APT_PROXY\";" > /etc/apt/apt.conf.d/99proxy; \
      echo "Acquire::https::Proxy \"$APT_PROXY\";" >> /etc/apt/apt.conf.d/99proxy; \
    fi; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s#http://deb.debian.org/debian-security#${DEBIAN_SECURITY_MIRROR}#g; s#http://deb.debian.org/debian#${DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg software-properties-common \
    xvfb x11vnc fluxbox novnc websockify ffmpeg x11-utils xdotool \
    libegl1 libegl-mesa0 libgles2 mesa-utils \
    fonts-noto-cjk fonts-noto-color-emoji \
    python3 procps netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Dora SSR official Linux package source. The base image is Debian Bookworm;
# Dora's README documents using the Ubuntu Jammy PPA on Debian Bookworm.
ARG DORA_INSTALL_COMMAND="apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys 9C7705BF && echo 'deb https://ppa.launchpadcontent.net/ippclub/dora-ssr/ubuntu jammy main' > /etc/apt/sources.list.d/dora-ssr.list && apt-get update && apt-get install -y --no-install-recommends dora-ssr"
RUN set -eux; \
    sh -c "$DORA_INSTALL_COMMAND"; \
    rm -rf /var/lib/apt/lists/*

# If Dora's development entrypoint is ever loaded, it should not require local
# Web IDE auth inside the isolated runtime. Public auth is enforced by the
# dora-game-gateway play token.
RUN set -eux; \
    entry_file="$(find / -path '*/Script/Dev/Entry.lua' -type f 2>/dev/null | head -n 1 || true)"; \
    if [ -n "$entry_file" ]; then \
      python3 -c "from pathlib import Path; import sys; p=Path(sys.argv[1]); s=p.read_text(encoding='utf-8'); s=s.replace('config.authRequired = not (\"Android\" == _val_0 or \"iOS\" == _val_0) -- 111', 'config.authRequired = false -- disabled inside isolated dora-runtime'); s=s.replace('HttpServer.authRequired = config.authRequired -- 112', 'HttpServer.authRequired = false -- isolated dora-runtime'); p.write_text(s, encoding='utf-8')" "$entry_file"; \
    else \
      echo "WARN: Dora Entry.lua not found; Web IDE auth may need manual disable" >&2; \
    fi

RUN useradd -m -u 1000 -s /bin/bash dora \
    && mkdir -p /workspace/project /workspace/assets /workspace/runtime /home/dora/.cache /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown -R dora:dora /workspace /home/dora

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER dora
WORKDIR /workspace
EXPOSE 6080
ENTRYPOINT ["/entrypoint.sh"]
