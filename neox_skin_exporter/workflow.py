"""Model-independent guided preparation; originals and game installation are separate."""
import re,uuid,datetime,json
from pathlib import Path
import bpy
from . import core,prepare,mmd_setup,uvflow,bridge


def issue(s,level,message):
    row=s.workflow_issues.add();row.level=level;row.message=message


def capture(context,s):
    found={}
    for selected in context.selected_objects:
        candidates=[selected] if selected.type=='MESH' else list(selected.children_recursive)
        for obj in candidates:
            if obj.type=='MESH' and len(obj.data.polygons) and obj.rigid_body is None and obj.rigid_body_constraint is None:
                if selected.type=='MESH' or (not obj.hide_render and not obj.hide_get()):
                    found[obj.name]=obj
    core.require(found,'请选择角色网格，或选择包含网格的骨架 / 模型根物体；物理碰撞体不参与导出')
    s.workflow_sources.clear()
    s.retarget_dirty=True
    s.last_report='';s.last_job=''
    for obj in found.values():s.workflow_sources.add().obj=obj
    s.workflow_summary=f'已选择 {len(found)} 个网格：'+', '.join(found)
    s.workflow_issues.clear()
    return list(found.values())


def target_directory(source,surface):
    stem=source.stem
    hero=stem.split('_')[0]
    core.require(re.fullmatch(r'\d{5}',hero),'无法从原 CMesh 识别五位角色编号；请使用高级模式设置资源目录')
    parent=source.parent.name
    skin=parent if re.fullmatch(r'\d{5}_\d+',parent) else '_'.join(stem.split('_')[:2])
    return f'char/{surface}/{hero}/fashions/{skin}' if re.fullmatch(r'\d{5}_\d+',skin) else f'char/{surface}/{hero}'


def choose_mode(objects,source,requested):
    if requested=='RETARGET':return 'RETARGET'
    target_hash=core.sha(source)
    def matches(obj):
        arm=obj.find_armature()
        if arm is None:return False
        p=Path(bpy.path.abspath(arm.get('neox_source_cmesh','')))
        return p.is_file() and core.sha(p)==target_hash
    same=all(matches(obj) for obj in objects)
    if requested=='NATIVE':
        core.require(same,'所选模型不是该目标的原生骨架；不能直接保留动作。请选择静态展示，或先完成骨架适配')
    return 'NATIVE' if requested=='NATIVE' or requested=='AUTO' and same else 'STATIC'


def native_copy(context,objects):
    arms={o.find_armature() for o in objects}
    core.require(len(arms)==1 and None not in arms,'多个原生网格必须使用同一个骨架')
    copies=[]
    for obj in objects:
        core.require(not obj.data.shape_keys or len(obj.data.shape_keys.key_blocks)<=1,
                     f'{obj.name} 有形态键；请在高级模式明确导出 Basis 或应用形态，不能自动丢弃')
        core.require(all(not m.show_viewport or m.type=='ARMATURE' for m in obj.modifiers),
                     f'{obj.name} 有未应用的几何修改器；请在工作副本中应用后再试')
    for obj in objects:
        copy=obj.copy();copy.data=obj.data.copy();context.scene.collection.objects.link(copy)
        for dst,src in zip(copy.material_slots,obj.material_slots):dst.material=src.material
        copies.append(copy)
    bpy.ops.object.select_all(action='DESELECT')
    for obj in copies:obj.select_set(True)
    context.view_layer.objects.active=copies[0]
    if len(copies)>1:core.require('FINISHED' in bpy.ops.object.join(),'合并原生工作副本失败')
    out=context.view_layer.objects.active;out.name='NeoX_原生导出副本'
    return out


