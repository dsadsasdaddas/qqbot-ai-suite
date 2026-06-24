# dora-runtime

Sandboxed Dora SSR cloud-runtime image for QQ bot generated games.

It runs:

- `Xvfb` virtual display
- Dora SSR native runtime launched against a temporary merged asset root
- `x11vnc`
- `noVNC`/`websockify` on port `6080`

The generated project is mounted read-only at `/workspace/project`. At startup,
the image builds `/tmp/dora-assets` from generated files plus built-in Dora
`Font/` and `Image/` resources, then runs:

```bash
dora-ssr --asset /tmp/dora-assets
```

Public access must go through `dora-game-gateway`; VNC/noVNC ports stay inside
the internal Docker network.
