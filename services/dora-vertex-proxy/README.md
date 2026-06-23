# dora-vertex-proxy

Node.js HTTP proxy for Google Vertex Gemini image generation. It supports per-request model selection with an allowlist:

```json
{
  "prompt": "generate a blue icon",
  "model": "gemini-2.5-flash-image",
  "referenceImages": ["data:image/png;base64,..."]
}
```

Health:

```bash
curl http://127.0.0.1:8877/health
```
