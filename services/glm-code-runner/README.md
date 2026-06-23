# glm-code-runner

Small HTTP service that runs Python in one-off Docker sandboxes.

Security defaults:

- `--network none`
- read-only container
- `--cap-drop ALL`
- `no-new-privileges`
- pids/memory/cpu limits
- unprivileged user
