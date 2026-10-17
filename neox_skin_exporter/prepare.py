"""Create a disposable static export object from selected evaluated meshes."""
import bpy
from . import core

def static_copy(context,uv_name='',resolve_color_uv=False):
    core.require(context.mode=='OBJECT','请先退出编辑或姿态模式')
    originals=[o for o in context.selected_objects if o.type=='MESH']
    core.require(originals,'请选择至少一个模型网格，可同时选择身体、头发和衣服')
    old_active=context.view_layer.objects.active;old_selection=list(context.selected_objects)
    copies=[];meshes=[];deps=context.evaluated_depsgraph_get()
    try:
        for original in originals:
            evaluated=original.evaluated_get(deps)
            mesh=bpy.data.meshes.new_from_object(evaluated,preserve_all_data_layers=True,depsgraph=deps);meshes.append(mesh)
            core.require(len(mesh.polygons)>0,f'{original.name} 没有可导出的面')
            source_uv=mesh.uv_layers.get(uv_name) if uv_name else next((uv for uv in mesh.uv_layers if uv.active_render),mesh.uv_layers.active)
            core.require(source_uv is not None,f'{original.name} 缺少选定 UV；留空可逐物体采用各自的活动 UV')
            coords=[tuple(item.uv) for item in source_uv.data]
            target=mesh.uv_layers.get('NXSE_ExportUV')
            if target is None:
                core.require(len(mesh.uv_layers)<8,f'{original.name} 的 UV 层已满；请先在副本中删除一个不用的 UV 层')
                target=mesh.uv_layers.new(name='NXSE_ExportUV')
            for item,coord in zip(target.data,coords):item.uv=coord
            mesh.uv_layers.active_index=mesh.uv_layers.find(target.name)
            obj=bpy.data.objects.new(original.name+'_静态副本',mesh);context.scene.collection.objects.link(obj)
            obj.matrix_world=evaluated.matrix_world.copy();copies.append(obj)
            # Evaluated mesh material indices refer to the object's effective slots, including object-linked materials.
            for index,slot in enumerate(evaluated.material_slots):
                if index<len(mesh.materials):
                    # Evaluated IDs can be temporary and disappear after reopening a .blend.
                    material=slot.material
                    stable=bpy.data.materials.get(material.name) if material else None
                    core.require(material is None or stable is not None,'求值材质无法持久保存，请先在原网格确认材质来源')
                    mesh.materials[index]=stable
            if resolve_color_uv:
                from . import uvflow
                sources=uvflow.plan(obj,sorted({p.material_index for p in mesh.polygons}))
                uvflow.consolidate(obj,sources,'NXSE_ExportUV')
                # Preserve per-object implicit UV semantics across a later multi-object join.
                mesh.uv_layers['NXSE_ExportUV'].active_render=True
        core.require(len({uv.name for obj in copies for uv in obj.data.uv_layers})<=8,'合并后的 UV 名称超过八层；请先在副本中整理多余 UV')
        bpy.ops.object.select_all(action='DESELECT')
        for obj in copies:obj.select_set(True)
        context.view_layer.objects.active=copies[0]
        if len(copies)>1:
            result=bpy.ops.object.join();core.require('FINISHED' in result,'合并导出副本失败')
        output=context.view_layer.objects.active;output.name='NeoX_静态导出副本'
        output.data.uv_layers.active_index=output.data.uv_layers.find('NXSE_ExportUV')
        output['nxse_prepared_from']=', '.join(o.name for o in originals)
        output['nxse_prepared_frame']=context.scene.frame_current
        for mesh in meshes:
            if mesh.users==0:bpy.data.meshes.remove(mesh)
        return output
    except Exception:
        for obj in copies:
            try:
                if bpy.data.objects.get(obj.name)==obj:bpy.data.objects.remove(obj,do_unlink=True)
            except ReferenceError:pass
        for mesh in meshes:
            try:
                if mesh.users==0:bpy.data.meshes.remove(mesh)
            except ReferenceError:pass
        bpy.ops.object.select_all(action='DESELECT')
        for obj in old_selection:obj.select_set(True)
        context.view_layer.objects.active=old_active
        raise
