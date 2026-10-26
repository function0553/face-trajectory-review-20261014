"""2026-10-26 review fixes against real C++ workers and isolated SQLite (synthetic vectors only).

P1  HOLD review bubbles are read through a target index; results equal the
    previous full scan (differential run against --baseline-worker), databases
    written by the previous worker are indexed once, retention drops index rows.
P2  PERSON_TRACK_EVENTS never creates automatic identities: a poor first
    capture of a registered person can no longer become a clone that absorbs
    later good captures. Clones left by the previous worker are ignored.
    TRAJECTORY activities still create automatic identities.
P3  The worker push contract is unchanged; P3 is handled in P1 (see
    face-recognition-engine/tests/test_delivery_capacity.py). A full activity
    queue still refuses with CAPACITY at 256 events, push_status and the
    retention report keep their fields, and no worker operation was added
    (compared with --baseline-worker when given).
"""
import argparse
import json
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

P2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P2 / 'evidence/2026-10-10-activity-retention'))
from verify_core import T0, SPACE, GRAPH, iso, scope, profile, clock

POLICY = json.loads((P2.parent / 'face-recognition-engine/examples/monitoring-platform-v41.example.json')
                    .read_text(encoding='utf-8'))['policy']
NOW = T0 + 100000000
PHOTO = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='  # 1x1 PNG
RETENTION_MS = 7 * 86400000  # ActivityStore::RetentionMs
RETENTION_NOW = NOW + RETENTION_MS + 86400000  # beyond every event's and capture's deadline


def unit(coefficients):
    vector = [0.0] * 512
    for index, value in coefficients.items():
        vector[index] = value
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


P, Q = unit({0: 1}), unit({1: 1})
POOR = unit({0: .30, 1: 0, 2: math.sqrt(1 - .09)})                     # 0.30 vs P
_b = (.80 - .30 * .62) / math.sqrt(.91)
GOOD = unit({0: .62, 2: _b, 3: math.sqrt(1 - .62 ** 2 - _b ** 2)})      # 0.62 vs P, 0.80 vs POOR
H1 = unit({0: .42, 5: math.sqrt(1 - .42 ** 2)})                         # inconclusive for P
H2 = unit({0: .45, 1: .44, 6: math.sqrt(1 - .45 ** 2 - .44 ** 2)})      # inconclusive P/Q
H4 = unit({0: .40, 7: math.sqrt(1 - .40 ** 2)})                         # inconclusive, unlike corrected h1


