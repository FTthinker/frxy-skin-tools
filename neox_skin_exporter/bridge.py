"""Read-only Blender mesh capture and suggested material source mapping."""
import struct
from collections import Counter
import bpy
import numpy as np
from . import core,metadata,decoder

def collect(obj,project_dir,binding='NATIVE',scale=0.08,mirror=True,flip_u=False,flip_v=True,root_bone=0,shape_basis=False,uv_map='',uv_map_2='UVMap_2',uv_map_3='UVMap_3',uv_map_4='UVMap_4',excluded_material_slots=None):
    core.require(obj is not None and obj.type=='MESH','请选择角色网格物体')
    core.require(obj.mode=='OBJECT','请先退出编辑/雕刻模式')
    core.require(scale>0,'比例必须大于零')
    _,src,a,meta=core.template(project_dir);bones=metadata.parse_skeleton(src)['bones'];arm=obj.find_armature()
    warnings=[]
    excluded=set(excluded_material_slots or [])
    core.require(not excluded or binding in {'STATIC','RETARGET'},'原生模式不能跳过 MMD 球面层')
    if binding=='RETARGET':
        core.require(obj.get('nxse_retarget') and arm and arm.get('nxse_retarget'),'请先通过 MMD 动作适配流程建立副本')
        warnings.append('实验性 MMD 蒙皮适配；表情、物理、VMD 不写入；须在游戏核对动作与关节变形。')
    if excluded:
        from . import uvflow
        proven={r['slot'] for r in uvflow.duplicate_sphere_layers(obj,include_skin=binding=='RETARGET')}
        core.require(excluded<=proven,'球面层重复关系已改变，请重新一键配置，不能删除未证明重复的面')
        warnings.append('仅导出时跳过重复球面高光材质槽：'+str(sorted(excluded)))
    for mod in obj.modifiers:
        core.require(not mod.show_viewport or mod.type=='ARMATURE',f'修改器 {mod.name} 尚未应用；请先在模型副本上应用几何修改器')
    keys=obj.data.shape_keys
    if keys and len(keys.key_blocks)>1:
        core.require(shape_basis,'模型有形态键；归零后勾选“仅导出 Basis”，或先在副本中应用所需形态')
        core.require(keys.use_relative and all(abs(k.value)<1e-6 for k in list(keys.key_blocks)[1:]),'仅导出 Basis 要求相对形态键数值全部归零')
        core.require(all((v.co-k.co).length<1e-6 for v,k in zip(obj.data.vertices,keys.key_blocks[0].data)),'基础网格与 Basis 不一致，请先在副本中应用形态')
        warnings.append('仅导出 Basis 基础形态；表情形态键和相关动画不写入游戏。')
    mesh=obj.data;uv_layer=mesh.uv_layers.get(uv_map) if uv_map else mesh.uv_layers.active
    core.require(uv_layer is not None,'找不到所选 UV；请先展开 UV 或重新选择导出 UV')
    uv_count=max(s[2] for s in a['submeshes']);uv_layers=[uv_layer]+[mesh.uv_layers.get(n) if n else None for n in (uv_map_2,uv_map_3,uv_map_4)][:max(0,uv_count-1)]
    if len(mesh.uv_layers)>1:warnings.append(f'模型有 {len(mesh.uv_layers)} 层 UV；按原分组保留所需的 1～{uv_count} 层，其余不直接写入。')
    proof={}
    if binding in {'NATIVE','RETARGET'}:
        core.require(arm is not None,'原生动作模式需要原骨架')
        scale=float(arm.get('neox_spatial_scale',scale));mirror=bool(arm.get('neox_mirror_x',mirror))
        flip_u=bool(obj.get('neox_uv_flip_u',flip_u));flip_v=bool(obj.get('neox_uv_flip_v',flip_v))
        indexed={int(b['neox_source_index']):b for b in arm.data.bones if 'neox_source_index' in b}
        core.require(len(indexed)==len(bones) and len(indexed)==sum('neox_source_index' in b for b in arm.data.bones),'原生骨骼编号缺失、重复或骨骼数量改变')
        for old in bones:
            b=indexed.get(old['index']);core.require(b is not None,'原生骨骼编号不完整')
            parent=int(b.parent.get('neox_source_index',-2)) if b.parent else -1
            core.require(parent==old['parent_index'],f'骨骼 {b.name} 的父子关系已改变')
            m=old['matrix_row_major']
            def cv(v):return np.array([-v[0] if mirror else v[0],-v[2],v[1]])
            core.require(np.max(np.abs(cv(m[12:15])*scale-np.array(b.head_local)))<1e-4,f'骨骼 {b.name} 的静止位置已改变')
            expected_y=cv(m[:3]);expected_y/=np.linalg.norm(expected_y)
            expected_z=cv(m[4:7]);expected_z/=np.linalg.norm(expected_z)
            actual=np.array(b.matrix_local)
            core.require(float(expected_y@actual[:3,1])>.9999 and float(expected_z@actual[:3,2])>.9999,f'骨骼 {b.name} 的静止方向已改变')
        groupmap={g.index:int(arm.data.bones[g.name]['neox_source_index']) for g in obj.vertex_groups if g.name in arm.data.bones and 'neox_source_index' in arm.data.bones[g.name]}
        rel=arm.matrix_world.inverted()@obj.matrix_world
        if any(any(abs(b.matrix_basis[r][c]-(1 if r==c else 0))>1e-6 for r in range(4) for c in range(4)) for b in arm.pose.bones):warnings.append('检测到当前动画姿势；已读取静止网格，未烘焙当前姿势。')
        remap,_=decoder.recover_joint_remap(a)
        if any(i!=v for i,v in enumerate(remap) if v>=0):
            from pathlib import Path
            origin=Path(bpy.path.abspath(arm.get('neox_source_cmesh','')))
            core.require(origin.is_file() and core.sha(origin)==core.sha(src),'非直接骨骼映射必须对应同一个 NeoX 导入源文件，当前骨架来源无法核对')
            proof={'bone_index_space':'imported_names','joint_remap':remap,'source_sha256':core.sha(src)}
            warnings.append('已按同一源文件的 NeoX 导入映射逆转换骨骼权重；仍需在游戏验证动作。')
    else:
        core.require(0<=root_bone<len(bones),'静态绑定骨骼编号无效')
        rel=obj.matrix_world.copy();groupmap={}
        warnings.append('静态模式：整个模型绑定到一个目标骨骼，不具有肢体动画。')
    normalmat=rel.to_3x3().inverted().transposed()
    def game(v):return [-v.x if mirror else v.x,v.z,-v.y]
    vertices=[];used={v for p in mesh.polygons for v in p.vertices}
    for v in mesh.vertices:
        if binding=='STATIC':influences=[(root_bone,1.0)]
        else:
            unknown=[obj.vertex_groups[g.group].name for g in v.groups if g.weight>1e-8 and g.group not in groupmap]
            # Non-bone utility groups are not skin influences. Bones without source IDs are unsafe.
            bad=[n for n in unknown if n in arm.data.bones]
            core.require(not bad,f'顶点 {v.index} 绑定了非原生骨骼：{bad[:3]}')
            influences=sorted([(groupmap[g.group],g.weight) for g in v.groups if g.group in groupmap and g.weight>1e-8],key=lambda x:-x[1])
            if v.index in used:core.require(1<=len(influences)<=4,f'顶点 {v.index} 的有效骨骼权重数量为 {len(influences)}，要求 1～4')
        vertices.append({'p':[x/scale for x in game(rel@v.co)],'weights':influences})
    mesh.calc_loop_triangles();corners=[]
    for i,l in enumerate(mesh.loops):
        normal=mesh.corner_normals[i].vector if hasattr(mesh,'corner_normals') else l.normal
        n=(normalmat@normal).normalized();uv=uv_layer.data[i].uv
        uv_values=[]
        for layer in uv_layers:
            value=layer.data[i].uv if layer else None
            uv_values.append([1-value.x if flip_u else value.x,1-value.y if flip_v else value.y] if value is not None else None)
        corners.append({'v':l.vertex_index,'n':game(n),'uv':uv_values[0],'uvs':uv_values})
    triangles=[]
    for t in mesh.loop_triangles:
        if t.material_index in excluded:continue
        li=list(t.loops)
        if (rel.to_3x3().determinant()<0)^mirror:li.reverse()
        triangles.append({'loops':li,'material':t.material_index})
    core.require(triangles,'网格没有三角面')
    return {'vertices':vertices,'corners':corners,'triangles':triangles,'binding':binding,'warnings':warnings,'scale':scale,'mirror':mirror,'uv_map':uv_layer.name,**proof},a,meta

