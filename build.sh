#!/bin/bash
# Android 16 / 6.12 GKI kernel build for the Infinix X6871 (MT6895).
#
# Builds arch/arm64/boot/Image into $OUT_DIR.  On first run the pinned
# toolchains (LLVM, rustc, AOSP build-tools) and bindgen are downloaded into
# $TOOLCHAIN_ROOT, so this script is self-contained on a fresh machine.
#
# Overridable environment:
#   TOOLCHAIN        C toolchain          (default: kernel-llvm)
#                    kernel-llvm  = kernel.org LLVM (default)
#                    google-clang = AOSP/GKI clang
#   TOOLCHAIN_ROOT   toolchain cache      (default: <repo>/toolchain)
#   OUT_DIR          kbuild output dir    (default: <repo>/out)
#   JOBS             parallel jobs        (default: all CPUs)
#   LOCALVERSION     UTS release suffix   (default: -android16-5-mtk-Rama982/r1)
#   TZ               build timezone       (default: Asia/Jakarta)
#   CLANG_URL / KERNEL_LLVM_URL / RUST_URL / BUILD_TOOLS_URL
#                    toolchain archives   (default: AOSP / kernel.org)
#   CLANG_VERSION / KERNEL_LLVM_VERSION / RUST_VERSION / BINDGEN_VERSION
#                    pinned versions       (see below)
#
# A host C compiler is not required: the downloaded clang is also used by
# cargo when installing bindgen.
#
# Usage: ./build.sh

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

# ==== Toolchains (C compiler selectable) ====
TOOLCHAIN=${TOOLCHAIN:-kernel-llvm}

CLANG_VERSION=${CLANG_VERSION:-r536225}
KERNEL_LLVM_VERSION=${KERNEL_LLVM_VERSION:-23.1.4}
RUST_VERSION=${RUST_VERSION:-1.82.0.p2}
# The AOSP rust archive is versioned "1.82.0" while the extracted directory
# keeps the full "1.82.0.p2" tag.
RUST_ARCHIVE_VERSION=${RUST_VERSION%%.p*}
# bindgen >= 0.72.1 is required for libclang >= 22 (older versions emit
# opaque structs for forward-declared types, breaking the Rust bindings).
BINDGEN_VERSION=0.72.1

