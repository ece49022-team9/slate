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
else
    git apply "$ROOT/sim/qemu.patch"
fi
command -v ninja > /dev/null || uv tool install ninja
if [ ! -f build/build.ninja ]; then
    ./configure --target-list=xtensa-softmmu --enable-gcrypt --enable-slirp \
        --disable-werror --disable-docs --disable-sdl --disable-gtk \
        --disable-cocoa --disable-gnutls
fi
ninja -C build qemu-system-xtensa
echo "slate.qemu: built $QEMU_DIR/build/qemu-system-xtensa"
