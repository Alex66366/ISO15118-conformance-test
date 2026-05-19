#!/bin/bash
# ==========================================
# TITAN TTCN-3 Build Script for Linux
# ISO 15118-2 SECC Conformance Test System
# ==========================================

set -e  # Exit on error

echo "[BUILD] Starting TITAN build process..."

# Get script directory (project root: demo/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ==========================================
# Step 0: Handle non-ASCII path issue
# TITAN's makefilegen + make cannot handle paths with Chinese/unicode characters.
# Solution: copy project to an ASCII temp directory, build there, then sync back.
# ==========================================
BUILD_WORK_DIR=""
ORIGINAL_DIR=""
KEEP_TEMP=""  # Set KEEP_TEMP=1 in environment to inspect the temp build dir on failure

_is_path_ascii() {
    # Returns 0 (success) if path is pure ASCII, 1 if it contains non-ASCII chars.
    # We test by checking if LC_ALL=C grep finds high-byte characters.
    ! printf '%s' "$1" | LC_ALL=C grep -q '[^\x00-\x7F]'
}

_handle_non_ascii_path() {
    local original_dir="$1"
    local work_dir

    # Always create a fresh temp directory to avoid stale files from previous runs
    work_dir=$(mktemp -d /tmp/titan_build_XXXXXX)
    if [ -z "$work_dir" ] || [ ! -d "$work_dir" ]; then
        echo "[ERROR] Failed to create temporary build directory."
        exit 1
    fi
    BUILD_WORK_DIR="$work_dir"

    project_name="demo_build_$$"

    echo "[BUILD] Project path contains non-ASCII characters."
    echo "[BUILD] Copying project to ASCII temp directory: $work_dir/$project_name"
    mkdir -p "$work_dir/$project_name"

    # Use --delete to ensure we have a clean copy without stale files
    # Also exclude logs to preserve any previous logs in the original location
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

    if [ "$exit_code" -ne 0 ] && [ -n "$KEEP_TEMP" ]; then
        echo ""
        echo "[BUILD] Build failed (exit code: $exit_code). Temp directory kept for inspection:"
        echo "[BUILD]   $work_dir"
        echo "[BUILD]   (Set KEEP_TEMP=0 or unset to auto-clean on failure)"
        return
    fi

    # Sync back build artifacts (only on success)
    if [ "$exit_code" -eq 0 ]; then
        echo "[BUILD] Syncing build artifacts back to original location..."
        if [ -d "$work_dir/demo_build_$$/build" ]; then
            mkdir -p "$original_dir/build"
            rsync -a --delete \
                "$work_dir/demo_build_$$/build/" "$original_dir/build/"
        fi
        if [ -d "$work_dir/demo_build_$$/config" ]; then
            mkdir -p "$original_dir/config"
            rsync -a --delete \
                "$work_dir/demo_build_$$/config/" "$original_dir/config/"
        fi
        if [ -d "$work_dir/demo_build_$$/logs" ]; then
            mkdir -p "$original_dir/logs"
            rsync -a \
                "$work_dir/demo_build_$$/logs/" "$original_dir/logs/"
        fi
    fi

    rm -rf "$work_dir"
    echo "[BUILD] Temp directory cleaned up."
}

# Detect non-ASCII in SCRIPT_DIR
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
# Step 2: Check TITAN installation & set up environment
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

# Set up all TITAN environment variables
# Priority: explicit env var > auto-detect > fallback

# TTCN3_DIR — base installation path
if [ -z "$TTCN3_DIR" ]; then
    if [ -d "/usr/include/titan" ]; then
        export TTCN3_DIR="/usr"
    elif [ -d "/opt/titan/include/titan" ]; then
        export TTCN3_DIR="/opt/titan"
    elif command -v makefilegen &> /dev/null; then
        # Resolve real path of the binary and infer prefix
        TTCN3_BIN=$(command -v makefilegen)
        TTCN3_BIN=$(readlink -f "$TTCN3_BIN" 2>/dev/null || echo "$TTCN3_BIN")
        if [ -n "$TTCN3_BIN" ]; then
            export TTCN3_DIR=$(dirname "$(dirname "$TTCN3_BIN")")
        fi
    fi
fi

