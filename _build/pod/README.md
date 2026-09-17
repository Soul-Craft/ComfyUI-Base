# podctl — the Claude-driven pod path (Mac side)

    uv run "base/comfyui-base/_build/pod/podctl.py" pods
    uv run "base/comfyui-base/_build/pod/podctl.py" status <pod>        # facts; env NAMES and placeholder verdicts, never values
    uv run "base/comfyui-base/_build/pod/podctl.py" image <pod>         # the image's entrypoint/cmd and how ensure wraps them
    uv run "base/comfyui-base/_build/pod/podctl.py" ensure <pod>        # TCP 22 · PUBLIC_KEY · the base's boot as start command · Host runpod
    uv run "base/comfyui-base/_build/pod/podctl.py" upload <file>...    # scp to /workspace/packages, size + sha256 checked both sides
    uv run "base/comfyui-base/_build/pod/podctl.py" stop|start|restart <pod> [--wait]

The API key comes from `RUNPOD_API_KEY`, else runpodctl's `~/.runpod/config.toml` (`runpodctl config --apiKey=…`,
run once by the user). It rides a bearer header and is never printed or placed in an argument.

`ensure` is idempotent. It exposes `22/tcp` when missing, sets `PUBLIC_KEY` to `~/.ssh/id_ed25519.pub`, waits for the
pod and its port mapping, probes for an ssh banner (a mapped port proves nothing on images that run no sshd), and when
nothing answers wraps the image's own entry in a start command: `boot.sh` when the base has been installed on the
volume, else the sshd bootstrap from `lib/boot.sh --print-sshd-bootstrap` followed by the image's original command.
Then it writes only the `HostName`/`Port` lines of the `Host runpod` block and runs `ssh runpod hostname`.

Tests: `base/comfyui-base/suite/test_podctl.py` (unit tier) against a fake RunPod REST server, a fake ssh banner and stubbed
`sshd`/`ssh-keygen`/`apt-get`/`scp`/`ssh`. Nothing in the suite touches the network or the user's ssh config.

## install — the one command (2.0.14)

    uv run "base/comfyui-base/_build/pod/podctl.py" install <pod> --base --pkg "<brand>/packages/<name>" [--pkg …] [--every 30] [--timeout 10800] [--no-latest] [--no-save-pins]

For each item in order: `package.py --check` locally (a stale zip stops before anything is uploaded), the volume size
recorded for the disk gate, upload, extract, `--check` on the pod (non-zero stops), the real run detached with `--latest`
(`BASE_RESTART=1` for a package), polled until `STEP RC=` (progress = the run's section headers), the pod's `state/logs`
copied to `<item>/_build/podruns/<date>/pod-logs/`, the summary block printed. Green → the printed pin rows are saved into
the local script and its zip rebuilt (commit both). Red → stop with the console's path. Exit 0 only when every item is green.

## tunnel

    uv run "base/comfyui-base/_build/pod/podctl.py" tunnel <pod> [--port 8188] [--port 8888]

The one ssh session that forwards ports (keepalives on, a taken port fails loudly). Every other podctl ssh/scp call
clears forwards, so a running tunnel never makes them print "bind: Address already in use".

