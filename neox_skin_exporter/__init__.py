bl_info = {
    'name':'NeoX 皮肤导出与回装', 'author':'NeoX skin export project',
    'version':(0,5,0), 'blender':(4,2,0), 'location':'3D 视图 > N > NeoX 导出',
    'description':'原皮肤模板、原生骨架网格重建、贴图转换、MuMu 安装与恢复（实验版）',
    'category':'Import-Export',
}

import json,os,subprocess,traceback,uuid,datetime,textwrap,re
from pathlib import Path
import bpy
from bpy.props import StringProperty,BoolProperty,IntProperty,FloatProperty,EnumProperty,PointerProperty,CollectionProperty
from bpy.types import Operator,Panel,PropertyGroup,UIList,AddonPreferences
from . import core,bridge

_JOBS={};_ENUM_CACHE={}
def settings(context):return context.scene.neox_skin_export
def abspath(value):return Path(bpy.path.abspath(value)).resolve()
def project_path(s):
    core.require(bool(s.project_dir.strip()),'请选择皮肤工程目录');return abspath(s.project_dir)
def source_items(self,context):
    if not context or not hasattr(context.scene,'neox_skin_export'):return [('-1','请选择原分组','')]
    s=settings(context);key=s.project_dir
    try:
        path=abspath(key)/'project.json';stamp=path.stat().st_mtime_ns
        if (key,stamp) not in _ENUM_CACHE:
            p=core.read(path);_ENUM_CACHE[(key,stamp)]=[('-1','请选择原分组','')]+[(str(i),f"{v['name']}（材质 {v['material_index']}）",'对应原始 GIM 子网格') for i,v in enumerate(p['submeshes'])]
        return _ENUM_CACHE[(key,stamp)]
    except Exception:return [('-1','请选择原分组','')]
def mesh_poll(self,obj):return obj.type=='MESH'

def workflow_changed(self,context):
    self.last_report='';self.last_job=''
    self.retarget_dirty=True
    self.workflow_summary='输入已改变，请重新生成替换包'
    self.workflow_issues.clear()

class NXSE_Material(PropertyGroup):
    slot: IntProperty()
    material: PointerProperty(type=bpy.types.Material)
    source: EnumProperty(name='原分组',items=source_items)
    mode: EnumProperty(name='材质处理',items=[('ORIGINAL','保留原游戏材质','Blender 节点效果不转换，沿用原 MTG'),('IMAGE','替换颜色贴图','使用选择的图像，保留模板其他参数'),('BAKE','烘焙颜色近似','后台 Cycles 烘焙颜色；光泽和视角效果不能完全还原')],default='ORIGINAL')
    image: PointerProperty(name='颜色图像',type=bpy.types.Image)
    alpha: EnumProperty(name='透明设置',items=[('KEEP','沿用模板','保持原透明模式'),('OPAQUE','不透明','使用不透明模式'),('ALPHA','透明模板模式','使用本案例验证过的透明模式 4')],default='KEEP')
    reason: StringProperty()
    double_sided: BoolProperty(name='双面显示',default=False)

class NXSE_Source(PropertyGroup):
    obj: PointerProperty(type=bpy.types.Object)

class NXSE_Issue(PropertyGroup):
    level: StringProperty()
    message: StringProperty()

def rig_changed(self,context):
    if context and hasattr(context.scene,'neox_skin_export'):
        s=settings(context);s.last_report='';s.last_job=''
        s.retarget_dirty=True

class NXSE_BoneMap(PropertyGroup):
    role: StringProperty()
    label: StringProperty()
    source: StringProperty(name='MMD 骨骼',update=rig_changed)
    target: StringProperty(name='游戏骨骼',update=rig_changed)

