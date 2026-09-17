# 40-packs.sh — node packs: the shared six the base owns, the extras each package declares; locate, clone,
# pin (or --latest), requirements under the torch constraint, an advisory import scan; strays consolidated.
#
# Row format (BASE_PACKS and every package's PACKS): dir|url|sha|cnr_id|why
#   dir     the directory name under custom_nodes (also how the pack is located)
#   sha     the 40-hex commit the suites were green against; --latest is the only way off it
#   cnr_id  the Comfy Registry id when it differs from dir (empty otherwise)

# The packs most workflows share, pinned by the base. A package must not redeclare one of these.
BASE_PACKS=(
 "rgthree-comfy|https://github.com/rgthree/rgthree-comfy|2c5342a8cb0eaecaabf61435a5f37dd594c510ba||switches, bypassers, any-switch, fast group bypassers/muters, radio panels, Power Lora Loader"
 "ComfyUI-KJNodes|https://github.com/kijai/ComfyUI-KJNodes|57105374f47d0fbb49c9c3926fb981702e0a4b5c|comfyui-kjnodes|SetNode/GetNode routing, resize, patches, video helpers"
 "ComfyUI-VideoHelperSuite|https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite|4d907bee61e92c2e65af3bd6383a4e4d356126d1|comfyui-videohelpersuite|video load/combine, SelectEveryNth, custom formats"
 "cg-use-everywhere|https://github.com/chrisgoringe/cg-use-everywhere|50ae9f8c5d8b9538589663c90a15d4067a02969c||Anything Everywhere broadcast links"
 "ComfyUI-Manager|https://github.com/Comfy-Org/ComfyUI-Manager|f82970b7cb63ad44928308f980a1d38fda103cbb|comfyui-manager|the node manager (hygiene sets its security level); RunPod's image bakes it, other images do not"
 "ComfyUI-advanced-model-manager|https://github.com/BISAM20/ComfyUI-advanced-model-manager|232997501a9ae6f9fa83f3594d16ca5341ecdbb2|comfyui-advanced-model-manager|the Hugging Face browser (2.1.0): Hub search and downloads into the library's folders via extra_model_paths.yaml, HF_TOKEN from the environment and sent to huggingface.co only (read once at this pin)"
)
# There is no image-owned pack list any more: the tree on the volume is the base's, so every pack in it is either
# a declared row (pinned, updated) or a directory the base leaves alone. Their requirements all go into the venv.
DEL_DIRS=()
BASE_PACKS_LATEST_ROWS=()

_base_pack_rows(){ # prints BASE_PACKS then PACKS; validates shape; exit 1 on any violation (message on stderr)
  local row dir url sha cnr why bad=0 seen=" " base_seen=" "
  for row in ${BASE_PACKS[@]+"${BASE_PACKS[@]}"}; do IFS='|' read -r dir url sha cnr why <<< "$row"; base_seen="$base_seen$dir "; done
  for row in ${BASE_PACKS[@]+"${BASE_PACKS[@]}"} ${PACKS[@]+"${PACKS[@]}"}; do
    IFS='|' read -r dir url sha cnr why <<< "$row"
    if [ -z "$dir" ] || [ -z "$url" ] || ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
      echo "  !! bad PACKS row (want dir|url|sha|cnr_id|why with a 40-hex sha): $row" >&2; bad=1; continue
    fi
    case "$seen" in *" $dir "*)
      case "$base_seen" in *" $dir "*) echo "  !! $dir is a base pack and may not be redeclared by ${PKG_NAME:-the package}" >&2;;
        *) echo "  !! $dir is declared twice" >&2;; esac
      bad=1; continue;;
    esac
    seen="$seen$dir "; echo "$row"
  done
  return $bad
}