# TTCN3_SUBDIR — subdirectory under TTCN3_DIR for include/lib
if [ -z "$TTCN3_SUBDIR" ]; then
    if [ -d "${TTCN3_DIR}/include/titan" ]; then
        export TTCN3_SUBDIR="/titan"
    elif [ -d "${TTCN3_DIR}/include" ]; then
        export TTCN3_SUBDIR=""
    fi
fi

# Derived include and library paths
if [ -z "$TTCN3_INC" ]; then
    export TTCN3_INC="${TTCN3_DIR}/include${TTCN3_SUBDIR}"
fi

if [ -z "$TTCN3_LIB_DIR" ]; then
    export TTCN3_LIB_DIR="${TTCN3_DIR}/lib${TTCN3_SUBDIR}"
fi

# Runtime library path (for dynamic linker at execution time)
if [ -z "$LD_LIBRARY_PATH" ]; then
    export LD_LIBRARY_PATH="${TTCN3_LIB_DIR}"
elif [[ ":$LD_LIBRARY_PATH:" != *":${TTCN3_LIB_DIR}:"* ]]; then
    export LD_LIBRARY_PATH="${TTCN3_LIB_DIR}:${LD_LIBRARY_PATH}"
fi

echo "[BUILD] TITAN environment:"
echo "       TTCN3_DIR     = $TTCN3_DIR"
echo "       TTCN3_SUBDIR = $TTCN3_SUBDIR"
echo "       TTCN3_INC    = $TTCN3_INC"
echo "       TTCN3_LIB_DIR= $TTCN3_LIB_DIR"

if [ ! -d "$TTCN3_INC" ]; then
    echo "[WARN] TTCN3_INC directory does not exist: $TTCN3_INC"
fi

# ==========================================
# Step 3: Copy port adapter files to generated_cpp/
# ==========================================
echo "[BUILD] Step 3: Copying port adapter files to generated_cpp/..."

cd build/generated_cpp

# The pipeline copies port files here before calling build.sh.
# If they already exist with real content (> 200 bytes), use them.
# Otherwise, search the project tree (they may be at root level, like in demo/).

find_port_file() {
    local filename="$1"
    local search_path=""

    # Option 1: Already in current directory (pipeline already copied them)
    if [ -f "$filename" ] && [ "$(stat -c%s "$filename" 2>/dev/null || echo 0)" -gt 200 ]; then
        echo "$filename"
        return 0
    fi

    # Option 2: In the project root (where demo places them)
    search_path="../../$filename"
    if [ -f "$search_path" ] && [ "$(stat -c%s "$search_path" 2>/dev/null || echo 0)" -gt 200 ]; then
        cp "$search_path" .
        echo "[BUILD] Copied $filename from project root"
        echo "$filename"
        return 0
    fi

    # Option 3: Search the entire project tree for the file
    search_path=$(find ../../src/cpp_adapters -name "$filename" 2>/dev/null | head -1)
    if [ -n "$search_path" ] && [ "$(stat -c%s "$search_path" 2>/dev/null || echo 0)" -gt 200 ]; then
        cp "$search_path" .
        echo "[BUILD] Copied $filename from $search_path"
        echo "$filename"
        return 0
    fi

    echo ""
    return 1
}

# Copy all four port adapter files
PORT_FILES=("V2G_TCP_TLS_Port.cc" "V2G_TCP_TLS_Port.hh" "V2G_UDP_SDP_Port.cc" "V2G_UDP_SDP_Port.hh")
PORT_OK=true

for port_file in "${PORT_FILES[@]}"; do
    if ! find_port_file "$port_file"; then
        echo "[ERROR] Port adapter file not found: $port_file"
        echo "[HINT] Searched: build/generated_cpp/, project root, and src/cpp_adapters/"
        PORT_OK=false
    fi
done

if [ "$PORT_OK" = false ]; then
    echo "[ERROR] One or more port adapter files are missing!"
    cd ../..
    exit 1
fi

echo "[BUILD] Port adapter files ready:"
ls -la V2G_TCP_TLS_Port.* V2G_UDP_SDP_Port.*

# ==========================================
# Step 4: Generate Makefile in generated_cpp/
# ==========================================
echo "[BUILD] Step 4: Generating Makefile..."

