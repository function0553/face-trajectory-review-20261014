"""CSV enrollment example using the same model and quality path as the CLI.

Usage: python examples/enroll_faces.py subjects.csv subjects.npz --config configs/default.yaml
CSV requires subject_id,image_path. Each input must contain exactly one detected
face that passes original-pixel quality gates before embedding. Multi-face images
are rejected even if only one face would pass quality.
"""
import argparse

from face_engine.cli import _enroll
from face_engine.config import load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="CSV with subject_id,image_path; exactly one face per image")
    parser.add_argument("output", help="output .npz index")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()
    _enroll(args, load_config(args.config))


if __name__ == "__main__":
    main()
