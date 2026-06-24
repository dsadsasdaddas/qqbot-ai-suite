FROM docker.io/library/rust:1-bookworm AS builder
WORKDIR /app
COPY src/main.rs /app/main.rs
RUN rustc -O /app/main.rs -o /app/qqbot-hookd

FROM docker.io/library/debian:bookworm-slim
COPY --from=builder /app/qqbot-hookd /usr/local/bin/qqbot-hookd
ENV BIND_ADDR=0.0.0.0:8788 \
    RUNNER_URL=http://claude-glm-runner:8790 \
    TOKEN_CONFIG_PATH=/config/claude_glm_runner.json \
    STATE_DIR=/data/.agent_state \
    MAX_CONCURRENCY=1
EXPOSE 8788
CMD ["/usr/local/bin/qqbot-hookd"]
