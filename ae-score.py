import argparse
import csv
import dataclasses
import http.server
import json
import os
import time
import typing
from tqdm import tqdm
from pathlib import Path, PurePath
from urllib.parse import parse_qsl, urlparse
from math import ceil

import clip
import torch
import torch.nn as nn
import torchvision.transforms
from PIL import Image
from platformdirs import user_cache_dir
from typing import List


APP_NAME = "ae-score"
APP_AUTHOR = "rockerBOO"

model_to_host = {
    "chadscorer": "https://github.com/grexzen/SD-Chad/raw/main/chadscorer.pth",
    "sac+logos+ava1-l14-linearMSE": "https://github.com/christophschuhmann/improved-aesthetic-predictor/blob/main/sac+logos+ava1-l14-linearMSE.pth?raw=true",
}

clip_models = ["ViT-B/32", "ViT-B/32", "ViT-L/14", "ViT-L/14@336px"]

MODEL = "sac+logos+ava1-l14-linearMSE"
CLIP_MODEL = "ViT-L/14"

assert CLIP_MODEL in clip_models
assert MODEL in model_to_host.keys()


class AestheticPredictor(nn.Module):
    def __init__(self, input_size):
        super().__init__()
        self.input_size = input_size
        self.layers = nn.Sequential(
            nn.Linear(self.input_size, 1024),
            nn.Dropout(0.2),
            # nn.ReLU()
            nn.Linear(1024, 128),
            nn.Dropout(0.2),
            # nn.ReLU()
            nn.Linear(128, 64),
            nn.Dropout(0.1),
            # nn.ReLU()
            nn.Linear(64, 16),
            nn.Linear(16, 1),
        )

    def forward(self, x):
        return self.layers(x)


def ensure_model(model=MODEL):
    """
    Make sure we have the model to score with.
    Saves into your cache directory on your system
    """
    cache_dir = user_cache_dir(APP_NAME, APP_AUTHOR)

    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    file = model + ".pth"
    full_file = os.path.join(cache_dir, file)
    if not Path(full_file).exists():
        if model not in model_to_host:
            raise ValueError(
                f"invalid model: {model}. try one of these: {', '.join(model_to_host.keys())}"
            )

        url = model_to_host[model]

        import requests

        print(f"downloading {url}")
        r = requests.get(url)
        with open(full_file, "wb") as f:
            f.write(r.content)
            print(f"saved to {full_file}")


def clear_cache():
    """
    Removes all the cached models
    """
    cache_dir = user_cache_dir(APP_NAME, APP_AUTHOR)

    for f in os.listdir(cache_dir):
        os.remove(os.path.join(cache_dir, f))


ModelName = typing.NewType("ModelName", str)


def load_model(
    model: ModelName,
    clip_model,
    device=torch.device("cpu"),
    dtype=torch.float16,
) -> AestheticPredictor:
    ensure_model(model)
    cache_dir = user_cache_dir(APP_NAME, APP_AUTHOR)

    pt_state = torch.load(
        os.path.join(cache_dir, model + ".pth"), map_location="cpu"
    )

    # CLIP embedding dim is 768 for CLIP ViT L 14
    if "ViT-L" in clip_model:
        predictor = AestheticPredictor(768)
    elif "ViT-B" in clip_model:
        predictor = AestheticPredictor(512)
    else:
        predictor = AestheticPredictor(768)

    predictor.load_state_dict(pt_state)
    predictor.to(device)

    # Disable grad
    predictor.eval()

    return predictor


# https://github.com/christophschuhmann/improved-aesthetic-predictor/blob/main/simple_inference.py
def normalized(a, axis=-1, order=2):
    import numpy as np  # pylint: disable=import-outside-toplevel

    l2 = np.atleast_1d(np.linalg.norm(a, order, axis))
    l2[l2 == 0] = 1
    return a / np.expand_dims(l2, axis)


def get_image_features(
    image: torch.Tensor,
    clip_model: clip.model.CLIP,
    device: torch.device,
):
    return normalized(clip_model.encode_image(image).cpu().detach().numpy())


# kohya-ss/sd-scripts train_util
def prepare_dtype(args: argparse.Namespace):
    weight_dtype = torch.float32
    if args.precision == "fp16":
        weight_dtype = torch.float16
    elif args.precision == "bf16":
        weight_dtype = torch.bfloat16

    save_dtype = None

    return weight_dtype, save_dtype


def get_aesthetic_predictor_scores(
    images: List[torch.Tensor],
    predictor: AestheticPredictor,
    clip_model,
    device: torch.device,
    dtype=torch.float16,
):
    image_features_list = get_image_features(
        torch.cat([image.unsqueeze(0).to(device) for image in images]),
        clip_model,
        device,
    )

    with torch.no_grad():
        scores = predictor(
            torch.from_numpy(image_features_list).to(device, dtype=dtype)
        )

    del image_features_list
    torch.cuda.empty_cache()

    return scores


