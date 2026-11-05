#!/usr/bin/env python3
"""Purple DSS 0.3.0: explicit contrast/hemisphere, Docker 1.2.0, six downloads."""
import argparse
import contextlib
import datetime
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

VERSION = '0.3.0'
IMAGE = 'pulks/docker_purple_mri@sha256:c8df848d4649c1c0a8f8a7ccf7911ac5bd5df86442ff0e66eaf5f3dd2ce9558d'
IMAGE_ID = 'sha256:fdd1e9eca00da2032aadeba8e0cbbc28bbd44b068e91249bbcf723fd9e102f3a'
TAGS = {'InputMRI_'+c.upper()+'_'+h.upper(): (c, h)
        for c in ('t2w', 'ciss', 'flash', 't2star') for h in ('lh', 'rh')}
ATLAS = {
    'DKT': ('aparc.DKTatlas+aseg', 'DKTatlas'),
    'Destrieux': ('aparc.a2009s+aseg', 'a2009s'),
    'Glasser': ('aparc.HCP-MMP1.glasser', 'HCP-MMP1.glasser'),
    'Schaefer400': ('aparc.Schaefer2018_400Parcels_17Networks', 'Schaefer2018_400Parcels_17Networks'),
    'Brainnetome': ('aparc.brainnetome', 'brainnetome'),
    'Economo': ('aparc.economo', 'economo'), 'Julich': ('aparc.julich', 'julich')}
GROUPS = [('classic', ('DKT', 'Destrieux'), ('aparc',)),
          ('modern', ('Glasser', 'Schaefer400', 'Brainnetome'), ()),
          ('additional', ('Economo', 'Julich'), ('wmparc',))]
BUNDLES = ['purple_segmentations.tar.gz'] + [
    'purple_parcellations_%02d_%s.zip' % (i, g[0]) for i, g in enumerate(GROUPS, 1)
] + ['purple_surfaces.tar.gz', 'purple_reports.tar.gz']

def require(ok, message):
    if not ok:
        raise ValueError(message)

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4*1024*1024), b''):
            h.update(block)
    return h.hexdigest()

def write_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data, indent=2)+'\n'); tmp.replace(path)

def read(path):
    return json.loads(Path(path).read_text())

def run(args, timeout=None, log=None, stdin=None):
    args = list(map(str, args))
    if log:
        with Path(log).open('a') as stream:
            return subprocess.run(args, input=stdin, text=True, stdout=stream,
                                  stderr=subprocess.STDOUT, timeout=timeout, check=True)
    return subprocess.run(args, input=stdin, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, check=True).stdout

def records(text):
    return [line.strip() for line in text.splitlines() if line.strip()]