class NXSE_Settings(PropertyGroup):
    interface_mode: EnumProperty(name='界面',items=[('GUIDE','引导流程','选择源模型和目标资源后自动准备'),('ADVANCED','高级设置','显示全部原有参数')],default='GUIDE')
    workflow_sources: CollectionProperty(type=NXSE_Source)
    workflow_issues: CollectionProperty(type=NXSE_Issue)
    workflow_summary: StringProperty(default='选择模型和游戏目标，然后生成替换包')
    workflow_mode: EnumProperty(name='处理方式',items=[('AUTO','自动判断','同源原骨架保留动作，其他模型静态展示'),('RETARGET','MMD 动作适配（实验）','转换身体权重到游戏原骨架；需要先检查副本'),('NATIVE','保留原角色动作','严格验证同源骨架'),('STATIC','静态展示','外来模型固定外观，不适配肢体动画')],default='AUTO',update=workflow_changed)
    retarget_map: CollectionProperty(type=NXSE_BoneMap)
    retarget_source_arm: PointerProperty(type=bpy.types.Object)
    retarget_signature: StringProperty()
    retarget_dirty: BoolProperty(default=True)
    retarget_fallback: BoolProperty(name='附属骨骼随已映射父骨骼（无物理）',default=True,update=rig_changed)
    retarget_show_map: BoolProperty(name='展开骨骼对应表',default=False)
    retarget_profile: StringProperty(name='映射配置 JSON',subtype='FILE_PATH',default='C:\\WORK\\Chatgpt\\骨骼适配配置.json')
    workflow_surface: EnumProperty(name='替换位置',items=[('charselect','局外展示','char/charselect'),('hero','局内模型','char/hero；需要分别验证')],default='charselect',update=workflow_changed)
    workflow_root: StringProperty(name='工程保存位置',subtype='DIR_PATH',default='C:\\WORK\\Chatgpt\\SkinProjects',update=workflow_changed)
    workflow_more: BoolProperty(name='更多选项',default=False)
    source_cmesh: StringProperty(name='原皮肤 CMesh',subtype='FILE_PATH',update=workflow_changed)
    project_dir: StringProperty(name='工程目录',subtype='DIR_PATH')
    relative_dir: StringProperty(name='原皮肤资源目录',default='')
    output_mode: EnumProperty(name='输出用途',items=[('REPLACE','替换目标原皮肤','生成当前目标原皮肤的配套替换文件'),('NEW_RESOURCE','新皮肤资源（仅生成文件）','新文件名和新路径；不自动添加游戏皮肤入口')],default='REPLACE',update=workflow_changed)
    new_stem: StringProperty(name='新资源文件名（不含扩展名）',update=workflow_changed)
    new_relative_dir: StringProperty(name='新资源目录',update=workflow_changed)
    mesh: PointerProperty(name='导出网格',type=bpy.types.Object,poll=mesh_poll)
    binding: EnumProperty(name='绑定方式',items=[('NATIVE','保留原角色动作','要求原生骨架静止姿势、层级与源编号一致'),('RETARGET','MMD 动作适配（实验）','须先生成动作适配副本'),('STATIC','静态展示','将整个模型绑定到指定目标骨骼')],default='NATIVE')
    scale: FloatProperty(name='导入比例',default=.08,min=.00001)
    mirror: BoolProperty(name='导入时 X 镜像',default=True)
    flip_u: BoolProperty(name='导入时翻转 U',default=False)
    flip_v: BoolProperty(name='导入时翻转 V',default=True)
    root_bone: IntProperty(name='静态目标骨骼编号',default=0,min=0,max=253)
    shape_basis: BoolProperty(name='仅导出 Basis（形态键须归零）',default=False)
    uv_map: StringProperty(name='第 1 层 UV（留空用活动层）',description='附加层按原分组的 UV 层数写入，颜色烘焙使用第 1 层')
    uv_map_2: StringProperty(name='第 2 层 UV',default='UVMap_2')
    uv_map_3: StringProperty(name='第 3 层 UV',default='UVMap_3')
    uv_map_4: StringProperty(name='第 4 层 UV',default='UVMap_4')
    prepare_uv: StringProperty(name='副本 UV（留空逐物体用活动层）')
    materials: CollectionProperty(type=NXSE_Material)
    material_index: IntProperty(default=0)
    show_material_details: BoolProperty(name='展开手动材质设置',default=False)
    skip_duplicate_sphere: BoolProperty(name='跳过完全重叠的球面高光层',default=True,description='只跳过经面与位置证明完全重复的加法球面层；原模型保持不变')
    excluded_material_slots: StringProperty(default='[]')
    texture_size: EnumProperty(name='烘焙尺寸',items=[('512','512',''),('1024','1024',''),('2048','2048','')],default='1024')
    status: StringProperty(default='请选择原皮肤并建立工程')
    last_job: StringProperty(subtype='DIR_PATH')
    last_report: StringProperty(subtype='FILE_PATH')
    stop_game: BoolProperty(name='安装或恢复前关闭游戏',default=False)
    backup_dir: StringProperty(name='恢复备份目录（留空使用上次安装）',subtype='DIR_PATH')

