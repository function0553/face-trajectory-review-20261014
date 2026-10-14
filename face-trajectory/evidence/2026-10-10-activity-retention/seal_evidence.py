"""Verify the recorded final evidence and seal local source/artifact hashes.

This does not run retention, connect to production, or open any state database.
Run after the build and isolated suites listed in the two existing HANDOFFs.
"""
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
P2 = HERE.parents[1]
WORKSPACE = P2.parent
P1 = WORKSPACE / 'face-recognition-engine'
P1_EVIDENCE = P1 / 'evidence/2026-10-10-activity-retention'
TAG = 'final-03'
SUITE_DIRS = {name: f'{name}-{TAG}' for name in ['core', 'service', 'retention', 'edges', 'outbox', 'push']}
SUITE_DIRS['push'] = 'push-final-04'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    data = path.read_bytes()
    return data.decode('utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')


def load(path):
    return json.loads(read(path))


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def relative(path):
    return path.resolve().relative_to(WORKSPACE).as_posix()


def slug(text):
    text = re.sub(r'<[^>]*>', '', text).strip().lower()
    text = ''.join(c for c in text if c in '_- ' or not unicodedata.category(c).startswith(('P', 'S')))
    return text.replace(' ', '-')


def check_links(files):
    checked, errors = [], []
    for path in files:
        text = read(path)
        if path.name == 'HANDOFF.md':
            marker = '## 27.' if path.parent == P2 else '## 18.'
            text = text[text.index(marker):]
        text = re.sub(r'```[\s\S]*?```', '', text)
        for destination in re.findall(r'\[[^\]\n]*\]\(([^)\n]+)\)', text):
            destination = destination.strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*://', destination):
                continue
            name, _, anchor = destination.partition('#')
            target = (path.parent / unquote(name)).resolve() if name else path
            item = {'source': relative(path), 'target': destination}
            if not target.exists():
                errors.append(dict(item, reason='MISSING_TARGET'))
            elif anchor and target.suffix == '.md':
                content = read(target)
                anchors = {slug(title) for title in re.findall(r'^#{1,6}\s+(.+)$', content, re.M)}
                anchors.update(re.findall(r'<a\s+id=["\']([^"\']+)["\']', content))
                if unquote(anchor) not in anchors:
                    errors.append(dict(item, reason='MISSING_ANCHOR'))
            checked.append(item)
    report = {'passed': not errors, 'files_checked': [str(path) for path in files],
              'links_checked': len(checked), 'errors': errors,
              'source_sha256': {str(path): digest(path) for path in files},
              'scope': 'Affected current documents and P2 section27/P1 section18; local files and explicit/heading anchors only.'}
    write(HERE / 'docs-link-validation-final.json', report)
    assert not errors, errors
    return report