makefilegen -f -a -o . -e TestExecutable \
    ../../src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn \
    ../../src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn \
    ../../src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn \
    ../../src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn \
    ../../src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/Services/CommonFunctions.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_Binding_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_InitConfiguration_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_InitConfiguration_002.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_PLCLinkStatusCombinedTesting_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_SetPowerFlowConfiguration_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_SetProximityPilot_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_StateA_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_StateB_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PostConditions/f_SECC_CMN_PO_InitialState_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PostConditions/f_SECC_CMN_PO_ShutdownConfiguration_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/PostConditions/f_SECC_PO_ShutdownConfiguration_002.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/TestBehavior/f_SECC_CMN_TB_VTB_SDP_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/TestBehavior/f_SECC_CMN_TB_VTB_SupportedAppProtocol_001.ttcn \
    ../../src/ttcn3/3_Behavior_Steps/TestBehavior/f_SECC_CMN_TB_VTB_V2GTPSessionSetup_001.ttcn \
    ../../src/ttcn3/4_TestCases/TestCases_SECC/TC_SECC_CMN_VTB_V2GTPSessionSetup_001.ttcn \
    ../../src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn \
    V2G_TCP_TLS_Port.cc \
    V2G_UDP_SDP_Port.cc \
    ../../src/cpp_adapters/common/Common.cc \
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
# Step 4.1: Verify Makefile contains TTCN-3 targets
# ==========================================
echo "[BUILD] Step 4.1: Verifying Makefile contains TTCN-3 compilation targets..."

if grep -q "\.ttcn\.cc:" Makefile; then
    echo "[BUILD] Makefile contains TTCN-3 compilation targets."
else
    echo "[WARN] Makefile may not contain TTCN-3 compilation targets."
    echo "[BUILD] Checking if makefilegen successfully compiled all .ttcn files..."

    # List all .ttcn files that should be compiled
    TTCN_FILES=$(find ../../src/ttcn3 -name "*.ttcn" -type f)
    TTCN_COUNT=$(echo "$TTCN_FILES" | wc -l)
    echo "[BUILD] Found $TTCN_COUNT .ttcn files in src/ttcn3/"

    # Check if .ttcn.cc files were generated
    TTCN_CC_COUNT=$(find . -maxdepth 1 -name "*.ttcn.cc" | wc -l)
    echo "[BUILD] Generated $TTCN_CC_COUNT .ttcn.cc files in generated_cpp/"

    if [ "$TTCN_CC_COUNT" -lt "$TTCN_COUNT" ]; then
        echo "[WARN] Some .ttcn files were not compiled to .cc"
        echo "[BUILD] Attempting to manually compile missing .ttcn files..."

        # Check if ttcn3_compiler is available
        if command -v ttcn3_compiler &> /dev/null; then
            for ttcn_file in $TTCN_FILES; do
                ttcn_base=$(basename "$ttcn_file" .ttcn)
                ttcn_cc="${ttcn_base}.ttcn.cc"
                if [ ! -f "$ttcn_cc" ]; then
                    echo "[BUILD] Compiling $ttcn_file -> $ttcn_cc"
                    ttcn3_compiler -I "$TTCN3_INC" "$ttcn_file" 2>&1 || true
                fi
            done
        else
            echo "[WARN] ttcn3_compiler not found, skipping manual compilation"
        fi
    fi
fi

# ==========================================
# Step 5: Fix SECC_TestSystem.hh to include port headers
# ==========================================
echo "[BUILD] Step 5: Fixing SECC_TestSystem.hh..."

if [ -f "SECC_TestSystem.hh" ]; then
    sed -i '/#include "V2G_TCP_TLS_Port.hh"/d' SECC_TestSystem.hh
    sed -i '/#include "V2G_UDP_SDP_Port.hh"/d' SECC_TestSystem.hh

    SECOND_NS_LINE=$(grep -n "^namespace SECC__TestSystem {" SECC_TestSystem.hh | head -2 | tail -1 | cut -d: -f1)
    if [ -n "$SECOND_NS_LINE" ]; then
        head -n "$SECOND_NS_LINE" SECC_TestSystem.hh > SECC_TestSystem.hh.tmp
        printf '#include "V2G_TCP_TLS_Port.hh"\n#include "V2G_UDP_SDP_Port.hh"\n' >> SECC_TestSystem.hh.tmp
        tail -n +$((SECOND_NS_LINE + 1)) SECC_TestSystem.hh >> SECC_TestSystem.hh.tmp
        mv SECC_TestSystem.hh.tmp SECC_TestSystem.hh
        echo "[BUILD] Added port includes at namespace line $SECOND_NS_LINE"
    fi