def load_clip_model(
    model: str, device=torch.device("cpu")
) -> tuple[clip.model.CLIP, torchvision.transforms.Compose]:
    clip_model, clip_image_processor = clip.load(model, device=device)

    return clip_model, clip_image_processor


@dataclasses.dataclass
class Args:
    image_file_or_dir: str
    model: str
    clip_model: str
    device: str
    batch_size: int
    no_progress: bool
    store_full_path: bool


Device = typing.NewType("Device", str)


def get_device(args: Args) -> torch.device:
    if args.device == str():
        return torch.device(args.device)

    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def main(args: Args):
    device = get_device(args)

    # load the model you trained previously or the model available in this repo

    print(f"Loading {args.model}")
    predictor = load_model(
        args.model, args.clip_model, device=device, dtype=torch.float16
    )

    print(f"Loading CLIP {args.clip_model}")
    clip_model, clip_image_processor = load_clip_model(
        args.clip_model, device=device
    )

    scores = []
    wdtype, _sdtype = prepare_dtype(args)

    files = get_files(args.image_file_or_dir)

    print(f"Files to score: {len(files)}")

    if args.no_progress is not True:
        progress_bar = tqdm(total=ceil(len(files) / args.batch_size))

    def chunks(lst, n):
        """Yield successive n-sized chunks from lst."""
        for i in range(0, len(lst), n):
            yield lst[i : i + n]

    for files_s in chunks(files, args.batch_size):
        images = []
        for file in files_s:
            with Image.open(file) as image:
                images.append(clip_image_processor(image))

        ae_scores = get_aesthetic_predictor_scores(
            images, predictor, clip_model, device, wdtype
        )

        for score in ae_scores:
            scores.append({"file": file, "score": score})

        progress_bar.set_postfix({"total": len(scores)})

        if args.no_progress is not True:
            progress_bar.update()

        torch.cuda.empty_cache()

    del predictor
    del clip_model
    torch.cuda.empty_cache()

    sorted_scores = sorted(scores, key=lambda x: x["score"], reverse=True)

    if len(sorted_scores) < 300:
        for score in sorted_scores:
            print(score["file"], score["score"].item())

    acc_scores = 0
    for score in sorted_scores:
        acc_scores = acc_scores + score["score"]

    if len(sorted_scores) > 0:
        full_path = args.image_file_or_dir if args.store_full_path else ""
        if args.save_csv:
            save_to_csv(scores, full_path=full_path, csv_file=args.csv_file)

        print(f"average score: {acc_scores.item() / len(sorted_scores)}")
    else:
        print("no scores. Did you put the correct directory/image in?")


def get_files(file_or_dir):
    input_images = Path(file_or_dir)
    files = []
    if input_images.is_dir():
        file_list = os.listdir(input_images)

        for file in file_list:
            full_file = os.path.join(input_images, file)

            if Path(full_file).is_dir():
                [files.append(f) for f in get_files(full_file)]
            else:
                if full_file.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".avif")
                ):
                    files.append(full_file)
    else:
        if args.image_file_or_dir.lower().endswith(
            (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".avif")
        ):
            files.append(args.image_file_or_dir)

    return files


def save_to_csv(scores, full_path, csv_file=None):
    scores = scores.copy()
    fieldnames = ["file", "score"]
    id = str(round(time.time()))

    if csv_file is None:
        csv_file = f"scores-{id}.csv"

    with open(csv_file, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for score in scores:
            score["score"] = score["score"].detach().cpu().item()
            score["file"] = os.path.join(full_path, score["file"])
            writer.writerow(score)

        print(f"Saved CSV to {csv_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "image_file_or_dir",
        type=str,
        help="Image file or directory containing the images.",
    )

    parser.add_argument(
        "--save_csv",
        default=False,
        action="store_true",
        help="Save the results to a csv file in the current directory.",
    )

    parser.add_argument(
        "--csv_file",
        default=None,
        help="path and filename of the CSV file with the scores",
    )

    parser.add_argument(
        "--store_full_path",
        default=False,
        action="store_true",
        help="Store the full path to the image in the CSV. Experimental and may be removed.",
    )

    parser.add_argument(
        "--batch_size",
        default=5,
        type=int,
        help="Batch size to score the images",
    )

    parser.add_argument(
        "--model",
        default=MODEL,
        help=f"Model to score with: {MODEL}. Options: {', '.join(model_to_host.keys())}.",
    )

    parser.add_argument(
        "--clip_model",
        default=CLIP_MODEL,
        help=f"CLIP model. Options: {', '.join(clip_models)}.",
    )

    parser.add_argument(
        "--no_progress",
        action="store_true",
        default=False,
        help="Show progress bars",
    )

    parser.add_argument(
        "--precision",
        default="float",
        type=str,
        help="bf16, fp16, fp32, float",
    )

    parser.add_argument(
        "--device",
        default=None,
        type=str,
        help="Device to do inference on. Options: 'cpu', 'cuda', 'cuda:0', ...",
    )

    args: Args = parser.parse_args()

    main(args)