class NXSE_Preferences(AddonPreferences):
    bl_idname=__package__
    mumu_dir: StringProperty(name='MuMu nx_main 目录',subtype='DIR_PATH',default='C:\\GAME\\MUMU\\MuMuPlayer\\nx_main')
    emulator_index: IntProperty(name='MuMu 实例编号',default=0,min=0)
    package: StringProperty(name='游戏包名',default='com.netease.fr26')
    encoder: StringProperty(name='自定义 ASTC 编码器（可留空）',subtype='FILE_PATH')
    def draw(self,context):
        for name in ('mumu_dir','emulator_index','package','encoder'):self.layout.prop(self,name)
        self.layout.label(text='Windows 包含 ASTC SSE2 编码器；其他平台请指定编码器。')

def prefs(context):
    addon=context.preferences.addons.get(__package__)
    return addon.preferences if addon else None
def options(s):return {'binding':s.binding,'scale':s.scale,'mirror':s.mirror,'flip_u':s.flip_u,'flip_v':s.flip_v,'root_bone':s.root_bone,'shape_basis':s.shape_basis,'uv_map':s.uv_map,'uv_map_2':s.uv_map_2,'uv_map_3':s.uv_map_3,'uv_map_4':s.uv_map_4,'excluded_material_slots':json.loads(s.excluded_material_slots) if s.binding in {'STATIC','RETARGET'} and s.skip_duplicate_sphere else []}
def infer_resource(source):
    path=Path(source);stem=path.stem;hero=stem.split('_')[0]
    skin=path.parent.name if re.fullmatch(r'\d{5}_\d+',path.parent.name) else '_'.join(stem.split('_')[:2])
    surface='hero' if 'hero' in path.parts else 'charselect'
    return f'char/{surface}/{hero}/fashions/{skin}' if re.fullmatch(r'\d{5}_\d+',skin) else f'char/{surface}/{hero}'
def output_variant(s):
    if s.output_mode=='REPLACE':return None
    core.require(s.new_stem.strip() and s.new_relative_dir.strip(),'请填写新资源文件名和新资源目录；插件不自动分配游戏皮肤编号')
    stem=core.safe_name(s.new_stem.strip());relative=core.resource_dir(s.new_relative_dir.strip())
    core.require('.' not in stem,'新资源文件名不含扩展名')
    p=core.load_project(project_path(s));core.require(stem!=p['stem'] and relative!=p['resource_dir'],'新资源应使用不同文件名和目录；同名替换请选择同角色同名回写')
    return {'stem':stem,'resource_dir':relative}
def bindings(s):
    result=[]
    for item in s.materials:
        core.require(item.material is not None,'映射的材质已被删除')
        core.require(item.slot<len(s.mesh.material_slots) and s.mesh.material_slots[item.slot].material==item.material,'材质槽已改变，请重新匹配')
        result.append({'slot':item.slot,'name':item.material.name,'source_sub':int(item.source),'mode':item.mode,'image':item.image.name if item.image else '', 'alpha':item.alpha,'double_sided':item.double_sided})
    return result
def fail(op,context,exc):
    settings(context).status=str(exc);op.report({'ERROR'},str(exc));print(traceback.format_exc());return {'CANCELLED'}

