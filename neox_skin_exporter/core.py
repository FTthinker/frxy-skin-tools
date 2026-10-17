"""Project snapshots and the validated v4/flag4 single-split writer (no bpy)."""
import copy, hashlib, json, math, re, shutil, struct, tempfile
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
from . import decoder, metadata

VERSION='0.5.0'
def require(condition,message):
    if not condition: raise ValueError(message)
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def dump(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def resource_dir(value):
    value=value.replace('\\','/').strip('/')
    require(re.fullmatch(r'char/(charselect|hero)/[A-Za-z0-9_/-]+',value) is not None and '..' not in value,'资源目录必须是 char/charselect/... 或 char/hero/...，不能包含 ..')
    return value
def safe_name(name):
    require(re.fullmatch(r'[A-Za-z0-9_.-]+',name) is not None and '..' not in name,'不安全的输出文件名')
    return name
def inspect(mesh,gim,cache):
    mesh=Path(mesh);structure=metadata.parse_gim(Path(gim))
    raw=mesh.read_bytes();version=struct.unpack_from('<H',raw,4)[0]
    require(version==4,f'当前写入器只支持 v4，模板为 v{version}；可将模型作为静态几何替换到受支持的目标皮肤')
    sk=decoder.parse_embedded_bone_bounds(raw)
    require(sk['split_count']==1,f"模板有 {sk['split_count']} 个分段；多分段写入尚未验证，请选择单分段目标模板")
    header,start,records,nv,nf,flag=metadata.find_header(raw,len(structure['submeshes']))
    require(flag in (4,5),f'当前支持 flag 4/5，模板为 flag {flag}；不能直接回写此模板')
    for item,record in zip(structure['submeshes'],records):item.update(zip(['vertices','triangles','uv_layers','color_layers'],record))
    structure.update(header_offset=header,data_start=start,vertex_count=nv,triangle_count=nf,flag=flag)
    dump(cache,{'display':structure});parsed=decoder.parse_cmesh(mesh,Path(cache))
    require(parsed['report']['version']==4 and flag in (4,5),'支持 CMesh v4 / flag 4 或 5')
    require(parsed['embedded_skeleton']['split_count']==1,'首版只支持单分段 CMesh')
    require(0<parsed['embedded_skeleton']['bone_count']<255,'首版要求 1～254 根原生骨骼')
    require(all((1<=v[2]<=4 or v[0]==0 and v[2]==0) and v[3] in (0,1) for v in records),'支持每组 1～4 层 UV、最多一层源顶点颜色')
    require(parsed['report']['remaining_before_trailer']==32,'模板尾部长度与已验证结构不符')
    require(not parsed['report']['invalid_faces'] and not parsed['report']['submesh_out_of_range_faces'],'模板包含无效或跨分组的面索引')
    return parsed,structure
def create_project(source,folder,relative):
    source=Path(source).resolve();folder=Path(folder).resolve();require(source.is_file(),'找不到原始 CMesh')
    require(not (folder/'project.json').exists(),'此目录已有工程，请打开已有工程或换一个目录')
    require(not folder.exists() or not any(folder.iterdir()),'新工程请选择空目录，避免覆盖已有数据')
    safe_name(source.stem);relative=resource_dir(relative)
    for ext in ('.cmesh','.gim','.mtg'):require(source.with_suffix(ext).is_file(),f'缺少同名 {ext}')
    # Validate before creating the immutable source snapshot.
    folder.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.inspect_',dir=folder) as tmp:
        parsed,structure=inspect(source,source.with_suffix('.gim'),Path(tmp)/'structure.json')
        ET.parse(source.with_suffix('.mtg'))
    dump(folder/'reports/template_structure.json',{'display':structure})
    (folder/'source').mkdir(exist_ok=True);files={}
    for ext in ('.cmesh','.gim','.mtg'):
        src=source.with_suffix(ext);dst=folder/'source'/src.name;shutil.copy2(src,dst);files[dst.name]=sha(dst)
    project={'schema':1,'addon_version':VERSION,'stem':source.stem,'resource_dir':relative,'source_files':files,'source_origin':str(source),'format':f"v4_flag{structure['flag']}_single",'submeshes':structure['submeshes']}
    dump(folder/'project.json',project)
    return project
def load_project(folder):
    folder=Path(folder).resolve();p=read(folder/'project.json');require(p.get('schema')==1,'不支持此工程版本');safe_name(p['stem']);resource_dir(p['resource_dir'])
    for n,h in p['source_files'].items():require(sha(folder/'source'/safe_name(n))==h,'原始模板快照已变化：'+n)
    return p
def template(folder,cache=None):
    folder=Path(folder);p=load_project(folder);src=folder/'source'/(p['stem']+'.cmesh')
    a,s=inspect(src,src.with_suffix('.gim'),cache or folder/'reports/template_structure.json')
    return p,src,a,s
def bbox(points):
    if not len(points):return [0.0]*7
    p=np.array(points);lo=p.min(axis=0);hi=p.max(axis=0);c=(lo+hi)/2;h=(hi-lo)/2
    return [*map(float,c),*map(float,h),float(np.linalg.norm(h))]
def vec(v):return ','.join(f'{float(x):.9g}' for x in v)
def pkey(p):return tuple(round(x*2048) for x in p)
def encode_pos(p):
    result=0
    for shift,value in zip((0,20,40),p):
        require(math.isfinite(value) and abs(value)<128,'顶点超出已验证坐标范围 (-128,128)')
        a=abs(value);integer=int(a);fraction=round((a-integer)*2048)
        result|=((int(value<0)<<19)|(integer<<12)|fraction)<<shift
    return struct.pack('<Q',result)
def quantweights(influences):
    require(1<=len(influences)<=4,'存在无权重或超过四权重的顶点')
    require(all(math.isfinite(w) and w>0 for _,w in influences),'权重包含非法数值')
    total=sum(w for _,w in influences);vals=[w/total*65535 for _,w in influences];q=[math.floor(x) for x in vals]
    for i in sorted(range(len(q)),key=lambda i:vals[i]-q[i],reverse=True)[:65535-sum(q)]:q[i]+=1
    return [j for j,_ in influences]+[255]*(4-len(q)),q+[0]*(4-len(q))

def build(folder,doc,bindings,outdir,texture_files,variant=None):
    """texture_files maps Blender material slots to already encoded ASTC paths."""
    outdir=Path(outdir);require(not outdir.exists(),'输出目录已存在，请使用新的导出任务')
    outdir.mkdir(parents=True)
    project,src,a,meta=template(folder,outdir.parent/'structure_source.json');raw=src.read_bytes();sk=a['embedded_skeleton'];flag=a['report']['flag']
    remap,remreport=decoder.recover_joint_remap(a)
    inverse=None
    if any(i!=v for i,v in enumerate(remap) if v>=0):
        require(doc.get('bone_index_space')=='imported_names' and doc.get('joint_remap')==remap and doc.get('source_sha256')==sha(src),'非直接骨骼映射需要来自同一原文件的 NeoX 原生骨架；不能猜测其他骨架的对应关系')
        inverse={named:runtime for runtime,named in enumerate(remap) if named>=0}
    target_dir=project['resource_dir'];output_stem=project['stem']
    if variant:
        output_stem=safe_name(variant['stem']);target_dir=resource_dir(variant['resource_dir'])
        require(output_stem!=project['stem'] and target_dir!=project['resource_dir'],'新皮肤资源必须填写不同的文件名和资源目录')
    usedslots={t['material'] for t in doc['triangles']};rules={int(b['slot']):b for b in bindings}
    require(usedslots<=rules.keys(),'有材质槽未配置')
    subs=[{'name':s['name'],'mtl':s['material_index'],'source_sub':i,'uv_count':a['submeshes'][i][2],'verts':[],'faces':[],'lut':{}} for i,s in enumerate(meta['submeshes'])]
    mt=ET.parse(src.with_suffix('.mtg'));mg=mt.getroot().find('MaterialGroup');require(mg is not None,'MTG 缺少 MaterialGroup')
    ids=[int(s.tag.split('_')[-1]) for s in mg];nextid=max(ids,default=-1)+1;claimed=set();slotmap={}
    for slot in sorted(usedslots):
        b=rules[slot];si=int(b['source_sub']);require(0<=si<len(subs),f'材质槽 {slot} 没有有效来源分组')
        if si not in claimed:subindex=si;claimed.add(si)
        else:
            subindex=len(subs);subs.append({'name':f"{subs[si]['name']}_edit_{slot}",'mtl':subs[si]['mtl'],'source_sub':si,'uv_count':subs[si]['uv_count'],'verts':[],'faces':[],'lut':{}})
        subs[subindex]['uv_count']=max(1,subs[subindex]['uv_count'])
        slotmap[slot]=subindex
        if b['mode']!='ORIGINAL':
            require(slot in texture_files,'缺少转换后的贴图')
            path=Path(texture_files[slot]);require(path.is_file(),'编码贴图不存在');name=safe_name(path.name);shutil.copy2(path,outdir/name)
            original_id=meta['submeshes'][si]['material_index'];base=mg.find(f'Material_{original_id}');require(base is not None,'找不到原材质模板')
            node=copy.deepcopy(base);node.tag=f'Material_{nextid}';node.find('Material').set('Name',b['name'])
            table=node.find('.//ParamTable');require(table is not None,'材质缺少 ParamTable')
            tex=table.find('diffuseTexture')
            if tex is None:tex=ET.SubElement(table,'diffuseTexture')
            tex.set('Value',(target_dir+'/'+path.stem+'.tga').replace('/','\\'))
            if b.get('double_sided'):
                state=node.find('.//RenderStates');require(state is not None,'材质缺少渲染状态')
                state.set('CullBack','False')
            if b.get('alpha','KEEP')!='KEEP':
                transp=node.find('.//TransparentMode');require(transp is not None,'材质缺少透明设置')
                transp.set('TransparentMode','1' if b['alpha']=='OPAQUE' else '4')
            mg.append(node);subs[subindex]['mtl']=nextid;nextid+=1
    mg.set('MaterialCount',str(len(mg)))
    # Source colors are not exposed by the old importer. Preserve exact source bytes
    # only when position/UV or UV alone resolves an unambiguous color.
    coloroffset=a['report']['uv_start']+sum(v*l*4 for v,f,l,c in a['submeshes']);base=0;face_start=0;colormaps={};color_faces={}
    for si,(nv,nf,uvlayers,colors) in enumerate(a['submeshes']):
        if colors:
            lut={}
            for i in range(nv):
                color=raw[coloroffset+i*4:coloroffset+i*4+4];uv=struct.pack('<2e',*a['uvs'][0][base+i])
                for key in [(pkey(a['positions'][base+i]),uv),('uv',uv)]:lut.setdefault(key,set()).add(color)
            colormaps[si]=lut
            full={};uvonly={};source_colors={}
            for i in range(nv):source_colors[base+i]=raw[coloroffset+i*4:coloroffset+i*4+4]
            for face in a['faces'][face_start:face_start+nf]:
                coords=[(pkey(a['positions'][v]),struct.pack('<2e',*a['uvs'][0][v])) for v in face]
                full.setdefault(tuple(sorted(coords)),[]).append(tuple(face))
                uvonly.setdefault(tuple(sorted(uv for p,uv in coords)),[]).append(tuple(face))
            color_faces[si]=(full,uvonly,source_colors)
        coloroffset+=nv*colors*4;base+=nv;face_start+=nf
    require(coloroffset==a['report']['joints_start'],'模板颜色流长度不符')
    if doc['binding'] in {'STATIC','RETARGET'}:colormaps={}
    for tri in doc['triangles']:
        sub=subs[slotmap[tri['material']]];face=[]
        face_candidates=None;face_uses_position=True
        if sub['source_sub'] in colormaps:
            full,uvonly,source_colors=color_faces[sub['source_sub']]
            triangle_keys=[(pkey(doc['vertices'][doc['corners'][li]['v']]['p']),struct.pack('<2e',*doc['corners'][li]['uv'])) for li in tri['loops']]
            face_candidates=full.get(tuple(sorted(triangle_keys)))
            if not face_candidates:face_candidates=uvonly.get(tuple(sorted(uv for p,uv in triangle_keys)));face_uses_position=False
        for li in tri['loops']:
            corner=doc['corners'][li];v=doc['vertices'][corner['v']]
            require(all(math.isfinite(x) for x in (*v['p'],*corner['n'],*corner['uv'])),'网格包含非有限数值')
            normal=struct.pack('<3b',*(max(-127,min(127,round(x*127))) for x in corner['n']));uv=struct.pack('<2e',*corner['uv'])
            source_uvs=corner.get('uvs',[corner['uv']]);count=sub['uv_count']
            require(len(source_uvs)>=count and all(v is not None for v in source_uvs[:count]),f"分组 {sub['name']} 需要 {count} 层 UV，请选择对应的附加 UV")
            require(all(math.isfinite(x) for pair in source_uvs[:count] for x in pair),'UV 包含非有限数值')
            packed_uvs=[struct.pack('<2e',*pair) for pair in source_uvs[:count]]
            color=b''
            if sub['source_sub'] in colormaps:
                lut=colormaps[sub['source_sub']];colors=lut.get((pkey(v['p']),uv)) or lut.get(('uv',uv))
                if (colors is None or len(colors)!=1) and face_candidates:
                    colors={source_colors[vi] for f in face_candidates for vi in f if struct.pack('<2e',*a['uvs'][0][vi])==uv and (not face_uses_position or pkey(a['positions'][vi])==pkey(v['p']))}
                require(colors is not None and len(colors)==1,f"分组 {sub['name']} 顶点 {corner['v']} 无法唯一恢复原顶点颜色；原数据需要更完整的拓扑或颜色属性")
                color=next(iter(colors))
            key=(corner['v'],normal,tuple(packed_uvs),color)
            if key not in sub['lut']:
                influences=v['weights']
                if inverse is not None:
                    require(all(j in inverse for j,w in influences),'使用了导入时无对应原始索引的骨骼，无法可靠恢复权重；请检查新增权重')
                    influences=[(inverse[j],w) for j,w in influences]
                js,ws=quantweights(influences);require(all(j==255 or 0<=j<sk['bone_count'] for j in js),'权重骨骼编号越界')
                sub['lut'][key]=len(sub['verts']);sub['verts'].append({'p':v['p'],'normal':normal,'uv':uv,'uvs':packed_uvs,'j':js,'w':ws,'color':color})
            face.append(sub['lut'][key])
        require(len(set(face))==3,'发现退化三角形，请先清理网格')
        sub['faces'].append(face)
    vertices=[v for s in subs for v in s['verts']];nv=len(vertices);nf=sum(len(s['faces']) for s in subs)
    require(0<nv<65536 and nf>0,'网格为空或超过首版单分段顶点容量（最多 65535）')
    start=sk['split_offsets'][0];data=bytearray(raw[:start]);bonepoints={}
    for v in vertices:
        for j,w in zip(v['j'],v['w']):
            if w:bonepoints.setdefault(j,[]).append(v['p'])
    for bi,points in bonepoints.items():
        matrix=np.linalg.inv(np.array(sk['matrices'][bi]).reshape(4,4));local=np.c_[np.array(points),np.ones(len(points))]@matrix
        struct.pack_into('<7f',data,sk['bounds_start']+bi*28,*bbox(local[:,:3]))
    for s in subs:data.extend(struct.pack('<IIBB',len(s['verts']),len(s['faces']),s['uv_count'],int(s['source_sub'] in colormaps)))
    data.extend(raw[a['report']['data_start']-10:a['report']['data_start']-8]);data.extend(struct.pack('<II',nv,nf));data.append(flag)
    for v in vertices:data.extend(encode_pos(v['p']) if flag==4 else struct.pack('<3f',*v['p']))
    for v in vertices:data.extend(v['normal'])
    data.extend(b'\0'*a['report']['index_padding'])
    last=0;base=0;faces=[]
    for s in subs:
        for f in s['faces']:
            face=tuple(i+base for i in f);faces.append(face)
            for i in face:
                data.extend(struct.pack('<H',(i-last)&65535 if a['report']['index_decoder']=='global_uint16_delta' else i));last=i
        base+=len(s['verts'])
    for s in subs:
        for layer in range(s['uv_count']):
            for v in s['verts']:data.extend(v['uvs'][layer])
    for v in vertices:data.extend(v['color'])
    for v in vertices:data.extend(struct.pack('<4B',*v['j']))
    for v in vertices:data.extend(struct.pack('<4H',*v['w']))
    mask=bytearray(32)
    for j in bonepoints:mask[j//8]|=1<<(j%8)
    data.extend(mask);end=len(data);data.extend(raw[sk['ending_address']:]);struct.pack_into('<I',data,sk['matrices_start']+sk['bone_count']*64+1,end)
    stem=output_stem;output=outdir/(stem+'.cmesh');output.write_bytes(data)
    gt=ET.parse(src.with_suffix('.gim'));g=gt.getroot();bb=bbox([v['p'] for v in vertices]);g.set('VertexCount',str(nv));g.set('TriangleCount',str(nf));g.set('BoundingInfo',f'({vec(bb[:3])}),({vec(bb[3:6])}),{bb[6]:.9g}')
    sm=g.find('SubMesh');sm.clear()
    for i,s in enumerate(subs):
        bb=bbox([v['p'] for v in s['verts']]);ET.SubElement(sm,f'Sub{i}',Name=s['name'],MtlIdx=str(s['mtl']),VertexCount=str(len(s['verts'])),TriangleCount=str(len(s['faces'])),BoundingCenter=vec(bb[:3]),BoundingHalf=vec(bb[3:6]))
    ET.indent(gt);gt.write(output.with_suffix('.gim'),encoding='utf-8',xml_declaration=True);ET.indent(mt);mt.write(output.with_suffix('.mtg'),encoding='utf-8',xml_declaration=True)
    check,_=inspect(output,output.with_suffix('.gim'),outdir.parent/'structure_export.json')
    require(check['faces']==faces,'回读的三角形索引与输入不一致')
    maxerr=max(abs(x-y) for v,p in zip(vertices,check['positions']) for x,y in zip(v['p'],p));require(maxerr<=1/2048+1e-9,'位置误差超出量化范围')
    for expected,j,w in zip(vertices,check['joints'],check['weights']):
        require(tuple(expected['j'])==j and max(abs(x/65535-y) for x,y in zip(expected['w'],w))<1e-6,'回读权重不一致')
    offset=0
    for s,record in zip(subs,check['submeshes']):
        require(record[2]==s['uv_count'],'回读 UV 层数不一致')
        for layer in range(s['uv_count']):
            for i,v in enumerate(s['verts']):require(struct.pack('<2e',*check['uvs'][layer][offset+i])==v['uvs'][layer],'附加 UV 回读不一致')
        offset+=len(s['verts'])
    dependencies=sorted({el.attrib['Value'].replace('\\','/') for el in mt.getroot().iter() if el.attrib.get('Value','').lower().endswith(('.tga','.astc','.dds','.png'))})
    return {'addon_version':VERSION,'format':f'v4_flag{flag}_single','local_verified':True,'client_verified':False,'purpose':'NEW_RESOURCE' if variant else 'REPLACE','deployment_ready':not bool(variant),'registration_required':bool(variant),'source_stem':project['stem'],'output_stem':stem,'texture_references':dependencies,'joint_mapping':'inverse_of_matching_importer' if inverse else 'direct','binding':doc['binding'],'vertices':nv,'triangles':nf,'submeshes':len(subs),'active_bones':len(bonepoints),'max_position_error':maxerr,'files':{p.name:sha(p) for p in outdir.iterdir()},'resource_dir':target_dir,'output_dir':str(outdir.resolve()),'warnings':doc.get('warnings',[]),'submesh_summary':[{'name':s['name'],'vertices':len(s['verts']),'triangles':len(s['faces']),'material':s['mtl'],'uv_layers':s['uv_count']} for s in subs]}