_base_pack_locate(){ # <dir> <url> → prints the checkout dir or nothing
  local name="$1" url="$2" found=""
  if [ -d "$CN/$name" ]; then echo "$CN/$name"; return 0; fi
  found="$(find "$CN" -mindepth 1 -maxdepth 1 -type d -iname "$name" 2>/dev/null | head -1 || true)"
  [ -z "$found" ] && found="$(grep -lis "url = ${url%.git}\(\.git\)\?\$" "$CN"/*/.git/config 2>/dev/null | head -1 | sed 's#/\.git/config$##' || true)"
  [ -n "$found" ] && echo "$found"; return 0
}
_base_fake_clone(){ # BASE_NO_NET: the shape of a clone, no network
  local url="$1" dir="$2"
  mkdir -p "$dir/.git"; printf '[remote "origin"]\n\turl = %s\n' "$url" > "$dir/.git/config"; : > "$dir/requirements.txt"
}
_base_pack_pin(){ # <dir> <url> <sha> → "" on success, a reason on failure (stdout is the error channel)
  # Packs are cloned --depth 1, so the pinned commit is usually not in the clone: ask the remote for that one
  # object first, widen to a full fetch only if the server refuses single-commit fetches. Both reach the
  # same commit, or this fails and the caller records it — never a different artifact.
  local dir="$1" url="$2" sha="$3" rmt=""
  rmt="$(_base_git_remote "$dir")" || true
  if [ -z "$rmt" ]; then rmt=origin; _base_git -C "$dir" remote add origin "$url" >/dev/null 2>&1 || true; fi
  if ! _base_git -C "$dir" cat-file -e "$sha^{commit}" >/dev/null 2>&1; then
    _base_git -C "$dir" fetch -q --depth 1 "$rmt" "$sha" >/dev/null 2>&1 \
      || _base_git -C "$dir" fetch -q --tags --force --prune "$rmt" >/dev/null 2>&1 || true
  fi
  if ! _base_git -C "$dir" cat-file -e "$sha^{commit}" >/dev/null 2>&1; then echo "commit $sha is not in $url"; return 0; fi
  if ! _base_git -C "$dir" checkout -q --detach "$sha" >/dev/null 2>&1; then echo "could not check out $sha"; return 0; fi
  echo ""
}
_base_pack_sync(){ # <dir> <url> → prints git's complaint if the pack could not move, nothing if it did (--latest)
  # Nothing here reads the repo's existing tracking config: baked clones sit on a branch with no upstream or
  # detached after `clone --branch <tag>`, so the remote, the branch and the upstream are re-established from
  # the canonical URL and the update is a fast-forward onto the branch the remote publishes as HEAD.
  local dir="$1" url="$2" rmt="" br="" cur="" out="" fix=""
  rmt="$(_base_git_remote "$dir")" || true
  if [ -z "$rmt" ]; then rmt=origin; _base_git -C "$dir" remote add origin "$url" >/dev/null 2>&1 || true; fix="no remote was configured — origin set to $url"; fi
  if [ "$(_base_git -C "$dir" rev-parse --is-shallow-repository 2>/dev/null || echo false)" = "true" ]; then
    _base_git -C "$dir" fetch -q --unshallow --tags --force --prune "$rmt" >/dev/null 2>&1 || true
  fi
  out="$(_base_git -C "$dir" fetch -q --tags --force --prune "$rmt" 2>&1)" || { printf '%s\n' "$out"; return 0; }
  br="$(_base_git -C "$dir" symbolic-ref -q --short "refs/remotes/$rmt/HEAD" 2>/dev/null | sed "s#^$rmt/##" || true)"
  if [ -z "$br" ]; then
    _base_git -C "$dir" remote set-head "$rmt" -a >/dev/null 2>&1 || true
    br="$(_base_git -C "$dir" symbolic-ref -q --short "refs/remotes/$rmt/HEAD" 2>/dev/null | sed "s#^$rmt/##" || true)"
  fi
  [ -z "$br" ] && br="$(_base_git -C "$dir" ls-remote --symref "$rmt" HEAD 2>/dev/null | sed -n 's#^ref:[[:space:]]*refs/heads/\([^[:space:]]*\).*#\1#p' | sed -n '1p' || true)"
  if [ -z "$br" ]; then echo "cannot tell which branch $rmt publishes as HEAD"; return 0; fi
  cur="$(_base_git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
  if [ "$cur" != "$br" ]; then
    _base_git -C "$dir" checkout -q "$br" >/dev/null 2>&1 || _base_git -C "$dir" checkout -q -b "$br" "$rmt/$br" >/dev/null 2>&1 || true
    fix="${fix:+$fix; }was on '$cur' — now on $br tracking $rmt/$br"
  elif ! _base_git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    fix="${fix:+$fix; }branch '$br' had no upstream — wired to $rmt/$br"
  fi
  [ -n "$fix" ] && note "$(basename "$dir"): $fix" >&2 || true
  _base_git -C "$dir" branch -q --set-upstream-to="$rmt/$br" "$br" >/dev/null 2>&1 || true
  out="$(_base_git -C "$dir" merge -q --ff-only "$rmt/$br" 2>&1)" || { printf '%s\n' "$out"; return 0; }   # diverged: reported, never rewritten
  return 0
}
_base_pack_reqfile(){ # <dir> → the requirements file a pack wants installed (Frame-Interpolation ships a no-cupy variant)
  local d="$1"
  if [ -f "$d/requirements-no-cupy.txt" ]; then echo "$d/requirements-no-cupy.txt"
  elif [ -f "$d/requirements.txt" ]; then echo "$d/requirements.txt"; fi
}
_base_all_reqfiles(){ # ComfyUI's + every pack directory PRESENT in the tree (managed or not) + every pack located this run + the ledger's
  # `if`, not `[ … ] && …`: an AND-list at the END OF A LOOP BODY trips errexit and the ERR trap exits.
  local f d name path m seen=" "
  if [ -f "$COMFY/requirements.txt" ]; then echo "$COMFY/requirements.txt"; fi
  for d in "${CN:-/nonexistent}"/*/; do
    d="${d%/}"
    if [ -d "$d" ]; then f="$(_base_pack_reqfile "$d")"; if [ -n "$f" ] && [[ "$seen" != *" $f "* ]]; then echo "$f"; seen="$seen$f "; fi; fi
  done
  for d in ${PACK_DIRS[@]+"${PACK_DIRS[@]}"}; do
    d="${d#*|}"; f="$(_base_pack_reqfile "$d")"; if [ -n "$f" ] && [[ "$seen" != *" $f "* ]]; then echo "$f"; seen="$seen$f "; fi
  done
  for m in "${BASE_STATE:-/nonexistent}"/packages/*.manifest; do
    [ -f "$m" ] || continue
    while IFS=$'\t' read -r name d _ path; do
      if [ "$name" = "pack" ] && [ -d "$path" ]; then f="$(_base_pack_reqfile "$path")"; if [ -n "$f" ] && [[ "$seen" != *" $f "* ]]; then echo "$f"; seen="$seen$f "; fi; fi
    done < "$m"
  done
}
_base_pack_imports_report(){ # <name|dir>… → for packs with no requirements file: what they import, and whether the venv has it. Advisory.
  [ "$#" -gt 0 ] || return 0
  if [ "$BASE_DRY" = "1" ]; then would "check every requirements-less pack's imports against $VENV"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then note "pack import check skipped (BASE_NO_NET: the fake venv has no packages)"; return 0; fi
  local nm req missing
  while IFS=$'\t' read -r nm req missing; do
    [ -n "$nm" ] || continue
    if [ -n "$missing" ]; then
      # a warning, not a stop: the scan cannot know whether the importing module is ever loaded. A pack that
      # fails to import outright is caught, and blocks the restart advice, in base_import_check.
      # 2.0.50: this used to assert "a node using it renders red". On the cold pod it said exactly that about the
      # gguf pack while /object_info showed LoaderGGUF and AILab_QwenVL_GGUF_PromptEnhancer both REGISTERED — the
      # scan sees imports the pack never reaches at load time. base_import_check is the one that actually knows.
      warn "$nm: no requirements file, and the venv cannot import ${missing//,/, } — a node that reaches one of those at run time fails (the import check below says whether the pack registered at all)"
    elif [ -n "$req" ]; then note "$nm: no requirements file — imports ${req//,/, }; all already provided"
    else note "$nm: no requirements file — imports nothing third-party"; fi
  done < <("$PY" "$BASE_DIR/py/import_scan.py" "$@" 2>/dev/null || true)
  return 0
}

base_packs(){ # base_packs git | pip
  local phase="${1:-git}" row name url sha cnr why dir out
  if [ "$phase" = "git" ]; then
    hdr "NODE PACKS · locate / clone / pin"
    [ "$BASE_DRY" = "1" ] || mkdir -p "$CN" 2>/dev/null || true
    PACK_DIRS=(); BASE_PACKS_LATEST_ROWS=()
    local rows; rows="$(_base_pack_rows)" || { err "the pack tables are invalid (see above)"; BASE_FAILED+=("packs: invalid rows"); return 1; }
    while IFS='|' read -r name url sha cnr why; do
      [ -n "$name" ] || continue
      dir="$(_base_pack_locate "$name" "$url")"
      if [ -z "$dir" ]; then
        todo "$name — $why"
        if [ "$BASE_DRY" = "1" ]; then would "git clone --depth 1 $url"; PACK_CLONED+=("$name (would clone)"); continue; fi
        if [ "$BASE_NO_NET" = "1" ]; then _base_fake_clone "$url" "$CN/$name"; dir="$CN/$name"; PACK_CLONED+=("$name (fake clone)")
        else
          # git's own exit status is the judge. Piping the clone through `grep -v | tail` made an EMPTY output (a quiet
          # clone on an image that prints no warnings) look like a failure under pipefail — every pack "failed" on a
          # pod where every directory was in place (2026-09-05).
          local cout="" crc=0
          cout="$(_base_git clone -q --depth 1 "$url" "$CN/$name" 2>&1)" || crc=$?
          [ -n "$cout" ] && { printf '%s\n' "$cout" | grep -v 'depth is ignored' | tail -2 || true; }
          if [ "$crc" = "0" ]; then dir="$CN/$name"; PACK_CLONED+=("$name")
          else err "clone failed: $name (git exit $crc)"; BASE_FAILED+=("pack: $name clone failed"); continue; fi
        fi
        if [ "$BASE_LATEST" != "1" ] && [ "$BASE_NO_NET" != "1" ]; then
          out="$(_base_pack_pin "$dir" "$url" "$sha")"
          if [ -n "$out" ]; then err "$name: $out"; BASE_FAILED+=("pack $name: $out")
          else PACK_CLONED[${#PACK_CLONED[@]}-1]="$name @ ${sha:0:12}"; ok "$name cloned at ${sha:0:12}"; fi
        else ok "$name cloned"; fi
      else
        if [ -d "$dir/.git" ] && _base_git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
          if [ -n "$(_base_git -C "$dir" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
            note "$name: local edits — left alone (not moved)"; PACK_DIRTY+=("$name"); BASE_WARN+=("pack $name has local edits, not updated")
          elif [ "$BASE_DRY" = "1" ]; then
            if [ "$BASE_LATEST" = "1" ]; then would "fetch $name and merge --ff-only onto the branch its remote publishes as HEAD"
            else would "check $name out at its pinned commit ${sha:0:12}"; fi
            PACK_PRESENT+=("$name")
          elif [ "$BASE_NO_NET" = "1" ]; then PACK_PRESENT+=("$name (no-net: not moved)")
          else
            local before after
            before="$(_base_git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo '?')"
            if [ "$BASE_LATEST" = "1" ]; then out="$(_base_pack_sync "$dir" "$url")"; else out="$(_base_pack_pin "$dir" "$url" "$sha")"; fi
            after="$(_base_git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo '?')"
            if [ -n "$out" ]; then
              PACK_PRESENT+=("$name @ $after")
              if [ "$BASE_LATEST" = "1" ]; then warn "$name not updated: $(_base_git_diag "$out")"
              else err "$name: $out"; BASE_FAILED+=("pack $name: $out"); fi   # an unreachable pin installs a pack the suite never saw
            elif [ "$before" != "$after" ]; then PACK_UPDATED+=("$name $before→$after"); ok "$name $before → $after"
            else PACK_PRESENT+=("$name @ $after"); ok "$name @ $after"; fi
          fi
        else PACK_PRESENT+=("$name (not a git checkout)"); note "$name: not a git checkout — left alone"; fi
      fi
      PACK_DIRS+=("$name|$dir")
      if [ "$BASE_LATEST" = "1" ] && [ -n "$dir" ]; then
        BASE_PACKS_LATEST_ROWS+=("$name|$url|$(_base_git -C "$dir" rev-parse HEAD 2>/dev/null || echo "$sha")|$cnr|$why")
      fi
    done <<< "$rows"
    if [ "$BASE_LATEST" = "1" ] && [ "$BASE_DRY" != "1" ]; then
      note "--latest: packs are at their remotes' HEAD, NOT at the commits the suites were green against."
      note "Paste these rows back into BASE_PACKS / PACKS, then re-run the suites before trusting them:"
      printf '  "%s"\n' "${BASE_PACKS_LATEST_ROWS[@]}"
      BASE_WARN+=("--latest: packs are off their pinned commits; the suites have not been run against these")
    fi
    # packs a package used to install and no longer needs: reported only when no installed package claims them
    for name in ${DROPPED_PACKS:-}; do
      if [ -d "$CN/$name" ] && ! _base_ledger_pack_claimed "$name"; then note "$name is no longer used by ${PKG_NAME:-this package} — still installed, remove by hand if unwanted"; fi
    done
    return 0
  fi
  # ---- pip half: every located pack's requirements under the torch constraint, every run
  hdr "NODE PACKS · requirements into $VENV"
  local d rf entry probe=()
  CONSTRAINTS="${CONSTRAINTS:-$BASE_STATE/constraints-torch.txt}"
  # ComfyUI's OWN requirements, every run — not only when the venv is BUILT. Seen on a live pod:
  # --latest moved ComfyUI 0.34.6 -> 0.35.0 on
  # a REUSED venv, so _base_venv_build never ran, ComfyUI's four new pins were never installed, and
  # main.py died on `ModuleNotFoundError: No module named 'comfy_aimdo.malloc_graph'`. Every PACK's
  # requirements were re-installed that run; the thing every pack sits on was not.
  #
  # -c "$CONSTRAINTS" is what makes this safe and is why the emergency repair needed --no-deps: a
  # bare `pip install -r $COMFY/requirements.txt` dies in resolution against the installed cu130
  # torch. Under the constraint file it is the same call _base_venv_build already makes.
  if [ -f "$COMFY/requirements.txt" ]; then
    if [ "$BASE_DRY" = "1" ]; then would "install ComfyUI's own requirements.txt under the torch constraint"
    elif [ "$BASE_NO_NET" = "1" ]; then note "ComfyUI requirements: pip skipped (BASE_NO_NET)"
    elif _base_pip install -q --upgrade --upgrade-strategy only-if-needed -c "$CONSTRAINTS" -r "$COMFY/requirements.txt" 2>&1 | tail -2; then ok "ComfyUI requirements (requirements.txt)"
    else miss "ComfyUI requirements failed"; BASE_FAILED+=("comfyui: own requirements failed"); fi
  fi
  for entry in ${PACK_DIRS[@]+"${PACK_DIRS[@]}"}; do
    name="${entry%%|*}"; d="${entry#*|}"; rf="$(_base_pack_reqfile "$d")"
    if [ -z "$rf" ]; then probe+=("$name|$d"); continue; fi
    if [ "$BASE_DRY" = "1" ]; then would "install $(basename "$rf") into the venv under the torch constraint ($name)"; continue; fi
    if [ "$BASE_NO_NET" = "1" ]; then note "$name: pip skipped (BASE_NO_NET)"; continue; fi
    if _base_pip install -q --upgrade --upgrade-strategy only-if-needed -c "$CONSTRAINTS" -r "$rf" 2>&1 | tail -2; then ok "$name requirements ($(basename "$rf"))"
    else miss "$name requirements failed"; BASE_FAILED+=("pack: $name requirements failed"); fi
  done
  # PIP_EXTRA (a package's extra wheels, e.g. ninja) every run, not only when the venv is built: a venv the base built
  # for another package, or before any package, has never seen this package's extras
  local -a extras=( ${PIP_EXTRA[@]+"${PIP_EXTRA[@]}"} )          # PIP_EXTRA may be undeclared (bash 3.2 + set -u)
  if [ "${#extras[@]}" -gt 0 ]; then
    if [ "$BASE_DRY" = "1" ]; then would "install PIP_EXTRA into the venv under the torch constraint: ${extras[*]}"
    elif [ "$BASE_NO_NET" = "1" ]; then note "PIP_EXTRA skipped (BASE_NO_NET): ${extras[*]}"
    elif _base_pip install -q --upgrade --upgrade-strategy only-if-needed -c "$CONSTRAINTS" --extra-index-url https://pypi.nvidia.com "${extras[@]}" 2>&1 | tail -2; then ok "PIP_EXTRA: ${extras[*]}"
    else miss "PIP_EXTRA install failed: ${extras[*]}"; BASE_FAILED+=("pip: PIP_EXTRA failed (${extras[*]})"); fi
  fi
  # A pack with no requirements file is only fine if what it imports is already in the venv. Writing one
  # into the checkout is not the fix (it dirties the tree and the pack is never updated again).
  _base_pack_imports_report ${probe[@]+"${probe[@]}"}
}

_base_sage_ver(){ # SageAttention 2 sets no __version__ attribute — the success line printed blank on the cold pod
  "$PY" -c 'import importlib.metadata as m; print(m.version("sageattention"))' 2>/dev/null || echo "(installed, no version metadata)"
}
BASE_CUDA_BUILD_OK=""      # "" not probed · 1 the toolkit can link cuBLAS · 0 it cannot
base_cuda_toolchain(){ # CAN THIS TOOLKIT LINK WHAT PACKAGES BUILD? "nvcc exists" is not the same question.
  # runpod/comfyui:1.4.7-cuda13.0 ships nvcc + cudart ONLY — no cuBLAS lib and no headers. llama-cpp-python died at
  # CMake ("Target ggml-cuda links to CUDA::cublas but the target was not found") and SageAttention died at nvcc
  # exit 255, seven minutes in — and because both builders return 0 on failure the run then downloaded 131 GB and
  # reported the wreck at the end. A two-second test compile answers the real question before any of that. The
  # older pod images all carried a full toolkit, which is the only reason this never showed up. (cold pod, 2026-09-08)
  [ -n "$BASE_CUDA_BUILD_OK" ] && return 0
  local nvcc="${CUDACXX:-/usr/local/cuda/bin/nvcc}"
  [ -x "$nvcc" ] || nvcc="$(command -v nvcc 2>/dev/null || true)"
  if [ -z "$nvcc" ] || [ ! -x "$nvcc" ]; then BASE_CUDA_BUILD_OK=0; return 0; fi   # no toolkit: the builders say so
  _base_tmp
  local src="$BASE_TMPD/cublas_probe.cu" bin="$BASE_TMPD/cublas_probe" ver pkg
  printf '%s\n' '#include <cublas_v2.h>' 'int main(){ cublasHandle_t h; cublasCreate(&h); cublasDestroy(h); return 0; }' > "$src"
  if "$nvcc" -o "$bin" "$src" -lcublas >/dev/null 2>&1; then
    BASE_CUDA_BUILD_OK=1; ok "CUDA toolchain links cuBLAS — source builds can proceed"; return 0
  fi
  ver="$("$nvcc" --version 2>/dev/null | sed -nE 's/.*release ([0-9]+)\.([0-9]+).*/\1-\2/p' | head -1)"
  pkg="cuda-libraries-dev-${ver:-13-0}"
  miss "the CUDA toolkit cannot link cuBLAS — nvcc is present but the math libraries are not"
  if [ "$BASE_DRY" = "1" ]; then
    would "apt-get install $pkg, then re-probe (source builds need cuBLAS: llama-cpp-python, SageAttention)"
    BASE_CUDA_BUILD_OK=0; return 0
  fi
  if command -v apt-get >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
    note "installing $pkg (a source build links cuBLAS; the image shipped nvcc + cudart only)"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$pkg" >/dev/null 2>&1 \
      || { apt-get update -qq >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$pkg" >/dev/null 2>&1; } || true
    if "$nvcc" -o "$bin" "$src" -lcublas >/dev/null 2>&1; then
      BASE_CUDA_BUILD_OK=1; ok "$pkg installed — the toolchain links cuBLAS now"; BASE_CHANGED+=("$pkg installed (the image shipped no cuBLAS)"); return 0
    fi
  fi
  BASE_CUDA_BUILD_OK=0
  BASE_WARN+=("CUDA toolkit incomplete: nvcc cannot link cuBLAS. Source builds (llama-cpp-python, SageAttention) will be skipped. Remedy: apt-get install -y $pkg")
  warn "cuBLAS still missing after trying $pkg — CUDA source builds will be SKIPPED, not attempted"
  return 0
}