class NXSE_OT_pick(Operator):
    bl_idname='nxse.pick_active';bl_label='使用当前网格 / 填写原路径'
    def execute(self,context):
        try:
            s=settings(context);obj=context.object;core.require(obj and obj.type=='MESH','先选中角色网格')
            s.mesh=obj;s.uv_map='';s.materials.clear();s.excluded_material_slots='[]';arm=obj.find_armature()
            if arm and arm.get('neox_source_cmesh'):
                s.source_cmesh=bpy.path.abspath(arm['neox_source_cmesh']);s.relative_dir=infer_resource(s.source_cmesh)
            return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_prepare(Operator):
    bl_idname='nxse.prepare_static';bl_label='制作当前外观静态副本（选中网格）';bl_options={'REGISTER','UNDO'}
    bl_description='仅在新副本中合并选中网格、应用当前形态和修改器；此模式不保留肢体动作'
    def execute(self,context):
        try:
            core.require(not _JOBS,'请等待导出完成')
            from . import prepare
            from .mmd_setup import solid_viewports
            solid_viewports()
            s=settings(context);obj=prepare.static_copy(context,s.prepare_uv)
            s.mesh=obj;s.binding='STATIC';s.uv_map='NXSE_ExportUV';s.shape_basis=False;s.materials.clear();s.excluded_material_slots='[]'
            s.status='静态副本已创建，原物体保留；请匹配材质。当前表情和姿势已固定。';return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_create(Operator):
    bl_idname='nxse.create_project';bl_label='建立工程（备份原文件）'
    def execute(self,context):
        try:
            core.require(not _JOBS,'导出期间请勿切换工程')
            s=settings(context);core.require(s.source_cmesh,'请选择原始 CMesh')
            if not s.relative_dir:s.relative_dir=infer_resource(bpy.path.abspath(s.source_cmesh))
            core.create_project(abspath(s.source_cmesh),project_path(s),s.relative_dir);s.status='工程已建立；接着匹配材质'
            return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_open_project(Operator):
    bl_idname='nxse.open_project';bl_label='读取已有工程'
    def execute(self,context):
        try:
            core.require(not _JOBS,'导出期间请勿切换工程')
            s=settings(context);p=core.load_project(project_path(s));s.relative_dir=p['resource_dir'];s.source_cmesh=str(project_path(s)/'source'/(p['stem']+'.cmesh'))
            recent=project_path(s)/'last_export.json'
            s.last_job='';s.last_report=core.read(recent)['report'] if recent.exists() else ''
            s.status='已读取工程；请选择对应网格并匹配材质';return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_match(Operator):
    bl_idname='nxse.match_materials';bl_label='匹配材质来源'
    def execute(self,context):
        try:
            s=settings(context);doc,a,meta=bridge.collect(s.mesh,project_path(s),**options(s));suggestions=bridge.suggest(s.mesh,doc,a,meta)
            s.materials.clear()
            for entry in suggestions:
                row=s.materials.add();row.slot=entry['slot'];row.material=entry['material'];row.source=str(entry['source_sub']);row.mode=entry['mode'];row.image=entry['image'];row.reason=entry['why']
                if row.mode=='BAKE':row.alpha='OPAQUE'
            s.status=f'已匹配 {len(s.materials)} 个材质槽；请确认来源和处理方式';return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_mmd_auto(Operator):
    bl_idname='nxse.mmd_auto';bl_label='一键配置 MMD 贴图（安全模式）';bl_options={'REGISTER','UNDO'}
    bl_description='先切换实体显示，自动识别颜色图、简单材质模板与图片透明通道；不修改原材质节点'
    def execute(self,context):
        try:
            core.require(not _JOBS,'请等待导出完成')
            from . import mmd_setup
            s=settings(context);result=mmd_setup.configure(context,s)
            s.status=f"已配置 {result['materials']} 个材质 / {result['unique_images']} 张图；保持实体显示，接着导出。"+'；'.join(result['warnings'])
            return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_check(Operator):
    bl_idname='nxse.check';bl_label='检查模型'
    def execute(self,context):
        try:
            s=settings(context);core.require(s.mesh and s.mesh.name in context.scene.objects,'导出网格已不在当前场景中，请重新点击“使用当前网格”')
            if s.binding=='RETARGET':
                from . import retarget
                core.require(not s.retarget_dirty and s.mesh.get('nxse_retarget_input')==retarget.input_signature(s),'适配输入已改变或准备失败，请重新准备动作副本')
            output_variant(s)
            doc,a,meta=bridge.collect(s.mesh,project_path(s),**options(s));rules=bindings(s);used={t['material'] for t in doc['triangles']};by={b['slot']:b for b in rules}
            if s.binding in {'STATIC','RETARGET'}:
                from . import uvflow
                uvflow.validate(s.mesh,rules,s.uv_map,options(s)['excluded_material_slots'])
            core.require(used<=by.keys(),'请先匹配材质')
            for slot in used:
                b=by[slot];core.require(0<=b['source_sub']<len(meta['submeshes']),f"请为 {b['name']} 选择原分组")
                if b['mode']=='IMAGE':core.require(bool(b['image']),f"请为 {b['name']} 选择图像")
            result={'input_vertices':len(doc['vertices']),'triangles':len(doc['triangles']),'binding':s.binding,'warnings':doc['warnings'],'bindings':rules}
            core.dump(project_path(s)/'reports/input_check.json',result)
            s.status=f"输入检查通过：{len(doc['vertices'])} 顶点 / {len(doc['triangles'])} 三角面；"+'；'.join(doc['warnings']);return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

