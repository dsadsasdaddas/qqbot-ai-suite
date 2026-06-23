#!/usr/bin/env node
/* global Buffer, URL, console, process, fetch, AbortController */

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { createSign, timingSafeEqual } from 'node:crypto';

const defaultPort = 8877;
const defaultHost = '0.0.0.0';
const defaultLocation = 'global';
const defaultModel = 'gemini-2.5-flash-image';
const maxBodyBytes = 24 * 1024 * 1024;
const cloudPlatformScope = 'https://www.googleapis.com/auth/cloud-platform';
const metadataTokenUrl = 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token';
const dataUrlPattern = /^data:([^;,]+);base64,(.*)$/s;
const rateLimitBuckets = new Map();

const getCorsOrigin = (request, config) => {
  if (config.allowedOrigins.includes('*')) return '*';
  const origin = request.headers.origin;
  if (origin !== undefined && config.allowedOrigins.includes(origin)) return origin;
  return config.allowedOrigins[0] || '*';
};

const commonHeaders = (request, config) => ({
  'Access-Control-Allow-Origin': getCorsOrigin(request, config),
  'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-API-Key',
  'Vary': 'Origin',
});

const jsonResponse = (request, response, config, status, data) => {
  const body = JSON.stringify(data, null, 2);
  response.writeHead(status, {
    ...commonHeaders(request, config),
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  response.end(body);
};

const emptyResponse = (request, response, config, status) => {
  response.writeHead(status, commonHeaders(request, config));
  response.end();
};

const readRequestBody = (request) => new Promise((resolve, reject) => {
  const chunks = [];
  let size = 0;
  request.on('data', (chunk) => {
    size += chunk.length;
    if (size > maxBodyBytes) {
      reject(new Error(`request body is larger than ${maxBodyBytes} bytes`));
      request.destroy();
      return;
    }
    chunks.push(chunk);
  });
  request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
  request.on('error', reject);
});

const runCommand = (command, args, options = {}) => {
  const result = spawnSync(command, args, {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    ...options,
  });
  if (result.error !== undefined) throw result.error;
  if (result.status !== 0) {
    throw new Error(result.stderr.trim() || `${command} exited with ${result.status}`);
  }
  return result.stdout.trim();
};

const findExecutable = (candidates) => {
  for (const candidate of candidates) {
    if (candidate === undefined || candidate === '') continue;
    if (candidate.includes('/') && existsSync(candidate)) return candidate;
    if (!candidate.includes('/')) {
      const result = spawnSync('/bin/sh', ['-lc', `command -v ${candidate}`], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      });
      const found = result.stdout.trim();
      if (result.status === 0 && found !== '') return found;
    }
  }
  return undefined;
};

const readConfiguredProject = (gcloud) => {
  if (gcloud === undefined) return '';
  try { return runCommand(gcloud, ['config', 'get-value', 'project']).trim(); }
  catch { return ''; }
};

const readConfig = () => {
  const gcloud = findExecutable([
    process.env.GCLOUD_BIN,
    'gcloud',
    '/opt/homebrew/share/google-cloud-sdk/bin/gcloud',
  ]);
  const configuredProject = readConfiguredProject(gcloud);
  const project = process.env.GOOGLE_VERTEX_PROJECT ||
    process.env.GOOGLE_CLOUD_PROJECT ||
    process.env.GCLOUD_PROJECT ||
    configuredProject;
  if (project === undefined || project === '' || project === '(unset)') {
    throw new Error('Google Cloud project is not configured. Set GOOGLE_VERTEX_PROJECT.');
  }
  const configuredModel = process.env.GOOGLE_VERTEX_MODEL || defaultModel;
  const allowedModels = [...new Set((process.env.GOOGLE_VERTEX_ALLOWED_MODELS || configuredModel)
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean))];
  if (!allowedModels.includes(configuredModel)) allowedModels.unshift(configuredModel);
  return {
    gcloud,
    project,
    location: process.env.GOOGLE_VERTEX_LOCATION || defaultLocation,
    model: configuredModel,
    allowedModels,
    port: Number.parseInt(process.env.GOOGLE_VERTEX_PROXY_PORT || process.env.PORT || `${defaultPort}`, 10),
    host: process.env.GOOGLE_VERTEX_PROXY_HOST || defaultHost,
    allowedOrigins: (process.env.DORA_ALLOWED_ORIGINS || '*').split(',').map((item) => item.trim()).filter(Boolean),
    apiTokens: (process.env.DORA_API_TOKENS || process.env.DORA_API_TOKEN || '').split(',').map((item) => item.trim()).filter(Boolean),
    requireAuth: process.env.DORA_REQUIRE_AUTH === 'true',
    rateLimitPerMinute: Number.parseInt(process.env.DORA_RATE_LIMIT_PER_MINUTE || '10', 10),
  };
};