@contextlib.contextmanager
def lock(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield

def docker(mounts, command, gpu=False, root=False, stdin=None, log=None, env=()):
    args = ['docker', 'run', '--rm', '--init', '--network', 'none', '--user',
            '0:0' if root else '%d:%d' % (os.getuid(), os.getgid()),
            '-e', 'HOME=/tmp', '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONUNBUFFERED=1']
    if stdin is not None:
        args += ['-i']
    if gpu:
        device = os.environ.get('GPU', '0')
        require(device.isdecimal(), 'GPU must be a single device number')
        args += ['--gpus', 'device='+device, '--shm-size=8g']
    for spec in env:
        args += ['-e', spec]
    for source, target, mode in mounts:
        source = Path(source).resolve()
        require(source.exists() and ':' not in str(source) and ',' not in str(source), 'Invalid mount: '+str(source))
        args += ['-v', str(source)+':'+target+':'+mode]
    args += ['--entrypoint', command[0], IMAGE] + list(command[1:])
    return run(args, log=log, stdin=stdin)

def preflight(needs_dss=False):
    require(shutil.which('docker'), 'docker not found')
    actual = run(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}']).strip()
    require(actual == IMAGE_ID, 'Docker release image differs from the tested v1.2.0 image')
    if needs_dss:
        require(shutil.which('itksnap-wt'), 'itksnap-wt not found')
        require(license_path().is_file(), 'FreeSurfer license missing')
    print('PREFLIGHT PASS: pinned Docker v1.2.0', flush=True)

def license_path():
    return Path(os.environ.get('FS_LICENSE_HOST', '/data/pulkit/exvivo_reg_reconstruct/freesurfer_installation/freesurfer/license.txt'))

def pipeline(run_dir, contrast, hemi, stage):
    root = Path(run_dir); output = root/'output'; output.mkdir(exist_ok=True)
    mounts = [(root/'input', '/input', 'ro'), (output, '/output', 'rw'),
              (license_path(), '/opt/freesurfer/license.txt', 'ro')]
    threads = int(os.environ.get('THREADS', '8')); require(threads > 0, 'THREADS must be positive')
    cmd = ['/bin/bash', '/src/commands_nnunet_inference.sh', stage,
           '--contrast', contrast, '--hemi', hemi, '--threads', str(threads),
           '--input-dir', '/input', '--dl-dir', '/output/dl',
           '--surface-dir', '/output/surface', '--atlas-output-dir', '/output/atlases']
    try:
        docker(mounts, cmd+['--preflight-only'], root=True, log=root/'pipeline.log')
        docker(mounts, cmd, gpu=stage == 'exvivo_dl', root=True, log=root/'pipeline.log')
    finally:
        docker([(output, '/output', 'rw')], ['/bin/chown', '-R', '-h', '--',
               '%d:%d' % (os.getuid(), os.getgid()), '/output'], root=True)

def validate_run(root, case, contrast, hemi, dest, full=True):
    # Mount data read-only. The shipped checker needs a writable directory for caches.
    code = r'''
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, '/src')
import merge_common as m
import merge_io as io
import nibabel as nib
import numpy as np
from check_outputs import check
case, contrast, hemi, full = sys.argv[1:]
dl=Path('/output/dl'); inputs=Path('/input')
if set(m.legacy.discover_cases(inputs)) != {case}: raise ValueError('Expected exactly the selected case')
report=json.loads((dl/'reports'/(case+'_structures.json')).read_text())
expected='flash' if contrast=='t2star' else contrast
if (report['version'],report['case'],report['contrast']) != (m.VERSION,case,expected): raise ValueError('Wrong run identity')
if not (dl/'STRUCTURES_COMPLETE').is_file(): raise ValueError('Missing structures completion marker')
ref=nib.load(inputs/(case+'_0000.nii.gz'))
for stage,key in [('11_final_merged','sha256'),('12_surface_labels','surface_sha256')]+([('13b_flash_task278_cleaned','cleaned_task278_sha256')] if expected=='flash' else []):
    p=dl/stage/(case+'.nii.gz'); m.native_match(p,ref)
    if m.sha(p)!=report[key]: raise ValueError('Checksum mismatch: '+stage)
for key,name in [('mtl_rim','mtl_rim_report.json')]+([('flash_GM_cleanup','component_audit.json')] if expected=='flash' else []):
    if report['refinement'][key]!=json.loads((dl/'reports'/case/name).read_text()): raise ValueError('Refinement report mismatch')
with tempfile.TemporaryDirectory(dir='/scratch') as tmp:
    shadow=Path(tmp)
    if full=='yes':
        if Path('/output/surface/hemisphere.txt').read_text().strip()!=hemi: raise ValueError('Wrong surface hemisphere')
        for name in ('dl','surface','atlases'): (shadow/name).symlink_to(Path('/output')/name)
        check(inputs, shadow)
    else:
        f=io.Volume(dl/'11_final_merged'/(case+'.nii.gz'),ref,shadow,'final',np.uint16)
        s=io.Volume(dl/'12_surface_labels'/(case+'.nii.gz'),ref,shadow,'surface',np.uint8)
        try:
            for box in io.slabs(ref.shape):
                m.legacy.check_labels(f.read(box), {0}|m.legacy.ALLOWED, 'Final')
                if not np.array_equal(s.read(box),m.coarse_surface_labels(f.read(box))): raise ValueError('Incorrect surface scaffold')
        finally: f.close(); s.close()
print('DSS_VALIDATION_PASS')
'''
    text = docker([(Path(root)/'input','/input','ro'), (Path(root)/'output','/output','ro'), (dest,'/scratch','rw')],
                  ['/surface_env/bin/python', '-', case, contrast, hemi, 'yes' if full else 'no'],
                  stdin=code, env=['LD_LIBRARY_PATH='])
    (Path(dest)/('validation_full.log' if full else 'validation_dl.log')).write_text(text)
    require('DSS_VALIDATION_PASS' in text.splitlines(), 'Validation did not finish')

def tar_bundle(target, entries):
    tmp = Path(str(target)+'.tmp')
    with tmp.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode='w') as archive:
            for path, arc in sorted(entries, key=lambda v:v[1]):
                info=archive.gettarinfo(str(Path(path).resolve()), arcname=arc)
                require(info.isfile(), 'Archive member must be a regular file: '+str(path))
                info.uid=info.gid=info.mtime=0; info.uname=info.gname=''; info.mode=0o644
                with Path(path).open('rb') as f: archive.addfile(info, f)
    # Verify every member against its source; read the gzip trailer as well.
    expected={arc:sha(p) for p,arc in entries}
    with tarfile.open(tmp, 'r:gz') as archive:
        require(set(archive.getnames())==set(expected), 'Tar member mismatch')
        for member in archive:
            h=hashlib.sha256()
            with archive.extractfile(member) as f:
                for block in iter(lambda:f.read(4*1024*1024),b''): h.update(block)
            require(h.hexdigest()==expected[member.name], 'Tar payload mismatch')
    with gzip.open(tmp,'rb') as f:
        for unused in iter(lambda:f.read(4*1024*1024),b''): pass
    tmp.replace(target)