base_build_sageattention(){ # for a package's pkg_post_venv: SageAttention 2 built from source for THIS GPU's sm (no PyPI wheel has
  # sm_100/sm_120 kernels; 1.0.6 crashes there). SAGE_WHEEL=<url-or-path> installs that instead; SAGE_REF pins the git ref
  # (default main). A failed build FAILS the run. Here because several packages want it and a missing build died at
  # the first render with `No module named 'sageattention'` (2.0.22).
  if [ "$BASE_DRY" = "1" ]; then would "build SageAttention from source for ${BASE_GPU_SM:-the GPU} into $VENV unless it already imports"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then note "SageAttention: skipped (fake venv — BASE_NO_NET)"; return 0; fi
  if "$PY" -c "import sageattention" >/dev/null 2>&1; then ok "sageattention $(_base_sage_ver) already in $VENV"; return 0; fi
  if [ -n "${SAGE_WHEEL:-}" ]; then
    echo "  installing SageAttention from SAGE_WHEEL=$SAGE_WHEEL"
    _base_pip install -q "$SAGE_WHEEL" 2>&1 | tail -3 || true
  else
    local cc="${BASE_GPU_SM:-}"; cc="${cc#sm_}"
    if [ -n "${SAGE_ARCHS:-}" ]; then cc="$SAGE_ARCHS"             # 2.1.0: the image build has no GPU and wants several ("9.0;12.0")
    else
      if [ -z "$cc" ]; then err "no CUDA device visible — SageAttention needs the GPU's compute capability to build (or pass SAGE_WHEEL=<wheel>, or SAGE_ARCHS=<list>)"; BASE_FAILED+=("SageAttention: no GPU visible, not built"); return 0; fi
      cc="${cc%?}.${cc: -1}"                                        # sm_120 → 12.0 · sm_100 → 10.0 · sm_89 → 8.9
    fi
    base_cuda_toolchain
    if [ "$BASE_CUDA_BUILD_OK" != "1" ]; then                       # 7 minutes of nvcc to reach a link error we can predict
      err "SageAttention links cuBLAS and this toolkit cannot — not starting a build that cannot finish"
      BASE_FAILED+=("SageAttention: the CUDA toolkit cannot link cuBLAS (the toolchain line above names the fix)"); return 0
    fi
    # ---- the cache key is the RESOLVED UPSTREAM COMMIT, not a version. A cache keyed on a version would be a pin:
    # SAGE_REF defaults to main, main moves, and a stale wheel would be served forever — the "stale pin is the same
    # debt with a delay" this repo refuses. Keyed on the commit (plus the venv's python, torch and the GPU's sm, all
    # of which change the artefact) the cache can only ever skip rebuilding byte-identical source. ls-remote resolves
    # the ref in about a second, so a hit costs no clone at all. A 10-20 minute build becomes a ~20 second install.
    local cache="$BASE_STATE/wheels" sha key hit pytag tv
    sha="$(git ls-remote https://github.com/thu-ml/SageAttention "${SAGE_REF:-main}" 2>/dev/null | awk 'NR==1{print substr($1,1,12)}')"
    pytag="$("$PY" -c 'import sys; print("cp%d%d" % sys.version_info[:2])' 2>/dev/null)"
    tv="$("$PY" -c 'import torch; print(torch.__version__.split("+")[0])' 2>/dev/null)"
    key="sageattention-${sha:-unresolved}-${pytag:-cp}-torch${tv:-0}-${SAGE_ARCHS:+archs-}$(printf '%s' "${SAGE_ARCHS:-${BASE_GPU_SM:-sm}}" | tr ';.' '_-')"
    hit=""; [ -n "$sha" ] && hit="$(ls "$cache/$key"/*.whl 2>/dev/null | head -1)"
    if [ -n "$hit" ]; then
      ok "SageAttention: a cached wheel matches this exact source and venv — installing instead of a 10-20 min build"
      note "$(basename "$hit")  ($key)"
      _base_pip install -q --force-reinstall --no-deps "$hit" 2>&1 | tail -2 || true
      if "$PY" -c "import sageattention" >/dev/null 2>&1; then
        BASE_CHANGED+=("SageAttention installed from the cached wheel ($sha)"); return 0
      fi
      warn "the cached wheel did not import — rebuilding from source and replacing it"; rm -rf "$cache/$key"
    fi
    _base_pip install -q ninja 2>&1 | tail -1 || true                 # the build's generator
    # torch 2.14's headers demand C++20 and upstream's setup.py hardcodes -std=c++17: every kernel failed at the first
    # include on the pod (2.0.25). Clone the ref, patch the flag, build from the checkout — pip sees the patched tree.
    local src="$BASE_STATE/sageattention-src"; rm -rf "$src"
    if ! git clone -q --depth 1 --branch "${SAGE_REF:-main}" https://github.com/thu-ml/SageAttention "$src" 2>&1 | tail -2; then
      err "could not clone SageAttention (${SAGE_REF:-main})"; BASE_FAILED+=("SageAttention: clone failed"); return 0
    fi
    sed -i.bak 's/-std=c++17/-std=c++20/g' "$src/setup.py" && rm -f "$src/setup.py.bak"
    note "SageAttention $(git -C "$src" rev-parse --short HEAD 2>/dev/null) — setup.py patched: -std=c++17 → -std=c++20 (torch ≥ 2.14 headers)"
    echo "  building SageAttention from source for ${SAGE_ARCHS:-$BASE_GPU_SM} (TORCH_CUDA_ARCH_LIST=$cc, MAX_JOBS=${MAX_JOBS:-32}; 10–20 min, a heartbeat prints while it runs)"
    # built as a WHEEL, not installed straight in: the wheel is the artefact the next venv rebuild reuses
    mkdir -p "$cache/$key"
    if ! TORCH_CUDA_ARCH_LIST="$cc" MAX_JOBS="${MAX_JOBS:-32}" CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}" PATH="/usr/local/cuda/bin:$PATH" \
         _base_run_watched "SageAttention build" _base_pip wheel --no-build-isolation --no-deps -w "$cache/$key" "$src"; then
      err "the SageAttention build failed (its last lines are above)"; rm -rf "$cache/$key"
    else
      hit="$(ls "$cache/$key"/*.whl 2>/dev/null | head -1)"
      if [ -n "$hit" ]; then _base_pip install -q --no-deps "$hit" 2>&1 | tail -2 || true
        ok "wheel cached at $cache/$key — the next venv rebuild on this pod skips the build"
      else err "the build produced no wheel"; rm -rf "$cache/$key"; fi
    fi
    rm -rf "$src"
  fi
  if "$PY" -c "import sageattention" >/dev/null 2>&1; then ok "sageattention $(_base_sage_ver) — built for $BASE_GPU_SM; a ⚡ Sage patch may go ON"
  else err "sageattention does not import in $VENV — a Sage patch node would fail at queue time"; BASE_FAILED+=("SageAttention not installed (SAGE_WHEEL=<url-or-path> skips the source build)"); fi
  return 0
}

