"""Scoped MuMu transport. Validate, snapshot, stage, commit, rollback, restore."""
import datetime,json,re,subprocess,uuid
from pathlib import Path
from . import core

class Device:
    def __init__(self,mumu,index=0,package='com.netease.fr26'):
        self.mumu=Path(mumu);self.package=package
        core.require(re.fullmatch(r'[a-zA-Z0-9_.]+',package) is not None,'包名不合法')
        self.adb=self.mumu/'adb.exe';cli=self.mumu/'mumu-cli.exe'
        core.require(self.adb.is_file() and cli.is_file(),'MuMu 目录应包含 adb.exe 和 mumu-cli.exe')
        info=json.loads(self.run([cli,'info','-v',str(index)]));core.require(info.get('is_android_started'),'MuMu 实例尚未启动')
        host=info.get('adb_host_ip','127.0.0.1');core.require(host in ('127.0.0.1','localhost'),'本版仅连接本机 MuMu')
        self.serial=f"{host}:{int(info['adb_port'])}";self.run([self.adb,'connect',self.serial])
    def run(self,args,check=True):
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        r=subprocess.run([str(x) for x in args],capture_output=True,timeout=45,creationflags=flags)
        if check and r.returncode:raise RuntimeError(r.stderr.decode(errors='replace') or r.stdout.decode(errors='replace'))
        return r.stdout.decode('utf-8',errors='replace')
    def call(self,*args,check=True):return self.run([self.adb,'-s',self.serial,*args],check)
    def prepare(self,relative,stop_game=False):
        relative=core.resource_dir(relative)
        self.remote=f'/sdcard/Android/data/{self.package}/files/netease/frxy/res/{relative}/'
        exists=self.call('shell',f'if [ -d {self.remote} ]; then echo exists; else echo absent; fi').strip()
        core.require(exists=='exists','模拟器中不存在目标目录，请检查局外/局内路径')
        pid=self.call('shell','pidof',self.package,check=False).strip()
        if pid:
            core.require(stop_game,'游戏仍在运行。请退出游戏进程，或勾选“安装前关闭游戏”再试。')
            self.call('shell','am','force-stop',self.package)
    def hash(self,name):
        path=self.remote+core.safe_name(name)
        s=self.call('shell',f'if [ -f {path} ]; then sha256sum {path}; else echo absent; fi').split()
        core.require(s,'无法读取目标文件状态')
        if s[0]=='absent':return None
        core.require(re.fullmatch('[0-9a-f]{64}',s[0]) is not None,'无法读取设备 SHA-256')
        return s[0]
    def pull(self,name,path):self.call('pull',self.remote+core.safe_name(name),path)
    def push(self,path,name):self.call('push',path,self.remote+core.safe_name(name))
    def move(self,src,dst):self.call('shell','mv',self.remote+core.safe_name(src),self.remote+core.safe_name(dst))
    def remove(self,name):self.call('shell','rm','-f',self.remote+core.safe_name(name))