fi

echo "[BUILD] Fixing SECC_TestSystem.cc..."
if [ -f "SECC_TestSystem.cc" ]; then
    sed -i '/#include "V2G_TCP_TLS_Port.hh"/d' SECC_TestSystem.cc
    sed -i '/#include "V2G_UDP_SDP_Port.hh"/d' SECC_TestSystem.cc

    SECOND_NS_LINE=$(grep -n "^namespace SECC__TestSystem {" SECC_TestSystem.cc | head -2 | tail -1 | cut -d: -f1)
    if [ -n "$SECOND_NS_LINE" ]; then
        head -n "$SECOND_NS_LINE" SECC_TestSystem.cc > SECC_TestSystem.cc.tmp
        printf '#include "V2G_TCP_TLS_Port.hh"\n#include "V2G_UDP_SDP_Port.hh"\n' >> SECC_TestSystem.cc.tmp
        tail -n +$((SECOND_NS_LINE + 1)) SECC_TestSystem.cc >> SECC_TestSystem.cc.tmp
        mv SECC_TestSystem.cc.tmp SECC_TestSystem.cc
        echo "[BUILD] Added port includes to .cc at namespace line $SECOND_NS_LINE"
    fi
fi

# ==========================================
# Step 6: Fix Makefile
# ==========================================
echo "[BUILD] Step 6: Fixing Makefile..."

# Backup original
cp Makefile Makefile.orig

# Set OBJDIR to ../obj so .o files go into build/obj/
sed -i 's|^\(OBJDIR\s*=\s*\).*$|\1../obj|' Makefile

# Fix EXECUTABLE to place output in ../bin/
sed -i "s|^EXECUTABLE = TestExecutable|EXECUTABLE = ../bin/TestExecutable|" Makefile

# Fix TTCN3_LIB and add pthread
# Use ttcn3 instead of ttcn3-parallel-dynamic for standalone execution
sed -i 's/^TTCN3_LIB = ttcn3-parallel-dynamic$/TTCN3_LIB = ttcn3-dynamic/' Makefile
sed -i 's/^TTCN3_LIB = ttcn3-parallel$/TTCN3_LIB = ttcn3-dynamic/' Makefile
sed -i 's/^LINUX_LIBS = -lxml2$/LINUX_LIBS = -lxml2 -lpthread/' Makefile

echo "[BUILD] Makefile fixed (output=../bin/TestExecutable, TTCN3_LIB=ttcn3-dynamic, +pthread)."

# ==========================================
# Step 7: Compile
# ==========================================
echo "[BUILD] Step 7: Compiling (objects -> ../obj/)..."

# Ensure TITAN environment variables are exported for compilation
export TTCN3_DIR="${TTCN3_DIR:-/usr}"
export TTCN3_INC="${TTCN3_INC:-${TTCN3_DIR}/include/titan}"
export TTCN3_LIB_DIR="${TTCN3_LIB_DIR:-${TTCN3_DIR}/lib/titan}"
export LD_LIBRARY_PATH="${TTCN3_LIB_DIR}:${LD_LIBRARY_PATH:-}"

make

if [ $? -ne 0 ]; then
    echo "[ERROR] Compilation failed!"
    cd ../..
    exit 1
fi

echo "[BUILD] Compilation successful."

# ==========================================
# Step 7.1: Validate compilation output
# ==========================================
echo "[BUILD] Step 7.1: Validating compilation output..."

OBJ_COUNT=$(find . -maxdepth 1 -name "*.o" 2>/dev/null | wc -l)
if [ "$OBJ_COUNT" -eq 0 ]; then
    echo "[ERROR] No object files were generated!"
    echo "[ERROR] Compilation failed."
    cd ../..
    exit 1
fi

echo "[BUILD] Found $OBJ_COUNT object files."

# ==========================================
# Step 8: Verify output structure
# ==========================================
echo ""
echo "[BUILD] Verifying build output structure..."
echo ""
echo "=== generated_cpp/ ==="
ls -la *.cc *.hh Makefile 2>/dev/null | head -20
echo ""
echo "=== bin/ (executable) ==="
ls -la ../bin/TestExecutable 2>/dev/null

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
