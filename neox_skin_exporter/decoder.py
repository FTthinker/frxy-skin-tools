# Pure binary parser adapted from the user-supplied NeoX importer.
"""Extract NeoX v4 CMesh directly into a Blender scene.

This is intentionally independent of PMXEditor/PEPlugin.  The CMesh v4 layout
used by the supplied model is decoded from the game's own ARM64 loader:
signed 20-bit fixed-point positions, byte normals/tangents, delta-coded uint16
triangle indices, half-float UVs, uint16 fixed-point weights, and byte joints.
"""
from __future__ import annotations
import argparse
import json
import math
import statistics
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
MMD_UNIT_SCALE = 0.08

def align4(value: int) -> int:
    return value + 3 & ~3

def half(data: bytes, offset: int) -> float:
    return struct.unpack_from('<e', data, offset)[0]

def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))

def game_to_blender(value, mirror_x: bool=False) -> tuple[float, float, float]:
    """Convert NeoX's Y-up coordinates to Blender's right-handed Z-up space."""
    x, y, z = value
    return (float(-x if mirror_x else x), float(-z), float(y))

def decode_position_v4(packed: int) -> tuple[float, float, float]:
    """Decode NeoX CMesh v4's three signed 20-bit position components.

    The game loader at libclient.so:0x88e5b8 stores each component as a
    12-bit fraction, a 7-bit integer and a sign bit.  Components begin at
    bits 0, 20 and 40 respectively; the upper four bits are stream metadata.
    """
    result = []
    for shift in (0, 20, 40):
        fraction = packed >> shift & 4095
        integer = packed >> shift + 12 & 127
        value = integer + fraction / 2048.0
        if packed >> shift + 19 & 1:
            value = -value
        result.append(value)
    return tuple(result)

def parse_embedded_bone_bounds(data: bytes) -> dict:
    """Read the v4 skeleton's per-bone local bounds and bind matrices.

    NeoX skin-joint bytes refer to the source skeleton table, but Blender may
    reorder ``armature.data.bones`` after edit bones receive their hierarchy.
    The 28-byte record belonging to each source bone stores a local AABB
    center, half extents and radius.  Those bounds let us validate (and, for a
    variant with a palette permutation, recover) the joint-to-name mapping
    without ever trusting Blender's collection index.
    """
    if len(data) < 16:
        raise ValueError('CMesh is too short for a v4 skeleton header')
    bone_type = struct.unpack_from('<H', data, 8)[0]
    if bone_type not in (1, 4):
        ending_address = struct.unpack_from('<I', data, 12)[0]
        if not 16 <= ending_address <= len(data):
            raise ValueError(f'CMesh ending address is invalid: {ending_address}')
        split_count = struct.unpack_from('<H', data, ending_address)[0]
        split_offsets = [struct.unpack_from('<I', data, ending_address + 2 + i * 4)[0] for i in range(split_count)] if ending_address + 2 + split_count * 4 <= len(data) else []
        return {'bone_type': bone_type, 'bone_count': 0, 'bounds': [], 'matrices': [], 'skeleton_flag': 0, 'ending_address': ending_address, 'split_count': split_count, 'split_offsets': split_offsets}
    bone_count = struct.unpack_from('<H', data, 12)[0]
    extra_flag_offset = 14 + bone_count + bone_count * 32
    if extra_flag_offset >= len(data):
        raise ValueError('CMesh skeleton header extends beyond file')
    has_bounds = data[extra_flag_offset]
    if not has_bounds:
        raise ValueError('CMesh has no per-bone bounds required for joint remapping')
    bounds_start = extra_flag_offset + 1
    matrices_start = bounds_start + bone_count * 28
    matrices_end = matrices_start + bone_count * 64
    if matrices_end + 5 > len(data):
        raise ValueError('CMesh bone bounds/matrices extend beyond file')
    bounds = [struct.unpack_from('<7f', data, bounds_start + i * 28) for i in range(bone_count)]
    matrices = [struct.unpack_from('<16f', data, matrices_start + i * 64) for i in range(bone_count)]
    skeleton_flag = data[matrices_end]
    ending_address = struct.unpack_from('<I', data, matrices_end + 1)[0]
    if not matrices_end + 5 <= ending_address <= len(data):
        raise ValueError(f'CMesh ending address is invalid: {ending_address}')
    split_count = struct.unpack_from('<H', data, ending_address)[0] if ending_address + 2 <= len(data) else 0
    split_offsets = []
    if ending_address + 2 + split_count * 4 <= len(data):
        split_offsets = [struct.unpack_from('<I', data, ending_address + 2 + i * 4)[0] for i in range(split_count)]
    return {'bone_type': bone_type, 'bone_count': bone_count, 'bounds_start': bounds_start, 'matrices_start': matrices_start, 'bounds': bounds, 'matrices': matrices, 'skeleton_flag': skeleton_flag, 'ending_address': ending_address, 'split_count': split_count, 'split_offsets': split_offsets}

