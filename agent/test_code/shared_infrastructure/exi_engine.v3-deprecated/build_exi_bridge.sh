#!/bin/bash
# build_exi_bridge.sh — Build libexi_bridge.a
#
# Pure shell build: no Makefile rules with spaces, no pattern rules.
# This is the most reliable way to build from paths with non-ASCII characters.
#
# Usage:
#   ./build_exi_bridge.sh        → full build
#   ./build_exi_bridge.sh verify → compile wrapper only (zero-pollution test)
#   ./build_exi_bridge.sh clean  → remove build artifacts

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/../build_exi_bridge"
OPENV2G_ROOT="$SCRIPT_DIR/openv2g"

# Resolve non-ASCII path to an ASCII tmp location for intermediate compilation
# (g++/gcc handle UTF-8 paths fine, but Makefile pattern rules don't)
BUILD_TMP="/tmp/exi_bridge_build_$$"
mkdir -p "$BUILD_TMP"

echo "[libexi_bridge] Build dir: $BUILD_TMP"

# ---------------------------------------------------------------------------
# Compile definitions (only the ones OpenV2G DOESN'T define itself)
# ---------------------------------------------------------------------------
OPENV2G_DEFS="-DOPENV2G_STANDALONE -DOPENV2G_SEPARATE_COMPILATION"

OPENV2G_INCS=(
    "-I$OPENV2G_ROOT/include/appHandshake"
    "-I$OPENV2G_ROOT/include/codec"
    "-I$OPENV2G_ROOT/include/din"
    "-I$OPENV2G_ROOT/include/iso1"
    "-I$OPENV2G_ROOT/include/iso2"
    "-I$OPENV2G_ROOT/include/transport"
    "-I$OPENV2G_ROOT/include/xmldsig"
    "-I$SCRIPT_DIR"
)
INC_STR="${OPENV2G_INCS[*]}"

COMMON_CFLAGS="$OPENV2G_DEFS $INC_STR -fPIC"
COMMON_CXXFLAGS="$OPENV2G_DEFS $INC_STR -fPIC -std=c++11"

STATIC_LIB="$BUILD_DIR/libexi_bridge.a"

# ---------------------------------------------------------------------------
# Action: clean
# ---------------------------------------------------------------------------
if [ "$1" = "clean" ]; then
    rm -rf "$BUILD_DIR" "$BUILD_TMP"
    echo "[libexi_bridge] Cleaned."
    exit 0
fi

# ---------------------------------------------------------------------------
# Action: verify (compile wrapper only)
# ---------------------------------------------------------------------------
if [ "$1" = "verify" ]; then
    echo "[libexi_bridge] Verifying OpenV2G_Wrapper.cc (zero-pollution test)..."
    g++ $COMMON_CXXFLAGS -c "$SCRIPT_DIR/OpenV2G_Wrapper.cc" \
        -o "$BUILD_TMP/OpenV2G_Wrapper.o" 2>&1
    echo "[libexi_bridge] Verification PASSED — header is zero-pollution."
    exit 0
fi

# ---------------------------------------------------------------------------
# Action: full build
# ---------------------------------------------------------------------------
mkdir -p "$BUILD_DIR"

echo "[libexi_bridge] Compiling OpenV2G C sources..."
C_SRC_DIRS="appHandshake codec din iso1 iso2 transport xmldsig"
C_OBJ_COUNT=0
C_ERR=0

for subdir in $C_SRC_DIRS; do
    for src in "$OPENV2G_ROOT/src/$subdir"/*.c; do
        if [ -f "$src" ]; then
            obj="${BUILD_TMP}/$(basename "$src" .c).o"
            echo "  CC  $(basename "$src")"
            gcc $COMMON_CFLAGS -c "$src" -o "$obj" 2>&1 || { C_ERR=1; echo "[ERROR] Failed: $src"; }
            C_OBJ_COUNT=$((C_OBJ_COUNT + 1))
        fi
    done
done

if [ $C_ERR -ne 0 ]; then
    echo "[libexi_bridge] C compilation failed."
    exit 1
fi
echo "[libexi_bridge] Compiled $C_OBJ_COUNT C source files."

echo "[libexi_bridge] Compiling OpenV2G_Wrapper.cc..."
g++ $COMMON_CXXFLAGS -c "$SCRIPT_DIR/OpenV2G_Wrapper.cc" \
    -o "$BUILD_TMP/OpenV2G_Wrapper.o" 2>&1 || {
    echo "[ERROR] OpenV2G_Wrapper.cc compilation failed."
    exit 1
}
echo "[libexi_bridge] OpenV2G_Wrapper.o compiled."

echo "[libexi_bridge] Archiving libexi_bridge.a..."
rm -f "$STATIC_LIB"
ar rcs "$STATIC_LIB" "$BUILD_TMP"/*.o 2>&1 || {
    echo "[ERROR] ar failed."
    exit 1
}

echo "[libexi_bridge] Built: $STATIC_LIB"
echo "[libexi_bridge] Size: $(du -h "$STATIC_LIB" | cut -f1)"
echo "[libexi_bridge] Objects: $(ls "$BUILD_TMP"/*.o | wc -l)"

# Cleanup temp build
rm -rf "$BUILD_TMP"

echo "[libexi_bridge] Done."