const parseDataUrl = (value) => {
  const match = value.match(dataUrlPattern);
  if (match === null) return undefined;
  return {
    mimeType: match[1],
    data: match[2].replace(/\s+/g, ''),
  };
};

const normalizeInlineImageValue = (value, mimeType) => {
  if (typeof value !== 'string') return undefined;
  const parsed = parseDataUrl(value);
  if (parsed !== undefined) return parsed;
  return {
    mimeType: typeof mimeType === 'string' && mimeType !== '' ? mimeType : 'image/png',
    data: value.replace(/\s+/g, ''),
  };
};

const normalizeInlineImages = (body) => {
  if (Array.isArray(body.referenceImages)) {
    return body.referenceImages
      .map((image) => normalizeInlineImageValue(image, body.referenceImageMimeType))
      .filter((image) => image !== undefined);
  }
  if (typeof body.referenceImage === 'string') {
    return [normalizeInlineImageValue(body.referenceImage, body.referenceImageMimeType)].filter(Boolean);
  }
  if (typeof body.referenceImageBase64 === 'string') {
    return [{
      mimeType: body.referenceImageMimeType || 'image/png',
      data: body.referenceImageBase64.replace(/\s+/g, ''),
    }];
  }
  return [];
};

const createVertexRequest = (body) => {
  if (typeof body.prompt !== 'string' || body.prompt.trim() === '') {
    throw new Error('request.prompt is required');
  }
  const parts = [{ text: body.prompt }];
  for (const image of normalizeInlineImages(body)) {
    parts.push({
      inlineData: {
        mimeType: image.mimeType,
        data: image.data,
      },
    });
  }
  return {
    contents: [{ role: 'user', parts }],
    generationConfig: {
      responseModalities: ['TEXT', 'IMAGE'],
    },
  };
};

const base64Url = (value) => Buffer.from(value).toString('base64url');

const signJwt = ({ clientEmail, privateKey, tokenUri }) => {
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: 'RS256', typ: 'JWT' };
  const claim = {
    iss: clientEmail,
    scope: cloudPlatformScope,
    aud: tokenUri,
    iat: now,
    exp: now + 3600,
  };
  const unsigned = `${base64Url(JSON.stringify(header))}.${base64Url(JSON.stringify(claim))}`;
  const signer = createSign('RSA-SHA256');
  signer.update(unsigned);
  signer.end();
  return `${unsigned}.${signer.sign(privateKey, 'base64url')}`;
};

const getServiceAccountToken = async (credentialsPath) => {
  const credentials = JSON.parse(await readFile(credentialsPath, 'utf8'));
  if (credentials.type !== 'service_account' || !credentials.client_email || !credentials.private_key) {
    throw new Error('GOOGLE_APPLICATION_CREDENTIALS must point to a service account JSON file.');
  }
  const tokenUri = credentials.token_uri || 'https://oauth2.googleapis.com/token';
  const assertion = signJwt({
    clientEmail: credentials.client_email,
    privateKey: credentials.private_key,
    tokenUri,
  });
  const response = await fetch(tokenUri, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
      assertion,
    }),
  });
  const body = await response.json();
  if (!response.ok || typeof body.access_token !== 'string') {
    throw new Error(body.error_description || body.error || `service account token request failed with HTTP ${response.status}`);
  }
  return body.access_token;
};

const getMetadataServerToken = async () => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 1200);
  try {
    const response = await fetch(metadataTokenUrl, {
      headers: { 'Metadata-Flavor': 'Google' },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`metadata server token request failed with HTTP ${response.status}`);
    const body = await response.json();
    if (typeof body.access_token !== 'string') throw new Error('metadata server response did not include access_token');
    return body.access_token;
  } finally {
    clearTimeout(timeout);
  }
};

