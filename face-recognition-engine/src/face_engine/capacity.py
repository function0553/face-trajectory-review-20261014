"""Configuration arithmetic for deployment planning, never a hardware benchmark."""
import math
from collections import Counter


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be a positive finite number')
    return value


def capacity_report(config, camera_config):
    cameras = camera_config.get('cameras', [])
    targets = config.get('deployment_targets', {})
    site_count = targets.get('camera_count', len(cameras))
    if type(site_count) is not int or site_count < len(cameras):
        raise ValueError('deployment camera_count must cover configured cameras')
    target = _positive(targets.get('target_result_latency_seconds', 2), 'target latency')
    acceptable = _positive(targets.get('acceptable_result_latency_seconds', 3), 'acceptable latency')
    if target > acceptable:
        raise ValueError('target latency cannot exceed acceptable latency')
    scheduling = config.get('scheduling', {})
    frequencies = scheduling.get('fps', [6, 2, 1])
    if not isinstance(frequencies, (list, tuple)) or len(frequencies) != 3:
        raise ValueError('three sampling frequencies required')
    frequencies = [_positive(value, 'sampling frequency') for value in frequencies]
    ids = [camera['camera_id'] for camera in cameras]
    if any(count != 1 for count in Counter(ids).values()):
        raise ValueError('duplicate camera_id in deployment configuration')
    placeholders = sum(not isinstance(key, str) or not key or key.startswith('REPLACE_') for key in ids)
    live = [camera for camera in cameras if camera.get('sub_url_env')]
    captures = [camera for camera in cameras if camera.get('hikvision', {}).get('enabled')]
    overlap = sum(bool(camera.get('sub_url_env')) for camera in captures)
    inactive = len(cameras) - len(live) - len(captures) + overlap
    output_fps = sum(_positive(camera.get('sample_fps', 10), 'decoder output frequency') for camera in live)
    max_queued = scheduling.get('max_queued_cameras', 128)
    notes = [
        'Camera count does not determine face-event rate; native captures and live streams must be counted separately.',
        'The live FFmpeg path decodes before applying its output FPS filter.',
        'Class quotas and fresh queued frames do not guarantee every camera is inspected within the result latency target.',
        'Measurements must include the intended gallery size, face density, graph, CPU/GPU, decoding and durable P2 writes.'
    ]
    if overlap:
        notes.append('Some cameras enable both continuous streaming and native capture; both consume resources.')
    if len(live) > max_queued:
        notes.append('Continuous stream count exceeds live queue slots; admission may wait even when queued-frame age is low.')
    if scheduling.get('max_frame_age_seconds', 2) >= target:
        notes.append('Maximum live queue age already consumes the target latency budget before inference and P2 processing.')
    classes = ('PRIORITY', 'NORMAL', 'UNREGISTERED')
    def demand(count):
        return {category: count * fps for category, fps in zip(classes, frequencies)}
    # Image/face demand are separate: one image can have zero or many qualified faces.
    offered = config.get('capacity_measurements', {})
    required = ('source_evidence_id', 'peak_native_capture_images_per_second',
                'qualified_faces_per_image', 'sustained_images_per_second',
                'sustained_p2_events_per_second', 'result_p95_seconds')
    assessment = {'status': 'UNMEASURED', 'missing': [key for key in required if key not in offered]}
    if not assessment['missing']:
        if not isinstance(offered['source_evidence_id'], str) or not offered['source_evidence_id'].strip():
            raise ValueError('capacity measurements require a source_evidence_id')
        native_rate = offered['peak_native_capture_images_per_second']
        if isinstance(native_rate, bool) or not isinstance(native_rate, (int, float)) or not math.isfinite(native_rate) or native_rate < 0:
            raise ValueError('native capture peak rate must be finite and nonnegative')
        for key in required[2:]:
            _positive(offered[key], key)
        scenarios = {}
        for category, fps in demand(len(live)).items():
            images = fps + native_rate
            events = images * offered['qualified_faces_per_image']
            scenarios[category] = dict(required_images_per_second=images, required_p2_events_per_second=events,
                within_reported_rates=images <= offered['sustained_images_per_second'] and events <= offered['sustained_p2_events_per_second'])
        assessment = dict(status='REPORTED_MEASUREMENTS_ONLY', source_evidence_id=offered['source_evidence_id'],
            scenarios=scenarios, reported_result_p95_seconds=offered['result_p95_seconds'],
            within_target_latency=offered['result_p95_seconds'] <= target,
            within_acceptable_latency=offered['result_p95_seconds'] <= acceptable)
    return dict(scope='configuration arithmetic only; not a 3090 or 600-camera benchmark',
        site_camera_target=site_count, configured_cameras=len(cameras), placeholder_cameras=placeholders,
        unconfigured_cameras=site_count-len(cameras), inactive_cameras=inactive,
        continuous_streams=len(live), native_capture_cameras=len(captures), dual_input_cameras=overlap,
        target_result_latency_seconds=target, acceptable_result_latency_seconds=acceptable,
        configured_live_sampling_images_per_second_if_all_in_class=demand(len(live)),
        whole_site_sampling_images_per_second_if_all_continuously_streamed=demand(site_count),
        configured_decoder_output_frames_per_second=output_fps,
        illustrative_ppm_pipe_bytes_per_second={f'{width}x{height}': int(output_fps*width*height*3)
            for width, height in ((640, 360), (1280, 720))},
        illustrative_sizes_are_not_measured=True, live_queue_slots=max_queued,
        assessment=assessment, notes=notes)
