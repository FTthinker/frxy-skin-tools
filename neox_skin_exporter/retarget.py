"""Experimental rest-mesh retargeting. Game bind matrices are never edited.

Only explicit/semantic bone correspondences are used. Unmapped weighted bones
may inherit a mapped ancestor, and are reported; no spatial nearest-bone guesses.
"""
import json, math, re, unicodedata
from pathlib import Path
from collections import Counter
import bpy
from mathutils import Vector, Matrix
from . import core, metadata, uvflow

CENTRAL = {
    'hips': ('骨盆', ['下半身','hips','pelvis']),
    'spine': ('脊柱', ['上半身','spine']),
    'chest': ('胸部', ['上半身2','chest','spine1','upperchest']),
    'neck': ('颈部', ['首','neck']),
    'head': ('头部', ['頭','head']),
    'root': ('根骨骼', ['全ての親','root','bip001','bip01']),
}
LIMBS = {
    'clavicle': ('肩', ['肩','clavicle','shoulder']),
    'upperarm': ('上臂', ['腕','upperarm','arm']),
    'forearm': ('前臂', ['ひじ','肘','forearm','lowerarm','elbow']),
    'hand': ('手腕', ['手首','hand','wrist']),
    'thigh': ('大腿', ['足','thigh','upleg','upperleg']),
    'calf': ('小腿', ['ひざ','膝','calf','leg','lowerleg','knee']),
    'foot': ('脚踝', ['足首','foot','ankle']),
    'toe': ('脚趾', ['つま先','足先ex','toe0','toe','toebase']),
}
ROLES = {k:v[0] for k,v in CENTRAL.items()}
for side,label in [('l','左'),('r','右')]:
    for k,v in LIMBS.items(): ROLES[side+'_'+k]=label+v[0]
    for finger in range(5):
        for joint in range(3):ROLES[f'{side}_finger{finger}_{joint}']=label+['拇指','食指','中指','无名指','小指'][finger]+str(joint+1)
REQUIRED = ['hips','spine','head']+[s+'_'+r for s in ('l','r') for r in ('upperarm','forearm','hand','thigh','calf','foot')]
NEXT = {'hips':'spine','spine':'chest','chest':'neck','neck':'head'}
for side in ('l','r'):
    for chain in [('clavicle','upperarm','forearm','hand'),('thigh','calf','foot','toe')]:
        for a,b in zip(chain,chain[1:]):NEXT[side+'_'+a]=side+'_'+b
    for f in range(5):
        for j in range(2):NEXT[f'{side}_finger{f}_{j}']=f'{side}_finger{f}_{j+1}'

def role(name):
    n=unicodedata.normalize('NFKC',name).lower().strip()
    if n.startswith(('_dummy','_shadow')):return None
    for k,(_,aliases) in CENTRAL.items():
        if n in aliases:return k
    n=re.sub(r'^bip\d+[ _]*','',n)
    for k,(_,aliases) in CENTRAL.items():
        if n in aliases:return k
    side=None
    if n.startswith(('左','右')):side='l' if n[0]=='左' else 'r';n=n[1:]
    else:
        m=re.match(r'^(left|right|l[ _]|r[ _])(.+)$',n)
        if m:side='l' if m[1].startswith('l') else 'r';n=m[2].strip(' _')
        else:
            m=re.match(r'^(.+?)[._ ](l|r)$',n)
            if m:n=m[1];side=m[2]
    if side is None:return None
    for k,(_,aliases) in LIMBS.items():
        if n in aliases:return side+'_'+k
    # PMX D deform legs are aliases, not IK controllers.
    if n in ('足d','ひざd','膝d','足首d'):
        return side+'_'+{'足d':'thigh','ひざd':'calf','膝d':'calf','足首d':'foot'}[n]
    m=re.fullmatch(r'finger([0-4])([12]?)',n)
    if m:return f'{side}_finger{m[1]}_{int(m[2]) if m[2] else 0}'
    m=re.fullmatch(r'(親指|人指|人差指|中指|薬指|小指)([0-3])',n)
    if m:
        f={'親指':0,'人指':1,'人差指':1,'中指':2,'薬指':3,'小指':4}[m[1]]
        j=int(m[2])-(0 if f==0 else 1)
        if 0<=j<=2:return f'{side}_finger{f}_{j}'
    return None

def source_arm(objects):
    arms={o.find_armature() for o in objects}
    core.require(len(arms)==1 and None not in arms,'动作适配要求所有源网格绑定到同一个 MMD 骨架；无骨架模型请先蒙皮')
    return next(iter(arms))