def _hungarian_rectangular(cost: list[list[float]]) -> list[int]:
    """Return the minimum-cost unique column for every row (rows <= cols)."""
    row_count = len(cost)
    column_count = len(cost[0]) if cost else 0
    if row_count > column_count:
        raise ValueError('joint remap has more active palettes than named bones')
    u = [0.0] * (row_count + 1)
    v = [0.0] * (column_count + 1)
    p = [0] * (column_count + 1)
    way = [0] * (column_count + 1)
    for i in range(1, row_count + 1):
        p[0] = i
        j0 = 0
        minv = [float('inf')] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float('inf')
            j1 = 0
            for j in range(1, column_count + 1):
                if used[j]:
                    continue
                current = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if current < minv[j]:
                    minv[j] = current
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(column_count + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    assignment = [-1] * row_count
    for j in range(1, column_count + 1):
        if p[j]:
            assignment[p[j] - 1] = j - 1
    return assignment

def recover_joint_remap(parsed: dict) -> tuple[list[int], dict]:
    """Map joint bytes to source-table bones using the embedded bone AABBs."""
    skeleton = parsed['embedded_skeleton']
    bone_count = int(skeleton['bone_count'])
    lows = [[float('inf')] * 3 for _ in range(bone_count)]
    highs = [[-float('inf')] * 3 for _ in range(bone_count)]
    counts = [0] * bone_count
    for position, joint_row, weight_row in zip(parsed['positions'], parsed['joints'], parsed['weights']):
        for raw_joint, weight in zip(joint_row, weight_row):
            if raw_joint == 255 or raw_joint >= bone_count or weight <= 1e-07:
                continue
            counts[raw_joint] += 1
            for axis in range(3):
                lows[raw_joint][axis] = min(lows[raw_joint][axis], position[axis])
                highs[raw_joint][axis] = max(highs[raw_joint][axis], position[axis])
    active = [index for index, count in enumerate(counts) if count]
    actual = {}
    for raw_joint in active:
        center = tuple(((lows[raw_joint][axis] + highs[raw_joint][axis]) * 0.5 for axis in range(3)))
        extents = tuple(((highs[raw_joint][axis] - lows[raw_joint][axis]) * 0.5 for axis in range(3)))
        actual[raw_joint] = (center, extents)
    predicted = []
    for bounds, matrix in zip(skeleton['bounds'], skeleton['matrices']):
        cx, cy, cz, ex, ey, ez, _radius = bounds
        center = (cx * matrix[0] + cy * matrix[4] + cz * matrix[8] + matrix[12], cx * matrix[1] + cy * matrix[5] + cz * matrix[9] + matrix[13], cx * matrix[2] + cy * matrix[6] + cz * matrix[10] + matrix[14])
        extents = (abs(matrix[0]) * ex + abs(matrix[4]) * ey + abs(matrix[8]) * ez, abs(matrix[1]) * ex + abs(matrix[5]) * ey + abs(matrix[9]) * ez, abs(matrix[2]) * ex + abs(matrix[6]) * ey + abs(matrix[10]) * ez)
        predicted.append((center, extents))

    def distance(left, right):
        return math.sqrt(sum(((a - b) ** 2 for a, b in zip(left, right))))

    def match_cost(raw_joint, named_bone):
        actual_center, actual_extents = actual[raw_joint]
        expected_center, expected_extents = predicted[named_bone]
        return distance(actual_center, expected_center) + 0.35 * distance(actual_extents, expected_extents)
    costs = [[match_cost(raw_joint, named_bone) for named_bone in range(bone_count)] for raw_joint in active]
    assignment = _hungarian_rectangular(costs)
    remap = [-1] * bone_count
    matched_costs = []
    mapping_rows = []
    for row, named_bone in enumerate(assignment):
        raw_joint = active[row]
        remap[raw_joint] = named_bone
        matched_costs.append(costs[row][named_bone])
        mapping_rows.append({'runtime_joint': raw_joint, 'named_bone_index': named_bone, 'cost': costs[row][named_bone], 'influenced_vertices': counts[raw_joint]})
    direct_costs = [match_cost(raw_joint, raw_joint) for raw_joint in active]
    ordered_costs = sorted(matched_costs)
    report = {'method': 'embedded_bone_bounds_global_assignment', 'active_runtime_joints': len(active), 'named_bones': bone_count, 'direct_mapping_mean_cost': sum(direct_costs) / len(direct_costs), 'recovered_mean_cost': sum(matched_costs) / len(matched_costs), 'recovered_median_cost': statistics.median(matched_costs), 'recovered_p90_cost': ordered_costs[int((len(ordered_costs) - 1) * 0.9)], 'mapping': mapping_rows}
    return (remap, report)

def parse_legacy_stream(data: bytes, path: Path, cursor: int, parsed_sub: list, embedded_skeleton: dict, version: int):
    """Parse NeoX v2/v3's uncompressed float vertex stream.

    This is the legacy layout used by the three old charselect assets in the
    corpus: float32 XYZ/normal, absolute uint16 faces, float32 UV, byte joints
    and float32 weights.  It mirrors NeoXtractor's MeshParser0 but returns the
    same strict structure as the v4 converter.
    """
    total_v = sum((item[0] for item in parsed_sub))
    total_f = sum((item[1] for item in parsed_sub))
    positions = [struct.unpack_from('<3f', data, cursor + i * 12) for i in range(total_v)]
    cursor += total_v * 12
    normals = [struct.unpack_from('<3f', data, cursor + i * 12) for i in range(total_v)]
    cursor += total_v * 12
    vertex_extra_flag = struct.unpack_from('<H', data, cursor)[0]
    cursor += 2
    if vertex_extra_flag == 1:
        cursor += total_v * 12
    elif vertex_extra_flag > 1:
        cursor += vertex_extra_flag * 4
    face_start = cursor
    indices = [struct.unpack_from('<H', data, cursor + i * 2)[0] for i in range(total_f * 3)]
    cursor += total_f * 6
    source_faces = [tuple(indices[i:i + 3]) for i in range(0, len(indices), 3)]
    faces = []
    cleaned_submeshes = []
    face_base = 0
    degenerate_faces = 0
    duplicate_faces = 0
    seen_faces = set()
    for vcount, fcount, layers, colors in parsed_sub:
        block = source_faces[face_base:face_base + fcount]
        valid_block = []
        for face in block:
            if len(set(face)) != 3:
                degenerate_faces += 1
                continue
            canonical = tuple(sorted(face))
            if canonical in seen_faces:
                duplicate_faces += 1
                continue
            seen_faces.add(canonical)
            valid_block.append(face)
        faces.extend(valid_block)
        cleaned_submeshes.append((vcount, len(valid_block), layers, colors))
        face_base += fcount
    max_layers = max((item[2] for item in parsed_sub), default=0)
    uvs = [[(0.0, 0.0) for _ in range(total_v)] for _ in range(max_layers)]
    nonfinite_uv_source = 0
    global_v = 0
    for vcount, _fcount, layers, _unknown in parsed_sub:
        for layer in range(layers):
            for local in range(vcount):
                u, v = struct.unpack_from('<2f', data, cursor)
                cursor += 8
                if not math.isfinite(u) or not math.isfinite(v):
                    nonfinite_uv_source += 1
                uvs[layer][global_v + local] = (float(u) if math.isfinite(u) else 0.0, float(v) if math.isfinite(v) else 0.0)
        global_v += vcount
    legacy_color_bytes = sum((vcount * color_layers * 4 for vcount, _fcount, _layers, color_layers in parsed_sub))
    cursor += legacy_color_bytes
    joints_start = cursor
    if embedded_skeleton['bone_count']:
        joints = [struct.unpack_from('<4B', data, cursor + i * 4) for i in range(total_v)]
        cursor += total_v * 4
        raw_weights = [struct.unpack_from('<4f', data, cursor + i * 16) for i in range(total_v)]
        cursor += total_v * 16
    else:
        joints = [(255, 255, 255, 255) for _ in range(total_v)]
        raw_weights = [(0.0, 0.0, 0.0, 0.0) for _ in range(total_v)]
    invalid_faces = sum((max(face) >= total_v for face in faces))
    invalid_positions = sum((not all((math.isfinite(x) for x in position)) for position in positions))
    weight_sums = [sum((max(0.0, float(w)) for w in row)) for row in raw_weights]
    invalid_weight_sums = sum((total > 1e-08 and (not 0.95 <= total <= 1.05) for total in weight_sums))
    invalid_joint_influences = sum((joint != 255 and weight > 1e-07 and (joint >= embedded_skeleton['bone_count']) for joint_row, weight_row in zip(joints, raw_weights) for joint, weight in zip(joint_row, weight_row)))
    weights = [tuple((max(0.0, float(weight)) / total for weight in row)) if total > 1e-08 else (0.0, 0.0, 0.0, 0.0) for row, total in zip(raw_weights, weight_sums)]
    report = {'file': str(path), 'version': version, 'flag': None, 'stream_layout': 'legacy_float32_v2_v3', 'vertices': total_v, 'triangles': len(faces), 'source_triangles': total_f, 'degenerate_faces_removed': degenerate_faces, 'duplicate_faces_removed': duplicate_faces, 'submeshes': len(parsed_sub), 'face_start': face_start, 'uv_start': face_start + total_f * 6, 'index_decoder': 'absolute_uint16', 'index_padding': 0, 'vertex_extra_flag': vertex_extra_flag, 'color_bytes': legacy_color_bytes, 'joints_start': joints_start, 'ending_cursor': cursor, 'ending_address': embedded_skeleton['ending_address'], 'invalid_faces': invalid_faces, 'invalid_positions': invalid_positions, 'nonfinite_uv_source': nonfinite_uv_source, 'invalid_weight_sums': invalid_weight_sums, 'invalid_joint_influences': invalid_joint_influences, 'weight_sum_min': min(weight_sums, default=0.0), 'weight_sum_max': max(weight_sums, default=0.0), 'weights_normalized_for_blender': True, 'max_joint': max((j for row in joints for j in row if j != 255), default=-1), 'position_decoder': {'mode': 'float32_xyz'}}
    if invalid_faces or invalid_positions or invalid_weight_sums or invalid_joint_influences:
        raise ValueError(f'legacy decoded data failed validation: {report}')
    return {'positions': positions, 'normals': normals, 'tangents': [], 'faces': faces, 'uvs': uvs, 'joints': joints, 'weights': weights, 'embedded_skeleton': embedded_skeleton, 'submeshes': cleaned_submeshes, 'report': report}

def parse_cmesh(path: Path, structure_path: Path, word_choice=(0, 1, 2), pos_mode='neox20'):
    data = path.read_bytes()
    if len(data) < 14:
        raise ValueError('CMesh 文件头不完整')
    magic, version = struct.unpack_from('<IH', data, 0)
    if magic != 3150479412:
        raise ValueError(f'不支持的 CMesh magic：0x{magic:08x}')
    if version not in (2, 3, 4):
        raise ValueError(f'当前支持 NeoX CMesh v2/v3/v4，检测到 v{version}')
    embedded_skeleton = parse_embedded_bone_bounds(data)
    structure = load_json(structure_path)
    sub = structure['display']['submeshes']
    vc = sum((int(x['vertices']) for x in sub))
    fc = sum((int(x['triangles']) for x in sub))
    offset = int.from_bytes(data[16:20], 'little') if False else None
    mesh_start = -1
    cursor = -1
    parsed_sub = []
    preferred_header = structure['display'].get('header_offset')
    candidates = [int(preferred_header)] if preferred_header is not None else []
    candidates.extend(range(0, min(len(data), 50000)))
    seen_headers = set()
    for candidate in candidates:
        if candidate in seen_headers:
            continue
        seen_headers.add(candidate)
        trial = candidate
        trial_sub = []
        try:
            for expected in sub:
                vcount, fcount, uv_layers, color_layers = struct.unpack_from('<IIBB', data, trial)
                if (vcount, fcount) != (int(expected['vertices']), int(expected['triangles'])):
                    raise ValueError
                uv_valid = 0 <= uv_layers <= 4
                if not (uv_valid and 0 <= color_layers <= 4):
                    raise ValueError
                trial_sub.append((vcount, fcount, uv_layers, color_layers))
                trial += 10
            _marker, _reserved = struct.unpack_from('<BB', data, trial)
            total_v, total_f = struct.unpack_from('<II', data, trial + 2)
            if total_v == vc and total_f == fc:
                mesh_start, cursor, parsed_sub = (candidate, trial + 10, trial_sub)
                break
        except (ValueError, struct.error):
            continue
    if mesh_start < 0:
        raise ValueError('could not locate CMesh submesh header')
    if total_v != vc or total_f != fc:
        raise ValueError(f'mesh totals mismatch: {total_v}/{total_f} vs {vc}/{fc}')
    data_start = cursor
    if version in (2, 3):
        return parse_legacy_stream(data, path, cursor, parsed_sub, embedded_skeleton, version)
    flag = data[cursor]
    cursor += 1
    if flag not in (0, 1, 4, 5, 6):
        raise ValueError(f'当前支持 NeoX v4 顶点流 0/1/4/5/6，检测到 flag={flag}')
    positions = []
    if flag in (1, 5):
        for _ in range(total_v):
            positions.append(struct.unpack_from('<3f', data, cursor))
            cursor += 12
    else:
        for info in sub:
            for _ in range(int(info['vertices'])):
                packed = struct.unpack_from('<Q', data, cursor)[0]
                cursor += 8
                positions.append(decode_position_v4(packed))
    normal_start = cursor
    normals = []
    tangents = []
    if flag in (0, 1):
        for _ in range(total_v):
            normal_xyz = struct.unpack_from('<3b', data, cursor)
            tangent_xyz = struct.unpack_from('<3b', data, cursor + 3)
            cursor += 6
            normals.append(tuple((max(-1.0, min(1.0, v / 127.0)) for v in normal_xyz)))
            tangents.append(tuple((max(-1.0, min(1.0, v / 127.0)) for v in tangent_xyz)))
        packed_attribute_bytes = (total_v * 3 + 7) // 8
        cursor += packed_attribute_bytes
    else:
        for _ in range(total_v):
            xyz = struct.unpack_from('<3b', data, cursor)
            cursor += 3
            normals.append(tuple((max(-1.0, min(1.0, v / 127.0)) for v in xyz)))
        packed_attribute_bytes = 0
    index_count = total_f * 3
    uv_bytes_expected = sum((vcount * layers * 4 for vcount, _fcount, layers, _colors in parsed_sub))
    color_bytes_expected = sum((vcount * colors * 4 for vcount, _fcount, _layers, colors in parsed_sub))
    skin_bytes_expected = total_v * 12 if embedded_skeleton['bone_count'] else 0
    split_offsets_early = embedded_skeleton.get('split_offsets', [])
    next_split_early = split_offsets_early[1] if len(split_offsets_early) > 1 else int(embedded_skeleton['ending_address'])
    footer_bytes_early = 32 if embedded_skeleton['bone_count'] else 0
    expected_index_end = next_split_early - footer_bytes_early - uv_bytes_expected - color_bytes_expected - skin_bytes_expected
    index_candidates = []
    for candidate in range(cursor, cursor + 4):
        index_bytes_end = candidate + index_count * 2
        if index_bytes_end > len(data):
            continue
        words = [struct.unpack_from('<H', data, candidate + index * 2)[0] for index in range(index_count)]
        for mode in ('delta', 'absolute'):
            if mode == 'delta':
                cumulative = 0
                candidate_indices = []
                for word in words:
                    cumulative = cumulative + word & 65535
                    candidate_indices.append(cumulative)
            else:
                candidate_indices = words
            candidate_faces = [tuple(candidate_indices[i:i + 3]) for i in range(0, index_count, 3)]
            invalid = sum((1 for face in candidate_faces if max(face) >= total_v))
            degenerate = sum((1 for face in candidate_faces if len(set(face)) != 3))
            cross = 0
            vb = fb = 0
            for vcount, fcount, _layers, _colors in parsed_sub:
                for face in candidate_faces[fb:fb + fcount]:
                    cross += min(face) < vb or max(face) >= vb + vcount
                vb += vcount
                fb += fcount
            index_candidates.append({'start': candidate, 'end': index_bytes_end, 'indices': candidate_indices, 'faces': candidate_faces, 'invalid': invalid, 'degenerate': degenerate, 'cross': cross, 'padding': candidate - cursor, 'mode': mode, 'end_mismatch': abs(index_bytes_end - expected_index_end)})
    if not index_candidates:
        raise ValueError('CMesh index stream extends beyond file')
    chosen_indices = min(index_candidates, key=lambda item: (item['end_mismatch'], item['invalid'], item['cross'], item['degenerate'], 0 if item['mode'] == 'delta' else 1, item['padding']))
    face_start = chosen_indices['start']
    indices = chosen_indices['indices']
    faces = chosen_indices['faces']
    index_decoder = 'global_uint16_delta' if chosen_indices['mode'] == 'delta' else 'absolute_uint16'
    global_invalid_faces = chosen_indices['invalid']
    degenerate_faces = chosen_indices['degenerate']
    index_padding = chosen_indices['padding']
    index_candidate_report = [{key: item[key] for key in ('start', 'mode', 'invalid', 'cross', 'degenerate', 'padding', 'end_mismatch')} for item in index_candidates]
    cursor = chosen_indices['end']
    submesh_out_of_range_faces = 0
    vertex_base = 0
    face_base = 0
    for vcount, fcount, _layers, _colors in parsed_sub:
        vertex_end = vertex_base + vcount
        for face in faces[face_base:face_base + fcount]:
            if min(face) < vertex_base or max(face) >= vertex_end:
                submesh_out_of_range_faces += 1
        vertex_base = vertex_end
        face_base += fcount
    uv_start = cursor
    max_layers = max((int(x[2]) for x in parsed_sub))
    uvs = [[(0.0, 0.0) for _ in range(total_v)] for _ in range(max_layers)]
    nonfinite_uv_source = 0
    global_v = 0
    for vcount, _fcount, layers, _colors in parsed_sub:
        for layer in range(layers):
            for local in range(vcount):
                u, v = struct.unpack_from('<2e', data, cursor)
                cursor += 4
                if not math.isfinite(u) or not math.isfinite(v):
                    nonfinite_uv_source += 1
                if layer < max_layers:
                    uvs[layer][global_v + local] = (float(u) if math.isfinite(u) else 0.0, float(v) if math.isfinite(v) else 0.0)
        global_v += vcount
    cursor += sum((vcount * 4 * colors for vcount, _fcount, _layers, colors in parsed_sub))
    color_end = cursor
    joints_start = cursor
    if embedded_skeleton['bone_count']:
        joints = [struct.unpack_from('<4B', data, cursor + i * 4) for i in range(total_v)]
        cursor += total_v * 4
        raw_weights = [tuple((v / 65535.0 for v in struct.unpack_from('<4H', data, cursor + i * 8))) for i in range(total_v)]
        cursor += total_v * 8
    else:
        joints = [(255, 255, 255, 255) for _ in range(total_v)]
        raw_weights = [(0.0, 0.0, 0.0, 0.0) for _ in range(total_v)]
    if cursor > len(data):
        raise ValueError('CMesh stream extends beyond file')
    ending_address = int(embedded_skeleton['ending_address'])
    split_offsets = embedded_skeleton.get('split_offsets', [])
    next_split = split_offsets[1] if len(split_offsets) > 1 else ending_address
    expected_footer_bytes = 32 if embedded_skeleton['bone_count'] else 0
    remaining_before_trailer = next_split - cursor
    invalid_faces = sum((1 for a, b, c in faces if max(a, b, c) >= total_v))
    invalid_uv = sum((1 for layer in uvs for u, v in layer if not math.isfinite(u) or not math.isfinite(v)))
    weight_sums = [sum((max(0.0, float(w)) for w in row)) for row in raw_weights]
    invalid_weight_sums = sum((total > 1e-08 and (not 0.95 <= total <= 1.05) for total in weight_sums))
    invalid_joint_influences = sum((joint != 255 and weight > 1e-07 and (joint >= embedded_skeleton['bone_count']) for joint_row, weight_row in zip(joints, raw_weights) for joint, weight in zip(joint_row, weight_row)))
    invalid_positions = sum((not all((math.isfinite(component) for component in position)) for position in positions))
    weights = [tuple((weight / total for weight in row)) if total > 1e-08 else (0.0, 0.0, 0.0, 0.0) for row, total in zip(raw_weights, weight_sums)]
    report = {'file': str(path), 'version': 4, 'flag': flag, 'stream_layout': 'float32_position_normal_tangent_3bit' if flag == 1 else 'packed_position_normal_tangent_3bit' if flag == 0 else 'float32_position_normal' if flag == 5 else 'static_packed_position_normal' if flag == 6 else 'packed_position_normal', 'packed_attribute_bytes': packed_attribute_bytes, 'vertices': total_v, 'triangles': total_f, 'submeshes': len(parsed_sub), 'data_start': data_start, 'face_start': face_start, 'uv_start': uv_start, 'index_decoder': index_decoder, 'index_padding': index_padding, 'index_candidates': index_candidate_report, 'global_invalid_faces': global_invalid_faces, 'degenerate_faces': degenerate_faces, 'submesh_out_of_range_faces': submesh_out_of_range_faces, 'joints_start': joints_start, 'ending_cursor': cursor, 'ending_address': ending_address, 'next_split_offset': next_split, 'remaining_before_trailer': remaining_before_trailer, 'expected_footer_bytes': expected_footer_bytes, 'invalid_faces': invalid_faces, 'invalid_uv': invalid_uv, 'nonfinite_uv_source': nonfinite_uv_source, 'invalid_positions': invalid_positions, 'invalid_weight_sums': invalid_weight_sums, 'invalid_joint_influences': invalid_joint_influences, 'weight_sum_min': min(weight_sums), 'weight_sum_max': max(weight_sums), 'weights_normalized_for_blender': True, 'max_joint': max((j for row in joints for j in row if j != 255), default=-1), 'position_decoder': {'mode': 'float32_xyz'} if flag in (1, 5) else {'mode': 'neox_signed_20bit_fixed', 'component_bits': 20, 'fraction_bits': 12, 'integer_bits': 7, 'sign_bits': [19, 39, 59], 'note': "Recovered from the game's ARM64 CMesh loader at libclient.so:0x88e5b8-0x88e644."}}
    if invalid_faces or invalid_uv or submesh_out_of_range_faces or invalid_positions or invalid_weight_sums or invalid_joint_influences or (remaining_before_trailer != expected_footer_bytes) or (nonfinite_uv_source > max(128, total_v // 100)):
        raise ValueError(f'decoded data failed validation: {report}')
    return {'positions': positions, 'normals': normals, 'tangents': tangents, 'faces': faces, 'uvs': uvs, 'joints': joints, 'weights': weights, 'embedded_skeleton': embedded_skeleton, 'submeshes': parsed_sub, 'report': report}
