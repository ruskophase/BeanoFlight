#!/usr/bin/env python3
"""Train/export a real ResNet18 with deliberately arbitrary integration labels."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torch import nn
from torch.utils.data import DataLoader, Dataset, Subset
from torchvision.models import resnet18
from torchvision.transforms.functional import pil_to_tensor

CATEGORIES = ("acceptable", "insect_damage", "mould", "broken")


class CropDataset(Dataset):
    def __init__(self, paths: list[Path], labels: list[int]) -> None:
        self.paths = paths
        self.labels = labels

    def __len__(self) -> int:
        return len(self.paths)

    def __getitem__(self, index: int):
        with Image.open(self.paths[index]) as image:
            rgb = image.convert("RGB")
            tensor = pil_to_tensor(rgb).to(dtype=torch.float32).div_(255.0)
        # Until paired CamR crop transport is implemented, a horizontal mirror
        # provides a distinct second tensor for training the fusion interface.
        # Production timing uses the actual CamL tensor in both engine inputs.
        return tensor, tensor.flip(2), self.labels[index]


class SharedLayer1StereoResNet18(nn.Module):
    """Shared towers through layer1, learned feature fusion, then one backbone."""

    def __init__(self, class_count: int, initial_state: Path | None = None) -> None:
        super().__init__()
        base = resnet18(weights=None, num_classes=class_count)
        if initial_state is not None and initial_state.is_file():
            base.load_state_dict(
                torch.load(
                    initial_state,
                    map_location="cpu",
                    weights_only=True,
                )
            )
        self.stem = nn.Sequential(base.conv1, base.bn1, base.relu, base.maxpool)
        self.layer1 = base.layer1
        self.fusion = nn.Sequential(
            nn.Conv2d(128, 64, kernel_size=1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
        )
        self.layer2 = base.layer2
        self.layer3 = base.layer3
        self.layer4 = base.layer4
        self.avgpool = base.avgpool
        self.fc = base.fc

    def tower(self, image: torch.Tensor) -> torch.Tensor:
        return self.layer1(self.stem(image))

    def forward(self, caml: torch.Tensor, camr: torch.Tensor) -> torch.Tensor:
        features = torch.cat((self.tower(caml), self.tower(camr)), dim=1)
        features = self.fusion(features)
        features = self.layer2(features)
        features = self.layer3(features)
        features = self.layer4(features)
        features = self.avgpool(features)
        return self.fc(torch.flatten(features, 1))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("crops", type=Path)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--epochs", type=int, default=3)
    result.add_argument("--batch-size", type=int, default=16)
    result.add_argument("--learning-rate", type=float, default=1e-3)
    result.add_argument("--seed", type=int, default=20260820)
    result.add_argument("--threads", type=int, default=5)
    result.add_argument("--initial-single-view", type=Path)
    return result


def main() -> int:
    arguments = parser().parse_args()
    random.seed(arguments.seed)
    np.random.seed(arguments.seed)
    torch.manual_seed(arguments.seed)
    torch.set_num_threads(max(1, arguments.threads))
    crop_root = arguments.crops.expanduser().resolve()
    paths = sorted(crop_root.glob("*.png"))
    if len(paths) < len(CATEGORIES) * 4:
        raise SystemExit("at least 16 PNG crops are required")

    # Brightness quartiles provide balanced, visually learnable labels while
    # deliberately carrying no claim about bean quality or defect truth.
    brightness = []
    for path in paths:
        with Image.open(path) as image:
            brightness.append(float(np.asarray(image.convert("RGB")).mean()))
    ranked = sorted(range(len(paths)), key=brightness.__getitem__)
    labels = [0] * len(paths)
    for rank, index in enumerate(ranked):
        labels[index] = min(len(CATEGORIES) - 1, rank * len(CATEGORIES) // len(paths))

    indices = list(range(len(paths)))
    random.shuffle(indices)
    validation_count = max(1, round(len(indices) * 0.2))
    validation_indices = indices[:validation_count]
    training_indices = indices[validation_count:]
    dataset = CropDataset(paths, labels)
    training = DataLoader(
        Subset(dataset, training_indices),
        batch_size=arguments.batch_size,
        shuffle=True,
        num_workers=0,
    )
    validation = DataLoader(
        Subset(dataset, validation_indices),
        batch_size=arguments.batch_size,
        shuffle=False,
        num_workers=0,
    )

    model = SharedLayer1StereoResNet18(
        len(CATEGORIES), arguments.initial_single_view
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=arguments.learning_rate)
    criterion = nn.CrossEntropyLoss()
    history = []
    for epoch in range(arguments.epochs):
        model.train()
        training_loss = 0.0
        training_correct = 0
        training_count = 0
        for caml, camr, targets in training:
            optimizer.zero_grad(set_to_none=True)
            logits = model(caml, camr)
            loss = criterion(logits, targets)
            loss.backward()
            optimizer.step()
            training_loss += float(loss) * len(targets)
            training_correct += int((logits.argmax(1) == targets).sum())
            training_count += len(targets)
        model.eval()
        validation_correct = 0
        validation_total = 0
        with torch.inference_mode():
            for caml, camr, targets in validation:
                logits = model(caml, camr)
                validation_correct += int((logits.argmax(1) == targets).sum())
                validation_total += len(targets)
        metrics = {
            "epoch": epoch + 1,
            "training_loss": training_loss / max(1, training_count),
            "training_accuracy": training_correct / max(1, training_count),
            "validation_accuracy": validation_correct / max(1, validation_total),
        }
        history.append(metrics)
        print(json.dumps(metrics), flush=True)

    output = arguments.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    model.eval()
    checkpoint = output / "mock-stereo-resnet18.pth"
    torch.save(model.state_dict(), checkpoint)
    onnx_path = output / "mock-stereo-resnet18.onnx"
    example = torch.zeros((1, 3, 224, 224), dtype=torch.float32)
    torch.onnx.export(
        model,
        (example, example),
        onnx_path,
        input_names=["CamL", "CamR"],
        output_names=["logits"],
        dynamic_axes={
            "CamL": {0: "batch"},
            "CamR": {0: "batch"},
            "logits": {0: "batch"},
        },
        opset_version=17,
        do_constant_folding=True,
    )
    metadata = {
        "schema": "beanoflight-mock-resnet18/v1",
        "purpose": "GPU timing and pipeline integration only",
        "warning": "brightness-quartile labels are arbitrary and not bean truth",
        "architecture": (
            "shared ResNet18 stem+layer1 towers, concat+1x1 fusion, "
            "shared layer2-layer4"
        ),
        "input": "CamL and CamR RGB float32 NCHW 224x224 scaled to [0,1]",
        "current_training_pair": "CamL crop plus horizontal mirror placeholder",
        "categories": list(CATEGORIES),
        "label_rule": "global RGB mean brightness quartile",
        "training_samples": len(training_indices),
        "validation_samples": len(validation_indices),
        "seed": arguments.seed,
        "history": history,
    }
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    print(f"exported {onnx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
