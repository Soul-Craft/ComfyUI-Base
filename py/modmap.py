#!/usr/bin/env python3
"""The Python distributions that provide modules node packs import without declaring them (3.3.0).

    modmap.py <import check log>     one distribution per line for each `No module named 'X'` the map knows;
                                     each module it does not know is named on stderr and left to the pack's upstream

This map is an ALLOWLIST, reviewed by hand. A distribution name is never derived from an error string: the import name
and the name on PyPI often differ (cv2 is opencv-python), and installing whatever an error happens to name is how a
typosquatted package gets onto a machine. A module not listed here is named for upstream, as before 3.3.0.
Headless and GPU builds are chosen where a server machine wants them (no display; an NVIDIA GPU).
"""
import re
import sys

MAP = {
    # import name -> distribution
    "cv2": "opencv-python-headless",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "Crypto": "pycryptodome",
    "jwt": "pyjwt",
    "fitz": "pymupdf",
    "docx": "python-docx",
    "git": "gitpython",
    "attr": "attrs",
    "zmq": "pyzmq",
    "serial": "pyserial",
    "OpenGL": "pyopengl",
    "magic": "python-magic",
    "huggingface_hub": "huggingface-hub",
    "segment_anything": "segment-anything",
    "google.protobuf": "protobuf",
    "sentencepiece": "sentencepiece",
    "einops": "einops",
    "timm": "timm",
    "transformers": "transformers",
    "diffusers": "diffusers",
    "accelerate": "accelerate",
    "safetensors": "safetensors",
    "kornia": "kornia",
    "omegaconf": "omegaconf",
    "scipy": "scipy",
    "numba": "numba",
    "gguf": "gguf",
    "soundfile": "soundfile",
    "librosa": "librosa",
    "imageio": "imageio",
    "matplotlib": "matplotlib",
    "pandas": "pandas",
    "piexif": "piexif",
    "spandrel": "spandrel",
    "onnx": "onnx",
    "onnxruntime": "onnxruntime-gpu",
    "insightface": "insightface",
    "mediapipe": "mediapipe",
    "rembg": "rembg",
    "ultralytics": "ultralytics",
    "color_matcher": "color-matcher",
    "simpleeval": "simpleeval",
    "numexpr": "numexpr",
    "lark": "lark",
    "webcolors": "webcolors",
    "addict": "addict",
    "yapf": "yapf",
    "ftfy": "ftfy",
    "tqdm": "tqdm",
}

MISSING = re.compile(r"No module named ['\"]([A-Za-z0-9_.]+)['\"]")


def dists(text):
    found, unknown = [], []
    for name in MISSING.findall(text):
        top = name.split(".")[0]
        # a dotted name the map lists whole (google.protobuf) wins; otherwise its top-level package
        d = MAP.get(name) or MAP.get(top)
        if d:
            if d not in found:
                found.append(d)
        elif top not in unknown:
            unknown.append(top)
    return found, unknown


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    with open(argv[1], encoding="utf-8", errors="replace") as fh:
        found, unknown = dists(fh.read())
    for d in found:
        print(d)
    for u in unknown:
        print("%s: not in the base's module map; named for the pack's upstream" % u, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
