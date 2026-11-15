const QUAD = `#version 300 es
in vec2 p;
void main() { gl_Position = vec4(p, 0.0, 1.0); }`;
const COPY = `#version 300 es
precision highp float;
uniform highp sampler2D src;
uniform ivec2 at;
out vec4 color;
void main() { color = texelFetch(src, ivec2(gl_FragCoord.xy) - at, 0); }`;
const WHOLE = `#version 300 es
precision highp float;
uniform highp sampler2D src;
uniform int k;
uniform int rows;
out vec4 color;
void main() {
	ivec2 o = ivec2(gl_FragCoord.xy);
	color = texelFetch(src, ivec2(o.x / k, (rows - 1 - o.y) / k), 0);
}`;
const LANCZOS = `#version 300 es
precision highp float;
uniform highp sampler2D src;
uniform float scale;
uniform int vertical;
uniform int rows;
uniform int last;
out vec4 color;
float lanczos(float x) {
	x = abs(x);
	if (x < 1e-5) return 1.0;
	if (x >= 3.0) return 0.0;
	float p = 3.14159265 * x;
	return 3.0 * sin(p) * sin(p / 3.0) / (p * p);
}
void main() {
	ivec2 o = ivec2(gl_FragCoord.xy);
	if (vertical == 1) o.y = rows - 1 - o.y;
	float c = (float(vertical == 1 ? o.y : o.x) + 0.5) / scale - 0.5;
	float shrink = min(scale, 1.0);
	float reach = 3.0 / shrink;
	int i0 = int(floor(c - reach)) + 1;
	int i1 = int(floor(c + reach));
	vec4 sum = vec4(0.0);
	float wsum = 0.0;
	for (int i = i0; i <= i1; i++) {
		float w = lanczos((float(i) - c) * shrink);
		int j = clamp(i, 0, last);
		sum += w * texelFetch(src, vertical == 1 ? ivec2(o.x, j) : ivec2(j, o.y), 0);
		wsum += w;
	}
	color = clamp(sum / wsum, 0.0, 1.0);
}`;