const getAccessToken = async (config) => {
  if (process.env.GOOGLE_VERTEX_ACCESS_TOKEN) return process.env.GOOGLE_VERTEX_ACCESS_TOKEN;
  if (process.env.GOOGLE_APPLICATION_CREDENTIALS) return getServiceAccountToken(process.env.GOOGLE_APPLICATION_CREDENTIALS);
  try { return await getMetadataServerToken(); }
  catch {
    if (config.gcloud === undefined) {
      throw new Error('No Google credentials found. Use Cloud Run service identity, GOOGLE_APPLICATION_CREDENTIALS, or local gcloud auth.');
    }
    return runCommand(config.gcloud, ['auth', 'print-access-token']);
  }
};

const selectRequestModel = (config, body) => {
  const requested = typeof body.model === 'string' ? body.model.trim()
    : typeof body.modelId === 'string' ? body.modelId.trim()
      : '';
  if (requested === '') return config.model;
  if (!config.allowedModels.includes(requested)) {
    throw new Error(`request.model is not allowed: ${requested}`);
  }
  return requested;
};

const callVertex = async (config, vertexRequest, model = config.model) => {
  const token = await getAccessToken(config);
  const url = `https://aiplatform.googleapis.com/v1/projects/${config.project}/locations/${config.location}/publishers/google/models/${model}:generateContent`;
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(vertexRequest),
  });
  const responseText = await response.text();
  let vertexResponse;
  try { vertexResponse = JSON.parse(responseText); }
  catch { vertexResponse = { error: { code: response.status, status: response.statusText, message: responseText } }; }
  return { httpCode: response.status, vertexResponse };
};

const extractImageResult = (vertexResponse) => {
  if (vertexResponse.error !== undefined) {
    return {
      success: false,
      message: vertexResponse.error.message || 'Vertex AI request failed',
      error: {
        code: vertexResponse.error.code,
        status: vertexResponse.error.status,
      },
    };
  }
  const parts = vertexResponse.candidates?.[0]?.content?.parts;
  if (!Array.isArray(parts)) {
    return { success: false, message: 'Vertex AI response did not include candidates[0].content.parts' };
  }
  const text = parts
    .filter((part) => typeof part.text === 'string')
    .map((part) => part.text)
    .join('\n')
    .trim();
  const imagePart = parts.find((part) => part.inlineData?.data !== undefined || part.inline_data?.data !== undefined);
  const inlineData = imagePart?.inlineData || imagePart?.inline_data;
  if (inlineData === undefined) {
    return {
      success: false,
      message: text === '' ? 'Vertex AI response did not include an image' : text,
      text,
      usage: vertexResponse.usageMetadata || vertexResponse.usage_metadata,
    };
  }
  return {
    success: true,
    text,
    mimeType: inlineData.mimeType || inlineData.mime_type || 'image/png',
    imageBase64: inlineData.data,
    usage: vertexResponse.usageMetadata || vertexResponse.usage_metadata,
  };
};

const constantTimeEquals = (left, right) => {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  return leftBuffer.length === rightBuffer.length && timingSafeEqual(leftBuffer, rightBuffer);
};

const extractClientToken = (request, url) => {
  const auth = request.headers.authorization || '';
  if (auth.startsWith('Bearer ')) return auth.slice(7).trim();
  const apiKey = request.headers['x-api-key'];
  if (typeof apiKey === 'string' && apiKey.trim() !== '') return apiKey.trim();
  return url.searchParams.get('token') || '';
};

const isAuthorized = (request, url, config) => {
  if (config.apiTokens.length === 0) return !config.requireAuth;
  const clientToken = extractClientToken(request, url);
  return config.apiTokens.some((token) => constantTimeEquals(clientToken, token));
};

const getClientKey = (request, url) => {
  const token = extractClientToken(request, url);
  if (token !== '') return `token:${token}`;
  return `ip:${request.headers['x-forwarded-for'] || request.socket.remoteAddress || 'unknown'}`;
};

