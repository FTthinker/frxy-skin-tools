#!/usr/bin/env python3
"""Prepare model metadata from an extracted NeoX NPK cmesh/gim pair."""

from __future__ import annotations

import json
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_skeleton(path: Path) -> dict:
    data = path.read_bytes()
    magic, version, marker, bone_type, reserved = struct.unpack_from("<IHHHH", data, 0)
    if bone_type not in (1, 4):
        return {
            "source": str(path), "magic_hex": f"{magic:08x}", "version": version,
            "marker": marker, "bone_type": bone_type, "reserved": reserved,
            "bone_count": 0, "parent_index_width": 0, "bones": [],
        }
    count = struct.unpack_from("<H", data, 12)[0]
    offset = 14
    # v4 skeletons in this game use one-byte parent indices for this asset.
    parent_width = 1
    parents = []
    for _ in range(count):
        value = data[offset]
        offset += parent_width
        parents.append(-1 if value == 255 else value)
    names = []
    for _ in range(count):
        raw = data[offset : offset + 32]
        offset += 32
        names.append(raw.split(b"\0", 1)[0].decode("utf-8", errors="replace"))
    has_extra = data[offset]
    offset += 1
    if has_extra:
        offset += count * 28
    matrices = [list(struct.unpack_from("<16f", data, offset + i * 64)) for i in range(count)]
    return {
        "source": str(path),
        "magic_hex": f"{magic:08x}",
        "version": version,
        "marker": marker,
        "bone_type": bone_type,
        "reserved": reserved,
        "bone_count": count,
        "parent_index_width": parent_width,
        "bones": [
            {
                "index": i,
                "name": names[i],
                "parent_index": parents[i],
                "parent_name": names[parents[i]] if parents[i] >= 0 else None,
                "matrix_row_major": matrices[i],
            }
            for i in range(count)
        ],
    }


def parse_gim(path: Path) -> dict:
    root = ET.parse(path).getroot()
    submeshes = []
    for child in root.findall("./SubMesh/*"):
        submeshes.append(
            {
                "name": child.attrib.get("Name", f"submesh_{len(submeshes)}"),
                "material_index": int(child.attrib.get("MtlIdx", "0")),
                "vertices": 0,
                "triangles": 0,
                "bounding_center": child.attrib.get("BoundingCenter", "0,0,0"),
                "bounding_half": child.attrib.get("BoundingHalf", "1,1,1"),
            }
        )
    return {
        "vertex_count": int(root.attrib.get("VertexCount", "0")),
        "triangle_count": int(root.attrib.get("TriangleCount", "0")),
        "bounding_info": root.attrib.get("BoundingInfo"),
        "submeshes": submeshes,
    }


def find_header(data: bytes, submesh_count: int):
    # Records are 4-byte vertex count, 4-byte triangle count, UV-layer byte,
    # color-layer byte.  A two-byte footer marker and totals follow the table.
    candidates = []
    try:
        _magic, version, _marker, bone_type, _reserved = struct.unpack_from("<IHHHH", data, 0)
        if bone_type in (1, 4):
            bone_count = struct.unpack_from("<H", data, 12)[0]
            parent_width = 1
            skeleton_pos = 14 + bone_count * parent_width + bone_count * 32
            has_extra = data[skeleton_pos]
            skeleton_pos += 1 + (bone_count * 28 if has_extra else 0) + bone_count * 64
            ending_address = struct.unpack_from("<I", data, skeleton_pos + 1)[0]
        else:
            ending_address = struct.unpack_from("<I", data, 12)[0]
        split_count = struct.unpack_from("<H", data, ending_address)[0]
        if split_count:
            candidates.append(struct.unpack_from("<I", data, ending_address + 2)[0])
    except (IndexError, struct.error):
        pass
    # Keep scanning only as a compatibility fallback for unusual legacy
    # skeleton headers; the authoritative end-table pointer is always tried
    # first and prevents one-byte false-positive tables.
    candidates.extend(range(0, min(len(data), 50000)))
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        pos = candidate
        records = []
        try:
            for _ in range(submesh_count):
                v, f, uv, colors = struct.unpack_from("<IIBB", data, pos)
                # GIM may contain an intentionally empty material/submesh
                # record (0 vertices, 0 triangles).  It is still present in
                # the CMesh table and must participate in record matching.
                uv_valid = 0 <= uv <= 4
                if not (0 <= v < 1_000_000 and 0 <= f < 2_000_000 and uv_valid and 0 <= colors <= 4):
                    raise ValueError
                records.append((v, f, uv, colors))
                pos += 10
            _marker, _reserved = struct.unpack_from("<BB", data, pos)
            total_v, total_f = struct.unpack_from("<II", data, pos + 2)
            flag = data[pos + 10]
        except (struct.error, ValueError):
            continue
        if (
            total_v > 0
            and total_f > 0
            and total_v == sum(x[0] for x in records)
            and total_f == sum(x[1] for x in records)
        ):
            return candidate, pos + 11, records, total_v, total_f, flag
    raise ValueError("unable to locate cmesh submesh table")


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: prepare_npk_model.py <cmesh> <gim> <output_dir>")
        return 2
    cmesh, gim, out = map(Path, sys.argv[1:])
    out.mkdir(parents=True, exist_ok=True)
    mesh = cmesh.read_bytes()
    structure = parse_gim(gim)
    candidate, data_start, records, total_v, total_f, flag = find_header(mesh, len(structure["submeshes"]))
    if structure["vertex_count"] and structure["vertex_count"] != total_v:
        print(f"warning: gim vertex count {structure['vertex_count']} differs from cmesh {total_v}")
    if structure["triangle_count"] and structure["triangle_count"] != total_f:
        print(f"warning: gim triangle count {structure['triangle_count']} differs from cmesh {total_f}")
    for item, record in zip(structure["submeshes"], records):
        item["vertices"], item["triangles"], item["uv_layers"], item["color_layers"] = record
    structure["vertex_count"] = total_v
    structure["triangle_count"] = total_f
    structure["header_offset"] = candidate
    structure["data_start"] = data_start
    structure["flag"] = flag
    bones = parse_skeleton(cmesh)
    (out / "模型结构.json").write_text(json.dumps({"display": structure}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "骨骼层级与矩阵.json").write_text(json.dumps(bones, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "cmesh": str(cmesh),
        "gim": str(gim),
        "submeshes": len(records),
        "vertices": total_v,
        "triangles": total_f,
        "bones": bones["bone_count"],
        "header_offset": candidate,
        "data_start": data_start,
        "flag": flag,
    }
    (out / "npk_model_metadata.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