def run(context,s):
    # Invalidate old output first: failure must never leave an installable stale package in the guide.
    s.last_report='';s.last_job='';s.workflow_issues.clear();s.workflow_summary='正在自动诊断'
    s.retarget_dirty=True
    stage='检查输入';before_objects=set(bpy.data.objects);selection=list(context.selected_objects);active=context.view_layer.objects.active
    folder=None
    try:
        core.require(context.mode=='OBJECT','请先退出编辑 / 姿态模式')
        if not s.workflow_sources:capture(context,s)
        objects=[r.obj for r in s.workflow_sources]
        core.require(objects and all(o and o.name in context.scene.objects for o in objects),'源模型已删除或不在当前场景，请重新选择模型')
        source=Path(bpy.path.abspath(s.source_cmesh)).resolve()
        core.require(source.is_file() and source.suffix.lower()=='.cmesh','请选择目标角色未修改的原始 .cmesh 文件')
        mode=choose_mode(objects,source,s.workflow_mode)
        relative=target_directory(source,s.workflow_surface)
        stage='建立独立工程'
        root=Path(bpy.path.abspath(s.workflow_root)).resolve();core.require(s.workflow_root.strip(),'请选择工程保存目录')
        folder=root/(s.workflow_surface+'_'+source.stem+'_'+core.sha(source)[:12])
        # Reuse target history so repeated exports retain deployment/rollback provenance.
        if s.project_dir and (Path(bpy.path.abspath(s.project_dir))/'project.json').is_file():
            prior=Path(bpy.path.abspath(s.project_dir))
            old=core.load_project(prior)
            if old['resource_dir']==relative and old['source_files'].get(source.name)==core.sha(source):folder=prior
        if (folder/'project.json').exists():
            p=core.load_project(folder)
            core.require(p['resource_dir']==relative and p['source_files'].get(source.name)==core.sha(source),'工程与目标资源不一致')
        else:p=core.create_project(source,folder,relative)
        s.project_dir=str(folder);s.relative_dir=relative;s.binding=mode;s.excluded_material_slots='[]';s.shape_basis=False
        issue(s,'INFO',f'目标：{relative} / {source.name}')
        issue(s,'INFO',{'NATIVE':'原角色改模：校验原骨架并保留权重','STATIC':'外来模型：静态展示，尚未适配肢体动画','RETARGET':'MMD 动作适配：原游戏骨架 + 新蒙皮；实验功能，需要检查变形'}[mode])
        if mode in {'STATIC','RETARGET'}:
            stage='诊断颜色材质与 UV'
            mmd_setup.simple_template(folder)
            # Collect every unsupported material before mutation, not just the first failure.
            errors=[]
            for obj in objects:
                for index in sorted({p.material_index for p in obj.data.polygons}):
                    try:
                        core.require(index<len(obj.material_slots),'面引用不存在的材质槽')
                        mat=obj.material_slots[index].material
                        mmd_setup.base_image(mat)
                        uvflow.plan(obj,[index])
                    except Exception as exc:
                        errors.append(f'{obj.name} / 材质 {index}：{exc}')
            for error in errors:issue(s,'ERROR',error)
            core.require(not errors,f'{len(errors)} 个材质问题需要处理，详见问题列表')
        stage='制作导出副本'
        mmd_setup.solid_viewports()
        bpy.ops.object.select_all(action='DESELECT')
        for obj in objects:obj.select_set(True)
        context.view_layer.objects.active=objects[0]
        if mode=='RETARGET':
            from . import retarget
            out,rig_report=retarget.build(context,s,objects,source)
            issue(s,'WARN',f"{len(rig_report['pairs'])} 项骨骼映射；附属骨骼回退权重占比 {rig_report['fallback_weight_fraction']:.1%}，详见 reports/retarget.json")
            issue(s,'WARN','只适配基础蒙皮；头发裙摆回退部分不带物理，表情/VMD 不写入。先检查副本再导出')
        else:out=native_copy(context,objects) if mode=='NATIVE' else prepare.static_copy(context,resolve_color_uv=True)
        s.mesh=out;s.uv_map='NXSE_ExportUV' if mode!='NATIVE' else next((uv.name for uv in out.data.uv_layers if uv.active_render),'')
        for i in range(2,5):setattr(s,f'uv_map_{i}',f'UVMap_{i}')
        stage='自动配置材质'
        if mode in {'STATIC','RETARGET'}:
            setup=mmd_setup.configure(context,s)
            for warning in setup['warnings']:issue(s,'WARN',warning)
            for skipped in setup['skipped_duplicate_sphere_layers']:issue(s,'WARN',f"导出时跳过完全重叠的高光层：{skipped['name']}")
            issue(s,'INFO',f"已配置 {setup['materials']} 个材质 / {setup['unique_images']} 张图及颜色 UV")
        else:
            core.require('FINISHED' in bpy.ops.nxse.match_materials(),s.status)
            missing=[r.material.name for r in s.materials if r.source=='-1']
            core.require(not missing,'无法确定原材质分组：'+', '.join(missing)+'；请在高级设置中指定')
            if any(r.mode=='ORIGINAL' for r in s.materials):issue(s,'WARN','原生改模沿用对应游戏材质；Blender 节点改色需在高级模式确认贴图后导出')
        stage='检查可导出性'
        if mode=='RETARGET':
            out['nxse_retarget_input']=retarget.input_signature(s);s.retarget_dirty=False
        core.require('FINISHED' in bpy.ops.nxse.check(),s.status)
        check=core.read(folder/'reports/input_check.json')
        for warning in check.get('warnings',[]):issue(s,'WARN',warning)
        s.workflow_summary=f"准备通过：{len(objects)} 个源网格 → {len(s.materials)} 个材质；"+{'NATIVE':'保留原角色动作','STATIC':'静态展示','RETARGET':'实验性动作适配，先检查关节变形'}[mode]
        s.status=s.workflow_summary
        core.dump(folder/'workflow.json',{'state':'prepared','mode':mode,'source_blend':bpy.data.filepath,
            'sources':[o.name for o in objects],'target':str(source),'resource_dir':relative,
            'output_object':out.name,'issues':[{'level':r.level,'message':r.message} for r in s.workflow_issues]})
        return out
    except Exception as exc:
        s.retarget_dirty=True
        issue(s,'ERROR',f'{stage}：{exc}');s.workflow_summary='未通过，请处理下方问题；未安装游戏文件'
        # Remove only new temporary objects created by this invocation; original/user objects are untouched.
        for obj in list(bpy.data.objects):
            if obj not in before_objects:
                kind=obj.type;data=obj.data if kind in {'MESH','ARMATURE'} else None
                bpy.data.objects.remove(obj,do_unlink=True)
                if data is not None and data.users==0:
                    (bpy.data.meshes if kind=='MESH' else bpy.data.armatures).remove(data)
        bpy.ops.object.select_all(action='DESELECT')
        for obj in selection:
            if obj.name in context.scene.objects:obj.select_set(True)
        context.view_layer.objects.active=active
        if folder and folder.exists():core.dump(folder/'workflow.json',{'state':'failed','stage':stage,'error':str(exc)})
        raise
