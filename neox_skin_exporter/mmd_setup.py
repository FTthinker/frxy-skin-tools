"""Configure foreign MMD color textures without compiling its preview shaders."""
from pathlib import Path
import xml.etree.ElementTree as ET
import bpy
import numpy as np
from . import core,uvflow


def solid_viewports():
    # Image-ID changes can trigger EEVEE compilation of imported MMD node groups.
    # Leave every stored 3D view in solid mode, including inactive workspaces.
    for screen in bpy.data.screens:
        for area in screen.areas:
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.type = 'SOLID'


def recover_slots(obj):
    """Repair only a proven single-source static copy; do not guess by slot number."""
    if all(s.material is not None for s in obj.material_slots):
        return 0
    origin = bpy.data.objects.get(obj.get('nxse_prepared_from', ''))
    core.require(origin is not None and origin.type == 'MESH',
                 '导出副本有空材质槽，请从原 MMD 网格重新制作静态副本')
    a, b = obj.data, origin.data
    core.require(len(a.vertices) == len(b.vertices) and len(a.polygons) == len(b.polygons)
                 and len(obj.material_slots) == len(origin.material_slots)
                 and all(tuple(p.vertices) == tuple(q.vertices) and p.material_index == q.material_index
                         for p, q in zip(a.polygons, b.polygons)),
                 '副本与原物体拓扑不同，不能自动恢复材质槽；请重新制作副本')
    core.require(all(s.material is not None for s in origin.material_slots), '原 MMD 也有空材质槽')
    count = 0
    for dst, src in zip(obj.material_slots, origin.material_slots):
        if dst.material is None:
            dst.material = src.material
            count += 1
    return count


def base_image(mat):
    core.require(mat and mat.use_nodes, '材质没有节点，无法识别颜色贴图')
    found = []
    for node in mat.node_tree.nodes:
        if node.type != 'TEX_IMAGE' or not node.image:
            continue
        # Use socket semantics; never choose the first image (it may be a toon/sphere).
        links = [link for output in node.outputs for link in output.links]
        if any((l.to_node.type == 'GROUP' and l.to_socket.name == 'Base Tex') or
               (l.to_node.type == 'BSDF_PRINCIPLED' and l.to_socket.name == 'Base Color')
               for l in links):
            if node.image not in found:
                found.append(node.image)
    core.require(len(found) == 1, f'{mat.name}：未找到唯一颜色贴图，请检查 Base Tex / Base Color 连接')
    im = found[0]
    core.require(im.source in {'FILE', 'GENERATED'}, f'{mat.name}：暂不自动处理序列或平铺贴图')
    core.require(im.packed_file or im.source == 'GENERATED' or Path(bpy.path.abspath(im.filepath)).is_file(),
                 f'{mat.name}：颜色贴图文件不存在：{im.filepath}')
    return im


def simple_template(folder):
    p = core.load_project(folder)
    mt = ET.parse(Path(folder) / 'source' / (p['stem'] + '.mtg'))
    candidates = []
    for i, sub in enumerate(p['submeshes']):
        mat = mt.find(f".//Material_{sub['material_index']}/Material")
        if mat is None:
            continue
        tech = mat.find('Technique')
        name = tech.get('TechName', '').replace('\\', '/').lower() if tech is not None else ''
        table = mat.find('ParamTable')
        if (name == 'shader/common_nolight_dye.fx::tshader' and table is not None
                and table.find('diffuseTexture') is not None
                and mat.find('.//TransparentMode') is not None and sub['uv_layers'] == 1):
            # Reject unexpected populated dependencies instead of reusing old masks/normals.
            extras = [e for e in table if e.tag != 'diffuseTexture' and e.get('Value', '').strip()]
            if not extras:
                candidates.append(i)
    core.require(candidates, '目标没有已支持的简单颜色材质模板，不能自动套用；请手动选择并验证')
    return p, candidates[0]


def configure(context, settings):
    solid_viewports()
    obj = settings.mesh
    core.require(settings.binding in {'STATIC','RETARGET'}, '外来 MMD 自动贴图用于静态或动作适配副本')
    core.require(obj is not None and obj.type == 'MESH' and obj.name in context.scene.objects,
                 '请先制作或选择 MMD 静态导出副本')
    core.require(obj.mode == 'OBJECT', '请先退出编辑模式')
    folder = Path(bpy.path.abspath(settings.project_dir))
    project, source = simple_template(folder)
    repaired = recover_slots(obj)
    used = sorted({p.material_index for p in obj.data.polygons})
    uv_sources = uvflow.plan(obj, used)
    duplicate_layers = uvflow.duplicate_sphere_layers(obj,include_skin=settings.binding=='RETARGET') if settings.skip_duplicate_sphere else []
    rules, alpha_cache, warnings = [], {}, []
    for index in used:
        core.require(index < len(obj.material_slots), '面引用了不存在的材质槽')
        mat = obj.material_slots[index].material
        image = base_image(mat)
        if image.name not in alpha_cache:
            core.require(image.size[0] > 0 and image.size[1] > 0, f'图像无法读取：{image.name}')
            if image.channels == 4 and image.alpha_mode != 'NONE':
                pixels = np.empty(len(image.pixels), dtype=np.float32)
                image.pixels.foreach_get(pixels)
                alpha_cache[image.name] = bool(np.any(pixels[3::4] < .999))
                del pixels
            else:
                alpha_cache[image.name] = False
        mmd = getattr(mat, 'mmd_material', None)
        sphere = str(getattr(mmd, 'sphere_texture_type', '0'))
        sphere_link = any(n.type == 'TEX_IMAGE' and n.image and
                          any(l.to_socket.name == 'Sphere Tex' for out in n.outputs for l in out.links)
                          for n in mat.node_tree.nodes)
        if sphere not in {'0', 'None'} or sphere_link:
            warnings.append(f'{mat.name} 含球面高光设置，仅导出颜色；游戏效果需单独验证')
        if float(getattr(mmd, 'alpha', 1.0)) < .999:
            warnings.append(f'{mat.name} 的材质整体透明度尚未烘入图像')
        rules.append((index, mat, image, 'ALPHA' if alpha_cache[image.name] else 'OPAQUE'))
    core.require(rules, '模型没有可导出的材质')
    settings.uv_map = uvflow.consolidate(obj, uv_sources)
    import json
    settings.excluded_material_slots = json.dumps([r['slot'] for r in duplicate_layers])
    # Complete all detection before replacing the old configuration.
    settings.materials.clear()
    for index, mat, image, alpha in rules:
        row = settings.materials.add()
        row.slot = index
        row.material = mat
        row.source = str(source)
        row.mode = 'IMAGE'
        row.image = image
        row.alpha = alpha
        shader = next((n for n in mat.node_tree.nodes if n.type == 'GROUP' and n.inputs.get('Double Sided')), None)
        row.double_sided = bool(shader and shader.inputs['Double Sided'].default_value > .5)
        row.reason = 'MMD 颜色连接自动识别；简单颜色模板；已切换实体显示'
    result = {'materials': len(rules), 'unique_images': len(alpha_cache), 'repaired_slots': repaired,
              'source': project['submeshes'][source]['name'], 'warnings': warnings,
              'images': {mat.name: im.name for _, mat, im, _ in rules},
              'color_uv_sources': {obj.material_slots[i].material.name:n for i,n in uv_sources.items()},
              'export_uv':settings.uv_map,'skipped_duplicate_sphere_layers':duplicate_layers}
    core.dump(folder / 'reports/mmd_auto_setup.json', result)
    return result