def zip_bundle(target, entries):
    tmp=Path(str(target)+'.tmp')
    with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for path,arc in sorted(entries,key=lambda v:v[1]):
            info=zipfile.ZipInfo(arc, date_time=(2026,1,1,0,0,0)); info.external_attr=0o100644<<16
            with Path(path).open('rb') as src, z.open(info,'w',force_zip64=True) as out:
                shutil.copyfileobj(src,out,4*1024*1024)
    with zipfile.ZipFile(tmp) as z:
        require(z.testzip() is None, 'ZIP CRC check failed')
        for path,arc in entries:
            h=hashlib.sha256()
            with z.open(arc) as f:
                for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
            require(h.hexdigest()==sha(path),'ZIP payload mismatch')
    tmp.replace(target)

def segmentation_entries(root,case,contrast):
    stages=['01_purple_original','05_purple_corrected','06_mtl','07_subcortical','11_final_merged','12_surface_labels']
    if contrast in ('flash','t2star'): stages+=['13_flash_task278','13b_flash_task278_cleaned']
    return [(Path(root)/'output/dl'/s/(case+'.nii.gz'),'dl/'+s+'/'+case+'.nii.gz') for s in stages]

def package(root, case, contrast, hemi, dest, full=True):
    root, dest=Path(root).resolve(), Path(dest).resolve()
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',case), 'Invalid case identifier')
    require(contrast in ('t2w','ciss','flash','t2star') and hemi in ('lh','rh'),'Invalid contrast or hemisphere')
    dest.mkdir(parents=True,exist_ok=True)
    with lock(dest/'package.lock'):
        require(not (dest/'delivery_manifest.json').exists(), 'Package already complete; use a fresh output or upload-only')
        validate_run(root,case,contrast,hemi,dest,full)
        entries=segmentation_entries(root,case,contrast)
        tar_bundle(dest/BUNDLES[0], entries)
        if not full: return
        sub=root/'output/surface'/case; atlases=root/'output/atlases'/case
        manifest=[]
        # Preserve the nine existing raw surface-parcellation downloads in
        # clearly named subfolders, and add the seven native-space final merges.
        with tempfile.TemporaryDirectory(prefix='converted_',dir=dest) as tmp:
            converted=Path(tmp)
            raw_names=[v[1] for v in ATLAS.values()]+['aparc','wmparc']
            code='source /opt/freesurfer/SetUpFreeSurfer.sh >/dev/null 2>&1; set -e; for name in "$@"; do mri_convert "/source/$name.mgz" "/converted/$name.nii.gz"; done'
            docker([(sub/'mri','/source','ro'),(converted,'/converted','rw'),
                    (license_path(),'/opt/freesurfer/license.txt','ro')],
                   ['/bin/bash','-c',code,'convert']+raw_names,log=dest/'conversion.log',env=['LD_LIBRARY_PATH='])
            for index,(_,names,extras) in enumerate(GROUPS,1):
                files=[]
                for name in names:
                    stem,raw=ATLAS[name]
                    files += [(atlases/'mri'/(stem+'_with_dl_structures.nii.gz'),case+'/final_merged/'+stem+'_with_dl_structures.nii.gz'),
                              (atlases/'lookup_tables'/(name+'.txt'),case+'/lookup_tables/'+name+'.txt'),
                              (converted/(raw+'.nii.gz'),case+'/surface_projection/'+raw+'.nii.gz')]
                files += [(converted/(name+'.nii.gz'),case+'/surface_projection/'+name+'.nii.gz') for name in extras]
                zip_bundle(dest/BUNDLES[index],files)
                manifest += [{'archive':BUNDLES[index], 'file':arc, 'sha256':sha(p)} for p,arc in files]
        files=[]
        for metric in ('white','pial','inflated','sphere.reg','thickness','area','area.pial','curv','curv.pial','sulc'):
            files.append((sub/'surf'/(hemi+'.'+metric),case+'/surf/'+hemi+'.'+metric))
        for folder in ('label','stats'):
            for p in sorted((sub/folder).iterdir()):
                if p.is_file() and (p.name.startswith(hemi+'.') or not p.name.startswith(('lh.','rh.'))):
                    files.append((p,case+'/'+folder+'/'+p.name))
        tar_bundle(dest/BUNDLES[4],files)
        write_json(dest/'parcellation_manifest.json',manifest)
        provenance={'service_version':VERSION,'docker_image':IMAGE,'case':case,'contrast':contrast,'hemisphere':hemi,
                    'validated':True,'review_items':[]}
        for name in ATLAS:
            qc=read(atlases/'qc'/(name+'.json'))
            names=qc.get('reuse_audit',{}).get('opposite_side_names_in_projection',{})
            if name!='Brainnetome' and names:
                provenance['review_items'].append(name+': '+str(len(names))+' opposite-side names; review source naming/mapping')
        write_json(dest/'validation_summary.json',provenance)
        files=[(dest/n,'reports/'+n) for n in ('validation_summary.json','parcellation_manifest.json','validation_full.log','conversion.log')]
        for base,arc in [(root/'output/dl/reports','reports/dl'),(atlases/'qc','reports/atlases'),
                         (sub/'qc','reports/surface'),(root/'output/timings','reports/timings')]:
            if base.is_dir():
                files += [(p,arc+'/'+str(p.relative_to(base))) for p in sorted(base.rglob('*'))
                          if p.is_file() and p.suffix in ('.json','.tsv','.txt','.log','.csv')]
        for p in (root/'pipeline.log',root/'run.log',root/'output/surface/surface_pipeline.log'):
            if p.is_file(): files.append((p,'reports/logs/'+p.name))
        tar_bundle(dest/BUNDLES[5],files)
        write_json(dest/'delivery_manifest.json',dict(provenance,
            bundles={name:{'sha256':sha(dest/name),'bytes':(dest/name).stat().st_size} for name in BUNDLES}))
        print('PACKAGING PASS: six archives verified; seven final merges plus nine surface projections.',flush=True)
        print('Output:', dest, flush=True)

