"""Run by a separate Blender process. No changes are saved to the user's working scene."""
import json,sys,subprocess,traceback
from pathlib import Path
import bpy,bmesh

def main(jobfile):
    jobfile=Path(jobfile);job=json.loads(jobfile.read_text(encoding='utf-8'));jobdir=jobfile.parent
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    from neox_skin_exporter import core,bridge
    status=jobdir/'status.json'
    def progress(stage):core.dump(status,{'state':'running','stage':stage})
    try:
        progress('读取网格和检查骨架')
        obj=bpy.data.objects.get(job['object']);core.require(obj is not None,'后台场景找不到选定网格')
        doc,parsed,meta=bridge.collect(obj,job['project'],**job['options'])
        bindings=job['bindings'];mapping={b['slot']:b for b in bindings};used={t['material'] for t in doc['triangles']}
        if doc['binding'] in {'STATIC','RETARGET'}:
            from neox_skin_exporter import uvflow
            uvflow.validate(obj,bindings,doc['uv_map'],job['options'].get('excluded_material_slots',[]))
        core.require(used<=mapping.keys(),'材质映射不完整')
        for slot in used:
            b=mapping[slot];core.require(obj.material_slots[slot].material.name==b['name'],'材质槽发生改变，请重新匹配')
            core.require(0<=b['source_sub']<len(meta['submeshes']),f"{b['name']} 没有来源分组")
        textures={};encoded_images={};texture_dir=jobdir/'textures';texture_dir.mkdir(exist_ok=True)
        for b in bindings:
            if b['slot'] not in used or b['mode']=='ORIGINAL':continue
            slot=b['slot'];progress(f"处理贴图：{b['name']}")
            if b['mode']=='IMAGE' and b.get('image') in encoded_images:
                textures[slot]=encoded_images[b['image']];continue
            png=texture_dir/f'color_{slot}.png'
            if b['mode']=='IMAGE':
                im=bpy.data.images.get(b.get('image',''));core.require(im is not None,'请选择颜色贴图：'+b['name'])
                # File-backed Image.copy() can have no pixel buffer in background Blender.
                # This process owns a disposable scene copy, so load and save its image directly.
                _=im.pixels[:1]
                core.require(im.has_data,'颜色图像无法加载，请检查文件路径：'+im.name)
                im.file_format='PNG';im.filepath_raw=str(png);im.save()
            else:
                # Work on a copied mesh containing only this material's faces.
                scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=16
                scene.render.bake.margin=12
                temp=obj.copy();temp.data=obj.data.copy();scene.collection.objects.link(temp);temp.modifiers.clear()
                # The selected export UV is the bake destination; other UV layers remain for shader inputs.
                temp.data.uv_layers.active_index=temp.data.uv_layers.find(doc['uv_map'])
                temp.data.uv_layers[doc['uv_map']].active_render=True
                bm=bmesh.new();bm.from_mesh(temp.data);bmesh.ops.delete(bm,geom=[f for f in bm.faces if f.material_index!=slot],context='FACES');bm.to_mesh(temp.data);bm.free()
                mat=obj.material_slots[slot].material.copy();temp.data.materials.clear();temp.data.materials.append(mat)
                for face in temp.data.polygons:face.material_index=0
                im=bpy.data.images.new(f'neox_bake_{slot}',width=job.get('texture_size',1024),height=job.get('texture_size',1024),alpha=False)
                node=mat.node_tree.nodes.new('ShaderNodeTexImage');node.image=im
                for n in mat.node_tree.nodes:n.select=False
                node.select=True;mat.node_tree.nodes.active=node
                bpy.ops.object.select_all(action='DESELECT');temp.select_set(True);bpy.context.view_layer.objects.active=temp
                bpy.ops.object.bake(type='DIFFUSE',pass_filter={'COLOR'},use_clear=True)
                im.file_format='PNG';im.filepath_raw=str(png);im.save()
                bpy.data.objects.remove(temp,do_unlink=True)
                doc['warnings'].append(f"{b['name']}：烘焙颜色近似；视角相关光泽、透明和节点着色不能等价还原。")
            astc=texture_dir/f"nx_{job['id'].replace('-','')[-12:]}_{slot}.astc"
            encoder=Path(job['encoder']);core.require(encoder.is_file(),'找不到 ASTC 编码器')
            result=subprocess.run([str(encoder),'-cl',str(png),str(astc),'6x6','-medium','-j','4','-silent'],capture_output=True,timeout=240)
            core.require(result.returncode==0,'贴图编码失败：'+result.stderr.decode(errors='replace')[-1500:])
            textures[slot]=astc
            if b['mode']=='IMAGE':encoded_images[b['image']]=astc
        progress('写入 CMesh/GIM/MTG 并回读验证')
        report=core.build(job['project'],doc,bindings,jobdir/'files',textures,job.get('variant'))
        if doc['binding']=='RETARGET':
            report['retarget']=json.loads(obj.get('nxse_retarget_report','{}'))
        if job.get('variant'):
            core.dump(jobdir/'registration_manifest.json',{'state':'resources_only_not_registered','source_stem':report['source_stem'],'new_stem':report['output_stem'],'resource_dir':report['resource_dir'],'files':report['files'],'texture_references':report['texture_references'],'required_client_work':['分配并登记皮肤编号与角色归属','配置局内/局外模型路径','配置动画、特效、图标与展示入口','确认该单机客户端的可用皮肤列表']})
        stage='新皮肤资源生成成功；尚未注册游戏入口' if job.get('variant') else '同名回写文件导出成功'
        core.dump(jobdir/'report.json',report);core.dump(status,{'state':'complete','stage':stage,'report':str(jobdir/'report.json'),'output':str(jobdir/'files')})
    except Exception as exc:
        core.dump(status,{'state':'failed','stage':'导出失败','error':str(exc),'traceback':traceback.format_exc()})
        traceback.print_exc();raise

if __name__=='__main__':main(sys.argv[sys.argv.index('--')+1])
