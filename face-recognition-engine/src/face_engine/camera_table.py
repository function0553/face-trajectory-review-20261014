"""Prepare P1 camera configuration from the local platform registration table.

This adapter reads files only. It never guesses stream URLs/passwords, probes a
camera, changes backend bindings, or starts an acquisition process.
"""
import argparse
import csv
import hashlib
import ipaddress
import json
from pathlib import Path

HEADERS=('平台摄像头ID','视频设备ID','平台设备名称','已核实设备IP','DB active标志','DB deleted标志')


def read_camera_table(path, *, sheet='平台注册通道', header_row=6):
    path=Path(path)
    if path.suffix.lower()=='.csv':
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream)
            if not set(HEADERS)<=set(reader.fieldnames or []):raise ValueError('camera table is missing required columns')
            return list(reader)
    if path.suffix.lower()!='.xlsx':raise ValueError('camera table requires .xlsx or .csv')
    if type(header_row) is not int or header_row<1:raise ValueError('positive header row required')
    try:from openpyxl import load_workbook
    except ImportError:raise RuntimeError('XLSX import requires the camera-table optional dependency') from None
    book=load_workbook(path,read_only=True,data_only=False)
    try:
        if sheet not in book.sheetnames:raise ValueError('camera table worksheet not found')
        rows=book[sheet].iter_rows(min_row=header_row,values_only=True)
        headers=next(rows,())
        if not set(HEADERS)<=set(headers):raise ValueError('camera table is missing required columns')
        if len([h for h in headers if h])!=len(set(h for h in headers if h)):
            raise ValueError('duplicate camera table column')
        return [dict(zip(headers,row)) for row in rows if any(v is not None and v!='' for v in row)]
    finally:book.close()


def _flag(value):
    if value in (0,'0',False):return False
    if value in (1,'1',True):return True
    return None


def compile_camera_rows(rows, context, source_id):
    if not isinstance(source_id,str) or not source_id.strip():raise ValueError('stable source_id required')
    if context is not None:
        for key in ('activity_id','activity_num','route_rule_id'):
            if not isinstance(context.get(key),str) or not context[key].strip():raise ValueError('backend activity context is incomplete')
        for key in ('config_version','binding_version','monitoring_version'):
            if type(context.get(key)) is not int or context[key]<0:raise ValueError('explicit backend activity versions required')
    cameras=[];bindings=[];catalog=[];pending=[];seen={};deleted=0;duplicates=0
    for line,row in enumerate(rows,1):
        camera=row.get(HEADERS[0])
        if not isinstance(camera,str) or not camera or camera!=camera.strip() or camera.startswith('=') or any(ord(c)<32 or ord(c)==127 for c in camera):
            raise ValueError(f'platform camera ID must be explicit opaque text (record {line})')
        name=row.get(HEADERS[2]) or None
        if name is not None and (not isinstance(name,str) or name.startswith('=')):
            raise ValueError(f'camera name must be literal text (record {line})')
        record=dict(camera_id=camera,camera_name=name,video_device_id=row.get(HEADERS[1]),
                    host=row.get(HEADERS[3]),snapshot_active=row.get(HEADERS[4]),snapshot_deleted=row.get(HEADERS[5]))
        if camera in seen:
            if seen[camera]!=record:raise ValueError('conflicting camera_id in local table')
            duplicates+=1;continue
        seen[camera]=record;catalog.append(record)
        removed=_flag(row.get(HEADERS[5]))
        if removed is True:deleted+=1;continue
        if removed is None:
            pending.append(dict(camera_id=camera,reason='UNKNOWN_DELETION_STATE'));continue
        try:host=str(ipaddress.ip_address(str(record['host']).strip()))
        except ValueError:
            pending.append(dict(camera_id=camera,reason='DEVICE_IP_UNVERIFIED'));continue
        record['host']=host
        prefix='FACE_CAMERA_'+hashlib.sha256(camera.encode()).hexdigest()[:20].upper()
        record['sub_url_env']=prefix+'_SUB_URL';record['main_url_env']=prefix+'_MAIN_URL'
        # Historical active is an online snapshot, not permission to exclude a
        # camera. Keep IDs distinct even when names or NVR addresses are shared.
        if context is not None:
            cameras.append(dict(camera_id=camera,source_id=source_id,activity_context=dict(context),role='interior',
                sub_url_env=record['sub_url_env'],main_url_env=record['main_url_env'],
                backend='ffmpeg',sample_fps=10,clock_profile_version=None))
            bindings.append(dict(source_id=source_id,device_id=camera,kind='CAMERA',
                route_rule_id=context['route_rule_id'],camera_name=name))
    return dict(catalog=catalog,config=dict(schema_version=1,cameras=cameras) if context is not None else None,
        bindings=bindings,pending=pending,deleted=deleted,duplicates=duplicates,runtime_ready=False,
        remaining=['BACKEND_ACTIVITY_CONTEXT']*(context is None)+['STREAM_CONNECTIONS','QUALIFIED_CAPTURE_CLOCK','BACKEND_PERSON_ROSTER'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--table',type=Path,required=True)
    parser.add_argument('--sheet',default='平台注册通道')
    parser.add_argument('--header-row',type=int,default=6)
    parser.add_argument('--activity-context',type=Path,help='JSON with actual backend activity IDs, three versions and route_rule_id')
    parser.add_argument('--source-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    context=json.loads(args.activity_context.read_text(encoding='utf-8-sig')) if args.activity_context else None
    result=compile_camera_rows(read_camera_table(args.table,sheet=args.sheet,header_row=args.header_row),context,args.source_id)
    args.output.mkdir(parents=True,exist_ok=False)
    def save(name,value):
        (args.output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    save('camera-catalog.json',result['catalog']);save('pending.json',result['pending'])
    if result['config'] is not None:
        save('cameras.json',result['config']);save('camera-bindings.json',result['bindings'])
    template=['# Fill each verified stream URL from the existing credential/connection configuration.']
    for camera in result['catalog']:
        if 'sub_url_env' in camera:
            template.extend([f"# camera_id={camera['camera_id']} host={camera['host']}",
                f"$env:{camera['sub_url_env']} = ''",f"$env:{camera['main_url_env']} = ''"])
    (args.output/'connections.template.ps1').write_text('\n'.join(template)+'\n',encoding='utf-8')
    report=dict(input=str(args.table.resolve()),input_sha256=hashlib.sha256(args.table.read_bytes()).hexdigest(),
        sheet=args.sheet,header_row=args.header_row,source_id=args.source_id,rows=len(result['catalog']),
        verified_ip_count=sum('sub_url_env' in x for x in result['catalog']),pending_count=len(result['pending']),
        deleted=result['deleted'],duplicate_rows=result['duplicates'],runtime_ready=False,remaining=result['remaining'])
    save('import-report.json',report)
    print(json.dumps(report,ensure_ascii=True))

if __name__=='__main__':main()
