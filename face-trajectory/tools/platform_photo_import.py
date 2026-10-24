"""Algorithm-owned stdin/stdout bridge for v4.1 enrollment. Never log its input."""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-config', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'face-recognition-engine' / 'src'))
    from face_engine.config import load_config
    from face_engine.monitoring_service import decode_image
    from face_engine.trajectory_bridge import TrajectoryExtractor
    from face_engine.platform_photos import prepare_people
    body = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if len(body) > 8 * 1024 * 1024:
        raise ValueError('import request too large')
    people = json.loads(body)['keyPersons']
    if not isinstance(people, list) or len(people) > 500:
        raise ValueError('invalid import batch')
    # Existing model wrappers may print diagnostics. Keep stdout exclusively JSON.
    with redirect_stdout(sys.stderr):
        extractor = TrajectoryExtractor.from_config(load_config(args.model_config))
        results = prepare_people(people, extractor, decode_image)
    json.dump(dict(keyPersons=results), sys.stdout, ensure_ascii=False, allow_nan=False)


if __name__ == '__main__':
    main()
