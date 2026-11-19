import { command } from "./voxl-protocol.js";

const BUTTONS = [
  [1, 1],
  [2, 3],
  [4, 2],
  [8, 8],
  [16, 9],
];

const NAMED = {
  Backspace: 0xff08,
  Tab: 0xff09,
  Enter: 0xff0d,
  Escape: 0xff1b,
  Delete: 0xffff,
  Home: 0xff50,
  ArrowLeft: 0xff51,
  ArrowUp: 0xff52,
  ArrowRight: 0xff53,
  ArrowDown: 0xff54,
  PageUp: 0xff55,
  PageDown: 0xff56,
  End: 0xff57,
  Insert: 0xff63,
  CapsLock: 0xffe5,
  NumLock: 0xff7f,
  ScrollLock: 0xff14,
  Pause: 0xff13,
  PrintScreen: 0xff61,
  ContextMenu: 0xff67,
  AltGraph: 0xfe03,
  " ": 0x20,
  ...Object.fromEntries(Array.from({ length: 24 }, (_, at) => [`F${at + 1}`, 0xffbe + at])),
};
const BY_CODE = {
  ShiftLeft: 0xffe1,
  ShiftRight: 0xffe2,
  AltLeft: 0xffe9,
  AltRight: 0xffea,
  NumpadEnter: 0xff0d,
};
const RETURN = 0xff0d;
const BACKSPACE = 0xff08;
const DELETE = 0xffff;
const LEAST_POINTER = 16;

const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

function characterKeysym(point) {
  return (point >= 0x20 && point <= 0x7e) || (point >= 0xa0 && point <= 0xff) ? point : 0x1000000 + point;
}

function keysymOf(event) {
  const { code, key } = event;
  if (code.startsWith("Control") || code.startsWith("Meta") || code.startsWith("OS")) {
    const right = code.endsWith("Right");
    if (code.startsWith("Control") || mac) return right ? 0xffe4 : 0xffe3;
    return right ? 0xffec : 0xffeb;
  }
  if (BY_CODE[code] !== undefined) return code === "AltRight" && key === "AltGraph" ? 0xfe03 : BY_CODE[code];
  if (NAMED[key] !== undefined) return NAMED[key];
  if (!key || key === "Dead" || key === "Unidentified" || key === "Process") return 0;
  if ([...key].length !== 1) return 0;
  return characterKeysym(key.codePointAt(0));
}

export class VoxlClient extends EventTarget {
  viewOnly = true;
  desk = { w: 0, h: 0, density: 1 };
  connected = false;
  ended = false;
  pressed = 0;
  keys = new Map();
  pendingPaste = null;
  cursor = null;
  scroll = { x: 0, y: 0, queued: false };
  undo = [];

  constructor(target, url) {
    super();
    this.root = document.createElement("div");
    Object.assign(this.root.style, { position: "relative", width: "100%", height: "100%", overflow: "hidden" });
    this.canvas = document.createElement("canvas");
    Object.assign(this.canvas.style, { position: "absolute", display: "block", outline: "none" });
    this.field = document.createElement("textarea");
    this.field.setAttribute("aria-hidden", "true");
    this.field.autocapitalize = "off";
    this.field.spellcheck = false;
    Object.assign(this.field.style, {
      position: "absolute",
      left: "0",
      top: "0",
      width: "1px",
      height: "1px",
      opacity: "0",
      border: "0",
      padding: "0",
      resize: "none",
      pointerEvents: "none",
    });
    this.root.append(this.canvas, this.field);
    target.appendChild(this.root);

    this.worker = new Worker(new URL("./voxl-worker.js", import.meta.url), { type: "module" });
    this.worker.addEventListener("message", (event) => this.heard(event.data));
    const offscreen = this.canvas.transferControlToOffscreen();
    this.post({ t: "open", canvas: offscreen, url }, [offscreen]);

    const observer = new ResizeObserver((entries) => {
      const box = entries.find((entry) => entry.target === this.canvas)?.devicePixelContentBoxSize?.[0];
      if (box) this.post({ t: "view", w: box.inlineSize, h: box.blockSize });
      else this.layout();
    });
    observer.observe(this.root);
    observer.observe(this.canvas, { box: "device-pixel-content-box" });
    this.undo.push(() => observer.disconnect());
    this.listen();
  }

  post(message, transfer = []) {
    this.worker.postMessage(message, transfer);
  }

