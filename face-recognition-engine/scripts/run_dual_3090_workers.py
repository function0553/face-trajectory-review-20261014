"""Launch four P1 monitoring workers across two RTX 3090 devices.

The workers keep independent local delivery queues and send to one shared P2
endpoint.  A camera template can be supplied with four entries; entry ``i`` is
assigned to the matching worker's explicit source_id.  ``--dry-run`` writes
the generated configs and prints the launch plan without loading models.

Example (from face-recognition-engine):
    python scripts/run_dual_3090_workers.py \
      --config examples/monitoring.example.json \
      --p2-endpoint http://127.0.0.1:8081 \
      --cameras-file examples/monitoring-cameras.example.json
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any

import yaml


WORKERS = (
    {"name": "p1-gpu0-a", "cuda_id": 0, "port": 8091},
    {"name": "p1-gpu0-b", "cuda_id": 0, "port": 8092},
    {"name": "p1-gpu1-a", "cuda_id": 1, "port": 8093},
    {"name": "p1-gpu1-b", "cuda_id": 1, "port": 8094},
)


def _absolute(value: str | None, base: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _prepare_model(base: dict[str, Any], base_dir: Path, worker: dict[str, Any], output: Path) -> str:
    source = _absolute(base.get("model_config"), base_dir)
    if source is None or not source.is_file():
        raise FileNotFoundError(f"model_config does not exist: {source}")
    model = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(model, dict):
        raise ValueError(f"model config must be a YAML mapping: {source}")
    model["device"] = f"cuda:{worker['cuda_id']}"
    destination = output / f"model-{worker['name']}.yaml"
    destination.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    return str(destination)


def _prepare_cameras(base: dict[str, Any], base_dir: Path, worker_index: int,
                     worker: dict[str, Any], output: Path, override: Path | None) -> str | None:
    source = override or _absolute(base.get("cameras_file"), base_dir)
    if source is None:
        return None
    data = _load_json(source)
    cameras = data.get("cameras")
    if not isinstance(cameras, list):
        raise ValueError(f"cameras must be an array: {source}")
    if len(cameras) >= len(WORKERS):
        selected = [copy.deepcopy(cameras[worker_index])]
    else:
        selected = copy.deepcopy(cameras)
    for item in selected:
        if not isinstance(item, dict):
            raise ValueError(f"camera entries must be objects: {source}")
        item["source_id"] = worker["name"]
    destination = output / f"cameras-{worker['name']}.json"
    destination.write_text(json.dumps({**data, "cameras": selected}, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return str(destination)


def generate_configs(template: Path, p2_endpoint: str, output: Path,
                     cameras_file: Path | None) -> list[tuple[dict[str, Any], Path]]:
    base = _load_json(template)
    output.mkdir(parents=True, exist_ok=True)
    generated: list[tuple[dict[str, Any], Path]] = []
    for index, worker in enumerate(WORKERS):
        config = copy.deepcopy(base)
        config["endpoint"] = p2_endpoint
        config["model_config"] = _prepare_model(base, template.parent, worker, output)
        config["cameras_file"] = _prepare_cameras(base, template.parent, index, worker, output, cameras_file)
        config["state_file"] = str(output / f"p1-{worker['name']}.sqlite")
        config["http"] = {**config.get("http", {}), "host": "127.0.0.1", "port": worker["port"]}
        # Keep the route visible in the generated file for operators and
        # adapters.  The actual camera source binding is the cameras file.
        config["worker_source_id"] = worker["name"]
        destination = output / f"config-{worker['name']}.json"
        destination.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        generated.append((worker, destination))
    return generated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="base monitoring runtime JSON")
    parser.add_argument("--p2-endpoint", required=True, help="one shared face-trajectoryd HTTP endpoint")
    parser.add_argument("--cameras-file", type=Path, help="optional camera template; four entries map one per worker")
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/dual-3090"))
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument("--run-seconds", type=float, default=0, help="pass a bounded run duration to each worker")
    parser.add_argument("--dry-run", action="store_true", help="generate configs and do not start workers")
    args = parser.parse_args()
    template = args.config.resolve()
    output = args.output_dir if args.output_dir.is_absolute() else (Path.cwd() / args.output_dir).resolve()
    generated = generate_configs(template, args.p2_endpoint, output,
                                  args.cameras_file.resolve() if args.cameras_file else None)
    plan = [{**worker, "config": str(config), "endpoint": args.p2_endpoint} for worker, config in generated]
    print(json.dumps({"workers": plan, "shared_p2_endpoint": args.p2_endpoint}, ensure_ascii=False, indent=2))
    if args.dry_run:
        return 0

    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    source_root = str(project_root / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for worker, config in generated:
            child_environment = environment.copy()
            child_environment["FACE_ENGINE_CUDA_DEVICE_ID"] = str(worker["cuda_id"])
            command = [args.python_executable, "-m", "face_engine.monitoring_service",
                       "--config", str(config)]
            if args.run_seconds:
                command.extend(["--run-seconds", str(args.run_seconds)])
            processes.append(subprocess.Popen(command, cwd=project_root, env=child_environment))
        return_code = 0
        for process in processes:
            code = process.wait()
            return_code = return_code or code
        return return_code
    except KeyboardInterrupt:
        return 130
    finally:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in processes:
            if process.poll() is None:
                process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