def refresh_scene(scene):
    s=scene.neox_skin_export
    if not s.last_job:return
    status=Path(s.last_job)/'status.json'
    if not status.exists():return
    data=core.read(status);s.status=data.get('stage','')+('：'+data['error'] if data.get('error') else '')
    if data['state']=='failed':
        s.last_report='';s.workflow_summary='导出未通过；没有生成可安装版本'
        if not any(row.level=='ERROR' and row.message==s.status for row in s.workflow_issues):
            row=s.workflow_issues.add();row.level='ERROR';row.message=s.status
    if data['state']=='complete':
        job=core.read(Path(s.last_job)/'job.json');folder=Path(job['project']).resolve()
        core.dump(folder/'last_export.json',{'report':data['report']})
        if folder==project_path(s):s.last_report=data['report'];s.workflow_summary='导出完成并通过本地回读；安装后请核对游戏显示'
        else:s.status='导出成功，但当前工程已切换；请读取原工程查看结果'
def poll_jobs():
    for key,value in list(_JOBS.items()):
        scene,proc,log=value
        try:refresh_scene(scene)
        except Exception:pass
        if proc.poll() is not None:
            log.close();_JOBS.pop(key,None)
            try:
                path=Path(scene.neox_skin_export.last_job)/'status.json'
                if not path.exists() or core.read(path).get('state')=='running':
                    core.dump(path,{'state':'failed','stage':'后台进程退出','error':f'退出码 {proc.returncode}，详见 worker.log'})
                refresh_scene(scene)
            except Exception:pass
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='VIEW_3D':area.tag_redraw()
    return 1.0 if _JOBS else None

class NXSE_OT_export(Operator):
    bl_idname='nxse.export';bl_label='导出游戏文件（后台）'
    def execute(self,context):
        try:
            core.require(not _JOBS,'已有导出任务正在运行')
            s=settings(context);core.require(s.mesh is not None,'请选择网格');core.load_project(project_path(s))
            check=bpy.ops.nxse.check();core.require('FINISHED' in check,'输入检查未通过')
            pref=prefs(context);encoder=abspath(pref.encoder) if pref and pref.encoder else Path(__file__).parent/'bin/astcenc-sse2.exe'
            core.require(encoder.is_file() or all(b.mode=='ORIGINAL' for b in s.materials),'找不到编码器，请在插件设置中指定')
            identifier=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:8];jobdir=project_path(s)/'exports'/identifier;jobdir.mkdir(parents=True)
            job={'id':identifier,'project':str(project_path(s)),'object':s.mesh.name,'options':options(s),'bindings':bindings(s),'texture_size':int(s.texture_size),'encoder':str(encoder.resolve()),'variant':output_variant(s)}
            core.dump(jobdir/'job.json',job);core.dump(jobdir/'status.json',{'state':'running','stage':'启动后台 Blender'})
            bpy.ops.wm.save_as_mainfile(filepath=str(jobdir/'source.blend'),copy=True)
            log=(jobdir/'worker.log').open('wb');cmd=[bpy.app.binary_path,'--background','--factory-startup',str(jobdir/'source.blend'),'--python',str(Path(__file__).parent/'worker.py'),'--',str(jobdir/'job.json')]
            try:proc=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except Exception:log.close();raise
            s.last_report='';s.last_job=str(jobdir);s.status='后台导出已启动';_JOBS[context.scene.as_pointer()]=(context.scene,proc,log)
            if not bpy.app.timers.is_registered(poll_jobs):bpy.app.timers.register(poll_jobs,first_interval=1.0)
            return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_refresh(Operator):
    bl_idname='nxse.refresh';bl_label='刷新状态'
    def execute(self,context):
        try:refresh_scene(context.scene);return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_cancel(Operator):
    bl_idname='nxse.cancel';bl_label='取消导出'
    def execute(self,context):
        value=_JOBS.get(context.scene.as_pointer())
        if value:
            scene,proc,log=value;proc.terminate();core.dump(Path(settings(context).last_job)/'status.json',{'state':'failed','stage':'用户取消导出','error':'未安装任何文件'})
        return {'FINISHED'}

