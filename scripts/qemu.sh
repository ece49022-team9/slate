set -eo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QEMU_DIR="${SLATE_QEMU_DIR:-$HOME/esp/qemu-slate}"
TAG=esp-develop-9.2.2-20260417

if [ ! -d "$QEMU_DIR" ]; then
    git clone --depth 1 --branch "$TAG" https://github.com/espressif/qemu.git "$QEMU_DIR"
fi
cd "$QEMU_DIR"
if git apply --reverse --check "$ROOT/sim/qemu.patch" 2> /dev/null; then
    echo "slate.qemu: Slate patch already applied"
elif git apply --check "$ROOT/sim/qemu.patch"; then
    git apply "$ROOT/sim/qemu.patch"
else
    echo "slate.qemu: checkout has an older patch or conflicting changes." >&2
    echo "Use a fresh checkout: SLATE_QEMU_DIR=\"\$HOME/esp/qemu-slate-speaker\" make sim-setup" >&2
    exit 1
fi
if [ "$(uname -s)" = Darwin ]; then
    brew install libgcrypt glib pixman libslirp
fi
command -v ninja > /dev/null || uv tool install ninja
if [ ! -f build/build.ninja ]; then
    ./configure --target-list=xtensa-softmmu --enable-gcrypt --enable-slirp \
        --disable-werror --disable-docs --disable-sdl --disable-gtk \
        --disable-cocoa --disable-gnutls
fi
build/pyvenv/bin/meson setup --reconfigure build -Dwerror=false -Ddtc:werror=false
ninja -C build qemu-system-xtensa
echo "slate.qemu: built $QEMU_DIR/build/qemu-system-xtensa"
