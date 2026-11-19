import { Bin, command, videoHeader, VIDEO_HEADER_BYTES } from "./voxl-protocol.js";
import { Renderer } from "./voxl-render.js";
let socket = null;
let renderer = null;
let stripes = [];
let lossless = false;
let drawnOnce = false;
let wantVideo = true;
const queue = [];
const frames = new Map;
const damages = new Map;
const decoders = [];
let waitingKey = true;
let keyAsked = 0;
const post = (m, transfer = []) => self.postMessage(m, { transfer });
const send = (s) => {
  if (socket?.readyState === WebSocket.OPEN)
    socket.send(s);
};
function askKeyframe() {
  if (performance.now() - keyAsked < 500)
    return;
  keyAsked = performance.now();
  send(command.keyframe);
}
function reset() {
  for (const d of decoders)
    if (d && d.decoder.state !== "closed")
      d.decoder.close();
  decoders.length = 0;
  for (const q of queue) {
    if (q.kind === "frame")
      for (const p of q.got)
        p.frame.close();
    else
      q.image?.close();
  }
  queue.length = 0;
  frames.clear();
  damages.clear();
  waitingKey = true;
}
const hex = (x) => x.toString(16).padStart(2, "0");
function codecOf(d) {
  for (let i = 0;i + 6 < d.length; i++)
    if (d[i] === 0 && d[i + 1] === 0 && d[i + 2] === 1 && (d[i + 3] & 31) === 7)
      return `avc1.${hex(d[i + 4])}${hex(d[i + 5])}${hex(d[i + 6])}`;
  return "avc1.640034";
}
function decoderFor(stripe, codec) {
  const found = decoders[stripe];
  if (found && found.codec === codec && found.decoder.state !== "closed")
    return found;
  if (found && found.decoder.state !== "closed")
    found.decoder.close();
  const made = {
    codec,
    ts: 0,
    ids: new Map,
    decoder: new VideoDecoder({
      output: (frame) => {
        const id = made.ids.get(frame.timestamp);
        made.ids.delete(frame.timestamp);
        const f = id === undefined ? undefined : frames.get(id);
        if (!f)
          return frame.close();
        f.got.push({ y: stripes[stripe]?.[0] ?? 0, frame });
        flush();
      },
      error: () => {
        reset();
        askKeyframe();
      }
    })
  };
  made.decoder.configure({ codec, optimizeForLatency: true });
  decoders[stripe] = made;
  return made;
}
function onVideo(b) {
  const h = videoHeader(b);
  const data = b.subarray(VIDEO_HEADER_BYTES);
  let f = frames.get(h.frame);
  if (!f) {
    f = { kind: "frame", id: h.frame, need: h.parts, got: [] };
    frames.set(h.frame, f);
    queue.push(f);
  }
  if (waitingKey && !h.key) {
    f.need--;
    askKeyframe();
    return flush();
  }
  try {
    const d = decoderFor(h.stripe, h.key ? codecOf(data) : decoders[h.stripe]?.codec ?? "");
    if (h.key)
      waitingKey = false;
    d.ids.set(++d.ts, h.frame);
    d.decoder.decode(new EncodedVideoChunk({ type: h.key ? "key" : "delta", timestamp: d.ts, data }));
  } catch {
    reset();
    askKeyframe();
  }
}
const u16 = (b, i) => b[i] << 8 | b[i + 1];
function onDamage(b) {
  if (!lossless)
    return;
  const frame = (b[2] << 24 | b[3] << 16 | b[4] << 8 | b[5]) >>> 0;
  if (b[1] & 1)
    return void damages.set(frame, null);
  const rects = [];
  for (let i = 0, n = u16(b, 6);i < n; i++) {
    const o = 8 + i * 8;
    rects.push({ x: u16(b, o), y: u16(b, o + 2), w: u16(b, o + 4), h: u16(b, o + 6) });
  }
  damages.set(frame, rects);
}
function onPatch(b) {
  const p = { kind: "patch", x: u16(b, 2), y: u16(b, 4), image: null, done: false };
  queue.push(p);
  createImageBitmap(new Blob([b.slice(10)], { type: "image/png" }), {
    premultiplyAlpha: "none",
    colorSpaceConversion: "none"
  }).then((image) => {
    if (queue.includes(p))
      p.image = image;
    else
      image.close();
  }, () => {
    return;
  }).finally(() => {
    p.done = true;
    flush();
  });
}
function flush() {
  let drew = false;
  while (queue.length) {
    const head = queue[0];
    if (head.kind === "patch") {
      if (!head.done)
        break;
      queue.shift();
      if (head.image) {
        renderer?.patch(head.image, head.x, head.y);
        head.image.close();
        drew = true;
      }
      continue;
    }
    if (head.got.length < head.need)
      break;
    queue.shift();
    frames.delete(head.id);
    const rects = damages.get(head.id) ?? null;
    damages.delete(head.id);
    for (const part of head.got) {
      renderer?.frame(part.frame, part.y, rects);
      part.frame.close();
    }
    if (head.got.length)
      drew = true;
    send(command.ack(head.id));
  }
  if (!drew || !renderer)
    return;
  renderer.present();
  if (!drawnOnce) {
    drawnOnce = true;
    post({ t: "drawn" });
  }
}
function onText(text) {
  let m;
  try {
    m = JSON.parse(text);
  } catch {
    return;
  }
  if (m.t === "ping")
    return send(command.pong(m.s));
  if (m.t === "close")
    return socket?.close();
  if (m.t === "clip")
    return post({ t: "clip", text: m.d });
  if (m.t === "hello") {
    lossless = !!m.lossless;
    stripes = m.stripes?.length ? m.stripes : [[0, m.h]];
    renderer?.setSource(m.w, m.h);
    reset();
    post({ t: "hello", hello: m });
  }
}
function open(url) {
  const s = new WebSocket(url);
  socket = s;
  s.binaryType = "arraybuffer";
  s.addEventListener("open", () => send(command.video(wantVideo)));
  s.addEventListener("close", () => {
    reset();
    post({ t: "closed" });
  });
  s.addEventListener("message", (e) => {
    if (typeof e.data === "string")
      return onText(e.data);
    const b = new Uint8Array(e.data);
    if (b[0] === Bin.Video)
      onVideo(b);
    else if (b[0] === Bin.Damage)
      onDamage(b);
    else if (b[0] === Bin.Patch)
      onPatch(b);
    else if (b[0] === Bin.Cursor)
      post({ t: "cursor", buf: e.data }, [e.data]);
  });
}
self.addEventListener("message", (e) => {
  const m = e.data;
  if (m.t === "open") {
    renderer = Renderer.create(m.canvas);
    if (!renderer)
      return post({ t: "closed" });
    open(m.url);
  } else if (m.t === "send")
    send(m.d);
  else if (m.t === "view") {
    renderer?.setView(m.w, m.h);
    renderer?.present();
  } else if (m.t === "video" && m.on !== wantVideo) {
    wantVideo = m.on;
    if (m.on)
      reset();
    send(command.video(m.on));
  }
});
