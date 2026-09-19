## What changed, and why

<!-- One or two sentences. The commit log's convention is a claim, not a category. -->

## Which host was this exercised on?

<!--
Say plainly. "Not run on hardware — unit tests only" is a fine answer; an untested claim that reads as
tested is not. RunPod is proven on live pods, Verda is experimental, Crusoe is an interface stub,
hosts/local is a recipe.
-->

- [ ] RunPod
- [ ] Verda
- [ ] Crusoe Cloud
- [ ] an owned NVIDIA box
- [ ] not run on hardware

## The gate

- [ ] `bash _build/verify.sh` is green
- [ ] no new warnings (`pytest.ini` sets `filterwarnings = error`; warnings are fixed, never filtered)
- [ ] `MANIFEST.sha256` and `comfyui-base.zip` were not hand-edited — if shipped files changed, they were
      rebuilt with `python3 _build/package.py --base`
