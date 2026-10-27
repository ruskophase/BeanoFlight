#!/usr/bin/env python3
"""Build the Jetson-local FP16 TensorRT engine for the mock-trained ResNet18."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("onnx", type=Path)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--max-batch", type=int, default=10)
    result.add_argument(
        "--trtexec", type=Path, default=Path("/usr/src/tensorrt/bin/trtexec")
    )
    return result


def main() -> int:
    arguments = parser().parse_args()
    output = arguments.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(arguments.trtexec),
        f"--onnx={arguments.onnx.expanduser().resolve()}",
        f"--saveEngine={output}",
        "--fp16",
        "--minShapes=CamL:1x3x224x224,CamR:1x3x224x224",
        "--optShapes=CamL:4x3x224x224,CamR:4x3x224x224",
        (
            f"--maxShapes=CamL:{arguments.max_batch}x3x224x224,"
            f"CamR:{arguments.max_batch}x3x224x224"
        ),
        "--builderOptimizationLevel=5",
        "--skipInference",
    ]
    print(" ".join(command), flush=True)
    subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
