#!/usr/bin/env node
/**
 * /config-rewriting proxy for the OIDC Playwright runner.
 *
 * Replaces the plain socat forward for localhost:8080 -> quay:8080. All
 * traffic passes through unchanged (UI, API, the OIDC browser redirects),
 * with one exception: `GET /config` gains a `*_LOGIN_CONFIG` entry under
 * `.config`, because Quay only serves `frontend_visible_config()` plus
 * `external_login` there (RCA-20261023-143827 §6.5). The bearer-token and
 * multi-issuer specs scan `quayConfig.config` for exactly that entry to
 * discover the OIDC provider; without the injection they always skip.
 *
 * The injected values mirror config/stack/config.yaml so the tests see the
 * same issuer/audience lists Quay actually validates against. Configure via
 * INJECT_* environment variables (see compose.yaml); INJECT_OIDC_* empty
 * disables the injection and makes this a pure pass-through proxy.
 */
import http from 'node:http';

const LISTEN_PORT = Number(process.env.CONFIG_PROXY_PORT || 8080);
const UPSTREAM_HOST = process.env.QUAY_SERVICE_HOST || 'quay';
const UPSTREAM_PORT = process.env.QUAY_SERVICE_PORT || '8080';

const LOGIN_SERVICE_NAME =
  process.env.INJECT_LOGIN_SERVICE_NAME || 'SOMEOIDC_LOGIN_CONFIG';

function list(varName) {
  const raw = process.env[varName] || '';
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

const oidcServer = process.env.INJECT_OIDC_SERVER || '';
const loginConfig = {
  OIDC_SERVER: oidcServer,
  CLIENT_ID: process.env.INJECT_OIDC_CLIENT_ID || '',
  OIDC_ISSUERS: list('INJECT_OIDC_ISSUERS'),
  OIDC_AUDIENCES: list('INJECT_OIDC_AUDIENCES'),
};

const injectEnabled = Boolean(
  oidcServer && loginConfig.CLIENT_ID && loginConfig.OIDC_ISSUERS.length,
);

/** Handle one proxied request. */
function handle(req, res) {
  const url = req.url || '/';
  const path = url.split('?')[0];
  const intercept =
    injectEnabled && req.method === 'GET' && path.replace(/\/$/, '') === '/config';

  const headers = {...req.headers};
  // Ask the upstream for an uncompressed /config so the JSON can be rewritten.
  if (intercept) delete headers['accept-encoding'];

  const upstream = http.request(
    {host: UPSTREAM_HOST, port: UPSTREAM_PORT, method: req.method, path: url, headers},
    (up) => {
      if (!intercept) {
        res.writeHead(up.statusCode || 502, up.headers);
        up.pipe(res);
        return;
      }
      const chunks = [];
      up.on('data', (c) => chunks.push(c));
      up.on('end', () => {
        let body = Buffer.concat(chunks);
        try {
          const parsed = JSON.parse(body.toString('utf8'));
          if (
            parsed &&
            typeof parsed === 'object' &&
            parsed.config &&
            typeof parsed.config === 'object' &&
            !(LOGIN_SERVICE_NAME in parsed.config)
          ) {
            parsed.config[LOGIN_SERVICE_NAME] = loginConfig;
            body = Buffer.from(JSON.stringify(parsed));
          }
        } catch {
          // Not JSON (or unexpected shape): pass the upstream body through.
        }
        const outHeaders = {...up.headers};
        delete outHeaders['content-length'];
        delete outHeaders['transfer-encoding'];
        res.writeHead(up.statusCode || 502, outHeaders);
        res.end(body);
      });
    },
  );
  upstream.on('error', (err) => {
    console.log(`[config-proxy] upstream error: ${err.message}`);
    if (!res.headersSent) res.writeHead(502, {'content-type': 'text/plain'});
    res.end(`config-proxy upstream error: ${err.message}`);
  });
  req.pipe(upstream);
}

// Mirror the old socat forwards: IPv4 loopback is what the suite depends on;
// the IPv6 listener is best-effort (localhost may resolve to ::1).
for (const host of ['127.0.0.1', '::1']) {
  const server = http.createServer(handle);
  server.on('error', (err) => {
    console.log(`[config-proxy] ${host}:${LISTEN_PORT}: ${err.message}`);
  });
  server.listen(LISTEN_PORT, host, () => {
    console.log(
      `[config-proxy] ${host}:${LISTEN_PORT} -> ${UPSTREAM_HOST}:${UPSTREAM_PORT}` +
        (injectEnabled ? ` (injecting ${LOGIN_SERVICE_NAME} into /config)` : ''),
    );
  });
}
