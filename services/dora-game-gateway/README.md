# dora-game-gateway

HTTP/WebSocket gateway for Dora SSR game sessions.

Responsibilities:

- accepts generated project files from the AstrBot plugin
- static-scans obvious dangerous APIs
- starts one Dora runtime Docker container per game
- creates short-lived play tokens
- reverse-proxies noVNC/WebSocket under `/play/<id>`
- stops expired sessions

Only this gateway should be exposed behind HTTPS. Runtime VNC `5900` and noVNC
`6080` stay on an internal Docker network.
