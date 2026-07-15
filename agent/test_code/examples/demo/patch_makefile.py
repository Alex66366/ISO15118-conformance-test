#!/usr/bin/env python3
"""Patch the generated Makefile for the demo build.
Handles Unicode paths and TITAN Makefile quirks automatically.
"""
import os, sys

MF = 'Makefile'
PATHS_FILE = '/tmp/open2g_build_paths_demo.txt'


def main():
    with open(MF, 'r') as f:
        content = f.read()

    # Read paths from Step 0
    OPENV2G_INC = None
    EXI_BRIDGE_A = None
    if os.path.isfile(PATHS_FILE):
        for line in open(PATHS_FILE):
            line = line.strip()
            if line.startswith('INFRA_EXI_ABSPATH='):
                OPENV2G_INC = line.split('=', 1)[1]
            elif line.startswith('INFRA_BUILD_ABSPATH='):
                EXI_BRIDGE_A = os.path.join(line.split('=', 1)[1], 'libexi_bridge.a')

    print('[PATCH] OpenV2G include: ' + str(OPENV2G_INC))
    print('[PATCH] libexi_bridge.a: ' + str(EXI_BRIDGE_A))

    # FIX 1: Inject -I<abs_path> into CXXFLAGS
    if OPENV2G_INC and os.path.isdir(OPENV2G_INC):
        old_cxx = 'CXXFLAGS = -Wall  ' + chr(10)
        new_cxx = 'CXXFLAGS = -Wall  -I' + OPENV2G_INC + ' ' + chr(10)
        if old_cxx in content:
            content = content.replace(old_cxx, new_cxx)
            print('[PATCH] Injected OpenV2G include into CXXFLAGS')
        else:
            print('[PATCH] WARNING: CXXFLAGS line not in expected format')
    else:
        print('[PATCH] ERROR: OpenV2G include dir not found')
        sys.exit(1)

    # FIX 2: Remove -D_GLIBCXX_USE_CXX11_ABI=0 (breaks fmt)
    content = content.replace('-D_GLIBCXX_USE_CXX11_ABI=0', '')
    print('[PATCH] Removed _GLIBCXX_USE_CXX11_ABI=0')

    # FIX 3: Override the executable link rule with a clean POSIX version.
    # TITAN's Makefile uses "if g++ ... && ...; then ... fi" which /bin/sh misparses.
    if not (EXI_BRIDGE_A and os.path.isfile(EXI_BRIDGE_A)):
        print('[PATCH] ERROR: libexi_bridge.a not found')
        sys.exit(1)

    # Find the start of the EXECUTABLE target
    start = content.find('$(EXECUTABLE): $(OBJECTS)' + chr(10))
    if start < 0:
        print('[PATCH] ERROR: Could not find $(EXECUTABLE) target')
        sys.exit(1)

    # Find the end: blank line before $(LIBRARY):
    lib_pos = content.find('$(LIBRARY):', start)
    if lib_pos < 0:
        print('[PATCH] ERROR: Could not find $(LIBRARY) target')
        sys.exit(1)

    blank_before = content.rfind(chr(10), start, lib_pos)
    end = blank_before + 1  # include the \n

    original_block = content[start:end]

    # Build replacement: simple POSIX link rule.
    # IMPORTANT: static .a must come AFTER object files.
    # Also include TITAN runtime + crypto + xml2 + pthread + dl libs.
    nl = chr(10)
    tab = chr(9)
    libs = '-L/usr/lib/titan -lttcn3-dynamic -L/lib -lcrypto -L/usr/lib -lxml2 -lpthread -ldl'
    new_rule = (
        '$(EXECUTABLE): $(OBJECTS)' + nl +
        tab + '$(CXX) -o $@ $(OBJECTS) ' +
        EXI_BRIDGE_A + ' ' + libs + ' $(LDFLAGS)' + nl + nl
    )

    content = content.replace(original_block, new_rule)
    print('[PATCH] Link rule overridden (POSIX-compatible, with TITAN libs)')

    with open(MF, 'w') as f:
        f.write(content)
    print('[PATCH] Makefile patched successfully')


if __name__ == '__main__':
    main()