import { spawn, execFile } from 'node:child_process';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { build } from '../.local/cloudflare-code-mode/node_modules/esbuild/lib/main.js';
import { Miniflare } from '../.local/cloudflare-code-mode/node_modules/miniflare/dist/src/index.js';

const run = promisify(execFile);
const scriptPath = fileURLToPath(import.meta.url);
const dependencies = fileURLToPath(new URL('../.local/cloudflare-code-mode/node_modules', import.meta.url));

if (process.argv.includes('--child')) {
  process.once('message', async ({ script, body, deviceUrl }) => {
    let runtime;
    try {
      runtime = new Miniflare({
        modules: true, script, compatibilityDate: '2026-07-30',
        compatibilityFlags: ['nodejs_compat'], workerLoaders: { LOADER: {} },
        bindings: { DEVICE_URL: deviceUrl },
      });
      const response = await runtime.dispatchFetch('http://localhost/execute', { method: 'POST', body });
      process.send({ status: response.status, body: await response.text() });
    } catch (error) {
      process.send({ status: 500, body: JSON.stringify({ status: 'error', error: { type: error.name, message: error.message } }) });
    } finally {
      await runtime?.dispose();
      process.disconnect();
    }
  });
} else {
  const bundled = await build({
    entryPoints: [fileURLToPath(new URL('./cloudflare_device_worker.js', import.meta.url))],
    bundle: true, write: false, format: 'esm', platform: 'neutral',
    nodePaths: [dependencies], external: ['cloudflare:workers'],
  });
  const script = bundled.outputFiles[0].text;
  const deviceUrl = (process.env.SLATE_DEVICE_URL ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');
  const children = new Set();
  const server = createServer(async (request, response) => {
    if (request.method !== 'POST' || request.url !== '/execute') {
      response.writeHead(404).end();
      return;
    }
    let child;
    let timeout;
    let memoryCheck;
    const stop = () => {
      clearTimeout(timeout);
      clearInterval(memoryCheck);
      if (child?.pid) {
        try { process.kill(-child.pid, 'SIGKILL'); } catch (error) {
          if (error.code !== 'ESRCH') console.error('Worker process cleanup failed', error.code);
        }
      }
    };
    const fail = (message) => {
      stop();
      if (!response.writableEnded) {
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({
          status: 'error',
          error: { type: 'ResourceLimit', message },
          calls_available: false,
          warning: 'Completed device actions may remain applied. Receipts are unavailable; do not replay automatically.',
          state_reset: true,
        }));
      }
    };
    response.once('close', stop);
    try {
      let body = '';
      for await (const chunk of request) {
        body += chunk;
        if (Buffer.byteLength(body) > 32768) throw new Error('Execution request exceeds the input limit');
      }
      child = spawn(process.execPath, [scriptPath, '--child'], {
        detached: true, stdio: ['ignore', 'inherit', 'inherit', 'ipc'],
      });
      children.add(child);
      timeout = setTimeout(() => fail('Worker exceeded the 5 second wall-time limit'), 5000);
      memoryCheck = setInterval(async () => {
        try {
          const { stdout } = await run('ps', ['-axo', 'pgid=,rss=']);
          const rss = stdout.trim().split('\n').reduce((sum, line) => {
            const [group, size] = line.trim().split(/\s+/).map(Number);
            return sum + (group === child.pid ? size : 0);
          }, 0);
          if (rss > 256 * 1024) fail('Worker process group exceeded the 256 MiB memory limit');
        } catch (error) { fail(`Worker memory supervision failed: ${error.code}`); }
      }, 100);
      child.once('message', ({ status, body: result }) => {
        if (Buffer.byteLength(result) > 65536) return fail('Worker response exceeds the output limit');
        response.writeHead(status, { 'Content-Type': 'application/json' });
        response.end(result);
      });
      child.once('exit', () => {
        children.delete(child);
        if (!response.writableEnded) fail('Worker exited before returning a result');
      });
      child.once('error', (error) => fail(`Worker startup failed: ${error.code}`));
      child.send({ script, body, deviceUrl });
    } catch (error) { fail(error.message); }
  });
  const port = Number(process.env.SLATE_CLOUDFLARE_CODE_PORT ?? 8650);
  for (const signal of ['SIGTERM', 'SIGINT']) {
    process.once(signal, () => {
      server.close();
      for (const child of children) {
        try { process.kill(-child.pid, 'SIGKILL'); } catch (error) {
          if (error.code !== 'ESRCH') console.error('Worker shutdown failed', error.code);
        }
      }
      process.exit(0);
    });
  }
  server.listen(port, '127.0.0.1', () => console.log(`Cloudflare local code mode listening on 127.0.0.1:${server.address().port}`));
}
