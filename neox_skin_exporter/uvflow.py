"""Follow the color texture's UV connection, not the editor's active UV selection."""
import numpy as np
from . import core


def color_node(mat):
    core.require(mat and mat.use_nodes, '材质没有可识别的颜色节点')
    nodes = []
    for node in mat.node_tree.nodes:
        if node.type != 'TEX_IMAGE' or not node.image:
            continue
        if any((l.to_node.type == 'GROUP' and l.to_socket.name == 'Base Tex') or
               (l.to_node.type == 'BSDF_PRINCIPLED' and l.to_socket.name == 'Base Color')
               for output in node.outputs for l in output.links):
            nodes.append(node)
    core.require(len(nodes) == 1, f'{mat.name}：未找到唯一颜色贴图节点')
    return nodes[0]


def render_uv(mesh):
    layers = [uv for uv in mesh.uv_layers if uv.active_render]
    core.require(len(layers) == 1, '无法确定用于渲染的 UV 层，请检查 UV 设置')
    return layers[0].name


def image_uv(mesh, node):
    def trace_input(socket, parents, depth=0):
        core.require(depth < 32, 'UV 节点嵌套过深或存在循环')
        core.require(len(socket.links) == 1, 'UV 连接不是唯一输入，需先烘焙')
        link = socket.links[0]
        src, output = link.from_node, link.from_socket
        if src.type == 'UVMAP':
            return src.uv_map or render_uv(mesh)
        if src.type == 'TEX_COORD' and output.name == 'UV':
            return render_uv(mesh)
        if src.type == 'REROUTE':
            return trace_input(src.inputs[0], parents, depth + 1)
        if src.type == 'GROUP':
            ends = [n for n in src.node_tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output]
            core.require(len(ends) == 1, 'UV 节点组没有唯一输出')
            index = list(src.outputs).index(output)
            return trace_input(ends[0].inputs[index], parents + [src], depth + 1)
        if src.type == 'GROUP_INPUT' and parents:
            parent = parents[-1]
            index = list(src.outputs).index(output)
            return trace_input(parent.inputs[index], parents[:-1], depth + 1)
        raise ValueError(f'颜色图使用 {src.type} 坐标变换，当前直传模式不能等价转换；请先烘焙')
    vector = node.inputs['Vector']
    name = trace_input(vector, []) if vector.is_linked else render_uv(mesh)
    core.require(mesh.uv_layers.get(name) is not None, f'颜色图引用了不存在的 UV：{name}')
    return name


def plan(obj, slots):
    result = {}
    for slot in slots:
        core.require(slot < len(obj.material_slots), '材质槽不存在')
        mat = obj.material_slots[slot].material
        try:
            result[slot] = image_uv(obj.data, color_node(mat))
        except ValueError as exc:
            raise ValueError(f'{mat.name if mat else slot}：{exc}') from exc
    return result


def coordinates(mesh, name):
    arr = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    mesh.uv_layers[name].data.foreach_get('uv', arr)
    return arr.reshape((-1, 2))


def consolidate(obj, sources, name='NXSE_ColorUV'):
    mesh = obj.data
    # Read all source layers before creating or overwriting the destination.
    data = {n: coordinates(mesh, n) for n in set(sources.values())}
    result = np.zeros((len(mesh.loops), 2), dtype=np.float32)
    for poly in mesh.polygons:
        if poly.material_index in sources:
            indexes = list(poly.loop_indices)
            result[indexes] = data[sources[poly.material_index]][indexes]
    layer = mesh.uv_layers.get(name)
    if layer is None:
        core.require(len(mesh.uv_layers) < 8, 'UV 层已满，无法建立按材质合并的颜色 UV')
        layer = mesh.uv_layers.new(name=name)
    layer.data.foreach_set('uv', result.ravel())
    return layer.name


def validate(obj, rules, export_name, excluded=()):
    """Only enforce for recognized MMD colors; ordinary native texture edits stay unchanged."""
    slots = []
    for rule in rules:
        slot = rule['slot']
        if slot in excluded or rule['mode'] != 'IMAGE':
            continue
        mat = obj.material_slots[slot].material
        if mat and mat.use_nodes and any(l.to_node.type == 'GROUP' and l.to_socket.name == 'Base Tex'
                                       for n in mat.node_tree.nodes for out in n.outputs for l in out.links):
            slots.append(slot)
    if not slots:
        return
    sources = plan(obj, slots)
    mesh = obj.data
    name = export_name or mesh.uv_layers.active.name
    target = coordinates(mesh, name)
    original = {n: coordinates(mesh, n) for n in set(sources.values())}
    for poly in mesh.polygons:
        source = sources.get(poly.material_index)
        if source is not None:
            ids = list(poly.loop_indices)
            core.require(np.allclose(target[ids], original[source][ids], atol=1e-6, rtol=0),
                         f'颜色贴图 UV 不匹配：{obj.material_slots[poly.material_index].material.name} 使用 {source}，'
                         f'当前导出 {name}；请点击“一键配置 MMD 贴图”重新识别')


def duplicate_sphere_layers(obj,include_skin=False):
    """Skip only additive sphere layers exactly duplicating another non-sphere surface."""
    from collections import Counter
    mesh = obj.data
    groups, additive = {}, set()
    for i, slot in enumerate(obj.material_slots):
        m = slot.material
        if m and m.use_nodes:
            for n in m.node_tree.nodes:
                if n.type == 'GROUP' and n.inputs.get('Sphere Mul/Add') and n.inputs.get('Sphere Tex Fac'):
                    if n.inputs['Sphere Mul/Add'].default_value > .5 and n.inputs['Sphere Tex Fac'].default_value > .5:
                        additive.add(i)
    if not additive:
        return []
    # Exact position/face-size proof (with winding), not material names or bounding boxes.
    positions = [tuple(v.co) for v in mesh.vertices]
    if include_skin:
        # Coincident bind-pose geometry alone is not proof for animated layers.
        # Their normalized bone assignments must also match at every corner.
        arm=obj.find_armature()
        core.require(arm is not None,'动画重复层检查缺少骨架')
        keys=[]
        for v,p in zip(mesh.vertices,positions):
            ws=sorted((obj.vertex_groups[g.group].name,float(g.weight)) for g in v.groups
                      if g.weight>1e-8 and obj.vertex_groups[g.group].name in arm.data.bones)
            total=sum(w for n,w in ws)
            keys.append((p,tuple((n,w/total) for n,w in ws) if total else ()))
        positions=keys
    for p in mesh.polygons:
        face = tuple(positions[i] for i in p.vertices)
        face = min(face[i:] + face[:i] for i in range(len(face)))
        groups.setdefault(p.material_index, Counter())[face] += 1
    return [{'slot':i, 'name':obj.material_slots[i].material.name, 'base_slot':j,
             'base_name':obj.material_slots[j].material.name, 'faces':sum(groups[i].values())}
            for i in sorted(additive) if i in groups
            for j in [next((j for j in groups if j not in additive and groups[j] == groups[i]), None)]
            if j is not None]