def source_names(arm):
    # Read standard Japanese metadata when mmd_tools is enabled; Blender names
    # remain the actual vertex-group keys.
    return {b.name:[b.name,getattr(getattr(arm.pose.bones[b.name],'mmd_bone',None),'name_j','')] for b in arm.data.bones}

def candidates(names):
    out={}
    for name,aliases in names.items():
        roles={role(a) for a in aliases if a};roles.discard(None)
        if len(roles)==1:out.setdefault(next(iter(roles)),[]).append(name)
    return out

def preferred(names):
    if len(names)==1:return names[0]
    primary=[n for n in names if not re.search(r'[dD](?:\.[LR])?$',n)]
    return primary[0] if len(primary)==1 else ''

def signature(arm,source):
    import hashlib
    return core.sha(source)+':'+hashlib.sha256(json.dumps([(b.name,b.parent.name if b.parent else '') for b in arm.data.bones],ensure_ascii=False).encode()).hexdigest()

def input_signature(s):
    return json.dumps({'source':str(Path(bpy.path.abspath(s.source_cmesh)).resolve()),
        'source_sha256':core.sha(Path(bpy.path.abspath(s.source_cmesh))),
        'surface':s.workflow_surface,'objects':[r.obj.name if r.obj else None for r in s.workflow_sources],
        'map':[(r.role,r.source,r.target) for r in s.retarget_map],
        'fallback':s.retarget_fallback,'project':s.project_dir},ensure_ascii=False,sort_keys=True)

def analyze(s,objects,source):
    arm=source_arm(objects);sk=metadata.parse_skeleton(source)
    core.require(1<=len(sk['bones'])<=254,'目标骨架不在已支持范围')
    src=candidates(source_names(arm));dst=candidates({b['name']:[b['name']] for b in sk['bones']})
    s.retarget_map.clear();s.retarget_source_arm=arm
    for k,label in ROLES.items():
        row=s.retarget_map.add();row.role=k;row.label=label
        row.source=preferred(src.get(k,[]));row.target=preferred(dst.get(k,[]))
    s.retarget_signature=signature(arm,source)
    s.last_report='';s.last_job=''
    s.status='已识别骨骼。必需项不能留空；可展开对应表修正，然后准备动作副本。'

def validate_map(s,objects,source):
    arm=source_arm(objects)
    if not s.retarget_map or s.retarget_signature!=signature(arm,source):analyze(s,objects,source)
    sk=metadata.parse_skeleton(source);dst={b['name']:b for b in sk['bones']}
    pairs={r.role:(r.source,r.target) for r in s.retarget_map if r.source and r.target}
    missing=[ROLES[k] for k in REQUIRED if k not in pairs]
    core.require(not missing,'缺少骨骼对应：'+', '.join(missing)+'；请点识别骨骼并在对应表补全')
    for k,(a,b) in pairs.items():
        core.require(a in arm.data.bones,f'{ROLES[k]} 源骨骼不存在：{a}')
        core.require(b in dst,f'{ROLES[k]} 目标骨骼不存在：{b}')
    core.require(len({a for a,b in pairs.values()})==len(pairs),'多个部位使用了同一源骨骼，请修正对应表')
    core.require(len({b for a,b in pairs.values()})==len(pairs),'多个部位使用了同一目标骨骼，请修正对应表')
    return arm,sk,pairs

def create_arm(context,source,sk,scale=.08,mirror=True):
    data=bpy.data.armatures.new('NeoX_目标原骨架');arm=bpy.data.objects.new('NeoX_动作适配骨架',data)
    context.scene.collection.objects.link(arm)
    bpy.ops.object.select_all(action='DESELECT');arm.select_set(True);context.view_layer.objects.active=arm
    def cv(v):return Vector((-v[0] if mirror else v[0],-v[2],v[1]))
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        made={}
        for old in sk['bones']:
            b=data.edit_bones.new(old['name']);m=old['matrix_row_major']
            b.head=cv(m[12:15])*scale;b.tail=b.head+cv(m[:3]).normalized()*.03;b.align_roll(cv(m[4:7]))
            b['neox_source_index']=old['index'];made[old['index']]=b
        for old in sk['bones']:
            if old['parent_index']>=0:made[old['index']].parent=made[old['parent_index']]
    finally:bpy.ops.object.mode_set(mode='OBJECT')
    arm['neox_source_cmesh']=str(source);arm['neox_spatial_scale']=scale;arm['neox_mirror_x']=mirror
    arm['nxse_retarget']=True;arm.show_in_front=True
    return arm