def select_input(tag_paths):
    active=[(tag,path) for tag,path in tag_paths.items() if path]
    require(len(active)==1, 'Assign exactly one contrast/hemisphere tag to the main MRI')
    tag,path=active[0]; require(tag in TAGS,'Unknown MRI tag')
    return path, TAGS[tag][0], TAGS[tag][1]

def service_hash(text):
    matches=[p[2] for p in (line.split() for line in text.splitlines())
             if len(p)>=4 and p[0]=='purple' and p[1]==VERSION and p[3]=='purple']
    require(len(matches)==1 and re.fullmatch('[0-9a-f]{40}',matches[0]), 'Expected exactly one registered purple '+VERSION+' service')
    return matches[0]

def dss(*args, critical=True, seconds=60):
    for attempt in range(2 if critical else 1):
        try: return run(['itksnap-wt']+list(args),timeout=seconds)
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired):
            if not critical: return ''
            if attempt: raise
            time.sleep(5)

def upload_one(ticket,dest,name):
    dest=Path(dest); path=dest/name; digest=sha(path)
    receipts=dest/'receipts'/ticket; receipts.mkdir(parents=True,exist_ok=True)
    receipt=receipts/(name+'.'+digest)
    if receipt.with_suffix(receipt.suffix+'.published').exists(): return
    uploaded=Path(str(receipt)+'.uploaded'); published=Path(str(receipt)+'.published')
    if not uploaded.exists():
        dss('-dssp-tickets-attach',ticket,'Purple - '+name,path,
            'application/zip' if name.endswith('.zip') else 'application/gzip',seconds=600)
        uploaded.write_text(digest+'\n')
    dss('-dssp-tickets-log',ticket,'info','Download ready: '+name+'. Use this message\'s paperclip attachment.')
    published.write_text(digest+'\n'); print('DELIVERED:',name,flush=True)

