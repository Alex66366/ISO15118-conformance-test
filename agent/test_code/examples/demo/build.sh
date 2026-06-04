#!/bin/bash
# ==========================================
# TITAN TTCN-3 Build Script for Linux
# ISO 15118-2 SECC Conformance Test System
# ==========================================

set -e

echo "[BUILD] Starting TITAN build process..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD_WORK_DIR=""
ORIGINAL_DIR=""
KEEP_TEMP="${KEEP_TEMP:-0}"

_is_path_ascii() {
    ! printf '%s' "$1" | LC_ALL=C grep -q '[^\x00-\x7F]'
}

_handle_non_ascii_path() {
    local original_dir="$1"
    local work_dir
    work_dir=$(mktemp -d /tmp/titan_build_XXXXXX)
    if [ -z "$work_dir" ] || [ ! -d "$work_dir" ]; then
        echo "[ERROR] Failed to create temporary build directory."
        exit 1
    fi
    BUILD_WORK_DIR="$work_dir"
    local project_name="demo_build_$$"

    echo "[BUILD] Project path contains non-ASCII characters."
    echo "[BUILD] Copying project to ASCII temp directory: $work_dir/$project_name"
    mkdir -p "$work_dir/$project_name"
    rsync -a --delete --exclude='build/' --exclude='logs/' \
        "$original_dir/" "$work_dir/$project_name/"
    echo "[BUILD] Project copied. Switching to temp directory for build."
    cd "$work_dir/$project_name"
}

_cleanup_and_restore() {
    local exit_code=$1
    local original_dir="$ORIGINAL_DIR"
    local work_dir="$BUILD_WORK_DIR"

    if [ -z "$work_dir" ] || [ ! -d "$work_dir" ]; then
        return
    fi

    if [ "$exit_code" -ne 0 ] && [ "$KEEP_TEMP" != "0" ]; then
        echo ""
        echo "[BUILD] Build failed (exit code: $exit_code). Temp directory kept:"
        echo "[BUILD]   $work_dir"
        echo "[BUILD]   (Set KEEP_TEMP=0 to auto-clean on failure)"
        return
    fi

    if [ "$exit_code" -eq 0 ]; then
        echo "[BUILD] Syncing build artifacts back to original location..."
        for subdir in build config logs; do
            if [ -d "$work_dir/demo_build_$$/$subdir" ]; then
                mkdir -p "$original_dir/$subdir"
                rsync -a --delete "$work_dir/demo_build_$$/$subdir/" "$original_dir/$subdir/"
            fi
        done
    fi

    if [ "$KEEP_TEMP" = "0" ]; then
        rm -rf "$work_dir"
        echo "[BUILD] Temp directory cleaned up."
    else
        echo "[BUILD] Temp directory kept at: $work_dir"
    fi
}

if ! _is_path_ascii "$SCRIPT_DIR"; then
    if ! command -v rsync &> /dev/null; then
        echo "[ERROR] rsync is required for non-ASCII path builds but is not installed."
        exit 1
    fi
    ORIGINAL_DIR="$SCRIPT_DIR"
    trap '_cleanup_and_restore $?' EXIT
    trap 'echo "[BUILD] Build interrupted."; _cleanup_and_restore 130; exit 130' INT TERM
    _handle_non_ascii_path "$SCRIPT_DIR"
else
    trap 'echo "[BUILD] Build interrupted."; exit 130' INT TERM
fi

# ==========================================
# Step 1: Clean and prepare build directory
# ==========================================
echo "[BUILD] Step 1: Cleaning and preparing build directory..."
rm -rf build
mkdir -p build/generated_cpp
mkdir -p build/obj
mkdir -p build/bin
mkdir -p logs/titan_traces
mkdir -p logs/system_logs
mkdir -p config
echo "[BUILD] Directory structure ready."

# ==========================================
# Step 2: Check TITAN installation
# ==========================================
echo "[BUILD] Step 2: Checking TITAN installation..."
if ! command -v makefilegen &> /dev/null; then
    echo "[ERROR] TITAN makefilegen not found in PATH."
    exit 1
fi
if ! command -v g++ &> /dev/null; then
    echo "[ERROR] No C++ compiler found."
    exit 1