const checkRateLimit = (request, url, config) => {
  if (!Number.isFinite(config.rateLimitPerMinute) || config.rateLimitPerMinute <= 0) {
    return { allowed: true, remaining: null, resetSeconds: null };
  }
  const now = Date.now();
  const windowMs = 60 * 1000;
  const key = getClientKey(request, url);
  const current = rateLimitBuckets.get(key);
  if (current === undefined || now >= current.resetAt) {
    rateLimitBuckets.set(key, { count: 1, resetAt: now + windowMs });
    return { allowed: true, remaining: config.rateLimitPerMinute - 1, resetSeconds: 60 };
  }
  if (current.count >= config.rateLimitPerMinute) {
    return { allowed: false, remaining: 0, resetSeconds: Math.max(1, Math.ceil((current.resetAt - now) / 1000)) };
  }
  current.count += 1;
  return { allowed: true, remaining: config.rateLimitPerMinute - current.count, resetSeconds: Math.max(1, Math.ceil((current.resetAt - now) / 1000)) };
};

setInterval(() => {
  const now = Date.now();
  for (const [key, bucket] of rateLimitBuckets) {
    if (now >= bucket.resetAt) rateLimitBuckets.delete(key);
  }
}, 60 * 1000).unref();

const handleGenerateFrame = async (request, response, config, url) => {
  if (!isAuthorized(request, url, config)) {
    jsonResponse(request, response, config, 401, {
      success: false,
      message: 'Unauthorized',
    });
    return;
  }
  const rateLimit = checkRateLimit(request, url, config);
  if (!rateLimit.allowed) {
    jsonResponse(request, response, config, 429, {
      success: false,
      message: `Rate limit exceeded. Try again in ${rateLimit.resetSeconds} seconds.`,
      resetSeconds: rateLimit.resetSeconds,
    });
    return;
  }
  let vertexRequest;
  let requestModel;
  try {
    const rawBody = await readRequestBody(request);
    const body = rawBody === '' ? {} : JSON.parse(rawBody);
    requestModel = selectRequestModel(config, body);
    vertexRequest = createVertexRequest(body);
  } catch (error) {
    jsonResponse(request, response, config, 400, {
      success: false,
      message: error instanceof Error ? error.message : String(error),
    });
    return;
  }
  const { httpCode, vertexResponse } = await callVertex(config, vertexRequest, requestModel);
  const result = extractImageResult(vertexResponse);
  result.model = requestModel;
  jsonResponse(request, response, config, result.success ? 200 : Number(httpCode) || 500, result);
};

const startServer = () => {
  const config = readConfig();
  if (config.requireAuth && config.apiTokens.length === 0) {
    throw new Error('DORA_REQUIRE_AUTH=true but DORA_API_TOKEN/DORA_API_TOKENS is empty.');
  }
  const server = createServer((request, response) => {
    void (async () => {
      const url = new URL(request.url || '/', `http://${request.headers.host || '127.0.0.1'}`);
      if (request.method === 'OPTIONS') {
        emptyResponse(request, response, config, 204);
        return;
      }
      if (request.method === 'GET' && url.pathname === '/health') {
        jsonResponse(request, response, config, 200, {
          success: true,
          project: config.project,
          location: config.location,
          model: config.model,
          allowedModels: config.allowedModels,
          authRequired: config.apiTokens.length > 0 || config.requireAuth,
          rateLimitPerMinute: config.rateLimitPerMinute,
        });
        return;
      }
      if (request.method === 'POST' && url.pathname === '/api/google-vertex/generate-frame') {
        await handleGenerateFrame(request, response, config, url);
        return;
      }
      jsonResponse(request, response, config, 404, {
        success: false,
        message: 'Not found. Use POST /api/google-vertex/generate-frame or GET /health.',
      });
    })().catch((error) => {
      jsonResponse(request, response, config, 500, {
        success: false,
        message: error instanceof Error ? error.message : String(error),
      });
    });
  });
  server.listen(config.port, config.host, () => {
    console.log(`Google Vertex image proxy listening on http://${config.host}:${config.port}`);
    console.log(`Project=${config.project} Location=${config.location} Model=${config.model}`);
    console.log(`Allowed models=${config.allowedModels.join(',')}`);
    console.log(`API auth=${config.apiTokens.length > 0 || config.requireAuth ? 'enabled' : 'disabled'}`);
    console.log(`Rate limit=${config.rateLimitPerMinute > 0 ? `${config.rateLimitPerMinute}/minute` : 'disabled'}`);
  });
};

startServer();