def deliver(ticket,dest,mark_success=False):
    require(str(ticket).isdecimal(),'Invalid ticket ID'); ticket=str(ticket); dest=Path(dest)
    with lock(dest/'package.lock'):
        manifest=read(dest/'delivery_manifest.json')
        require(manifest.get('validated') is True and set(manifest['bundles'])==set(BUNDLES),'Incomplete validated package')
        # Verify all six before any upload; receipts only suppress unchanged uploads.
        for name in BUNDLES:
            require(sha(dest/name)==manifest['bundles'][name]['sha256'],'Bundle changed: '+name)
        for name in BUNDLES: upload_one(ticket,dest,name)
        if mark_success:
            dss('-dssp-tickets-set-progress',ticket,'0','1','1',critical=False)
            dss('-dssp-tickets-success',ticket)
        print('DELIVERY PASS: all six attachments published',flush=True)

def stage_input(source,run_dir):
    inputs=Path(run_dir)/'input'; inputs.mkdir(parents=True)
    code='''import sys,nibabel as n,numpy as np
p=n.load(sys.argv[1]); p=n.funcs.squeeze_image(p)
if p.ndim!=3 or any(x<=0 for x in p.shape): raise ValueError('Expected a single 3-D MRI')
if p.header.get_xyzt_units()[0]!='mm': raise ValueError('MRI must declare millimetres')
if not np.isfinite(p.affine).all() or abs(np.linalg.det(p.affine[:3,:3]))<1e-12: raise ValueError('Invalid geometry')
n.save(p,'/staged/case_0000.nii.gz')
'''
    docker([(source,'/source/'+Path(source).name,'ro'),(inputs,'/staged','rw')],
           ['/surface_env/bin/python','-','/source/'+Path(source).name],stdin=code,env=['LD_LIBRARY_PATH='])

