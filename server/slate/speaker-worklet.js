// QEMU I2S1: signed little-endian 16-bit stereo at 16 kHz.
registerProcessor('slate-speaker', class extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = 8000;
    this.left = new Float32Array(this.capacity);
    this.right = new Float32Array(this.capacity);
    this.read = 0;
    this.write = 0;
    this.position = 0;
    this.playing = false;
    this.port.onmessage = ({data}) => {
      if (data === 'reset') { this.reset(); return; }
      this.port.postMessage('consumed');
      // Ignore packets that spent too long waiting on the browser main thread.
      if (currentTime - data.time > 0.25) { this.reset(); return; }
      const pcm = new DataView(data.pcm);
      const frames = Math.floor(pcm.byteLength / 4);
      if (this.write - this.read + frames > 4800) this.reset();
      for (let i = Math.max(0, frames - this.capacity); i < frames; i++) {
        const slot = this.write++ % this.capacity;
        this.left[slot] = pcm.getInt16(i * 4, true) / 32768;
        this.right[slot] = pcm.getInt16(i * 4 + 2, true) / 32768;
      }
    };
  }
  reset() {
    this.read = this.write = this.position = 0;
    this.playing = false;
  }
  process(_inputs, outputs) {
    const [left, right] = outputs[0];
    if (!left) return true;
    if (!this.playing && this.write - this.read >= 1600) this.playing = true;
    for (let i = 0; i < left.length; i++) {
      if (!this.playing) break;
      if (this.read + 1 >= this.write) { this.reset(); break; }
      const a = this.read % this.capacity, b = (this.read + 1) % this.capacity;
      left[i] = this.left[a] + (this.left[b] - this.left[a]) * this.position;
      if (right) right[i] = this.right[a] + (this.right[b] - this.right[a]) * this.position;
      this.position += 16000 / sampleRate;
      const advance = Math.floor(this.position);
      this.read += advance;
      this.position -= advance;
    }
    return true;
  }
});
