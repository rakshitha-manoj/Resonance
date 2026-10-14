import json
from pathlib import Path
import numpy as np

root = Path(__file__).resolve().parents[3]
m = json.loads((root / "models/selected/model_export.json").read_text())
t = m["trees"][0]
names = t["feature_name"]
used = m["used_feature_names"]
classes = m["class_order"]


def hexf(v):
    return float.hex(float(np.float32(v))) + "f"


def emit(node, depth, out):
    pad = "    " * depth
    if t["left"][node] < 0:
        out.append(f"{pad}return {t['leaf_class'][node]};")
        return
    out.append(f"{pad}if ({names[node]} <= {hexf(t['threshold'][node])}) {{")
    emit(t["left"][node], depth + 1, out)
    out.append(f"{pad}}} else {{")
    emit(t["right"][node], depth + 1, out)
    out.append(f"{pad}}}")


body = []
emit(0, 1, body)
n = np.arange(128)
cs = np.cos(2 * np.pi * n / 128).astype(np.float32)
sn = np.sin(2 * np.pi * n / 128).astype(np.float32)


def table(name, a):
    rows = []
    for i in range(0, 128, 4):
        rows.append("    " + ", ".join(hexf(v) for v in a[i:i + 4]) + ",")
    return f"static const float {name}[128] = {{\n" + "\n".join(rows) + "\n};"


lines = [
    "#pragma once",
    f"static const char *const class_names[{len(classes)}] = {{" + ", ".join(f'"{c}"' for c in classes) + "};",
    "static const char *const used_features[] = {" + ", ".join(f'"{u}"' for u in used) + "};",
    table("tw_cos", cs),
    table("tw_sin", sn),
    "static inline int predict(" + ", ".join(f"float {u}" for u in used) + ") {",
    *body,
    "}",
    "",
]
out = root / "firmware/esp32/include/model.h"
out.write_text("\n".join(lines))
print("wrote", out)
