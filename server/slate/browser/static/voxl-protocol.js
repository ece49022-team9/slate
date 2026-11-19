export const Bin = {
  Video: 0x01,
  Cursor: 0x03,
  Patch: 0x05,
  Damage: 0x06,
};

export const command = {
  ack: (frame) => `a,${frame}`,
  move: (x, y) => `m,${x},${y}`,
  button: (button, down) => `b,${button},${down ? 1 : 0}`,
  scroll: (dx, dy) => `sp,${dx.toFixed(1)},${dy.toFixed(1)}`,
  key: (keysym, down) => `k,${keysym},${down ? 1 : 0}`,
  releaseAll: "r",
  clipboard: (text) => `c,${text}`,
  video: (on) => `v,${on ? 1 : 0}`,
  keyframe: "kf",
  pong: (sent) => `po,${sent}`,
};

export const VIDEO_HEADER_BYTES = 16;

export function videoHeader(b) {
  const u32 = (i) => ((b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]) >>> 0;
  return { key: (b[1] & 1) === 1, frame: u32(2), stripe: b[10], parts: b[12] };
}