def worker(root,once=False):
    preflight(True); root=Path(root).resolve(); root.mkdir(parents=True,exist_ok=True)
    with lock(root/'purple_provider.lock'):
        while True:
            code=service_hash(dss('-P','-dssp-services-list'))
            # Never retry an ambiguous claim: a timed-out request might already
            # have claimed a ticket. A second request could claim another one.
            try: claimed=run(['itksnap-wt','-dssp-services-claim',code,'purple',os.environ.get('DSS_PROVIDER_CODE','clam_v120')],timeout=60)
            except (subprocess.CalledProcessError,subprocess.TimeoutExpired):
                raise RuntimeError('Claim failed or timed out; inspect DSS ticket state before restarting the worker')
            matched=[m.groups() for m in re.finditer(r'^\s*1>\s+(\d+)\s+([a-f0-9]{40})\s+claimed\b',claimed,re.M)]
            if not matched:
                require(not re.search(r'^\s*1>\s+\d+',claimed,re.M), 'Unrecognized numeric claim response; inspect ticket state before restarting')
                if once: print('No ticket claimed'); return
                time.sleep(10); continue
            ticket,actual=matched[0]; require(actual==code,'Claim returned unexpected service')
            # Every claimed attempt gets a fresh directory, preserving older runs.
            work=Path(tempfile.mkdtemp(prefix='ticket_'+ticket+'_',dir=root)); run_dir=work/'run'; bundles=work/'downloads'
            try:
                download=work/'input_workspace'; download.mkdir()
                dss('-dssp-tickets-download',ticket,download,seconds=600)
                ws=list(download.rglob('*.itksnap')); require(len(ws)==1,'Expected one downloaded workspace')
                tagged={}
                for tag in TAGS:
                    try: found=records(run(['itksnap-wt','-P','-i',ws[0],'-llf',tag],timeout=30))
                    except subprocess.CalledProcessError: found=[]
                    existing=[]
                    for value in found:
                        p=Path(value); p=p if p.is_absolute() else ws[0].parent/p
                        if p.is_file(): existing.append(p.resolve())
                    require(len(existing)<=1,'Tag resolves to multiple images: '+tag)
                    tagged[tag]=existing[0] if existing else None
                source,contrast,hemi=select_input(tagged)
                require(download.resolve() in source.parents,'Tagged image lies outside downloaded workspace')
                write_json(work/'ticket.json',dict(ticket=ticket,service_hash=code,contrast=contrast,hemisphere=hemi))
                stage_input(source,run_dir)
                for stage in ('exvivo_dl','exvivo_surface'):
                    dss('-dssp-tickets-log',ticket,'info','Purple '+VERSION+': '+contrast+' '+hemi+' '+stage,critical=False)
                    pipeline(run_dir,contrast,hemi,stage)
                    if stage=='exvivo_dl':
                        package(run_dir,'case',contrast,hemi,bundles,False)
                        try: upload_one(ticket,bundles,BUNDLES[0])
                        except (subprocess.CalledProcessError,subprocess.TimeoutExpired):
                            print('Early segmentation delivery deferred until final delivery',flush=True)
                package(run_dir,'case',contrast,hemi,bundles)
                deliver(ticket,bundles,True)
                write_json(work/'completed.json',{'ticket':ticket,'status':'success'})
            except Exception as exc:
                write_json(work/'failure.json',{'ticket':ticket,'error':str(exc)})
                dss('-dssp-tickets-fail',ticket,'Purple failed; provider log retained. '+str(exc)[:300],critical=False)
                print('FAILED ticket',ticket,exc,'Work:',work,flush=True)
                if once: raise
            if once: return

def main():
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('preflight')
    q=sub.add_parser('package'); q.add_argument('--run-dir',type=Path,required=True); q.add_argument('--case',required=True)
    q.add_argument('--contrast',choices=('t2w','ciss','flash','t2star'),required=True)
    q.add_argument('--hemi',choices=('lh','rh'),required=True); q.add_argument('--output-dir',type=Path,required=True)
    q=sub.add_parser('run'); q.add_argument('--work-root',type=Path,default=Path('/data/pulkit/purple-dss/work/v120')); q.add_argument('--once',action='store_true')
    q=sub.add_parser('upload-only'); q.add_argument('ticket'); q.add_argument('directory',type=Path); q.add_argument('--mark-success',action='store_true')
    a=p.parse_args()
    if a.action=='preflight': preflight(True)
    elif a.action=='package':
        preflight(); require(not a.output_dir.exists(),'Choose a new packaging directory')
        package(a.run_dir,a.case,a.contrast,a.hemi,a.output_dir)
    elif a.action=='run': worker(a.work_root,a.once)
    else: deliver(a.ticket,a.directory,a.mark_success)

if __name__=='__main__':
    try: main()
    except Exception as exc: raise SystemExit('ERROR: '+str(exc))