def frame(points):
    z=points['head']-points['hips'];x=points['l_upperarm']-points['r_upperarm']
    core.require(z.length>1e-6 and x.length>1e-6,'骨盆、头部或双臂位置重叠，无法自动对齐')
    z.normalize();x=x-z*x.dot(z);core.require(x.length>1e-6,'身体坐标轴退化，请检查对应骨骼')
    x.normalize();y=z.cross(x).normalized()
    return Matrix((x,y,z)).transposed()

def build(context,s,objects,source):
    arm,sk,pairs=validate_map(s,objects,source)
    core.require(arm.matrix_world.to_3x3().determinant()>0,'源骨架含镜像变换，请先在副本中正确应用镜像并检查左右骨骼')
    # Rest geometry is deliberate: no evaluated MMD physics, IK or posed mesh.
    for obj in objects:
        core.require(all(not m.show_viewport or m.type=='ARMATURE' for m in obj.modifiers),f'{obj.name} 有几何修改器，请先在源模型副本中应用')
        keys=obj.data.shape_keys
        core.require(not keys or keys.use_relative and all(abs(k.value)<1e-6 for k in list(keys.key_blocks)[1:]),f'{obj.name} 表情形态键未归零；请归零后适配')
        core.require(not keys or all((v.co-k.co).length<1e-6 for v,k in zip(obj.data.vertices,keys.key_blocks[0].data)),f'{obj.name} 网格与 Basis 不一致')
    target=create_arm(context,source,sk)
    sp={k:arm.matrix_world@arm.data.bones[a].head_local for k,(a,b) in pairs.items()}
    tp={k:target.data.bones[b].head_local.copy() for k,(a,b) in pairs.items()}
    # MMD lower-body heads often sit at the waist, almost coincident with the
    # upper-body head. Use the anatomical hip landmark rather than stretching
    # that tiny, oppositely directed control segment into a whole pelvis.
    sp['hips']=(sp['l_thigh']+sp['r_thigh'])*.5
    rot=frame(tp)@frame(sp).transposed()
    size=(tp['head']-tp['hips']).length/(sp['head']-sp['hips']).length
    core.require(math.isfinite(size) and size>0,'对齐比例异常')
    transforms={}
    for k,(a,b) in pairs.items():
        r=rot.copy();stretch=Matrix.Identity(3)
        nxt=NEXT.get(k)
        if nxt in pairs:
            sv=sp[nxt]-sp[k];tv=tp[nxt]-tp[k]
            if sv.length>1e-6 and tv.length>1e-6:
                r=(rot@sv).rotation_difference(tv).to_matrix()@rot
                # Longitudinal stretch fits different limb lengths; radial scale
                # stays global so wrists do not widen with a longer forearm.
                u=sv.normalized();ratio=tv.length/(size*sv.length)
                core.require(.2<ratio<5,f'{ROLES[k]} 长度差异过大，请核对骨骼映射')
                stretch=Matrix([[float(i==j)+(ratio-1)*u[i]*u[j] for j in range(3)] for i in range(3)])
        transforms[a]=(sp[k],tp[k],r@stretch*size)
    direct={a:(b,a,'mapped') for a,b in pairs.values()}
    # Reuse the primary rest transform for D bones. Their raw bone rolls are
    # unrelated to the game and must not be interpreted as target rest axes.
    for name,aliases in source_names(arm).items():
        roles={role(n) for n in aliases if n};roles.discard(None)
        if name not in direct and len(roles)==1:
            k=next(iter(roles))
            if k in pairs:direct[name]=(pairs[k][1],pairs[k][0],'alias')
    resolved={};fallback=Counter();unmapped=Counter();trimmed=0;total=0;copies=[]
    def resolve(name):
        if name in resolved:return resolved[name]
        b=arm.data.bones.get(name);original=name
        while b:
            if b.name in direct:
                dst,anchor,kind=direct[b.name]
                found=(dst,anchor,kind if name==b.name else 'ancestor')
                resolved[original]=found;return found
            if not s.retarget_fallback:break
            b=b.parent
        resolved[original]=None;return None
    for original in objects:
        obj=original.copy();obj.data=original.data.copy();obj.animation_data_clear();context.scene.collection.objects.link(obj);copies.append(obj)
        obj.parent=None;obj.matrix_world=Matrix.Identity(4)
        for mod in list(obj.modifiers):obj.modifiers.remove(mod)
        if obj.data.shape_keys:obj.shape_key_clear()
        for c in list(obj.constraints):obj.constraints.remove(c)
        for dst,src in zip(obj.material_slots,original.material_slots):dst.material=src.material
        coords=[];weights=[];used={v for p in original.data.polygons for v in p.vertices}
        for v in original.data.vertices:
            pos=original.matrix_world@v.co;acc=Vector();influences={};weight_sum=0
            for g in v.groups:
                if g.weight<=1e-8:continue
                name=original.vertex_groups[g.group].name
                if name not in arm.data.bones:continue
                result=resolve(name)
                if result is None:unmapped[name]+=1;continue
                dst,anchor,kind=result
                if kind=='ancestor':fallback[name]+=g.weight
                p,q,m=transforms[anchor];acc+=(q+m@(pos-p))*g.weight
                influences[dst]=influences.get(dst,0)+g.weight;weight_sum+=g.weight
            core.require(weight_sum>1e-8 or v.index not in used,f'{original.name} 顶点 {v.index} 没有可转换的权重，不能自动绑定根骨骼')
            if weight_sum<=1e-8:
                coords.append(tp['hips']+size*(rot@(pos-sp['hips'])));weights.append([]);continue
            total+=1;coords.append(acc/weight_sum)
            ordered=sorted(influences.items(),key=lambda x:-x[1]);trimmed+=len(ordered)>4
            ordered=ordered[:4];norm=sum(w for n,w in ordered);weights.append([(n,w/norm) for n,w in ordered])
        core.require(not unmapped,'有加权骨骼无法映射或回退：'+', '.join(list(unmapped)[:12])+'；请补全对应表或启用随父骨骼')
        obj.vertex_groups.clear();groups={n:obj.vertex_groups.new(name=n) for n in sorted({n for ws in weights for n,w in ws})}
        for v,co,ws in zip(obj.data.vertices,coords,weights):
            v.co=co
            for n,w in ws:groups[n].add([v.index],w,'REPLACE')
        uvflow.consolidate(obj,uvflow.plan(obj,sorted({p.material_index for p in obj.data.polygons})),'NXSE_ExportUV')
        obj.data.uv_layers['NXSE_ExportUV'].active_render=True
        # Recompute normals on the newly fitted surface, not old custom normals.
        if obj.data.has_custom_normals:obj.data.normals_split_custom_set([(0,0,0)]*len(obj.data.loops))
        obj.data.update()
    bpy.ops.object.select_all(action='DESELECT')
    for obj in copies:obj.hide_set(False);obj.select_set(True)
    context.view_layer.objects.active=copies[0]
    if len(copies)>1:core.require('FINISHED' in bpy.ops.object.join(),'合并动作副本失败')
    out=context.view_layer.objects.active;out.name='NeoX_MMD动作适配副本';out['nxse_retarget']=True
    mod=out.modifiers.new('NeoX 游戏原骨架','ARMATURE');mod.object=target;out.parent=target
    out['neox_uv_flip_u']=False;out['neox_uv_flip_v']=True
    out['nxse_retarget_roles']=json.dumps({k:b for k,(a,b) in pairs.items()})
    report={'experimental':True,'client_verified':False,'source_armature':arm.name,'target_sha256':core.sha(source),
        'pairs':{k:{'source':a,'target':b} for k,(a,b) in pairs.items()},'global_scale':size,
        'weighted_vertices':total,'trimmed_vertices':trimmed,'ancestor_fallback':dict(fallback),
        'fallback_weight_fraction':sum(fallback.values())/max(total,1),
        'unsupported':['PMX physics','VMD animation','morph animation'],'output':out.name}
    out['nxse_retarget_report']=json.dumps(report,ensure_ascii=False)
    core.dump(Path(bpy.path.abspath(s.project_dir))/'reports/retarget.json',report)
    return out,report

def preview(s,reset=False):
    obj=s.mesh;core.require(obj and obj.get('nxse_retarget'),'请先准备动作适配副本')
    arm=obj.find_armature();core.require(arm is not None and arm.get('nxse_retarget'),'适配骨架已删除')
    for b in arm.pose.bones:b.matrix_basis=Matrix.Identity(4)
    if not reset:
        roles=json.loads(obj['nxse_retarget_roles'])
        for k,angle in [('l_forearm',.65),('r_forearm',-.4),('l_calf',.35),('head',.15)]:
            b=arm.pose.bones[roles[k]];b.rotation_mode='XYZ';b.rotation_euler.x=angle
    bpy.context.view_layer.update()
