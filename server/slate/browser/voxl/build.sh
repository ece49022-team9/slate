#!/usr/bin/env bash
set -euo pipefail
COMMIT=47bb94ec4d253638c1470eb81906676e1bef4b00
RUST=1.99.1
ESBUILD=0.20.1
ESBUILD_SHA512=e6044f93ba4ab9a201fad987fb229dd9a413469aa57f51387ff982fad6b0226fc21897a665c1d9a69da273ca03f379ca8143cc11dd1f35a9eb9c59638abbaec8
out=${1:?usage: build.sh <out>}
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl git build-essential pkg-config \
  clang libclang-dev libx264-dev libyuv-dev libopus-dev libpulse-dev libxcb1-dev libxcb-shm0-dev \
  libxcb-xfixes0-dev libxcb-randr0-dev libxcb-xtest0-dev libxcb-damage0-dev
curl -fsSLo "$work/esbuild.tgz" "https://registry.npmjs.org/@esbuild/linux-x64/-/linux-x64-$ESBUILD.tgz"
echo "$ESBUILD_SHA512  $work/esbuild.tgz" | sha512sum -c -
tar -xzf "$work/esbuild.tgz" -C "$work"
git -C "$work" init -q voxl
git -C "$work/voxl" fetch -q --depth 1 https://github.com/tembo/voxl "$COMMIT"
git -C "$work/voxl" checkout -q FETCH_HEAD
git -C "$work/voxl" -c user.name=Slate -c user.email=slate@localhost am -q "$here/blob.patch"
export CARGO_HOME="$work/cargo" RUSTUP_HOME="$work/rustup" ESBUILD="$work/package/bin/esbuild"
curl -fsSL https://sh.rustup.rs |
  sh -s -- -y -q --profile minimal --default-toolchain "$RUST" --no-modify-path
(cd "$work/voxl" && "$CARGO_HOME/bin/cargo" build -q --release --locked)
install -D -m 755 -s "$work/voxl/target/release/voxl" "$out/voxl"
"$out/voxl" --version