fi
echo "[BUILD] TITAN tools and compiler found."

export TTCN3_DIR="${TTCN3_DIR:-/usr}"
export TTCN3_SUBDIR="${TTCN3_SUBDIR:-/titan}"
export TTCN3_INC="${TTCN3_DIR}/include${TTCN3_SUBDIR}"
export TTCN3_LIB_DIR="${TTCN3_DIR}/lib${TTCN3_SUBDIR}"
export LD_LIBRARY_PATH="${TTCN3_LIB_DIR}:${LD_LIBRARY_PATH:-}"

echo "[BUILD] TITAN environment:"
echo "       TTCN3_DIR    = $TTCN3_DIR"
echo "       TTCN3_SUBDIR = $TTCN3_SUBDIR"
echo "       TTCN3_INC    = $TTCN3_INC"
echo "       TTCN3_LIB_DIR= $TTCN3_LIB_DIR"

# ==========================================
# Step 3: Copy port adapter files to generated_cpp/
# ==========================================
echo "[BUILD] Step 3: Copying port adapter files to generated_cpp/..."
cd build/generated_cpp

cp_port_file() {
    local filename="$1"
    local src=""
    if [ -f "../../src/cpp_adapters/SA/ports/$filename" ]; then
        src="../../src/cpp_adapters/SA/ports/$filename"
    elif [ -f "$filename" ]; then
        src="$filename"
    fi
    if [ -n "$src" ]; then
        cp -f "$src" . 2>/dev/null && echo "[BUILD] Copied $filename"
    fi
}

for f in V2G_TCP_TLS_Port.cc V2G_TCP_TLS_Port.hh \
         V2G_UDP_SDP_Port.cc V2G_UDP_SDP_Port.hh; do
    cp_port_file "$f"
done

# ==========================================
# Step 4: Generate Makefile
# ==========================================
echo "[BUILD] Step 4: Generating Makefile..."
makefilegen -f -a -o . -e TestExecutable \
    ../../src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn \
    ../../src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn \
    ../../src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn \
    ../../src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/Services/CommonFunctions.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_InitConfiguration_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PostConditions/f_SECC_CMN_PO_InitialState_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/TestBehavior/f_SECC_CMN_TB_VTB_V2GTPSessionSetup_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/TestBehavior/f_SECC_CMN_TB_VTB_SDP_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/TestBehavior/f_SECC_CMN_TB_VTB_SupportedAppProtocol_001.ttcn \
    ../../src/ttcn3/4_TestCases/TestCases_SECC/TC_SECC_CMN_VTB_V2GTPSessionSetup_001.ttcn \
    ../../src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn \
    V2G_TCP_TLS_Port.cc \
    V2G_UDP_SDP_Port.cc \
    ../../src/cpp_adapters/common/Common.cc \
    ../../src/cpp_adapters/common/StaticExiDecoder.cc \
    ../../src/cpp_adapters/SA/hardware/TCPTLSPortAdapter.cc \
    ../../src/cpp_adapters/SA/hardware/HAL61851Adapter.cc \
    ../../src/cpp_adapters/SA/titan_tri/TitanTri.cc \
    ../../src/cpp_adapters/CD/titan_tci/TitanTci.cc \
    ../../src/cpp_adapters/CD/exi_engine/ExiCodecAdapter.cc

if [ $? -ne 0 ]; then
    echo "[ERROR] makefilegen failed!"
    cd ../..
    exit 1
fi
echo "[BUILD] Makefile generated successfully."

# ==========================================
# Step 5: Compile TTCN-3 to C++
# ==========================================
echo "[BUILD] Step 5: Translating TTCN-3 modules to C++..."
make compile 2>&1 | tail -5
if [ ${PIPESTATUS[0]} -ne 0 ]; then
    echo "[ERROR] TTCN-3 compilation failed!"
    cd ../..
    exit 1
fi
echo "[BUILD] TTCN-3 compilation done."

# ==========================================
# Step 6: Fix Makefile
# ==========================================
echo "[BUILD] Step 6: Fixing Makefile..."

