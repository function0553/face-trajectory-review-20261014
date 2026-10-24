"""Prepare complete initial activity/person requests using the deployed P1 extractor.

Run from the P1 project directory using its Python environment. This example
only writes local JSON; submit with the platform's signed algorithm credential.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('model-config','photo','activity-id','activity-num','venue-id','source-id','camera-id',
                 'person-id','person-name','profile-photo-url','output'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--camera-name',default=None,help='Deprecated; PERSON_TRACK_EVENTS reads names from the camera device')
    args=parser.parse_args()
    # Stable platform IDs and filenames are inputs, never inferred from names.
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'face-recognition-engine'/'src'))
    from face_engine.config import load_config
    from face_engine.monitoring_service import decode_image
    from face_engine.trajectory_bridge import TrajectoryExtractor
    extractor=TrajectoryExtractor.from_config(load_config(args.model_config))
    raw=Path(args.photo).read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    scope=dict(activity_id=args.activity_id,activity_num=args.activity_num,config_version=1,binding_version=1,monitoring_version=1)
    metadata=dict(scope,source_id=args.source_id,camera_id='enrollment',frame_id=digest,
                  time_source='UNKNOWN',purpose='GALLERY_ENROLLMENT')
    detail=extractor.extract_detailed([(decode_image(raw),metadata)])[0]
    if detail['detected_faces']!=1 or len(detail['observations'])!=1:
        raise ValueError('registration requires exactly one detected, quality-qualified face')
    template_id='enrolled:'+hashlib.sha256(json.dumps([args.activity_id,args.person_id,1,digest],
        separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    requests={
        '01-activity.json':dict(activity_id=args.activity_id,activity_num=args.activity_num,venue_id=args.venue_id,
            config_version=1,expected_config_version=0,configuration=dict(output_mode='PERSON_TRACK_EVENTS')),
        '02-bindings.json':dict(activity_id=args.activity_id,config_version=1,binding_version=1,expected_binding_version=0,
            bindings=[dict(source_id=args.source_id,device_id=args.camera_id,kind='CAMERA',route_rule_id='PERSON-TRACK',camera_name=None)]),
        '03-monitoring.json':dict(activity_id=args.activity_id,config_version=1,binding_version=1,monitoring_version=1,
            expected_monitoring_version=0,enabled=True),
        '04-person.json':dict(scope,person_id=args.person_id,person_name=args.person_name,profile_photo_url=args.profile_photo_url,
            category='PRIORITY',revision=1,expected_revision=0,evidence_id='profile-sha256:'+digest,
            templates=[dict(template_id=template_id,feature_space=extractor.feature_space,embedding=detail['observations'][0]['embedding'])])
    }
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=False)
    for name,body in requests.items():
        (output/name).write_text(json.dumps(body,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps(dict(prepared=True,registered=False,output=str(output),templates=1),ensure_ascii=False))

if __name__=='__main__':main()