def main():
    worker = P2 / 'cpp/build-activity/Release/face_trajectory_worker.exe'
    service = HERE / 'face-trajectoryd.exe'
    adapter = HERE / 'p1-retention-test/build/Release/p1_retention_test.exe'
    binaries = {relative(path): digest(path) for path in (worker, service, adapter)}
    summaries, proofs = {}, {}
    for suite, expected in [('core', 40), ('service', 31), ('retention', 81), ('edges', 45), ('outbox', 5), ('push', 12)]:
        path = HERE / SUITE_DIRS[suite] / 'summary.json'
        result = load(path)
        assert result['passed'] == expected and not result.get('failure') and not result.get('failures')
        assert not result.get('failed') and all(c['passed'] for c in result['checks'])
        assert result['worker_sha256'] == digest(worker), (suite, 'WORKER_HASH_MISMATCH')
        if 'service_sha256' in result:
            assert result['service_sha256'] == digest(service), (suite, 'SERVICE_HASH_MISMATCH')
        summaries[suite] = {'evidence': relative(path), 'checks_passed': expected, 'worker_sha256': result['worker_sha256']}
        for check in result['checks']:
            proofs[f'{suite}:{check["name"]}'] = {'evidence': relative(path), 'check': check['name']}
    for name, expected in [('scope', 12), ('retention', 10), ('maintenance', 8), ('dependency', 7)]:
        path = P1_EVIDENCE / f'p1-{name}-result.json'
        result = load(path)
        assert result.get('ok', result.get('status') == 'PASS') and len(result['checks']) == expected
        if name != 'scope':
            assert result['adapter_sha256'] == digest(adapter), (name, 'ADAPTER_HASH_MISMATCH')
        if name == 'scope':
            for name_in_source, value in result['source_sha256'].items():
                assert digest(P1 / name_in_source) == value, (name_in_source, 'P1_SOURCE_CHANGED')
        if name == 'dependency':
            assert any(r.get('worker_sha256') == digest(worker) for r in result['reports'])
        summaries[f'p1_{name}'] = {'evidence': relative(path), 'checks_passed': expected}
    api = load(HERE / 'openapi-validation-final.json')
    assert api['passed'] and api['full_openapi_spec_validation'] and api['openapi_sha256'] == digest(P2 / 'openapi.yaml')
    assert api['validator_sha256'] == digest(P2 / 'docs/validate_activity_contract.py')
    assert api['recorded_http_schema_checks'][0]['calls'] == str((HERE / f'service-{TAG}/calls.json').resolve())
    prune = load(HERE / 'prune-test/result.json')
    assert prune['passed'] and len(prune['checks']) == 18
    assert 'PASS state scoped isolation' in read(HERE / 'state-view-test/result.log')
    go_log = read(HERE / 'go-final-tests.log')
    assert '--- FAIL:' not in go_log and len(re.findall(r'^ok\s+face-trajectory/', go_log, re.M)) == 5
    go_summary = {'status': 'PASS', 'build_exit_code': 0, 'vet_exit_code': 0, 'test_exit_code': 0,
                  'build_vet_evidence': 'Successful tool executions recorded in the goal; repeat commands are in HANDOFF section27.',
                  'top_level_tests_passed': len(re.findall(r'^--- PASS:', go_log, re.M)),
                  'subtests_passed': len(re.findall(r'^\s+--- PASS:', go_log, re.M)),
                  'packages_passed': 5, 'evidence': 'go-final-tests.log'}
    write(HERE / 'go-final-summary.json', go_summary)
    docs_report = load(HERE / 'docs-link-validation-final.json')
    docs = [Path(path) for path in docs_report['files_checked']]
    for handoff in [P2 / 'HANDOFF.md', P1 / 'HANDOFF.md']:
        if handoff not in docs:
            docs.append(handoff)
    check_links(docs)

    rows = []
    def requirement(ids, description, checks=(), sources=()):
        for key in checks:
            assert key in proofs, ('MISSING_BEHAVIOR_PROOF', key)
        for source in sources:
            assert (P2 / source).exists(), source
        rows.append({'requirement_ids': ids.split(','), 'requirement': description,
                     'status': 'VERIFIED_LOCAL', 'behavior_proofs': [proofs[key] for key in checks],
                     'current_sources': [relative(P2 / source) for source in sources]})
    requirement('2.activities.1,2.activities.2,4.activity.1,3.stage2.3', 'A/B查询、旧响应与旧通知按活动和用户上下文识别丢弃', ['service:client_switch_discards_old_reply', 'service:client_switch_discards_old_push', 'service:subscription_actual_notification'], ['go/internal/api/subscriptions.go'])
    requirement('2.activities.3,2.activities.5,3.stage2.5,4.activity.2', '选择、创建与启停独立，退出订阅不停止后台', ['core:creation_not_start_A', 'service:logout_does_not_stop_monitor', 'service:query_cannot_start'], ['cpp/src/activity.cpp'])
    requirement('2.activities.4,2.data.3,2.data.4,3.stage1.2,3.stage1.8,4.activity.3,4.activity.5,4.activity.12,4.activity.14', '并行用户、同人类别/会话/轨迹及EXIT/停止A与B隔离，人员ID不改写', ['service:simultaneous_users_context_isolation', 'core:independent_categories', 'core:physical_activity_namespaces', 'core:A_exit_leaves_B', 'core:stop_A_not_B', 'core:activity_trajectory_only_B'], ['cpp/src/state.cpp', 'cpp/src/worker.cpp'])
    requirement('2.activities.6,2.activities.8,3.stage1.5,3.stage2.1,4.activity.10', '新活动CAS同步、设备范围修订及明确启停，无须改核心，不按计划结束自动停', ['core:new_binding_ingests_without_code_change', 'core:old_binding_new_event_quarantined', 'core:planned_end_not_stop', 'core:stale_config_isolated', 'core:stopped_late_intake_hold'], ['docs/activity-service.md', 'collection/activity-service.example.json'])
    requirement('2.activities.7,2.data.5,2.data.6,3.stage1.7,4.activity.11', '不猜最新活动、不按共享场地扇出；缺归属保持隔离', ['core:shared_device_no_fanout', 'core:unknown_activity_reject', 'core:no_legacy_observation_write', 'core:event_authoritative_mapping_conflict_hold'], ['cpp/src/activity.cpp'])
    requirement('2.activities.9,3.stage1.1,3.stage2.6,4.activity.4', '签名身份/动作覆盖读写订阅，裸参数不能越权', ['service:missing_identity_rejected', 'service:cross_activity_denied_/v1/activities/get', 'service:cross_activity_denied_/v1/monitoring/get', 'service:cross_activity_denied_/v1/subscriptions/open', 'service:cross_activity_denied_/v1/activities/monitoring'], ['go/internal/trust/auth.go', 'go/internal/api/server.go'])
    requirement('2.data.1,2.data.2', '活动id、唯一num与venue权威映射，矛盾编号拒绝或入流隔离', ['core:mapping_immutable', 'core:query_authoritative_mapping_conflict_rejected', 'service:activity_query_mapping_conflict_rejected', 'service:monitoring_query_mapping_conflict_rejected'], ['cpp/src/activity.cpp'])
    requirement('2.data.7,2.data.8,2.data.8.a,2.data.8.b,2.data.8.c,2.data.8.d,2.data.8.e', '登记图库与动态状态分开；保留统一身份标准、优先/普通配额、匿名轨迹、漏入口和明确人脸出闸', ['core:unregistered_anonymous_trajectory', 'core:A_exit_closes_A', 'core:anonymous_context_retains_business_id'], ['cpp/src/gallery.cpp', 'cpp/src/association.cpp', 'cpp/src/monitoring.cpp', '../face-recognition-engine/src/face_engine/scheduling.py', '../face-recognition-engine/src/face_engine/monitoring_service.py'])
    requirement('2.data.9.a,2.data.9.b,4.activity.16,4.activity.17', '核验相机名义UTC、60秒误差准入及重叠顺序歧义', ['core:time_over60_hold', 'core:unverified_time_hold', 'core:unknown_time_hold', 'core:admitted_overlap_preserved', 'core:untrusted_server_reference_denied', 'edges:retention_preserves_crossing_interval_order_ambiguity'], ['cpp/src/clock_adapter.cpp', 'go/internal/trust/clock.go'])
    requirement('2.data.10', '约600路/3090优先/2秒目标3秒可接受作为设计约束记录，明确尚未容量验收', sources=['HANDOFF.md', '../face-recognition-engine/docs/monitoring-runtime.md', '../face-recognition-engine/examples/monitoring.example.json'])
    requirement('3.stage1.3,3.stage1.4,3.stage2.4,4.activity.6,4.activity.7,4.activity.8', 'activity/source/event幂等，同内容不重复副作用、异内容409及冷启动重放隔离', ['core:same_person_cross_activity', 'core:same_event_other_source', 'core:same_key_retry', 'core:same_key_changed_conflict', 'service:restart_idempotency', 'retention:live_retry_has_no_persisted_business_side_effect'], ['cpp/src/activity.cpp'])
    requirement('3.stage1.6,4.activity.9', '按活动轮转与持久推送配额，失败活动退避，保持其他活动处理机会', checks=[key for key in proofs if key.startswith(('outbox:', 'push:'))], sources=['go/internal/worker/fair.go', 'go/internal/push/sender.go', 'cpp/src/worker.cpp'])
    requirement('4.activity.13,4.activity.15', '迟到EXIT不关闭新会话，重启恢复各活动状态', ['core:late_exit_not_close_new_session', 'core:restart_isolated_monitoring', 'service:restart_lifecycle_A', 'service:restart_lifecycle_B', 'edges:late_exit_after_prune_does_not_close_new_session'])
    requirement('3.stage3.1,3.stage3.2,3.stage3.3,4.retention.1', '可信UTC和名义capture唯一截止口径，等于168小时保留', ['retention:known_capture_deadline_uses_capture_only', 'retention:capture_equal_cutoff_retained', 'retention:capture_before_cutoff_removed'], ['cpp/src/retention.cpp', 'go/internal/trust/clock.go'])
    requirement('3.stage3.4,3.stage3.5,4.retention.7,4.retention.8', '清理所有历史版本、共享节点、索引、缓存、投递与重放源；有效引用不损坏', ['retention:all_historical_versions_scrubbed_1', 'retention:legacy_refs_v1_live_shared_object_retained', 'retention:expired_marker_absent_all_state_columns', 'retention:subscription_replay_has_no_expired_markers', 'edges:live_gate_own_capture_history_retained_with_scrubbed_expired_dependency'], ['cpp/src/trajectory.cpp', 'cpp/src/retention.cpp', 'cpp/src/p1_retention.cpp'])
    requirement('3.stage3.6,4.retention.2,4.retention.3,4.retention.9', '过期补传/重复/恢复不复活，窗口内重试无重复副作用、异内容冲突', ['retention:expired_duplicate_cannot_resurrect', 'retention:expired_late_new_key_rejected', 'retention:expired_restart_retry_cannot_resurrect', 'retention:live_retry_remains_idempotent', 'retention:live_retry_changed_content_conflicts'])
    requirement('3.stage3.7,3.stage3.12,4.retention.4,4.retention.5', '未知/超限首次隔离最多168小时，不借重试、迁移或未来时间续期；未归属历史同样有界', ['retention:unknown_first_quarantine_unchanged', 'retention:quarantine_one_ms_after_deadline_expired', 'retention:unknown_restart_retry_cannot_renew', 'retention:changed_unknown_requarantine_cannot_renew_key', 'retention:legacy_orphan_held_intake_does_not_gain_new_lease', 'edges:future_timestamp_cannot_extend_quarantine_past_168_hours'], ['cpp/src/p1_retention.cpp'])
    requirement('3.stage3.8,4.retention.6', '持续清理、指标可观测，真实kill/读事务阻塞后恢复，物理未完成不会伪称success', ['service:periodic_real_cleanup_executes', 'service:http_retention_observable', 'retention:actual_process_kill_recovers_partial_transaction', 'retention:terminated_process_cleanup_resumes', 'edges:reader_blocked_physical_cleanup_reports_failure_and_incomplete', 'edges:released_reader_next_retention_finishes_physical_cleanup'], ['go/internal/retention/manager.go'])
    requirement('3.stage3.9,4.retention.10', '清理A不删除B的窗口内轨迹', ['retention:A_cleanup_leaves_B_valid_trajectory_identical', 'edges:pruned_face_binding_does_not_damage_B_valid_trajectory'])
    requirement('3.stage3.10,4.retention.11', '登记图库/活动/绑定与外部验票录像排除', ['retention:registered_gallery_excluded', 'retention:legacy_registered_gallery_excluded'], ['cpp/src/retention.cpp', 'cpp/src/p1_retention.cpp', 'HANDOFF.md'])
    requirement('3.stage3.11', '最小运行状态不保存超窗时间、相机或事件原文', ['retention:runtime_seed_has_no_expired_time_camera_or_event', 'edges:crossing_dependencies_are_minimal_hash_kind_tokens', 'edges:corrected_gate_expiry_keeps_closed_minimum_state_without_gate_history'])
    requirement('3.stage3.13,4.retention.12', '本地隔离验证，默认dry-run，生产删除另行授权；精确统计不改任何源列或辅助文件', ['retention:dry_run_preserves_every_persisted_column', 'edges:dry_total_delete_prediction_exact', 'edges:dry_shared_object_prediction_exact', 'service:http_dry_run_no_state_changes'], ['docs/activity-service.md', '../face-recognition-engine/evidence/2026-10-10-activity-retention/p1-maintenance-result.json'])
    requirement('3.design,3.stage2.2,3.stage2.7,3.stage4,5.handoff.1,5.handoff.2,5.handoff.3,5.handoff.4,5.handoff.5,5.handoff.6,5.handoff.7,5.handoff.8,5.handoff.9,5.handoff.10', '统一设计、实际接收接口、契约/示例/迁移回退/坑点、两份唯一交接及前后端最小配合', sources=['docs/activity-retention.md', 'docs/activity-service.md', 'openapi.yaml', 'collection/activity-service.example.json', 'HANDOFF.md', '../face-recognition-engine/HANDOFF.md'])
    requirement('4.build.cpp,4.build.go,4.integration', 'C++构建及Go build/vet/test通过，真实Go/C++/新建SQLite集成作为验收', ['service:real_worker_results_scoped', 'service:http_real_cleanup_reports'], ['evidence/2026-10-10-activity-retention/cpp-final-build.log', 'evidence/2026-10-10-activity-retention/go-final-tests.log'])
    requirement('4.storage.1,4.storage.2,4.storage.3,4.storage.4,4.storage.5', '查询/重放、有效数据量、可复用空间、主库/WAL、六轮增长分开报告', ['retention:expired_duplicate_cannot_resurrect', 'retention:explicit_cleanup_reports_physical_storage', 'retention:expired_marker_absent_file_main', 'retention:expired_marker_absent_file_-wal', 'retention:multi_cycle_trajectory_storage_growth_bounded', 'retention:minimal_hash_tombstone_growth_separately_measured'])
    coverage = {'status': 'LOCAL_REQUIREMENTS_VERIFIED', 'scope': 'No production deployment, frontend/backend live integration or capacity acceptance.',
                'requirements': rows, 'suite_results': summaries,
                'fairness_additional_evidence': [summaries['outbox'], summaries['push']],
                'p1_compatibility_evidence': [summaries['p1_' + name] for name in ['scope', 'retention', 'maintenance', 'dependency']],
                'explicitly_not_performed': ['Real frontend/login/production authorization integration', 'Production deployment or cleanup', '600-camera/3090/2-3s capacity acceptance'],
                'remaining_limits': ['P1 non-Windows offline external-store freeze fails closed', 'Minimal idempotency hashes and excluded registration/configuration history still grow', 'One P1 maintenance recovery assertion failed without preserved report; eight later runs passed; original cause remains unknown'],
                'sealed_client_date': '2026-10-12'}
    write(HERE / 'requirement-coverage.json', coverage)

    paths = {P2 / 'HANDOFF.md', P1 / 'HANDOFF.md', P2 / 'openapi.yaml', P2 / 'cpp/CMakeLists.txt'}
    for base, patterns in [(P2 / 'cpp/src', ['*.cpp']), (P2 / 'cpp/include', ['*.hpp']),
                           (P2 / 'go', ['*.go', 'go.mod', 'go.sum']), (P1 / 'src/face_engine', ['*.py'])]:
        for pattern in patterns:
            paths.update(base.rglob(pattern))
    paths.update(docs)
    paths.update(HERE.glob('verify*.py'))
    paths.add(Path(__file__).resolve())
    paths.update(P1_EVIDENCE.glob('verify*.py'))
    paths.update(P1_EVIDENCE.glob('p1-*-result.json'))
    for directory in ['state-view-test', 'prune-test', 'p1-retention-test']:
        paths.update((HERE / directory).glob('*.cpp'))
        paths.add(HERE / directory / 'CMakeLists.txt')
    paths.add(HERE / 'test_entry.cpp')
    paths.update(P2 / 'cpp/vendor/sqlite' / name for name in ['sqlite3.c', 'sqlite3.h'])
    paths.update([P2 / 'docs/validate_activity_contract.py', P2 / 'docs/contract-validation-requirements.txt', P2 / 'docs/activity-config.schema.json', P2 / 'collection/activity-service.example.json'])
    paths.update(P1 / 'examples' / name for name in ['monitoring.example.json', 'monitoring-cameras.example.json', 'monitoring-persons.example.json'])
    for name in summaries:
        paths.add(WORKSPACE / summaries[name]['evidence'])
    for suite in ['core', 'service', 'retention', 'edges', 'outbox', 'push']:
        paths.add(HERE / SUITE_DIRS[suite] / 'calls.json')
    paths.update(HERE / name for name in ['before-manifest.json', 'cpp-final-build.log', 'go-final-tests.log', 'go-final-summary.json', 'openapi-validation-final.json', 'docs-link-validation-final.json', 'requirement-coverage.json', 'validation-history.json', 'state-view-test/result.log', 'prune-test/result.json'])
    baseline = load(HERE / 'before-manifest.json')
    before = {item['path'].replace('\\', '/'): item['sha256'] for item in baseline}
    entries = [{'path': relative(path), 'bytes': path.stat().st_size, 'sha256': digest(path)} for path in sorted(paths) if path.exists()]
    manifest = {'status': 'SEALED_LOCAL_DELIVERY', 'sealed_client_date': '2026-10-12', 'binaries': binaries,
                'suite_results': summaries, 'go_checks': go_summary,
                'baseline_files': len(baseline), 'files': entries,
                'baseline_differences': [{'path': item['path'], 'before': before[item['path']], 'after': item['sha256']} for item in entries if item['path'] in before and item['sha256'] != before[item['path']]],
                'test_databases': 'Fresh isolated fixtures only. Databases, credentials/proofs, model/runtime caches and build trees are not packaged by this manifest.',
                'evidence_bounds': coverage['explicitly_not_performed'], 'not_a_capacity_acceptance': True}
    write(HERE / 'final-manifest.json', manifest)
    print(json.dumps({'status': manifest['status'], 'files_hashed': len(entries), 'requirement_groups': len(rows), 'suite_results': summaries}, ensure_ascii=False))


if __name__ == '__main__':
    main()