sed -i 's|^\(OBJDIR\s*=\s*\).*$|\1../obj|' Makefile
sed -i "s|^EXECUTABLE = TestExecutable|EXECUTABLE = ../bin/TestExecutable|" Makefile
sed -i 's/^TTCN3_LIB = ttcn3-parallel-dynamic$/TTCN3_LIB = ttcn3-dynamic/' Makefile
sed -i 's/^TTCN3_LIB = ttcn3-parallel$/TTCN3_LIB = ttcn3-dynamic/' Makefile
sed -i 's/^LINUX_LIBS = -lxml2$/LINUX_LIBS = -lxml2 -lpthread/' Makefile
# (No JNI flags needed - using pure static data-driven decoder)
sed -i 's|^CXXFLAGS = -Wall  $|CXXFLAGS = -Wall -I. -D_GLIBCXX_USE_CXX11_ABI=0 -std=c++17 |' Makefile
# (No JNI flags needed - using pure static data-driven decoder)
sed -i "s|^LDFLAGS =  $|LDFLAGS =  -D_GLIBCXX_USE_CXX11_ABI=0 |" Makefile
# Fix LINUX_LIBS: add -ldl for dlopen
sed -i 's/^LINUX_LIBS = -lxml2 -lpthread$/LINUX_LIBS = -lxml2 -lpthread -ldl/' Makefile

echo "[BUILD] Makefile patched."

# ==========================================
# Step 7: Patch SECC_TestSystem.hh to include port headers
# ==========================================
echo "[BUILD] Step 7: Patching SECC_TestSystem.hh..."

if [ -f "SECC_TestSystem.hh" ]; then
    python3 -c "
import sys
with open('SECC_TestSystem.hh', 'r') as f:
    content = f.read()

if '#include \"V2G_TCP_TLS_Port.hh\"' in content:
    print('[BUILD] Port includes already present, skipping patch.')
    sys.exit(0)

# Find the line: '#include \"V2G_UDP_SDP_Port.hh\"' followed by '#endif'
# These are at the END of the file, before the final #endif
lines = content.split('\n')
insert_before = None
for i in range(len(lines)-1, -1, -1):
    line = lines[i].strip()
    if line == '#include \"V2G_UDP_SDP_Port.hh\"':
        insert_before = i
        break

if insert_before is not None:
    inject = '\n#include \"V2G_TCP_TLS_Port.hh\"\n'
    lines.insert(insert_before, inject)
    with open('SECC_TestSystem.hh', 'w') as f:
        f.write('\n'.join(lines))
    print('[BUILD] Patched: injected V2G_TCP_TLS_Port.hh include')
else:
    print('[BUILD] WARNING: Could not find insertion point for port include')
"
fi

echo "[BUILD] Step 7 done."

# ==========================================
# Step 8: Build executable
# ==========================================
echo "[BUILD] Step 8: Compiling executable..."
make -j$(nproc) 2>&1

if [ $? -ne 0 ]; then
    echo "[ERROR] C++ compilation failed!"
    cd ../..
    exit 1
fi

echo ""
echo "[BUILD] Compilation successful!"

# ==========================================
# Step 9: Verify output
# ==========================================
echo ""
echo "[BUILD] Verifying build output..."
OBJ_COUNT=$(find . -maxdepth 1 -name "*.o" | wc -l)
echo "[BUILD] Found $OBJ_COUNT object files."

if [ -f "../bin/TestExecutable" ]; then
    EXE_SIZE=$(stat -c%s "../bin/TestExecutable" 2>/dev/null || echo "unknown")
    echo "[BUILD] Executable: ../bin/TestExecutable (${EXE_SIZE} bytes)"
else
    echo "[ERROR] Executable not found at ../bin/TestExecutable"
    cd ../..
    exit 1
fi

cd ..

echo ""
echo "=========================================="
echo "[BUILD] Build completed successfully!"
echo "=========================================="
echo ""
echo "To run the tests:"
echo "  cd $([ -n "$ORIGINAL_DIR" ] && echo "$ORIGINAL_DIR" || echo "$SCRIPT_DIR")/build/bin"
echo "  ./TestExecutable ../config/titan_runtime.cfg"
echo ""