class Worker:
    def __init__(self, binary, database, now=NOW):
        self.now = now
        self.process = subprocess.Popen([str(binary), '--state-file', str(database), '--push-results', 'true',
                                         '--retention-enabled', 'true'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding='utf-8')

    def call(self, body, ok=True):
        self.process.stdin.write(json.dumps(dict(body, _trusted_clock=clock(self.now)), ensure_ascii=False) + '\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(self.process.stderr.read())
        result = json.loads(line)
        if ok is not None and bool(result.get('ok')) != ok:
            raise AssertionError((body.get('op'), result))
        return result

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=60)


def trajectory_activity(w, a, persons):
    w.call(dict(op='activity_sync', activity_id=a, activity_num='NUM-' + a, venue_id='V', config_version=1,
                expected_config_version=0, configuration={}))
    w.call(dict(op='activity_bindings', activity_id=a, config_version=1, binding_version=1, expected_binding_version=0,
                bindings=[dict(source_id='S', device_id='C1', kind='CAMERA', route_rule_id='ROUTE-' + a, camera_name=None)]))
    w.call(dict(op='activity_monitoring', activity_id=a, config_version=1, binding_version=1, monitoring_version=1,
                expected_monitoring_version=0, enabled=True))
    for person, embedding in persons.items():
        w.call(dict(scope(a), op='monitoring_person_put', person_id=person, person_name='模拟' + person,
                    profile_photo_url='https://photos.example.test/p.jpg', category='PRIORITY', revision=1,
                    expected_revision=0, evidence_id='SYNTHETIC:import',
                    templates=[dict(template_id='photo:' + person, feature_space=SPACE, embedding=embedding)]))


def trajectory_observe(a, event, t, embedding):
    return dict(scope(a), op='monitoring_observe', route_rule_id='ROUTE-' + a, graph=GRAPH, policy=POLICY,
                observation=dict(observation_id=event, source_id='S', camera_id='C1', frame_id='f:' + event,
                                 feature_space=SPACE, embedding=embedding, detection_score=.99, face_size_px=96),
                time_record=dict(profile_version='good', raw_time=iso(t)))


def platform_activity(w, a):
    w.call(dict(op='platform_v4_sync', activity_id=a, body=dict(activityName='SYN', activityNum='NUM-' + a, activityState='0',
                start_unix_ms=T0, end_unix_ms=T0 + 10 * 86400000, cameras=[dict(deviceId='C1', deviceName='后端C1')])))
    w.call(dict(op='platform_v4_person', activity_id=a, person=dict(personId='P', personName='模拟P', personIdNum='SYN-P',
                personPhoto='https://photos.example.test/P.jpg', photo_sha256='SYN:P',
                templates=[dict(template_id='photo:P', feature_space=SPACE, embedding=P)])))
    w.call(dict(op='platform_v4_monitoring', activity_id=a, body=dict(enabled=True)))


def platform_observe(a, event, t, embedding):
    return dict(activity_id=a, activity_num='NUM-' + a, config_version=1, binding_version=1, monitoring_version=1,
                op='monitoring_observe', route_rule_id='R', observation=dict(
                    observation_id=event, source_id='S', camera_id='C1', camera_name='x', frame_id='f:' + event,
                    feature_space=SPACE, embedding=embedding, detection_score=.99, face_size_px=96,
                    capture_photo_base64=PHOTO),
                time_record=dict(profile_version='good', raw_time=iso(t)), policy=POLICY)


def rows(database, namespace, activity=None):
    with sqlite3.connect(database) as connection:
        found = connection.execute('SELECT ns,key,value,ref,track FROM state').fetchall()
    def match(ns):
        try:
            value = json.loads(ns)
        except ValueError:
            value = ns
        if activity is None:
            return value == namespace
        return value == ['activity', activity, namespace]
    return [row for row in found if match(row[0])]


def indexed(database, activity):
    """(target, observation_id) pairs; activity receipts are keyed [source_id, observation_id]."""
    return {(json.loads(r[1])[0], json.loads(r[4])[-1]) for r in rows(database, 'monitoring_held_candidate', activity)}


def held(w, a, target):
    return w.call(dict(scope(a), op='monitoring_get', target_id=target))['monitoring']['held_candidates']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--baseline-worker', type=Path, help='previous worker for differential and upgrade checks')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    worker, baseline = args.worker.resolve(), args.baseline_worker.resolve() if args.baseline_worker else None
    checks = []

    def check(name, value, detail=None):
        checks.append(dict(name=name, passed=bool(value), **({'detail': detail} if detail is not None and not value else {})))
        if not value:
            raise AssertionError((name, detail))

    def bubbles(w):
        """Same requests for every worker: HOLD receipts, a confirmation, a correction."""
        out = []
        w.call(profile_request)
        trajectory_activity(w, 'H', {'P': P, 'Q': Q})
        for event, t, embedding in (('h1', T0 + 1000, H1), ('h2', T0 + 2000, H2), ('m1', T0 + 3000, P)):
            out.append(w.call(trajectory_observe('H', event, t, embedding))['status'])
        out += [held(w, 'H', 'P'), held(w, 'H', 'Q')]
        return out

    def continue_bubbles(w):
        out = []
        w.call(dict(scope('H'), op='association_correct', source_id='S', observation_id='h1', target_id='P',
                    evidence_id='SYNTHETIC:verified:P'))
        out.append(held(w, 'H', 'P'))
        out.append(w.call(trajectory_observe('H', 'h4', T0 + 4000, H4))['status'])
        out += [held(w, 'H', 'P'), held(w, 'H', 'Q')]
        return out

    profile_request = profile()
    profile_request['profile']['camera_id'] = 'C1'
    failure = None
    try:
        # ---------------- P1: review bubbles through the index ----------------
        new_db = args.output / 'p1-new.sqlite'
        w = Worker(worker, new_db)
        first = bubbles(w)
        check('p1_hold_receipts_and_confirmed_match', first[:3] == ['HOLD', 'HOLD', 'ACCEPTED'], first[:3])
        # Candidates are the visual Top8, so a small gallery lists every person.
        check('p1_bubbles_name_each_candidate_target', [h['observation_id'] for h in first[3]] == ['h1', 'h2']
              and [h['observation_id'] for h in first[4]] == ['h1', 'h2'], first[3:5])
        rest = continue_bubbles(w)
        check('p1_corrected_receipt_leaves_bubble_and_new_hold_appears',
              [h['observation_id'] for h in rest[0]] == ['h2'] and [h['observation_id'] for h in rest[2]] == ['h2', 'h4']
              and [h['observation_id'] for h in rest[3]] == ['h2', 'h4'], rest)
        w.close()
        index = indexed(new_db, 'H')
        check('p1_index_rows_cover_each_target_candidate', index >= {
            ('P', 'h1'), ('P', 'h2'), ('Q', 'h1'), ('Q', 'h2'), ('P', 'h4')} and ('P', 'm1') not in index, sorted(index))
        check('p1_index_marked_ready', rows(new_db, 'monitoring_meta', 'H') and any(
            r[1] == 'held_candidate_index_v1' for r in rows(new_db, 'monitoring_meta', 'H')))
        if baseline:
            base_db = args.output / 'p1-baseline.sqlite'
            b = Worker(baseline, base_db)
            expected = bubbles(b) + continue_bubbles(b)
            b.close()
            check('p1_results_equal_previous_full_scan', first + rest == expected, dict(new=first + rest, baseline=expected))
            # Upgrade: a database written by the previous worker has no index rows.
            upgrade_db = args.output / 'p1-upgrade.sqlite'
            b = Worker(baseline, upgrade_db)
            before = bubbles(b)
            b.close()
            check('p1_previous_worker_wrote_no_index', not rows(upgrade_db, 'monitoring_held_candidate', 'H'))
            w = Worker(worker, upgrade_db)
            migrated = [held(w, 'H', 'P'), held(w, 'H', 'Q')]
            check('p1_upgrade_reads_same_bubbles', migrated == before[3:5], dict(migrated=migrated, before=before[3:5]))
            after = continue_bubbles(w)
            w.close()
            check('p1_upgraded_database_continues_identically', after == rest, dict(after=after, fresh=rest))
            check('p1_upgrade_indexed_old_receipts', indexed(upgrade_db, 'H') >= {('P', 'h1'), ('P', 'h2'), ('Q', 'h2')})

        # ---------------- P2: priority-person tracking has no automatic identities ----------------
        p2_db = args.output / 'p2-new.sqlite'
        w = Worker(worker, p2_db)
        w.call(profile_request)
        platform_activity(w, 'A')
        poor = w.call(platform_observe('A', 'a1-poor', T0 + 1000, POOR))
        check('p2_poor_capture_is_held_without_identity', poor['status'] == 'HOLD' and poor['association']['decision'] == 'UNASSIGNED'
              and poor['association']['target_id'] is None and poor['association']['candidates'] == []
              and 'FACE_UNASSIGNED' in poor['reason_codes'], poor.get('association'))
        good = w.call(platform_observe('A', 'a2-good', T0 + 30000, GOOD))
        check('p2_later_good_capture_matches_registered_person', good['status'] == 'ACCEPTED'
              and good['association']['decision'] == 'MATCHED' and good['association']['target_id'] == 'P', good.get('association'))
        head = w.call(dict(op='push_next'))['event']
        check('p2_registered_person_is_pushed', head and head['person_track_event']['person_id'] == 'P'
              and head['person_track_event']['observation_id'] == 'a2-good', head)
        w.call(dict(op='push_ack', event_id=head['event_id']))
        check('p2_no_automatic_identity_in_priority_activity', not any(
            'auto:' in r[1] for r in rows(p2_db, 'association_target', 'A')))
        check('p2_held_poor_capture_is_not_a_review_bubble', held(w, 'A', 'P') == [])
        trajectory_activity(w, 'L', {'P': P})
        stranger = w.call(trajectory_observe('L', 'l1', T0 + 1000, unit({9: 1})))
        check('p2_trajectory_mode_still_creates_automatic_identity', stranger['status'] == 'ACCEPTED'
              and stranger['association']['decision'] == 'CREATED'
              and stranger['association']['target_id'].startswith('auto:'), stranger.get('association'))
        w.close()
        if baseline:
            clone_db = args.output / 'p2-upgrade.sqlite'
            b = Worker(baseline, clone_db)
            b.call(profile_request)
            platform_activity(b, 'C')
            old = b.call(platform_observe('C', 'c1-poor', T0 + 1000, POOR))
            b.close()
            check('p2_previous_worker_created_clone', old['association']['decision'] == 'CREATED')
            w = Worker(worker, clone_db)
            good = w.call(platform_observe('C', 'c2-good', T0 + 30000, GOOD))
            w.close()
            check('p2_existing_clone_no_longer_absorbs_registered_person', good['association']['decision'] == 'MATCHED'
                  and good['association']['target_id'] == 'P', good.get('association'))

        # ---------------- P3: worker push contract unchanged (handled in P1) ----------------
        def full_queue(binary, database):
            w = Worker(binary, database)
            w.call(profile_request)
            trajectory_activity(w, 'F', {'P': P})
            setup_pending = w.call(dict(op='push_status'))['push']['pending']  # person registration events
            for i in range(256 - setup_pending):
                w.call(trajectory_observe('F', 'f%03d' % i, T0 + 1000 + i, P))
            blocked = w.call(trajectory_observe('F', 'f-next', T0 + 2000, P), ok=False)
            status = w.call(dict(op='push_status'))['push']
            unknown = w.call(dict(op='push_reject', event_id='x', reason='x'), ok=False)
            w.close()
            w = Worker(binary, database, RETENTION_NOW)
            report = w.call(dict(op='retention_run', dry_run=False))
            w.close()
            return dict(error=blocked.get('error'), pending=status['pending'], status_fields=sorted(status),
                        unknown_operation=unknown.get('error'), retention_report_fields=sorted(report))
        p3 = full_queue(worker, args.output / 'p3-new.sqlite')
        check('p3_full_activity_queue_still_refuses_with_capacity', str(p3['error']).startswith('CAPACITY:')
              and p3['pending'] == 256, p3)
        check('p3_no_dead_letter_fields', not any('dead_letter' in f for f in p3['status_fields'] + p3['retention_report_fields']), p3)
        if baseline:
            expected = full_queue(baseline, args.output / 'p3-baseline.sqlite')
            check('p3_push_contract_equals_previous_worker', p3 == expected, dict(new=p3, baseline=expected))
        w = Worker(worker, new_db, RETENTION_NOW)
        w.call(dict(op='retention_run', dry_run=False))
        w.close()
        check('p1_retention_removes_index_rows_with_receipts', not rows(new_db, 'monitoring_held_candidate', 'H')
              and not rows(new_db, 'monitoring_observation', 'H'))
    except Exception as error:  # report every completed check
        failure = repr(error)
    report = dict(passed=failure is None and all(c['passed'] for c in checks), failure=failure,
                  check_count=len(checks), baseline=bool(baseline), checks=checks)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(dict(passed=report['passed'], check_count=len(checks), failure=failure), ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
