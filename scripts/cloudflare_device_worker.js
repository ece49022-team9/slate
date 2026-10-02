import { DynamicWorkerExecutor } from '@cloudflare/codemode';

const bytes = (value) => new TextEncoder().encode(JSON.stringify(value)).length;

export default {
  async fetch(request, env) {
    const calls = [];
    let active = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
      const { scope, code } = await request.json();
      if (!/^[A-Za-z0-9_-]{1,128}$/.test(scope)) throw new Error('Invalid device scope');
      if (typeof code !== 'string' || !code.trim() || bytes(code) > 16384) {
        throw new Error('Code must contain 1..16384 UTF-8 bytes');
      }
      const dispatch = async (tool, args, count = true) => {
        if (!active) throw new Error('This device execution has ended');
        if (count && calls.length >= 12) throw new Error('Device tool-call budget exceeded');
        const call = { tool, arguments: args, status: 'started' };
        if (count) calls.push(call);
        try {
          const endpoint = tool === 'get_status' ? 'status' : tool;
          const response = await fetch(`${env.DEVICE_URL}/api/device/${scope}/${endpoint}`, {
            method: tool === 'get_status' ? 'GET' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: tool === 'get_status' ? undefined : JSON.stringify(args),
            signal: controller.signal,
          });
          const text = await response.text();
          if (new TextEncoder().encode(text).length > 16384) throw new Error('Device response exceeds the output limit');
          const receipt = JSON.parse(text);
          if (!response.ok) throw new Error(`Device HTTP ${response.status}: ${receipt.detail}`);
          if (receipt.operation !== tool || !/^[0-9a-f]{32}$/.test(receipt.request_id)) {
            throw new Error('Device acknowledgment does not match the request');
          }
          Object.assign(call, { status: 'completed', receipt });
          return receipt;
        } catch (error) {
          Object.assign(call, { status: 'failed', error_type: error.name });
          throw error;
        }
      };
      const scopeStatus = await dispatch('get_status', {}, false);
      const executor = new DynamicWorkerExecutor({ loader: env.LOADER, timeout: 3000, globalOutbound: null });
      const result = await executor.execute(code, [{
        name: 'device',
        fns: {
          set_orb: async (color, radius = 24) => {
            if (typeof color !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(color) ||
                typeof radius !== 'number' || !Number.isFinite(radius) || radius < 10 || radius > 45) {
              throw new Error('Color must be #RRGGBB and radius must be 10..45');
            }
            return dispatch('set_orb', { color, radius });
          },
          show_text: async (text) => {
            if (typeof text !== 'string' || text.length > 64 || !/^[ -~]*$/.test(text)) {
              throw new Error('Text must contain at most 64 printable ASCII characters');
            }
            return dispatch('show_text', { text });
          },
          get_status: async () => dispatch('get_status', {}),
        },
      }]);
      if (bytes(result) > 16384) throw new Error('Code result exceeds the output limit');
      return Response.json({
        status: result.error ? 'error' : 'completed',
        result: result.result ?? null,
        ...(result.error ? { error: { type: 'WorkerError', message: result.error } } : {}),
        output: (result.logs ?? []).join('\n'), calls, scope_status: scopeStatus, state_reset: true,
      });
    } catch (error) {
      return Response.json({ status: 'error', error: { type: error.name, message: error.message }, calls, state_reset: true });
    } finally {
      active = false;
      controller.abort();
      clearTimeout(timeout);
    }
  },
};
