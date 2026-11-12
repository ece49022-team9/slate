const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../server/slate/display.html'), 'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const code = fs.readFileSync(path.join(__dirname, '../server/slate/speaker-worklet.js'), 'utf8');
for (const rate of [16000, 44100, 48000]) {
  let Processor;
  const env = {
    sampleRate: rate, currentTime: 0,
    AudioWorkletProcessor: class { constructor() { this.port = {postMessage() {}}; } },
    registerProcessor: (_, cls) => { Processor = cls; },
  };
  vm.runInNewContext(code, env);
  const player = new Processor();
  const pcm = new ArrayBuffer(64000);
  const view = new DataView(pcm);
  for (let i = 0; i < 16000; i++) {
    const sample = Math.round(20000 * Math.sin(2 * Math.PI * 440 * i / 16000));
    view.setInt16(i * 4, sample, true);
    view.setInt16(i * 4 + 2, -sample, true);
  }
  let frame = 0, crossings = 0, last = 0, loud = 0, tail = 0;
  for (let block = 0; block < Math.ceil(rate * 1.5 / 128); block++) {
    const time = block * 128 / rate;
    env.currentTime = time;
    while (frame < Math.min(16000, Math.floor(time * 16000 / 256 + 1) * 256)) {
      const end = Math.min(frame + 256, 16000);
      player.port.onmessage({data: {pcm: pcm.slice(frame * 4, end * 4), time}});
      frame = end;
    }
    const left = new Float32Array(128), right = new Float32Array(128);
    player.process([], [[left, right]]);
    for (let i = 0; i < 128; i++) {
      assert.equal(left[i], -right[i] || 0);
      if (left[i] > 0 && last <= 0) crossings++;
      last = left[i];
      if (Math.abs(left[i]) > 0.01) loud++;
      if (time > 1.2) tail += Math.abs(left[i]);
    }
  }
  assert.ok(crossings >= 438 && crossings <= 442, `${rate}: ${crossings} crossings`);
  assert.ok(loud > rate * 0.9);
  assert.equal(tail, 0, 'No repeated tone after underrun');
  // Flooding resets old audio and stays within the configured ring capacity.
  for (let i = 0; i < 100; i++) player.port.onmessage({data: {pcm: pcm.slice(0, 4096), time: env.currentTime}});
  assert.ok(player.write - player.read <= 4800);
  player.port.onmessage({data: 'reset'});
  assert.equal(player.write - player.read, 0);
  player.port.onmessage({data: {pcm, time: env.currentTime - 1}});
  assert.equal(player.write, 0, 'Stale packet discarded');
}
console.log('Speaker worklet: resampling, stereo, silence, overflow and reset passed');

(async () => {
  const button = {};
  const status = {};
  const pcm = new ArrayBuffer(80000);
  const view = new DataView(pcm);
  for (let i = 0; i < 16000; i++) {
    view.setInt16(i * 4, Math.round(20000 * Math.sin(2 * Math.PI * 440 * i / 16000)), true);
    view.setInt16(i * 4 + 2, view.getInt16(i * 4, true), true);
  }
  let buffer, starts = 0, resets = 0;
  const session = {
    gain: {}, worklet: {port: {postMessage: () => resets++}},
    audio: {
      createBuffer(channels, frames, rate) {
        assert.equal(channels, 2); assert.equal(frames, 20000); assert.equal(rate, 16000);
        buffer = [new Float32Array(frames), new Float32Array(frames)];
        return {getChannelData: channel => buffer[channel]};
      },
      createBufferSource() {
        return {
          connect(gain) { assert.equal(gain, session.gain); },
          start() { starts++; queueMicrotask(() => this.onended()); },
          disconnect() {},
        };
      },
    },
  };
  const env = {
    document: {querySelector: () => button, hidden: false},
    speakerAudio: session, speakerStatus: status, AbortController,
    startSpeaker: async () => {},
    fetch: async (url) => {
      assert.equal(url, '/speaker-test');
      assert.equal(session.testing, true);
      assert.equal(starts, 0, 'Playback waits for complete capture');
      return {ok: true, arrayBuffer: async () => pcm};
    },
  };
  const handler = html.slice(html.indexOf("document.querySelector('#speaker').onclick"), html.indexOf("document.addEventListener('visibilitychange'"));
  vm.runInNewContext(handler, env);
  await button.onclick({currentTarget: button});
  assert.equal(starts, 1, 'Exactly one buffered playback');
  assert.equal(resets, 2, 'Live queue cleared before and after test');
  assert.equal(session.testing, false);
  assert.equal(button.disabled, false);
  for (let i = 0; i < 20000; i++) {
    assert.equal(buffer[0][i], view.getInt16(i * 4, true) / 32768);
    assert.equal(buffer[1][i], buffer[0][i]);
  }
  console.log('Speaker test button: complete firmware capture, one playback and cleanup passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