CLANG_URL=${CLANG_URL:-https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/refs/heads/main-kernel-2025/clang-${CLANG_VERSION}.tar.gz}
KERNEL_LLVM_URL=${KERNEL_LLVM_URL:-https://mirrors.edge.kernel.org/pub/tools/llvm/files/llvm-${KERNEL_LLVM_VERSION}-x86_64.tar.xz}
RUST_URL=${RUST_URL:-https://android.googlesource.com/platform/prebuilts/rust/+archive/refs/heads/main-kernel-2025/linux-x86/${RUST_ARCHIVE_VERSION}.tar.gz}
BUILD_TOOLS_URL=${BUILD_TOOLS_URL:-https://android.googlesource.com/kernel/prebuilts/build-tools/+archive/refs/heads/main-kernel-2025/linux-x86.tar.gz}

readonly CLANG_VERSION KERNEL_LLVM_VERSION RUST_VERSION RUST_ARCHIVE_VERSION BINDGEN_VERSION
readonly CLANG_URL KERNEL_LLVM_URL RUST_URL BUILD_TOOLS_URL

# ==== Paths ====
TOOLCHAIN_ROOT=${TOOLCHAIN_ROOT:-$SCRIPT_DIR/toolchain}
OUT_DIR=${OUT_DIR:-$SCRIPT_DIR/out}
JOBS=${JOBS:-$(nproc --all 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)}
LOCALVERSION=${LOCALVERSION:-"-android16-5-mtk-Rama982/r1"}
# Embedded in UTS_VERSION by init/Makefile ("date" honors TZ).
TZ=${TZ:-Asia/Jakarta}

GOOGLE_CLANG_HOME=$TOOLCHAIN_ROOT/clang-$CLANG_VERSION
KERNEL_LLVM_HOME=$TOOLCHAIN_ROOT/llvm-$KERNEL_LLVM_VERSION-x86_64
CLANG_HOME=
RUST_HOME=$TOOLCHAIN_ROOT/rust-$RUST_VERSION
BUILD_TOOLS_HOME=$TOOLCHAIN_ROOT/build-tools
BINDGEN_HOME=$TOOLCHAIN_ROOT/bindgen

# ==== Helpers ====
log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

TMP_DIR=
SECONDS=0

on_exit() {
	local rc=$?

	if [[ -n $TMP_DIR ]]; then
		rm -rf "$TMP_DIR"
	fi
	if ((rc != 0)); then
		printf 'Build failed (rc=%d) after %dm %02ds\n' \
			"$rc" "$((SECONDS / 60))" "$((SECONDS % 60))" >&2
	fi
}
trap on_exit EXIT

require_host_tools() {
	local missing=() cmd

	for cmd in make tar xz gzip perl python3 bc bison flex openssl; do
		have "$cmd" || missing+=("$cmd")
	done
	if ((${#missing[@]} > 0)); then
		die "missing host tools: ${missing[*]} (install your distro's kernel build dependencies)"
	fi
}

fetch() {
	local url=$1 dest=$2

	if have curl; then
		curl --fail --location --retry 3 --retry-delay 2 \
			--connect-timeout 30 --output "$dest" "$url"
	elif have wget; then
		wget --tries=3 --timeout=30 --output-document="$dest" "$url"
	else
		die "curl or wget is required to download the toolchains"
	fi
}

# install_toolchain <name> <url> <dest> <marker> [strip-components]
# Skips the download when <dest>/<marker> already exists.  Unpacks into a
# staging directory first so an interrupted download/extract never leaves a
# half-populated toolchain behind.
install_toolchain() {
	local name=$1 url=$2 dest=$3 marker=$4 strip=${5:-0}
	local ext=${url##*.} archive stage

	if [[ -e $dest/$marker ]]; then
		return
	fi

	log "Fetching $name"
	mkdir -p "$TOOLCHAIN_ROOT"
	TMP_DIR=$(mktemp -d "$TOOLCHAIN_ROOT/.tmp.XXXXXX")
	archive=$TMP_DIR/archive.$ext
	stage=$TMP_DIR/stage

	fetch "$url" "$archive"
	mkdir -p "$stage"
	case $ext in
	xz) tar -xJf "$archive" -C "$stage" --strip-components="$strip" ;;
	gz) tar -xzf "$archive" -C "$stage" --strip-components="$strip" ;;
	*) die "$name: unsupported archive type .$ext" ;;
	esac
	[[ -e $stage/$marker ]] || die "$name archive does not contain $marker"

	rm -rf "$dest"
	mv "$stage" "$dest"
	rm -rf "$TMP_DIR"
	TMP_DIR=
	log "$name -> $dest"
}

select_toolchain() {
	case $TOOLCHAIN in
	kernel-llvm)
		install_toolchain "LLVM $KERNEL_LLVM_VERSION (kernel.org)" \
			"$KERNEL_LLVM_URL" "$KERNEL_LLVM_HOME" bin/clang 1
		CLANG_HOME=$KERNEL_LLVM_HOME
		;;
	google-clang)
		install_toolchain "clang $CLANG_VERSION (Google)" \
			"$CLANG_URL" "$GOOGLE_CLANG_HOME" bin/clang
		CLANG_HOME=$GOOGLE_CLANG_HOME
		;;
	*)
		die "unknown TOOLCHAIN='$TOOLCHAIN' (expected kernel-llvm or google-clang)"
		;;
	esac
	log "$TOOLCHAIN -> $CLANG_HOME"
}

setup_toolchain_env() {
	export PATH="$CLANG_HOME/bin:$RUST_HOME/bin:$BUILD_TOOLS_HOME/bin:$BINDGEN_HOME/bin:$PATH"
	hash -r

	# bindgen uses clang-sys, which dlopen()s libclang from LIBCLANG_PATH.
	# Nothing else needs LD_LIBRARY_PATH: the toolchains carry RUNPATH.
	export LIBCLANG_PATH=$CLANG_HOME/lib

	# gendwarfksyms links against libdw/libelf from the AOSP build-tools.
	if [[ -d $BUILD_TOOLS_HOME/include ]]; then
		export HOSTCFLAGS="-I$BUILD_TOOLS_HOME/include ${HOSTCFLAGS:-}"
		export HOSTLDFLAGS="-L$BUILD_TOOLS_HOME/lib64 -Wl,-rpath,$BUILD_TOOLS_HOME/lib64 ${HOSTLDFLAGS:-}"
	fi

	# LLVM=1 makes kbuild pick clang/ld.lld/llvm-* itself and export them.
	export LLVM=1
	export ARCH=arm64 LOCALVERSION TZ
	export KBUILD_GENDWARFKSYMS_STABLE=1
}

ensure_bindgen() {
	local have_version=

	if [[ -x $BINDGEN_HOME/bin/bindgen ]]; then
		have_version=$("$BINDGEN_HOME/bin/bindgen" --version 2>/dev/null |
			sed -n 's/^bindgen \([0-9][0-9.]*\).*/\1/p')
		if [[ $have_version == "$BINDGEN_VERSION" ]]; then
			return
		fi
		log "Replacing bindgen ${have_version:-unknown} with $BINDGEN_VERSION"
	fi

	log "Installing bindgen-cli $BINDGEN_VERSION into $BINDGEN_HOME (one-time)"
	have cargo || die "cargo not found; cannot install bindgen-cli"
	# Cargo's build scripts and rustc's linker invocation need a host `cc`.
	# When the machine has no gcc, expose the selected clang under that name.
	# A wrapper is used rather than a symlink because clang's shared driver
	# dispatches on argv[0], and is only "clang" when invoked under that name.
	if ! have cc && ! have gcc; then
		local shim=$TOOLCHAIN_ROOT/host-cc

		mkdir -p "$shim"
		printf '#!/bin/sh\nexec %q "$@"\n' "$CLANG_HOME/bin/clang" > "$shim/cc"
		chmod +x "$shim/cc"
		export PATH="$shim:$PATH"
		hash -r
	fi
	cargo install --locked --root "$BINDGEN_HOME" --force "bindgen-cli@$BINDGEN_VERSION"
	[[ -x $BINDGEN_HOME/bin/bindgen ]] || die "bindgen-cli installation failed"
}

# Kbuild does not track the bindgen/libclang version in its command line, so
# switching toolchains would otherwise silently reuse stale bindings.
stale_bindings() {
	local stamp=$OUT_DIR/.bindgen-stamp want have_stamp=

	want="$BINDGEN_VERSION $(basename "$CLANG_HOME")"
	mkdir -p "$OUT_DIR"
	if [[ -f $stamp ]]; then
		have_stamp=$(<"$stamp")
	fi
	if [[ $have_stamp == "$want" ]]; then
		return
	fi
	if [[ -f $OUT_DIR/rust/bindings/bindings_generated.rs ]]; then
		log "bindgen/libclang changed (${have_stamp:-unknown} -> $want); regenerating Rust bindings"
		rm -f "$OUT_DIR"/rust/bindings/bindings_generated.rs \
			"$OUT_DIR"/rust/bindings/bindings_helpers_generated.rs \
			"$OUT_DIR"/rust/uapi/uapi_generated.rs
	fi
	printf '%s\n' "$want" > "$stamp"
}

report() {
	local image=$OUT_DIR/arch/arm64/boot/Image
	local uts_file=$OUT_DIR/include/generated/utsrelease.h
	local version=

	if [[ -f $uts_file ]]; then
		version=$(sed -n 's/^#define[[:space:]]\+UTS_RELEASE[[:space:]]\+"\(.*\)"/\1/p' "$uts_file")
	fi

	printf '\nBuild time: %dm %02ds\n' "$((SECONDS / 60))" "$((SECONDS % 60))"
	printf 'Toolchain: %s (%s)\n' "$TOOLCHAIN" \
		"$("$CLANG_HOME/bin/clang" --version | head -n1)"
	printf 'bindgen: %s\n' "$("$BINDGEN_HOME/bin/bindgen" --version 2>/dev/null || echo unknown)"
	if [[ -n $version ]]; then
		printf 'Kernel version: %s\n' "$version"
	fi

	[[ -f $image ]] || die "build finished but $image is missing"
	printf 'Image: %s (%s bytes)\n' "$image" "$(stat -c %s "$image")"
	printf 'sha256: %s\n' "$(sha256sum "$image" | awk '{print $1}')"
}

main() {
	require_host_tools

	select_toolchain
	install_toolchain "rustc $RUST_VERSION (Google)" "$RUST_URL" "$RUST_HOME" bin/rustc
	install_toolchain "AOSP build-tools" "$BUILD_TOOLS_URL" "$BUILD_TOOLS_HOME" bin/pahole

	setup_toolchain_env
	ensure_bindgen
	stale_bindings

	log "Configuring kbuild (O=$OUT_DIR)"
	make O="$OUT_DIR" rustavailable gki_defconfig savedefconfig

	log "Building Image (-j$JOBS)"
	make -j"$JOBS" O="$OUT_DIR" dtbs Image

	report
}

main "$@"