  send(text) {
    if (!this.ended) this.post({ t: "send", d: text });
  }

  heard(message) {
    if (message.t === "hello") return this.hello(message.hello);
    if (message.t === "drawn") return this.dispatchEvent(new Event("drawn"));
    if (message.t === "cursor") {
      this.cursor = message.buf;
      return this.drawCursor();
    }
    if (message.t === "clip") {
      if (!this.viewOnly && document.hasFocus()) navigator.clipboard?.writeText(message.text).catch(() => {});
      return;
    }
    if (message.t === "closed") this.end();
  }

  hello(hello) {
    this.desk = { w: hello.w, h: hello.h, density: hello.density || 1 };
    this.layout();
    if (this.connected) return;
    this.connected = true;
    this.dispatchEvent(new Event("connect"));
  }

  end() {
    if (this.ended) return;
    this.ended = true;
    for (const undo of this.undo) undo();
    this.undo = [];
    this.worker.terminate();
    this.root.remove();
    this.dispatchEvent(new Event("disconnect"));
  }

  layout() {
    const { w: dw, h: dh, density } = this.desk;
    if (!dw || this.ended) return;
    const dpr = window.devicePixelRatio || 1;
    const box = this.root.getBoundingClientRect();
    const vw = this.root.clientWidth * dpr;
    const vh = this.root.clientHeight * dpr;
    let s = Math.min(dpr / density, vw / dw, vh / dh);
    if (s >= 1 && s - Math.floor(s) < 0.06) s = Math.floor(s);
    const w = Math.max(1, Math.round(dw * s));
    const h = Math.max(1, Math.round(dh * s));
    const left = box.left * dpr;
    const top = box.top * dpr;
    const x = Math.max(0, Math.floor(left + (vw - w) / 2) - left);
    const y = Math.max(0, Math.floor(top + (vh - h) / 2) - top);
    Object.assign(this.canvas.style, {
      left: `${x / dpr}px`,
      top: `${y / dpr}px`,
      width: `${w / dpr}px`,
      height: `${h / dpr}px`,
    });
    this.post({ t: "view", w, h });
    this.drawCursor();
  }

  drawCursor() {
    const message = this.cursor;
    if (!message) return;
    const view = new DataView(message);
    const w = view.getUint16(2);
    const h = view.getUint16(4);
    const hx = view.getUint16(6);
    const hy = view.getUint16(8);
    if (!w || !h) {
      this.canvas.style.cursor = "none";
      return;
    }
    const picture = document.createElement("canvas");
    picture.width = w;
    picture.height = h;
    picture.getContext("2d").putImageData(new ImageData(new Uint8ClampedArray(message, 14, w * h * 4), w, h), 0, 0);
    const dpr = window.devicePixelRatio || 1;
    const shown = this.canvas.clientWidth / (this.desk.w || 1);
    const s = Math.max(shown, LEAST_POINTER / Math.max(w, h));
    const scaled = document.createElement("canvas");
    scaled.width = Math.max(1, Math.round(w * s * dpr));
    scaled.height = Math.max(1, Math.round(h * s * dpr));
    const context = scaled.getContext("2d");
    context.imageSmoothingQuality = "high";
    context.drawImage(picture, 0, 0, scaled.width, scaled.height);
    const url = scaled.toDataURL("image/png");
    this.canvas.style.cursor = `image-set(url("${url}") ${dpr}x) ${Math.round(hx * s)} ${Math.round(hy * s)}, default`;
  }

  toDesk(event) {
    const box = this.canvas.getBoundingClientRect();
    const k = this.desk.w / (this.canvas.clientWidth || 1);
    const x = Math.round((event.clientX - box.left) * k);
    const y = Math.round((event.clientY - box.top) * k);
    return [Math.max(0, Math.min(this.desk.w - 1, x)), Math.max(0, Math.min(this.desk.h - 1, y))];
  }

  mouse(event) {
    if (this.viewOnly || this.ended || !this.desk.w) return;
    this.send(command.move(...this.toDesk(event)));
    const buttons = event.buttons ?? 0;
    for (const [bit, button] of BUTTONS) {
      if ((buttons & bit) !== (this.pressed & bit)) this.send(command.button(button, !!(buttons & bit)));
    }
    this.pressed = buttons;
  }

