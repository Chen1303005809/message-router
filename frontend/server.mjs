import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer, request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(fileURLToPath(new URL('./dist', import.meta.url)))
const upstream = new URL(process.env.WEB_API_TARGET || 'http://web:8000')
const port = Number(process.env.PORT || 8080)
const hopByHopHeaders = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
])
const mimeTypes = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
}

function isBackendPath(pathname) {
  return (
    pathname === '/api' ||
    pathname.startsWith('/api/') ||
    pathname === '/auth' ||
    pathname.startsWith('/auth/') ||
    pathname === '/healthz' ||
    /^\/issues\/[^/]+\/media\/[^/]+$/.test(pathname) ||
    /^\/issues\/drafts\/[^/]+\/media\/[^/]+$/.test(pathname) ||
    /^\/issues\/private-intakes\/media\/[^/]+$/.test(pathname)
  )
}

function proxyRequest(request, response) {
  const target = new URL(request.url || '/', upstream)
  const headers = { ...request.headers, host: upstream.host }
  for (const name of hopByHopHeaders) delete headers[name]
  const send = target.protocol === 'https:' ? httpsRequest : httpRequest
  const forwarded = send(
    target,
    { method: request.method, headers },
    (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers)
      upstreamResponse.pipe(response)
    },
  )
  forwarded.on('error', (error) => {
    if (!response.headersSent) {
      response.writeHead(502, { 'content-type': 'application/json; charset=utf-8' })
    }
    response.end(JSON.stringify({ detail: `后端暂时不可用：${error.message}` }))
  })
  request.pipe(forwarded)
}

function serveFile(request, response, pathname) {
  let decoded
  try {
    decoded = decodeURIComponent(pathname)
  } catch {
    response.writeHead(400).end('Bad Request')
    return
  }
  const candidate = resolve(root, `.${decoded}`)
  if (candidate !== root && !candidate.startsWith(`${root}${sep}`)) {
    response.writeHead(403).end('Forbidden')
    return
  }
  const file = existsSync(candidate) && statSync(candidate).isFile() ? candidate : resolve(root, 'index.html')
  if (!existsSync(file)) {
    response.writeHead(404).end('Not Found')
    return
  }
  const contentType = mimeTypes[extname(file)] || 'application/octet-stream'
  const cacheControl = file.includes(`${sep}assets${sep}`)
    ? 'public, max-age=31536000, immutable'
    : 'no-cache'
  response.writeHead(200, { 'content-type': contentType, 'cache-control': cacheControl })
  if (request.method === 'HEAD') response.end()
  else createReadStream(file).pipe(response)
}

createServer((request, response) => {
  const url = new URL(request.url || '/', 'http://frontend.local')
  if (url.pathname === '/__healthz') {
    response.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' }).end('ok')
    return
  }
  if (isBackendPath(url.pathname)) {
    proxyRequest(request, response)
    return
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    response.writeHead(405, { allow: 'GET, HEAD' }).end('Method Not Allowed')
    return
  }
  serveFile(request, response, url.pathname)
}).listen(port, '0.0.0.0', () => {
  console.log(`kefu frontend listening on :${port}; backend=${upstream.origin}`)
})