class NXSE_OT_open_output(Operator):
    bl_idname='nxse.open_output';bl_label='打开导出文件夹'
    def execute(self,context):
        try:
            s=settings(context);core.require(s.last_report and Path(s.last_report).exists(),'尚无成功导出')
            path=core.read(s.last_report)['output_dir'];bpy.ops.wm.path_open(filepath=path);return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_device(Operator):
    bl_idname='nxse.device';bl_label='MuMu 操作'
    action: EnumProperty(items=[('INSTALL','安装',''),('BASELINE','备份当前版本为基线',''),('RESTORE','恢复上次安装','')])
    def execute(self,context):
        try:
            from . import deploy
            core.require(not _JOBS,'请等待导出完成后再操作模拟器')
            s=settings(context);pref=prefs(context);core.require(pref is not None,'请先正式安装并启用插件，然后设置 MuMu 路径')
            device=deploy.Device(bpy.path.abspath(pref.mumu_dir),pref.emulator_index,pref.package)
            if self.action=='RESTORE':
                path=deploy.restore(project_path(s),device,abspath(s.backup_dir) if s.backup_dir else None,s.stop_game)
                s.status='已恢复备份，请重新打开游戏：'+str(path)
            else:
                core.require(s.last_report and Path(s.last_report).is_file(),'请先完成一次成功导出')
                if self.action=='INSTALL':
                    path=deploy.install(project_path(s),device,s.last_report,s.stop_game);s.status='安装完成并校验；请重新打开游戏。备份：'+str(path)
                else:
                    path=deploy.baseline(project_path(s),device,s.last_report,s.stop_game);s.status='已备份设备当前版本；未替换资源：'+str(path)
            return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_UL_materials(UIList):
    def draw_item(self,context,layout,data,item,icon,active_data,active_propname,index):
        layout.label(text=f'{item.slot}: '+(item.material.name if item.material else '(材质已删除)'),icon='MATERIAL')
        layout.label(text={'ORIGINAL':'原材质','IMAGE':'换贴图','BAKE':'烘焙'}.get(item.mode,''))