base_consolidate(){ # what is already installed elsewhere on this pod: strays of ours moved in, the rest reported
  hdr "CONSOLIDATE · what is already installed on this pod"
  local VOLROOT="${BASE_PERSIST_ROOT:-$BASE_VOLUME}"; [ -n "$BASE_FAKE_ROOT" ] && VOLROOT="$BASE_FAKE_ROOT"
  local d name url sha cnr why other rows
  # ---- other ComfyUI trees: reported, never touched
  local others=()
  while IFS= read -r d; do
    if [ -n "$d" ] && [ -f "$d/main.py" ] && [ -d "$d/comfy" ] && [ "$d" != "$COMFY" ]; then others+=("$d"); fi
  done < <(_base_find_dirs 'ComfyUI' 'ComfyUI-*' 'comfyui')
  if [ "${#others[@]}" -gt 0 ]; then
    for other in "${others[@]}"; do warn "another ComfyUI tree: $other ($(_base_bytes_to_gb "$(_base_dir_bytes "$other")") GB) — this run uses $COMFY; models may be split across two libraries"; done
  else ok "no second ComfyUI tree"; fi
  # ---- stray node packs: a pack of ours cloned somewhere other than custom_nodes is invisible to ComfyUI
  local moved=0 dupe=0
  rows="$(_base_pack_rows 2>/dev/null || true)"
  while IFS='|' read -r name url sha cnr why; do
    [ -n "$name" ] || continue
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      case "$d/" in "$CN"/*) continue;; esac
      if [ ! -f "$d/__init__.py" ] && [ ! -d "$d/.git" ]; then continue; fi
      if [ -e "$CN/$name" ]; then
        case "$d/" in "$VOLROOT"/*) ;; *) note "$name also at $d — the image's own copy, outside the volume (left alone; ComfyUI loads $CN only)"; continue;; esac
        miss "$name also at $d — duplicate of $CN/$name"
        if [ "$BASE_DRY" = "1" ]; then would "queue $d for deletion"; else DEL_DIRS+=("$d"); DEL_BYTES=$((DEL_BYTES + $(_base_dir_bytes "$d"))); dupe=$((dupe + 1)); fi
      elif [ "$BASE_DRY" = "1" ]; then would "move $d → $CN/$name"
      else
        mkdir -p "$CN"
        if mv "$d" "$CN/$name" 2>/dev/null; then ok "$name moved into custom_nodes ($d → $CN/$name)"; moved=$((moved + 1))
        else warn "stray pack not moved: $d"; fi
      fi
    done < <(_base_find_dirs "$name")
  done <<< "$rows"
  if [ "$moved" = "0" ] && [ "$dupe" = "0" ]; then ok "no stray node packs outside custom_nodes"; fi
  # ---- orphaned venvs and backups: reported with sizes, never removed here
  local vfound=0 vb
  while IFS= read -r d; do
    if [ -z "$d" ] || [ "$d" = "$VENV" ]; then continue; fi
    vb="$(_base_dir_bytes "$d")"; vfound=$((vfound + 1))
    case "$d" in
      *.pre-*) note "venv backup: $d ($(_base_bytes_to_gb "$vb") GB) — kept as the rollback for $VENV; rm -rf '$d' to reclaim it";;
      *)       warn "other venv: $d ($(_base_bytes_to_gb "$vb") GB) — not touched; remove it by hand if it is dead";;
    esac
  done < <(_base_find_dirs '.venv-*' '.venv' '*.pre-*')
  if [ "$vfound" = "0" ]; then ok "no orphaned venvs or old backups"; fi
  return 0
}

base_list_packs(){ # <pkg dir>... → deduplicated dir|url|sha rows (base packs + every package's), exit 1 on a SHA conflict
  local d s out rc=0
  out="$(printf '%s\n' "${BASE_PACKS[@]}")"
  for d in "$@"; do
    d="${d%/}"; s="$(ls "$d"/*"-script.sh" 2>/dev/null | head -1 || true)"
    if [ -z "$s" ]; then echo "  !! no '*-script.sh' in $d" >&2; rc=1; continue; fi
    if [ "$(cd "$d" && pwd -P)" = "$BASE_DIR" ]; then continue; fi                       # the base's own entry script is not a package
    if ! grep -q 'base_main' "$s"; then echo "  ○ $(basename "$s") does not source the base yet — its packs are not derived" >&2; continue; fi
    local got; got="$(BASE_DECLARE_ONLY=1 bash "$s" | grep '^PACKROW ' | cut -c9- || true)"
    if [ -n "$got" ]; then out="$out"$'\n'"$got"; fi
  done
  printf '%s\n' "$out" | awk -F'|' 'NF>=3 && $1!="" { if ($1 in sha) { if (sha[$1]!=$3) { print "  !! " $1 " pinned to two SHAs: " sha[$1] " vs " $3 > "/dev/stderr"; bad=1 } } else { sha[$1]=$3; print $1 "|" $2 "|" $3 } } END { exit bad }' || rc=1
  return $rc
}
