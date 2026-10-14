"""Validate current OpenAPI against source routes and recorded real service calls.

Validation dependencies are optional isolated tooling, never production modules.
"""
import argparse
import hashlib
import importlib.metadata
import json
import re
import os
import subprocess
import sys
import warnings
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deps', type=Path, help='isolated validator dependency directory')
    parser.add_argument('--calls', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--p1-python', type=Path, help='P1 runtime Python for actual --validate/--capacity-report checks')
    args = parser.parse_args()
    if args.deps:
        sys.path.insert(0, str(args.deps))
    import yaml
    from openapi_spec_validator import validate_spec
    from openapi_schema_validator import OAS30Validator
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        from jsonschema import RefResolver, Draft202012Validator

    class Unique(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        result = {}
        for key, value in node.value:
            name = loader.construct_object(key, deep=deep)
            if name in result:
                raise AssertionError('duplicate YAML key: ' + str(name))
            result[name] = loader.construct_object(value, deep=deep)
        return result

    Unique.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    root = Path(__file__).resolve().parents[1]
    spec_path = root / 'openapi.yaml'
    spec = yaml.load(spec_path.read_text('utf-8'), Loader=Unique)
    validate_spec(spec)
    source = (root / 'go/internal/api/server.go').read_text('utf-8')
    actual_routes = {}
    for method, path, action, global_scope in re.findall(r'mux\.HandleFunc\("(GET|POST) ([^"]+)", s\.(?:post|get)\("[^"]+", "([^"]+)", (true|false)\)\)', source):
        actual_routes[(method.lower(), path)] = (action, global_scope == 'true')
    for path, operation, action, global_scope in re.findall(r'\{"(/v1/[^"]+)", "([^"]+)", "([^"]+)", (true|false)\}', source):
        actual_routes[('post', path)] = (action, global_scope == 'true')
    for path in re.findall(r'mux\.HandleFunc\("POST ([^"]+)", s\.subscription\("[^"]+"\)\)', source):
        actual_routes[('post', path)] = ('subscribe', False)
    for method, path in re.findall(r'mux\.HandleFunc\("(POST|GET) ([^"]+)", s\.retention(?:Run|Status)\)', source):
        actual_routes[(method.lower(), path)] = ('retention', True)
    assert 'claims.AllowsGlobal("retention")' in (root / 'go/internal/api/retention.go').read_text('utf-8')
    assert 'mux.HandleFunc("DELETE /v1/templates/{id}"' in source
    assert 's.dispatch(w, r, body, "remove_template", "identity", false)' in source
    actual_routes[('delete', '/v1/templates/{id}')] = ('identity', False)
    assert 'mux.HandleFunc("GET /health"' in source
    expected_operations = {(method, path) for path, methods in spec['paths'].items() for method in methods if method in ['get','post','delete','put','patch'] and path != '/health'}
    assert set(actual_routes) == expected_operations, ('HTTP/source mismatch', set(actual_routes)^expected_operations)
    for (method, path), (action, global_scope) in actual_routes.items():
        operation = spec['paths'][path][method]
        assert operation['x-authorization-action'] == action, (path, 'wrong action')
        assert operation['x-activity-scope'] == ('global' if global_scope else 'activity'), (path, 'wrong scope')
    assert spec['info']['version'] == '0.13.0'
    assert spec['paths']['/health']['get']['security'] == []
    assert spec['security'] == [{'BackendSignedBearer': []}]
    for path, methods in spec['paths'].items():
        if path == '/health':
            continue
        for method, operation in methods.items():
            assert operation['x-authorization-action']
            assert operation['x-activity-scope'] in ['activity', 'global']
            for status in ['401', '403', '503']:
                assert status in operation['responses'], (path, method, status)
    references = []

    def refs(value):
        if isinstance(value, dict):
            if '$ref' in value:
                pointer = value['$ref']
                assert pointer.startswith('#/')
                part = spec
                for segment in pointer[2:].split('/'):
                    part = part[segment.replace('~1', '/').replace('~0', '~')]
                references.append(pointer)
            for item in value.values():
                refs(item)
        elif isinstance(value, list):
            for item in value:
                refs(item)

    refs(spec)
    resolver = RefResolver.from_schema(spec)
    def resolve(value):
        while '$ref' in value:
            pointer = value['$ref']
            value = spec
            for segment in pointer[2:].split('/'):
                value = value[segment.replace('~1', '/').replace('~0', '~')]
        return value

    examples = json.loads((root / 'collection/activity-service.example.json').read_text('utf-8'))
    for step in examples['request_steps']:
        operation = spec['paths'][step['path']][step['method'].lower()]
        OAS30Validator(operation['requestBody']['content']['application/json']['schema'], resolver=resolver).validate(step['body'])
    for name, schema in [('authorization_claims','SignedAuthorizationClaims'),('clock_authority_proof','ClockAuthorityProof')]:
        OAS30Validator(spec['components']['schemas'][schema], resolver=resolver).validate(examples[name])
    config_schema = json.loads((root / 'docs/activity-config.schema.json').read_text('utf-8'))
    Draft202012Validator.check_schema(config_schema)
    p1_root = root.parent / 'face-recognition-engine'
    config_examples = []
    for name, schema in [('monitoring.example.json','runtime'),('monitoring-cameras.example.json','cameras'),('monitoring-persons.example.json','persons')]:
        selected_schema = dict(config_schema, **{'$ref':'#/$defs/'+schema})
        path = p1_root / 'examples' / name
        Draft202012Validator(selected_schema).validate(json.loads(path.read_text('utf-8')))
        config_examples.append(str(path.resolve()))
    p1_python = args.p1_python or p1_root / '.venv/Scripts/python.exe'
    assert p1_python.is_file(), 'Use --p1-python with the P1 dependencies for actual runtime validation'
    runtime_checks = []
    runtime_env = dict(os.environ, PYTHONPATH=str(p1_root / 'src'))
    for mode in ['--validate','--capacity-report']:
        command = [str(p1_python), '-m', 'face_engine.monitoring_service', '--config', str(p1_root / 'examples/monitoring.example.json'), mode]
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8', env=runtime_env, timeout=30)
        runtime_checks.append({'command':command, 'result':json.loads(result.stdout)})
    validated = []
    for calls_path in args.calls:
        recorded = json.loads(calls_path.read_text('utf-8'))
        calls = recorded.get('calls', []) if isinstance(recorded, dict) else recorded
        count = 0
        response_count = 0
        errors_count = 0
        for call in calls:
            path = call['path']
            request = call.get('request', call.get('body'))
            response = call.get('response', call.get('reply'))
            method = call.get('method', 'get' if request is None else 'post').lower()
            operation = spec['paths'][path][method]
            if request is not None and call.get('status') == 200:
                request = {k: v for k, v in request.items() if k != 'op'}
                OAS30Validator(operation['requestBody']['content']['application/json']['schema'], resolver=resolver).validate(request)
            status = str(call['status'])
            assert status in operation['responses'], (path, method, 'undocumented status', status)
            schema = resolve(operation['responses'][status])['content']['application/json']['schema']
            OAS30Validator(schema, resolver=resolver).validate(response)
            response_count += 1
            if status == '200':
                count += 1
            else:
                errors_count += 1
        validated.append({'calls': str(calls_path.resolve()), 'successful_request_response_pairs': count, 'all_response_schemas':response_count, 'error_response_schemas':errors_count})
    report = {
        'passed': True, 'version': spec['info']['version'], 'paths': len(spec['paths']),
        'resolved_references': len(references), 'strict_duplicate_keys': True,
        'full_openapi_spec_validation': True, 'actual_go_operations_matched': len(actual_routes)+1,
        'authorization_actions_and_scopes_match_source':True,
        'workflow_request_examples':len(examples['request_steps']), 'unsigned_claim_proof_examples':2,
        'p1_config_schema_examples':config_examples, 'p1_actual_runtime_config_checks':runtime_checks,
        'recorded_http_schema_checks': validated,
        'openapi_sha256': hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        'validator_python':sys.executable,
        'validator_package_versions':{name:importlib.metadata.version(name) for name in ['PyYAML','openapi-spec-validator','openapi-schema-validator','jsonschema']},
        'validator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'go_route_source_sha256':hashlib.sha256((root/'go/internal/api/server.go').read_bytes()).hexdigest(),
        'limitations': 'Recorded local signed-client contracts; real backend/frontend authentication and production remain unverified.'
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
