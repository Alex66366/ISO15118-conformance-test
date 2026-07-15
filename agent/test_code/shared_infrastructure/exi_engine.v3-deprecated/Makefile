# Makefile for libexi_bridge.a — OpenV2G dynamic EXI encoder bridge
#
# Location  : agent/test_code/shared_infrastructure/exi_engine/
# Purpose    : Compile OpenV2G (C library) + OpenV2G_Wrapper.cc into a
#              single static library consumed by all generated test projects.
#
# Targets:
#   make        → build libexi_bridge.a
#   make clean  → remove build artifacts
#   make verify → dry-run compile of OpenV2G_Wrapper.cc (no main)
#
# Usage from build.sh:
#   The build.sh invokes make in this directory BEFORE the TITAN makefile
#   runs.  libexi_bridge.a is placed at:
#     ${PROJECT_ROOT}/build/libexi_bridge.a
#   and linked into the final TestExecutable via $(PROJECT_ROOT)/build.sh
#   (see Step 4 in build.sh).
#
# Author: Infrastructure Agent — Phase 2 Architecture

# ---------------------------------------------------------------------------
# Paths (relative to this Makefile's location)
# ---------------------------------------------------------------------------
SHARED_INFRA_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
OPENV2G_ROOT     := $(SHARED_INFRA_ROOT)/openv2g
WRAPPER_DIR      := $(SHARED_INFRA_ROOT)

# Output
BUILD_DIR        := $(abspath $(SHARED_INFRA_ROOT)/../build_exi_bridge)
STATIC_LIB       := $(BUILD_DIR)/libexi_bridge.a

# ---------------------------------------------------------------------------
# Toolchain
# ---------------------------------------------------------------------------
CC  := gcc          # OpenV2G is pure C
CXX := g++         # OpenV2G_Wrapper.cc is C++

# OpenV2G compile definitions.
# Do NOT define DEPLOY_ISO1_CODEC / DEPLOY_ISO1_CODEC_FRAGMENT here —
# iso1EXIDatatypes.h defines them at line 47-48.  Passing -D on the command
# line causes "redefined" warnings (harmless but noisy).  The other defines
# are essential (they control which parts of the codec are compiled).
OPENV2G_DEFS := -DOPENV2G_STANDALONE -DOPENV2G_SEPARATE_COMPILATION

# OpenV2G include paths
OPENV2G_INCS := \
    -I$(OPENV2G_ROOT)/include/appHandshake \
    -I$(OPENV2G_ROOT)/include/codec \
    -I$(OPENV2G_ROOT)/include/din \
    -I$(OPENV2G_ROOT)/include/iso1 \
    -I$(OPENV2G_ROOT)/include/iso2 \
    -I$(OPENV2G_ROOT)/include/transport \
    -I$(OPENV2G_ROOT)/include/xmldsig \
    -I$(WRAPPER_DIR)

# Common flags
COMMON_CFLAGS   := $(OPENV2G_DEFS) $(OPENV2G_INCS) -fPIC
COMMON_CXXFLAGS := $(OPENV2G_DEFS) $(OPENV2G_INCS) -fPIC -std=c++11

# ---------------------------------------------------------------------------
# OpenV2G C source files — use full paths, not pattern rules with spaces in paths.
# We use explicit rules instead of pattern rules to avoid space-in-path issues.
# ---------------------------------------------------------------------------
OPENV2G_SRCS_FULL := \
    $(wildcard $(OPENV2G_ROOT)/src/appHandshake/*.c) \
    $(wildcard $(OPENV2G_ROOT)/src/codec/*.c) \
    $(wildcard $(OPENV2G_ROOT)/src/din/*.c) \
    $(wildcard $(OPENV2G_ROOT)/src/iso1/*.c) \
    $(wildcard $(OPENV2G_ROOT)/src/iso2/*.c) \
    $(wildcard $(OPENV2G_ROOT)/src/transport/*.c) \
    $(wildcard $(OPENV2G_ROOT)/src/xmldsig/*.c)

# Compute .o names by taking the source filename and mapping to BUILD_DIR
define OPENV2G_OBJ_RULE
$(BUILD_DIR)/$(notdir $(patsubst %.c,%.o,$(word 1,$(1)))): $(word 1,$(1))
	@mkdir -p $$(dir $$@)
	$$(CC) $$(COMMON_CFLAGS) -c $$< -o $$@
endef

# Generate one rule per C source file
$(foreach src,$(OPENV2G_SRCS_FULL),\
    $(eval $(call OPENV2G_OBJ_RULE,$(src))))

# OpenV2G_Wrapper.cc
$(BUILD_DIR)/OpenV2G_Wrapper.o: $(WRAPPER_DIR)/OpenV2G_Wrapper.cc
	@mkdir -p $$(dir $$@)
	$(CXX) $(COMMON_CXXFLAGS) -c $< -o $@

# Collect all .o files (computed from sources)
OPENV2G_OBJS := $(patsubst %.c,$(BUILD_DIR)/%.o,$(notdir $(OPENV2G_SRCS_FULL)))
WRAPPER_OBJ  := $(BUILD_DIR)/OpenV2G_Wrapper.o
ALL_OBJS     := $(OPENV2G_OBJS) $(WRAPPER_OBJ)

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
.PHONY: all clean verify

all: $(STATIC_LIB)
	@echo "[libexi_bridge] Built: $<"

$(STATIC_LIB): $(ALL_OBJS) | $(BUILD_DIR)
	@echo "[libexi_bridge] Archiving $@"
	@ar rcs $@ $^

$(BUILD_DIR):
	@mkdir -p $@

clean:
	rm -rf $(BUILD_DIR)
	@echo "[libexi_bridge] Cleaned."

# Dry-run verification: compile only OpenV2G_Wrapper.cc without linking
# This is the real acid test — if this compiles, the header is clean.
verify: $(WRAPPER_OBJ)
	@echo "[libexi_bridge] Verification PASSED — header is zero-pollution."