def unique():return datetime.datetime.now().strftime('%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:6]
def get_report(reportfile):
    reportfile=Path(reportfile).resolve();r=core.read(reportfile)
    core.require(r.get('deployment_ready',True),'这是尚未注册的新皮肤资源；不能按同名替换安装。请先完成对应客户端的皮肤配置。')
    core.require(r.get('local_verified') is True,'导出包尚未通过本地检查')
    out=Path(r['output_dir']).resolve();core.require(out==reportfile.parent/'files','报告的输出目录不属于本次导出')
    for name,sha in r['files'].items():core.require(core.sha(out/core.safe_name(name))==sha,'导出文件已被改动：'+name)
    return r,out
def snapshot(folder,device,names,kind):
    dest=Path(folder)/'device_backups'/unique();dest.mkdir(parents=True)
    hashes={}
    for name in names:
        h=device.hash(name);hashes[name]=h
        if h:
            device.pull(name,dest/name);core.require(core.sha(dest/name)==h,'设备备份校验失败')
    record={'schema':1,'kind':kind,'remote':device.remote,'serial':device.serial,'files':hashes}
    core.dump(dest/'manifest.json',record);return dest,record
def baseline(folder,device,reportfile,stop_game=False):
    project=core.load_project(folder);r,out=get_report(reportfile);device.prepare(project['resource_dir'],stop_game)
    dest,_=snapshot(folder,device,r['files'],'accepted-baseline');return dest
def accepted_hashes(folder,project,name):
    hashes={None,project['source_files'].get(name)}
    for path in (Path(folder)/'device_backups').glob('*/manifest.json'):
        r=core.read(path)
        # A user-requested baseline, or a completed transaction, establishes known contents.
        if r.get('kind')=='accepted-baseline' or r.get('state')=='complete':
            if name in r['files']:hashes.add(r['files'][name])
            if name in r.get('installed',{}):hashes.add(r['installed'][name])
    return hashes
def install(folder,device,reportfile,stop_game=False):
    project=core.load_project(folder);report,out=get_report(reportfile)
    core.require(report['resource_dir']==project['resource_dir'],'导出目录与工程目标不一致')
    device.prepare(project['resource_dir'],stop_game)
    for name,newhash in report['files'].items():
        h=device.hash(name);core.require(h in accepted_hashes(folder,project,name)|{newhash},f'设备上的 {name} 是未知版本。请先检查，再用“备份当前版本为基线”明确保留它。')
        core.require(h is not None or name not in project['source_files'],'设备缺少目标原文件：'+name)
    backup,record=snapshot(folder,device,report['files'],'installation');record.update(state='staging',installed=report['files'],committed=[])
    core.dump(backup/'manifest.json',record)
    suffix='.nxstage_'+uuid.uuid4().hex[:8]
    try:
        for name,h in report['files'].items():
            device.push(out/name,name+suffix);core.require(device.hash(name+suffix)==h,'上传校验失败：'+name)
        for name,h in record['files'].items():core.require(device.hash(name)==h,'传输期间目标文件发生变化：'+name)
        record['state']='committing';core.dump(backup/'manifest.json',record)
        for name,h in report['files'].items():
            # Persist intent before mv so interruption recovery includes this file.
            record['committed'].append(name);core.dump(backup/'manifest.json',record)
            device.move(name+suffix,name);core.require(device.hash(name)==h,'安装后校验失败：'+name)
        record['state']='complete';core.dump(backup/'manifest.json',record)
        core.dump(Path(folder)/'last_install.json',{'backup':str(backup)})
        return backup
    except Exception:
        errors=[]
        for name in record['committed']:
            try:
                current=device.hash(name);core.require(current in (record['files'][name],record['installed'][name]),'回滚遇到其他程序的修改')
                if record['files'][name]:
                    device.push(backup/name,name+suffix);core.require(device.hash(name+suffix)==record['files'][name],'回滚上传校验失败');device.move(name+suffix,name)
                else:device.remove(name)
                core.require(device.hash(name)==record['files'][name],'回滚校验失败')
            except Exception as exc:errors.append(f'{name}: {exc}')
        record.update(state='rollback_failed' if errors else 'rolled_back',rollback_errors=errors);core.dump(backup/'manifest.json',record)
        raise
    finally:
        for name in report['files']:
            try:device.remove(name+suffix)
            except Exception:pass
def restore(folder,device,backup=None,stop_game=False):
    folder=Path(folder);project=core.load_project(folder)
    if backup is None:backup=Path(core.read(folder/'last_install.json')['backup'])
    backup=Path(backup).resolve();core.require(backup.is_relative_to((folder/'device_backups').resolve()),'恢复记录不属于当前工程')
    record=core.read(backup/'manifest.json');device.prepare(project['resource_dir'],stop_game)
    core.require(record['remote']==device.remote,'备份目标路径与当前设备不一致')
    for name,h in record['files'].items():
        core.safe_name(name)
        if h:core.require(core.sha(backup/name)==h,'本地备份校验失败')
        core.require(device.hash(name) in (h,record.get('installed',{}).get(name)),'目标存在未知修改，停止恢复：'+name)
    suffix='.nxrestore_'+uuid.uuid4().hex[:8]
    for name,h in record['files'].items():
        if h:device.push(backup/name,name+suffix);core.require(device.hash(name+suffix)==h,'恢复上传校验失败')
    for name,h in record['files'].items():
        if h:device.move(name+suffix,name)
        else:device.remove(name)
        core.require(device.hash(name)==h,'恢复校验失败')
    record['state']='restored';core.dump(backup/'manifest.json',record)
    return backup