class NXSE_OT_workflow_capture(Operator):
    bl_idname='nxse.workflow_capture';bl_label='使用当前选择的模型'
    def execute(self,context):
        try:
            core.require(not _JOBS,'请等待导出完成')
            from . import workflow
            workflow.capture(context,settings(context));return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_workflow(Operator):
    bl_idname='nxse.workflow';bl_label='生成替换包（自动准备）'
    export_after: BoolProperty(default=True)
    def execute(self,context):
        try:
            core.require(not _JOBS,'请等待导出完成')
            from . import workflow
            workflow.run(context,settings(context))
            return bpy.ops.nxse.export() if self.export_after else {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

class NXSE_OT_rig(Operator):
    bl_idname='nxse.rig';bl_label='MMD 动作适配'
    action: EnumProperty(items=[('ANALYZE','识别骨骼',''),('PREVIEW','测试关节',''),('RESET','恢复姿势',''),('SAVE','保存配置',''),('LOAD','读取配置',''),('EXPORT','导出当前适配副本','')])
    def execute(self,context):
        try:
            core.require(not _JOBS,'请等待导出完成')
            from . import retarget,workflow
            s=settings(context)
            if self.action in {'PREVIEW','RESET'}:
                retarget.preview(s,self.action=='RESET');s.status='关节测试仅供检查蒙皮，不是游戏动作；导出使用静止网格';return {'FINISHED'}
            if self.action=='EXPORT':
                core.require(not s.retarget_dirty,'对应表或选项已改变，请重新准备动作副本')
                core.require(s.binding=='RETARGET' and s.mesh and s.mesh.get('nxse_retarget'),'请先准备动作副本')
                core.require(s.mesh.get('nxse_retarget_input')==retarget.input_signature(s),'源模型或目标设置已改变，请重新准备动作副本')
                return bpy.ops.nxse.export()
            if not s.workflow_sources:workflow.capture(context,s)
            objects=[r.obj for r in s.workflow_sources];core.require(all(objects),'源模型已删除')
            source=abspath(s.source_cmesh);core.require(source.is_file(),'请选择原始 CMesh')
            if self.action=='ANALYZE':retarget.analyze(s,objects,source)
            elif self.action=='SAVE':
                retarget.validate_map(s,objects,source)
                core.dump(abspath(s.retarget_profile),{'schema':1,'signature':s.retarget_signature,'rows':[{'role':r.role,'source':r.source,'target':r.target} for r in s.retarget_map]})
                s.status='骨骼对应配置已保存'
            elif self.action=='LOAD':
                data=core.read(abspath(s.retarget_profile));arm=retarget.source_arm(objects)
                core.require(data.get('schema')==1 and data.get('signature')==retarget.signature(arm,source),'配置不属于此源骨架和目标原文件，请重新识别')
                retarget.analyze(s,objects,source);rows={r.role:r for r in s.retarget_map}
                for entry in data['rows']:
                    core.require(entry['role'] in rows,'配置包含未知部位');r=rows[entry['role']];r.source=entry['source'];r.target=entry['target']
                s.status='配置已读取，请重新准备动作副本'
            return {'FINISHED'}
        except Exception as exc:return fail(self,context,exc)

def draw_guide(layout,context,s):
    box=layout.box();box.enabled=not _JOBS;box.label(text='1. 选择要使用的模型')
    box.operator('nxse.workflow_capture',icon='RESTRICT_SELECT_OFF')
    if s.workflow_sources:
        box.label(text=f'已选择 {len(s.workflow_sources)} 个网格')
        for row in list(s.workflow_sources)[:4]:box.label(text=row.obj.name if row.obj else '源物体已删除',icon='MESH_DATA')
    else:box.label(text='可选多个网格，也可选模型根物体 / 骨架')
    box=layout.box();box.enabled=not _JOBS;box.label(text='2. 选择要替换的游戏目标')
    box.prop(s,'source_cmesh',text='目标原始 CMesh');box.prop(s,'workflow_surface')
    box.label(text='同目录需要同名 GIM、MTG；请用原版资源')
    box.prop(s,'workflow_mode')
    if s.workflow_mode=='RETARGET':
        rig=box.box();rig.label(text='实验功能：先准备、检查关节，再导出')
        rig.operator('nxse.rig',text='识别骨骼对应').action='ANALYZE'
        rig.prop(s,'retarget_fallback');rig.prop(s,'retarget_show_map')
        if s.retarget_show_map:
            from .retarget import REQUIRED
            for mapping in s.retarget_map:
                row=rig.column(align=True);row.label(text=mapping.label+(' *' if mapping.role in REQUIRED else ''))
                if s.retarget_source_arm:row.prop_search(mapping,'source',s.retarget_source_arm.data,'bones')
                else:row.prop(mapping,'source')
                row.prop(mapping,'target')
            rig.prop(s,'retarget_profile');row=rig.row();row.operator('nxse.rig',text='保存配置').action='SAVE';row.operator('nxse.rig',text='读取配置').action='LOAD'
    box.prop(s,'workflow_more')
    if s.workflow_more:
        box.prop(s,'workflow_root');box.prop(s,'skip_duplicate_sphere');box.prop(s,'output_mode')
    if s.output_mode=='NEW_RESOURCE':
        box.prop(s,'new_stem');box.prop(s,'new_relative_dir');box.label(text='仅生成资源，游戏皮肤入口需另行配置')
    box.label(text='自动判断使用旧流程；动作适配需手动选择')
    box=layout.box();box.label(text='3. 生成与安装')
    if s.workflow_mode=='RETARGET':
        row=box.row();row.enabled=not _JOBS;row.operator('nxse.workflow',text='准备动作副本',icon='ARMATURE_DATA').export_after=False
        row=box.row();row.operator('nxse.rig',text='测试关节').action='PREVIEW';row.operator('nxse.rig',text='恢复姿势').action='RESET'
        row=box.row();row.enabled=not _JOBS;row.operator('nxse.rig',text='导出当前适配副本',icon='EXPORT').action='EXPORT'
        box.label(text='可在副本中微调网格 / 权重；重新准备会重建')
    else:
        row=box.row();row.enabled=not _JOBS;row.operator('nxse.workflow',icon='EXPORT')
        row=box.row();row.enabled=not _JOBS;row.operator('nxse.workflow',text='只诊断并准备',icon='CHECKMARK').export_after=False
    for line in textwrap.wrap(s.workflow_summary,width=27):box.label(text=line)
    for row in sorted(s.workflow_issues,key=lambda r:{'ERROR':0,'WARN':1,'INFO':2}.get(r.level,3))[:12]:
        for i,line in enumerate(textwrap.wrap(row.message,width=27)):
            box.label(text=line,icon=('ERROR' if row.level=='ERROR' else 'INFO') if i==0 else 'NONE')
    if len(s.workflow_issues)>12:box.label(text='完整诊断见工程 workflow.json')
    for line in textwrap.wrap(s.status,width=27):box.label(text=line)
    if _JOBS:
        row=box.row();row.operator('nxse.refresh');row.operator('nxse.cancel')
    if s.last_report:
        box.operator('nxse.open_output')
        if s.output_mode=='REPLACE':
            box.prop(s,'stop_game')
            row=box.row();row.enabled=not _JOBS;row.operator('nxse.device',text='备份并安装到 MuMu').action='INSTALL'
            box.label(text='安装后重新进入目标页面检查外观与动作')
        else:box.label(text='新资源已生成；需要另行注册游戏入口')
    if s.project_dir:
        box=layout.box();box.label(text='安装恢复 / 版本管理')
        box.operator('nxse.device',text='恢复安装前版本').action='RESTORE'
        if s.workflow_more:
            box.operator('nxse.device',text='备份当前版本为基线').action='BASELINE';box.prop(s,'backup_dir')

class NXSE_PT_panel(Panel):
    bl_label='NeoX 模型替换 0.5';bl_idname='NXSE_PT_panel';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='NeoX 导出'
    def draw(self,context):
        l=self.layout;s=settings(context)
        l.prop(s,'interface_mode',expand=True)
        if s.interface_mode=='GUIDE':draw_guide(l,context,s);return
        l.label(text='实验版：v4 / flag 4、5 / 单分段')
        box=l.box();box.enabled=not _JOBS;box.label(text='1. 原皮肤工程');box.operator('nxse.pick_active');box.prop(s,'mesh');box.prop(s,'source_cmesh');box.prop(s,'project_dir');box.prop(s,'relative_dir')
        row=box.row();row.operator('nxse.create_project');row.operator('nxse.open_project')
        box.prop(s,'output_mode')
        if s.output_mode=='NEW_RESOURCE':
            box.prop(s,'new_stem');box.prop(s,'new_relative_dir');box.label(text='仅生成资源，游戏皮肤入口需另行配置')
        box=l.box();box.label(text='2. 网格与材质');box.prop(s,'binding');box.prop(s,'shape_basis')
        if s.mesh:
            box.prop_search(s,'uv_map',s.mesh.data,'uv_layers')
            try:
                project=core.read(project_path(s)/'project.json');layers=max(v['uv_layers'] for v in project['submeshes'])
            except Exception:layers=min(4,len(s.mesh.data.uv_layers))
            for i in range(2,layers+1):box.prop_search(s,f'uv_map_{i}',s.mesh.data,'uv_layers')
        box.prop(s,'prepare_uv');box.operator('nxse.prepare_static')
        if s.binding=='STATIC':
            box.prop(s,'skip_duplicate_sphere')
            box.operator('nxse.mmd_auto',icon='SHADING_SOLID')
            box.label(text='MMD：先点一键配置，再导出；保持实体显示')
            box.prop(s,'root_bone');box.prop(s,'scale');box.prop(s,'mirror');row=box.row();row.prop(s,'flip_u');row.prop(s,'flip_v')
        if s.binding=='STATIC':
            box.label(text=f'已配置 {len(s.materials)} 个材质；一键配置后可直接导出')
            box.prop(s,'show_material_details')
        if s.binding!='STATIC' or s.show_material_details:
            box.operator('nxse.match_materials');box.template_list('NXSE_UL_materials','',s,'materials',s,'material_index',rows=4)
            if s.materials and s.material_index<len(s.materials):
                item=s.materials[s.material_index];box.prop(item,'source');box.prop(item,'mode')
                if item.mode=='IMAGE':box.prop(item,'image')
                if item.mode!='ORIGINAL':box.prop(item,'alpha');box.prop(item,'double_sided')
                box.label(text=item.reason)
        box.prop(s,'texture_size');box.operator('nxse.check');box.operator('nxse.export')
        row=box.row();row.operator('nxse.refresh');row.operator('nxse.cancel');box.operator('nxse.open_output')
        box=l.box();box.label(text='3. 安装与恢复（解包客户端）');box.prop(s,'stop_game')
        box.operator('nxse.device',text='安装到 MuMu').action='INSTALL';box.operator('nxse.device',text='备份当前版本为基线').action='BASELINE'
        box.prop(s,'backup_dir');box.operator('nxse.device',text='恢复安装前版本').action='RESTORE'
        for line in textwrap.wrap(s.status,width=32,break_long_words=True):l.label(text=line)

CLASSES=(NXSE_Material,NXSE_Source,NXSE_Issue,NXSE_BoneMap,NXSE_Settings,NXSE_Preferences,NXSE_OT_pick,NXSE_OT_prepare,NXSE_OT_create,NXSE_OT_open_project,NXSE_OT_match,NXSE_OT_mmd_auto,NXSE_OT_check,NXSE_OT_export,NXSE_OT_refresh,NXSE_OT_cancel,NXSE_OT_open_output,NXSE_OT_device,NXSE_UL_materials,NXSE_OT_workflow_capture,NXSE_OT_workflow,NXSE_OT_rig,NXSE_PT_panel)
def register():
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.neox_skin_export=PointerProperty(type=NXSE_Settings)
def unregister():
    if bpy.app.timers.is_registered(poll_jobs):bpy.app.timers.unregister(poll_jobs)
    if hasattr(bpy.types.Scene,'neox_skin_export'):del bpy.types.Scene.neox_skin_export
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
