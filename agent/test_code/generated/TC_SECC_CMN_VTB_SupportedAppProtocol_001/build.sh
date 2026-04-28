#!/usr/bin/env bash
# =============================================================================
# build.sh — TITAN TTCN-3 build script
# Project  : TC_SECC_CMN_VTB_SupportedAppProtocol_001
# SUT      : SECC  |  Category: CMN  |  Mode: SIL
# Generated: 2026-04-27
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Environment checks
# ---------------------------------------------------------------------------
command -v ttcn3_compiler >/dev/null 2>&1 || {
    echo "ERROR: ttcn3_compiler not found. Source TITAN environment first."
    echo "       e.g.: source /usr/local/titan/titan.env"
    exit 1
}
command -v makefilegen >/dev/null 2>&1 || {
    echo "ERROR: makefilegen not found. Source TITAN environment first."
    exit 1
}

# ---------------------------------------------------------------------------
# 1. Resolve paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"
BUILD_DIR="${PROJECT_ROOT}/build"
SRC_DIR="${PROJECT_ROOT}/src"

mkdir -p "${BUILD_DIR}"
mkdir -p "${PROJECT_ROOT}/logs/titan_traces"
mkdir -p "${PROJECT_ROOT}/logs/system_logs"

echo "=================================================================="
echo " Building TC_SECC_CMN_VTB_SupportedAppProtocol_001"
echo " Project root : ${PROJECT_ROOT}"
echo " Build dir    : ${BUILD_DIR}"
echo "=================================================================="

# ---------------------------------------------------------------------------
# 2. Generate port skeleton files via ttcn3_compiler -t
#    This creates V2G__TCP__TLS__Port.hh/.cc and V2G__UDP__SDP__Port.hh/.cc
#    in the current directory (build/).  The pipeline then overlays
#    implementations from port_implementation_stub.txt.
# ---------------------------------------------------------------------------
echo ""
echo "[Step 1/4] Generating port skeleton files ..."
cd "${BUILD_DIR}"

ttcn3_compiler -t \
    "${SRC_DIR}/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn"

echo "  Generated: V2G__TCP__TLS__Port.hh  V2G__TCP__TLS__Port.cc"
echo "  Generated: V2G__UDP__SDP__Port.hh  V2G__UDP__SDP__Port.cc"

# ---------------------------------------------------------------------------
# 3. Apply port implementation stubs (pipeline step)
#    The pipeline overlays method bodies from port_implementation_stub.txt.
#    If the stub file exists alongside build.sh, apply it automatically.
# ---------------------------------------------------------------------------
STUB_FILE="${PROJECT_ROOT}/port_implementation_stub.txt"
if [[ -f "${STUB_FILE}" ]]; then
    echo ""
    echo "[Step 2/4] Applying port implementation stubs ..."
    # The pipeline's apply_port_stubs() function handles this;
    # as a fallback the raw stub file is noted here for manual application.
    echo "  Stub file found: ${STUB_FILE}"
    echo "  (Pipeline will overlay method bodies into generated .cc files)"
else
    echo "[Step 2/4] No port_implementation_stub.txt found — using ttcn3_compiler -t skeletons."
fi

# ---------------------------------------------------------------------------
# 4. Generate Makefile via makefilegen
#
#    File list:
#      TTCN-3 skeleton modules (this agent)
#      PreConditions + PostConditions (Core Agent, placed by pipeline)
#      Generated test case (Core Agent, placed by pipeline)
#      Port skeleton .cc files (from Step 1)
#      C++ adapter .cc files (copied from demo/ by pipeline)
# ---------------------------------------------------------------------------
echo ""
echo "[Step 3/4] Running makefilegen ..."

makefilegen -f -e TestExecutable \
    "${SRC_DIR}/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn" \
    "${SRC_DIR}/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn" \
    "${SRC_DIR}/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn" \
    "${SRC_DIR}/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn" \
    "${SRC_DIR}/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn" \
    "${SRC_DIR}/ttcn3/1_Data_Definitions/PIXIT/PIXIT_SECC_15118_2.ttcn" \
    "${SRC_DIR}/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn" \
    "${SRC_DIR}/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn" \
    "${SRC_DIR}/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_InitConfiguration_001.ttcn" \
    "${SRC_DIR}/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_Binding_001.ttcn" \
    "${SRC_DIR}/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_InitConfiguration_002.ttcn" \
    "${SRC_DIR}/ttcn3/3_Behavior_Steps/PostConditions/f_SECC_CMN_PO_InitialState_001.ttcn" \
    "${SRC_DIR}/ttcn3/3_Behavior_Steps/PostConditions/f_SECC_CMN_PO_ShutdownConfiguration_001.ttcn" \
    "${SRC_DIR}/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn" \
    "${SRC_DIR}/ttcn3/4_TestCases/TC_SECC_CMN_VTB_SupportedAppProtocol_001.ttcn" \
    V2G__TCP__TLS__Port.cc \
    V2G__UDP__SDP__Port.cc \
    "${SRC_DIR}/cpp_adapters/common/Common.cc" \
    "${SRC_DIR}/cpp_adapters/SA/hardware/TCPTLSPortAdapter.cc" \
    "${SRC_DIR}/cpp_adapters/SA/hardware/HAL61851Adapter.cc" \
    "${SRC_DIR}/cpp_adapters/SA/titan_tri/TitanTri.cc" \
    "${SRC_DIR}/cpp_adapters/CD/titan_tci/TitanTci.cc" \
    "${SRC_DIR}/cpp_adapters/CD/exi_engine/ExiCodecAdapter.cc"

# ---------------------------------------------------------------------------
# 5. Compile
# ---------------------------------------------------------------------------
echo ""
echo "[Step 4/4] Compiling (make) ..."
make -j"$(nproc 2>/dev/null || echo 4)"

echo ""
echo "=================================================================="
echo " Build SUCCESS"
echo " Executable : ${BUILD_DIR}/TestExecutable"
echo " Config     : ${PROJECT_ROOT}/config/titan_runtime.cfg"
echo ""
echo " To execute the test:"
echo "   cd ${BUILD_DIR}"
echo "   ./TestExecutable ${PROJECT_ROOT}/config/titan_runtime.cfg"
echo "=================================================================="