  listen() {
    const on = (at, type, handler, options) => {
      at.addEventListener(type, handler, options);
      this.undo.push(() => at.removeEventListener(type, handler, options));
    };
    const canvas = this.canvas;
    const field = this.field;
    on(canvas, "mousedown", (event) => {
      if (this.viewOnly) return;
      event.preventDefault();
      this.focus();
      this.mouse(event);
    });
    on(window, "mousemove", (event) => {
      if (event.target === canvas || this.pressed) this.mouse(event);
    });
    on(window, "mouseup", (event) => {
      if (event.target === canvas || this.pressed) this.mouse(event);
    });
    on(canvas, "contextmenu", (event) => {
      if (!this.viewOnly) event.preventDefault();
    });
    on(
      canvas,
      "wheel",
      (event) => {
        if (this.viewOnly) return;
        event.preventDefault();
        this.send(command.move(...this.toDesk(event)));
        const unit = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? 800 : 1;
        const k = unit * (this.desk.w / (canvas.clientWidth || 1));
        this.scroll.x += event.deltaX * k;
        this.scroll.y += event.deltaY * k;
        if (this.scroll.queued) return;
        this.scroll.queued = true;
        requestAnimationFrame(() => {
          this.scroll.queued = false;
          const { x, y } = this.scroll;
          this.scroll.x = 0;
          this.scroll.y = 0;
          if (Math.abs(x) >= 0.5 || Math.abs(y) >= 0.5) this.send(command.scroll(x, y));
        });
      },
      { passive: false },
    );
    on(field, "keydown", (event) => {
      if (this.viewOnly || event.isComposing) return;
      const keysym = keysymOf(event);
      if (!keysym) return;
      const accel = mac ? event.metaKey : event.ctrlKey;
      if (accel && !event.altKey && event.key.toLowerCase() === "v" && !event.repeat) {
        this.pendingPaste = { code: event.code, keysym, timer: setTimeout(() => this.flushPaste(), 150) };
        return;
      }
      event.preventDefault();
      const held = this.keys.get(event.code);
      if (event.repeat && held !== undefined) return this.send(command.key(held, true));
      this.press(event.code, keysym);
    });
    on(field, "keyup", (event) => {
      if (this.viewOnly) return;
      event.preventDefault();
      if (this.pendingPaste?.code === event.code) this.flushPaste();
      this.release(event.code);
      if (mac && event.code.startsWith("Meta")) {
        for (const code of [...this.keys.keys()]) {
          if (!/^(Shift|Control|Alt|Meta)/.test(code)) this.release(code);
        }
      }
    });
    on(field, "paste", (event) => {
      if (this.viewOnly) return;
      event.preventDefault();
      const text = event.clipboardData?.getData("text/plain");
      if (text) this.send(command.clipboard(text));
      this.flushPaste();
    });
    const tap = (keysym) => {
      this.send(command.key(keysym, true));
      this.send(command.key(keysym, false));
    };
    const type = (text) => {
      for (const character of text) tap(character === "\n" ? RETURN : characterKeysym(character.codePointAt(0)));
    };
    on(field, "compositionend", (event) => {
      if (!this.viewOnly && event.data) type(event.data);
      field.value = "";
    });
    on(field, "input", (event) => {
      if (this.viewOnly || event.isComposing) return;
      if (event.inputType === "insertText" && event.data) type(event.data);
      else if (event.inputType === "deleteContentBackward") tap(BACKSPACE);
      else if (event.inputType === "deleteContentForward") tap(DELETE);
      else if (event.inputType === "insertLineBreak" || event.inputType === "insertParagraph") tap(RETURN);
      field.value = "";
    });
    on(window, "blur", () => this.releaseKeys());
    on(document, "visibilitychange", () => this.post({ t: "video", on: !document.hidden }));
    on(window, "resize", () => this.layout());
  }

  press(code, keysym) {
    this.keys.set(code, keysym);
    this.send(command.key(keysym, true));
  }

  release(code) {
    const keysym = this.keys.get(code);
    if (keysym === undefined) return;
    this.keys.delete(code);
    this.send(command.key(keysym, false));
  }

  flushPaste() {
    const pending = this.pendingPaste;
    if (!pending) return;
    this.pendingPaste = null;
    clearTimeout(pending.timer);
    this.press(pending.code, pending.keysym);
  }

  releaseKeys() {
    if (!this.keys.size) return;
    this.keys.clear();
    this.send(command.releaseAll);
  }

  focus() {
    this.field.focus({ preventScroll: true });
  }

  blur() {
    this.releaseKeys();
    this.field.blur();
  }
}
