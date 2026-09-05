#!/usr/bin/env node
// Minimal CONNECT proxy for Gemini CLI 0.51 paid API model transport.

import http from 'node:http';
import net from 'node:net';

const ALLOWED = new Set([
  'generativelanguage.googleapis.com',
]);

function reject(socket, status = '403 Forbidden') {
  socket.end(`HTTP/1.1 ${status}\r\nConnection: close\r\n\r\n`);
}

const server = http.createServer((request, response) => {
  if (request.method === 'GET' && request.url === '/') {
    response.writeHead(200, { 'content-type': 'text/plain' });
    response.end('ready\n');
    return;
  }
  response.writeHead(403, { connection: 'close' });
  response.end();
});

server.on('connect', (request, client, head) => {
  const separator = request.url?.lastIndexOf(':') ?? -1;
  const host = separator > 0 ? request.url.slice(0, separator).toLowerCase() : '';
  const port = separator > 0 ? Number(request.url.slice(separator + 1)) : 0;
  if (!ALLOWED.has(host) || port !== 443 || net.isIP(host)) {
    reject(client);
    return;
  }
  const upstream = net.connect({ host, port });
  upstream.setTimeout(30_000);
  client.setTimeout(30_000);
  upstream.once('connect', () => {
    client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    if (head.length) upstream.write(head);
    upstream.pipe(client);
    client.pipe(upstream);
  });
  upstream.once('error', () => reject(client, '502 Bad Gateway'));
  upstream.once('timeout', () => upstream.destroy());
  client.once('timeout', () => client.destroy());
});

server.listen(8877, '0.0.0.0');