export class Renderer {
  canvas;
  gl;
  copy;
  whole;
  lanczos;
  quad;
  rects;
  screen = null;
  screenFb = null;
  frameTex;
  between = null;
  betweenFb = null;
  betweenSize = [0, 0];
  floatTargets;
  src = [0, 0];
  view = [0, 0];
  static create(canvas) {
    const gl = canvas.getContext("webgl2", {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      preserveDrawingBuffer: false,
      desynchronized: true
    });
    return gl ? new Renderer(gl, canvas) : null;
  }
  constructor(gl, canvas) {
    this.canvas = canvas;
    this.gl = gl;
    this.floatTargets = !!gl.getExtension("EXT_color_buffer_float");
    this.copy = this.program(COPY, ["src", "at"]);
    this.whole = this.program(WHOLE, ["src", "k", "rows"]);
    this.lanczos = this.program(LANCZOS, ["src", "scale", "vertical", "rows", "last"]);
    this.quad = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    this.rects = gl.createBuffer();
    this.frameTex = this.texture();
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
  }
  program(fragment, uniforms) {
    const gl = this.gl;
    const p = gl.createProgram();
    for (const [type, src] of [
      [gl.VERTEX_SHADER, QUAD],
      [gl.FRAGMENT_SHADER, fragment]
    ]) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
        throw new Error("shader: " + gl.getShaderInfoLog(s));
      gl.attachShader(p, s);
    }
    gl.bindAttribLocation(p, 0, "p");
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS))
      throw new Error("program: " + gl.getProgramInfoLog(p));
    const u = {};
    for (const n of uniforms)
      u[n] = gl.getUniformLocation(p, n);
    return { p, u };
  }
  texture() {
    const gl = this.gl;
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }
  target(w, h, float) {
    const gl = this.gl;
    const t = this.texture();
    gl.texStorage2D(gl.TEXTURE_2D, 1, float ? gl.RGBA16F : gl.RGBA8, w, h);
    const fb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return [t, fb];
  }
  setSource(w, h) {
    const gl = this.gl;
    if (this.src[0] === w && this.src[1] === h)
      return;
    if (this.screen)
      gl.deleteTexture(this.screen);
    if (this.screenFb)
      gl.deleteFramebuffer(this.screenFb);
    [this.screen, this.screenFb] = this.target(w, h, false);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.screenFb);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    this.src = [w, h];
  }
  setView(w, h) {
    const c = this.canvas;
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
    }
    this.view = [w, h];
  }
  frame(frame, y, rects) {
    const gl = this.gl;
    if (!this.screenFb)
      return;
    const { displayWidth: fw, displayHeight: fh } = frame;
    const area = rects ?? [{ x: 0, y, w: fw, h: fh }];
    const [sw, sh] = this.src;
    const v = [];
    for (const r of area) {
      const x0 = Math.max(r.x, 0), y0 = Math.max(r.y, y), x1 = Math.min(r.x + r.w, fw, sw), y1 = Math.min(r.y + r.h, y + fh, sh);
      if (x1 <= x0 || y1 <= y0)
        continue;
      const a = x0 / sw * 2 - 1, b = y0 / sh * 2 - 1, c = x1 / sw * 2 - 1, d = y1 / sh * 2 - 1;
      v.push(a, b, c, b, a, d, a, d, c, b, c, d);
    }
    if (!v.length)
      return;
    gl.bindTexture(gl.TEXTURE_2D, this.frameTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, frame);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.screenFb);
    gl.viewport(0, 0, sw, sh);
    gl.useProgram(this.copy.p);
    gl.uniform1i(this.copy.u.src, 0);
    gl.uniform2i(this.copy.u.at, 0, y);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.frameTex);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.rects);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(v), gl.STREAM_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLES, 0, v.length / 2);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }
  patch(image, x, y) {
    const gl = this.gl;
    if (!this.screen)
      return;
    gl.bindTexture(gl.TEXTURE_2D, this.screen);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, x, y, gl.RGBA, gl.UNSIGNED_BYTE, image);
  }
  present() {
    const gl = this.gl;
    const [sw, sh] = this.src;
    const [vw, vh] = this.view;
    if (!this.screen || !vw || !vh)
      return;
    gl.activeTexture(gl.TEXTURE0);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    const k = vw / sw;
    if (Number.isInteger(k) && k === vh / sh) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, vw, vh);
      gl.useProgram(this.whole.p);
      gl.uniform1i(this.whole.u.src, 0);
      gl.uniform1i(this.whole.u.k, k);
      gl.uniform1i(this.whole.u.rows, vh);
      gl.bindTexture(gl.TEXTURE_2D, this.screen);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      return;
    }
    if (this.betweenSize[0] !== vw || this.betweenSize[1] !== sh) {
      if (this.between)
        gl.deleteTexture(this.between);
      if (this.betweenFb)
        gl.deleteFramebuffer(this.betweenFb);
      [this.between, this.betweenFb] = this.target(vw, sh, this.floatTargets);
      this.betweenSize = [vw, sh];
    }
    gl.useProgram(this.lanczos.p);
    gl.uniform1i(this.lanczos.u.src, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.betweenFb);
    gl.viewport(0, 0, vw, sh);
    gl.uniform1f(this.lanczos.u.scale, vw / sw);
    gl.uniform1i(this.lanczos.u.vertical, 0);
    gl.uniform1i(this.lanczos.u.rows, sh);
    gl.uniform1i(this.lanczos.u.last, sw - 1);
    gl.bindTexture(gl.TEXTURE_2D, this.screen);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, vw, vh);
    gl.uniform1f(this.lanczos.u.scale, vh / sh);
    gl.uniform1i(this.lanczos.u.vertical, 1);
    gl.uniform1i(this.lanczos.u.rows, vh);
    gl.uniform1i(this.lanczos.u.last, sh - 1);
    gl.bindTexture(gl.TEXTURE_2D, this.between);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
}
