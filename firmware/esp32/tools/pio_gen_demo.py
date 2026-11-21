# PlatformIO pre-script (extra_scripts = pre:tools/pio_gen_demo.py): regenerates include/demo_traces.h from
# test/data/demo_traces.bin when it is missing or older (tools/gen_demo_header.py, standard library only).
import os
import sys

Import("env")  # noqa: F821  (provided by PlatformIO / SCons)

proj = env.subst("$PROJECT_DIR")  # noqa: F821
sys.path.insert(0, os.path.join(proj, "tools"))
import gen_demo_header  # noqa: E402

gen_demo_header.generate(os.path.join(proj, "test", "data", "demo_traces.bin"),
                         os.path.join(proj, "include", "demo_traces.h"))