def suggest(obj,doc,parsed,meta):
    """Use exact names first, then unambiguous UV-triangle evidence; never nearest positions."""
    names={s['name']:i for i,s in enumerate(meta['submeshes'])};tri_uv={};start=0
    for si,sub in enumerate(parsed['submeshes']):
        for f in parsed['faces'][start:start+sub[1]]:
            key=tuple(sorted(struct.pack('<2e',*parsed['uvs'][0][v]) for v in f));tri_uv.setdefault(key,set()).add(si)
        start+=sub[1]
    votes={}
    for t in doc['triangles']:
        key=tuple(sorted(struct.pack('<2e',*doc['corners'][i]['uv']) for i in t['loops']))
        candidates=tri_uv.get(key,set())
        if len(candidates)==1:votes.setdefault(t['material'],Counter()).update(candidates)
    result=[]
    for slot in sorted({t['material'] for t in doc['triangles']}):
        core.require(slot<len(obj.material_slots),f'面引用的材质槽 {slot} 不存在，请为网格设置材质')
        mat=obj.material_slots[slot].material;core.require(mat is not None,f'材质槽 {slot} 为空')
        base=mat.name.split(' · ')[0];base=base[:-4] if base.endswith('_mat') else base
        si=names.get(base,-1);why='名称匹配' if si>=0 else '需要选择来源';mode='ORIGINAL';image=None
        if si<0:
            counts=votes.get(slot,Counter()).most_common()
            if counts and (len(counts)==1 or counts[0][1]>=4*counts[1][1]) and counts[0][1]>=3:si=counts[0][0];why=f'UV 三角形匹配 {counts[0][1]} 个（请确认）';mode='BAKE'
        packed=[n.image for n in mat.node_tree.nodes if n.type=='TEX_IMAGE' and n.image and n.image.packed_file] if mat.use_nodes else []
        # For original-named materials, a new packed diffuse image is an explicit edit.
        if packed and base in names:
            diffuse=mat.get('neox_diffuse_texture','')
            candidates=[im for im in packed if diffuse and im.name.startswith(diffuse)]
            if len(candidates)==1:mode='IMAGE';image=candidates[0];why+='；检测到内嵌颜色贴图'
        if mode=='ORIGINAL' and (not mat.use_nodes or not any(n.type=='BSDF_PRINCIPLED' for n in mat.node_tree.nodes)):why+='；节点效果默认沿用游戏材质'
        result.append({'slot':slot,'material':mat,'name':mat.name,'source_sub':si,'mode':mode,'image':image,'why':why})
    return result
