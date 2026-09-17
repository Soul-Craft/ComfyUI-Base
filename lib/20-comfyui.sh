# 20-comfyui.sh — tokens (asked once, stored 0600), ComfyUI to its newest release tag, the version floor.

_base_pid1_env(){ # <NAME> — the pod's own environment (PID 1's), which an ssh session does not inherit
  local name="$1" f
  if [ -n "$BASE_FAKE_PID1_ENV" ]; then [ -f "$BASE_FAKE_PID1_ENV" ] && sed -n "s/^$name=//p" "$BASE_FAKE_PID1_ENV" | head -1
  elif [ -n "$BASE_FAKE_ROOT" ]; then :                              # a fake run has NO other PID 1: never the host's (a real token leaked into a test log on the pod, 2.0.4)
  else f="${BASE_PID1_ENVIRON:-/proc/1/environ}"                     # BASE_PID1_ENVIRON = the suite's stand-in for /proc/1/environ
    [ -r "$f" ] && tr '\0' '\n' < "$f" 2>/dev/null | sed -n "s/^$name=//p" | head -1
  fi
  return 0
}
_base_token_is_placeholder(){ # the values templates ship as "fill me in"
  local v; v="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$v" in ""|token_here|replace_with_ids|changeme|your_token|your_api_key|"<"*) return 0;; esac
  return 1
}
_base_token_aliases(){ # the names other templates use for the same secret; the base's name first
  case "$1" in
    HF_TOKEN)      echo "HF_TOKEN HUGGING_FACE_HUB_TOKEN HF_HUB_TOKEN";;
    *)             echo "$1";;
  esac
}
_base_auth_file(){ # <NAME> → a 0600 file holding "Authorization: Bearer <value>", for curl -H @file (never argv)
  local name="$1" f="$BASE_TMPD/.auth-$name"
  ( umask 077; printf 'Authorization: Bearer %s\n' "${!name}" > "$f" ); echo "$f"
}
_base_token_validate(){ # <NAME> <from> — 200 accepted; 401/403 FAIL naming the source, never the value; else "could not validate"
  local name="$1" from="$2" url="" code
  if [ -n "$BASE_FAKE_ROOT" ] && [ -z "${BASE_TOKEN_CHECK_URL_HF:-}" ]; then
    note "$name present ($from) — not validated: a fake run touches no network"; return 0     # 2.0.15: a fake-mode test had reached huggingface.co
  fi
  case "$name" in
    HF_TOKEN)      url="${BASE_TOKEN_CHECK_URL_HF:-https://huggingface.co/api/whoami-v2}";;
    *) return 0;;
  esac
  if [ "$BASE_DRY" = "1" ]; then would "validate $name against ${url#https://}"; return 0; fi
  # offline runs validate only against an override (the suite's fake service)
  if [ "$BASE_NO_NET" = "1" ] && [ -z "${BASE_TOKEN_CHECK_URL_HF:-}" ]; then return 0; fi
  [ -n "${BASE_TMPD:-}" ] || _base_tmp
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "@$(_base_auth_file "$name")" "$url" 2>/dev/null || echo 000)"
  case "$code" in
    200) ok "$name accepted by ${url#https://}";;
    401|403) err "$name ($from) rejected by ${url#https://} (HTTP $code) — fix that token; nothing downloads until it is accepted"
             BASE_FAILED+=("$name ($from) rejected ($code)");;
    *) warn "could not validate $name against ${url#https://} (HTTP $code) — continuing";;
  esac
  return 0
}
base_tokens(){ # TOKENS rows: NAME|optional|why. Shell env → PID 1's env (aliases) → state/tokens.env → TTY prompt → anonymous. Never re-asked.
  hdr "TOKENS"
  TOKENS_FILE="$BASE_STATE/tokens.env"
  local row why optional from new="" alias v
  for row in ${TOKENS[@]+"${TOKENS[@]}"}; do
    IFS='|' read -r name optional why <<< "$row"
    [ -n "$name" ] || continue
    val=""; from=""
    for alias in $(_base_token_aliases "$name"); do              # 1. the shell environment
      v="${!alias:-}"; [ -n "$v" ] || continue
      if _base_token_is_placeholder "$v"; then note "$name in the environment ($alias) is the placeholder '$v' — ignored"; continue; fi
      val="$v"; from="env"; [ "$alias" != "$name" ] && from="env: $alias"; break
    done
    if [ -z "$val" ]; then                                       # 2. the pod's own environment (PID 1)
      for alias in $(_base_token_aliases "$name"); do
        v="$(_base_pid1_env "$alias")"; [ -n "$v" ] || continue
        if _base_token_is_placeholder "$v"; then note "$name in the pod env ($alias) is the placeholder '$v' — ignored"; continue; fi
        val="$v"; from="pod env"; [ "$alias" != "$name" ] && from="pod env: $alias"; break
      done
    fi
    if [ -z "$val" ] && [ -f "$TOKENS_FILE" ]; then             # 3. the file an earlier run stored
      val="$(grep -m1 "^$name=" "$TOKENS_FILE" | cut -d= -f2- | tr -d '"'"'" || true)"; from="$TOKENS_FILE"
    fi
    if [ -n "$val" ]; then
      ok "$name present ($from)"
    elif [ "$BASE_DRY" = "1" ]; then
      note "no $name (not prompted in --check) — $why"
    elif [ "$BASE_TTY" = "1" ]; then                            # 4. the operator, once
      printf '%s' "$name ($why; Enter to skip): "
      IFS= read -r val || val=""
      val="${val//[[:space:]]/}"; from="typed"
    else
      note "no TTY and no $name — continuing without it ($why)"
    fi
    if [ -n "$val" ] && [ "$BASE_DRY" != "1" ] && ! grep -qs "^$name=" "$TOKENS_FILE" 2>/dev/null; then
      new="$new$name"$'\n'
      mkdir -p "$BASE_STATE"; chmod 700 "$BASE_STATE"
      ( umask 077; printf '%s=%s\n' "$name" "$val" >> "$TOKENS_FILE" ); chmod 600 "$TOKENS_FILE"
    fi
    printf -v "$name" '%s' "$val"; export "$name"
    [ -n "$val" ] && _base_token_validate "$name" "$from" || true
  done
  [ -n "$new" ] && ok "saved to $TOKENS_FILE (mode 600) — you will not be asked again" || true
  _base_tokens_export_buttons
}
_base_tokens_export_buttons(){ # 2.1.0: the Hub browser pack reads its own name (HF_TOKEN itself, and HUGGINGFACE_API_KEY for the
  # packs that use that name). The values ride the environment ComfyUI is started with (85-launch.sh), never a log or argv.
  # Called by base_tokens; boot.sh, which sources none of lib/[0-9]*, carries the same lines as boot_tokens.
  local names=""
  if [ -n "${HF_TOKEN:-}" ]; then export HUGGINGFACE_API_KEY="$HF_TOKEN"; names="HUGGINGFACE_API_KEY"; fi
  [ -n "$names" ] && note "exported for the download buttons: $names" || true
}

# ---------------------------------------------------------------- git helpers
_base_git_diag(){ # the line that actually says what went wrong, whatever shape the git error takes
  local all err
  all="$(printf '%s\n' "$1" | grep -vE '^[[:space:]]*$' || true)"
  err="$(printf '%s\n' "$all" | grep -E '^(fatal|error|warning):|^ *! |\[rejected\]|would clobber' || true)"
  if [ -n "$err" ]; then printf '%s\n' "$err" | sed -n '1p'; return 0; fi
  # a fetch opens with a "From <url>" banner and the useful line is the last; a pull puts its diagnosis first
  if printf '%s\n' "$all" | sed -n '1p' | grep -q '^From '; then
    err="$(printf '%s\n' "$all" | grep -vE '^From ' || true)"
    if [ -n "$err" ]; then printf '%s\n' "$err" | sed -n '$p'; return 0; fi
  fi
  printf '%s\n' "$all" | sed -n '1p'
  return 0
}
_base_git_remote(){ # origin, else whatever single remote the template configured
  if _base_git -C "$1" remote get-url origin >/dev/null 2>&1; then echo origin; return 0; fi
  _base_git -C "$1" remote 2>/dev/null | head -1 || true
}

_base_fake_comfy_tree(){ # BASE_NO_NET: the fake "clone" — the fake image's code files when there is a fake image, else a minimal tree
  local t="$1" src="${BASE_FAKE_IMAGE_ROOT:+$BASE_FAKE_IMAGE_ROOT/ComfyUI}" f
  mkdir -p "$t/comfy_extras"
  if [ -n "$src" ] && [ -f "$src/main.py" ]; then
    for f in main.py requirements.txt comfyui_version.py; do [ -f "$src/$f" ] && cp "$src/$f" "$t/$f" || true; done
    [ -d "$src/comfy_extras" ] && cp -R "$src/comfy_extras/." "$t/comfy_extras/" || true
  else
    : > "$t/main.py"; : > "$t/requirements.txt"; printf '__version__ = "0.34.7"\n' > "$t/comfyui_version.py"
  fi
}
base_comfy_materialize(){ # the volume holds no ComfyUI tree: create one at $VOL/ComfyUI, beside whatever persist dirs are already there
  local target="$VOL/ComfyUI" tag="" d
  [ -f "$target/main.py" ] && return 0
  hdr "COMFYUI TREE · none on $VOL"
  if [ -d "$target/.git" ]; then err "$target has a .git but no main.py — half a checkout; move it aside and re-run"; BASE_FAILED+=("half a checkout at $target"); return 1; fi
  if [ "$BASE_DRY" = "1" ]; then would "materialise $target at the newest ComfyUI release tag (beside the models/user/output/input/custom_nodes already there)"; return 4; fi
  mkdir -p "$target" || { err "cannot create $target"; return 1; }
  if [ "$BASE_NO_NET" = "1" ]; then
    _base_fake_comfy_tree "$target"; tag="(fake tree)"
  else
    if ! _base_git -C "$target" rev-parse --git-dir >/dev/null 2>&1; then
      _base_git init -q "$target" && _base_git -C "$target" remote add origin https://github.com/comfyanonymous/ComfyUI.git \
        || { err "git init failed in $target"; BASE_FAILED+=("materialise: git init"); return 1; }
    fi
    if [ -n "${COMFY_TAG:-}" ]; then tag="$COMFY_TAG"; note "COMFY_TAG=$tag: materialising the tag the image was built with, not the newest"   # 2.1.0: the seed build and pinned pods
    else tag="$(_base_git -C "$target" ls-remote --tags --refs origin 'v*' 2>/dev/null | awk '{print $2}' | sed 's|refs/tags/||' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1 || true)"; fi
    [ -n "$tag" ] || { err "could not read ComfyUI's release tags from GitHub"; BASE_FAILED+=("materialise: no release tag"); return 1; }
    if ! _base_git -C "$target" fetch -q --depth 1 origin "refs/tags/$tag:refs/tags/$tag" || ! _base_git -C "$target" checkout -q -f -B comfy-base-stable "$tag"; then
      err "fetch/checkout of $tag failed in $target"; BASE_FAILED+=("materialise: $tag"); return 1
    fi
  fi
  for d in models custom_nodes user/default/workflows input output; do mkdir -p "$target/$d"; done
  ok "materialised $target at ${tag} — the persist dirs beside it are untouched"
  BASE_CHANGED+=("materialised ComfyUI at $target ($tag)")
  return 0
}
base_update_comfyui(){ # newest v* release tag, checked out on branch comfy-base-stable (a branch: Manager's updater keeps working)
  hdr "COMFYUI"
  local tag head_at_tag=0 dirty=0 rmt
  COMFY_OLD="$(_base_comfy_version "$COMFY" 2>/dev/null)"; COMFY_NEW="$COMFY_OLD"
  echo "  current      v$COMFY_OLD"
  # 2.1.0: a pinned pod (the template's image) runs the ComfyUI the image was tested with: no fetch, no checkout, no
  # remote asked. The version gate still judges COMFY_OLD; a seed below a package's COMFY_MIN fails there, loudly.
  if [ "$BASE_PINNED" = "1" ]; then ok "pinned at v$COMFY_OLD (BASE_PINNED=1${COMFY_TAG:+, image tag $COMFY_TAG}) — no fetch, no checkout"; return 0; fi
  if ! _base_git -C "$COMFY" rev-parse --git-dir >/dev/null 2>&1; then
    warn "ComfyUI $COMFY_OLD is not a git checkout — left as the template shipped it; the version gate still applies"; return 0
  fi
  rmt="$(_base_git_remote "$COMFY")"
  if [ "$BASE_NO_NET" = "1" ]; then note "BASE_NO_NET: no fetch"
  elif [ -z "$rmt" ]; then
    err "ComfyUI checkout has no git remote — the release tags are unreachable, staying on $COMFY_OLD"
    BASE_FAILED+=("ComfyUI: no git remote, still $COMFY_OLD"); return 0
  elif [ "$BASE_DRY" = "1" ]; then
    # A dry run must not fetch — but it can ask the remote, read-only, what it WOULD move to, so the gate
    # judges the post-update version instead of the current one.
    local rtag
    rtag="$(_base_git -C "$COMFY" ls-remote --tags --refs "$rmt" 'v*' 2>/dev/null | awk '{print $2}' | sed 's|refs/tags/||' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1 || true)"
    if [ -n "$rtag" ]; then COMFY_NEW="${rtag#v}"; would "checkout -B comfy-base-stable $rtag   (ComfyUI $COMFY_OLD → $COMFY_NEW; the gate judges $COMFY_NEW)"
    else would "fetch tags and check out the newest v* tag"; note "could not reach the remote — the gate judges the CURRENT version"; fi
    return 0
  else
    local fetch_out fetch_rc=0
    # --force: the template ships tags upstream has since moved; without it the whole fetch is refused
    fetch_out="$(_base_git -C "$COMFY" fetch --tags --force --prune "$rmt" 2>&1)" || fetch_rc=$?
    # a --depth 1 template cannot see release tags until it is unshallowed
    if [ "$fetch_rc" -ne 0 ] && [ "$(_base_git -C "$COMFY" rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
      note "shallow checkout — unshallowing so the release tags become reachable"
      fetch_rc=0; fetch_out="$(_base_git -C "$COMFY" fetch --unshallow --tags --force --prune "$rmt" 2>&1)" || fetch_rc=$?
    fi
    if [ "$fetch_rc" -ne 0 ]; then
      err "git fetch from '$rmt' failed (exit $fetch_rc) — staying on $COMFY_OLD"
      echo "      $(_base_git_diag "$fetch_out")"
      BASE_FAILED+=("ComfyUI: git fetch failed, still $COMFY_OLD"); return 0
    fi
  fi
  tag="$(_base_git -C "$COMFY" tag -l 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true)"
  if [ -z "$tag" ]; then note "no release tags visible — ComfyUI left at $COMFY_OLD"; return 0; fi
  [ "$(_base_git -C "$COMFY" rev-parse HEAD)" = "$(_base_git -C "$COMFY" rev-parse "$tag^{commit}")" ] && head_at_tag=1 || true
  if [ "$head_at_tag" = "1" ]; then
    COMFY_NEW="${tag#v}"; ok "ComfyUI already at $tag (branch $(_base_git -C "$COMFY" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?'))"; return 0
  fi
  if [ "$BASE_DRY" = "1" ]; then would "checkout -B comfy-base-stable $tag"; COMFY_NEW="${tag#v}"; return 0; fi
  _base_git -C "$COMFY" diff --quiet && _base_git -C "$COMFY" diff --cached --quiet || dirty=1
  local ts; ts="$(_base_ts)"
  if [ "$dirty" = "1" ]; then
    if _base_git -C "$COMFY" stash push -q -m "comfy-base $ts"; then
      BASE_STASH_CMD="git -C \"$COMFY\" stash pop"
      miss "local edits stashed as 'comfy-base $ts' — restore with: $BASE_STASH_CMD"
    else miss "stash failed — checkout may refuse"; fi
  fi
  if _base_git -C "$COMFY" checkout -q -B comfy-base-stable "$tag"; then
    COMFY_NEW="${tag#v}"; ok "ComfyUI $COMFY_OLD → $COMFY_NEW (branch comfy-base-stable at $tag)"
    BASE_CHANGED+=("ComfyUI $COMFY_OLD → $COMFY_NEW")
  else
    miss "checkout $tag failed — staying on $COMFY_OLD"; BASE_WARN+=("ComfyUI: checkout $tag failed, still $COMFY_OLD")
  fi
}

base_comfy_gate(){ # an out-of-date ComfyUI wrecked a run only *after* 123 GB had been pulled — gate first
  local cur="${COMFY_NEW:-${COMFY_OLD:-0}}" want="${COMFY_MIN:-0.34.0}"
  [ -z "$cur" ] && cur=0
  if _base_vge "$cur" "$want"; then ok "version gate: $cur >= $want"; return 0; fi
  if [ "$BASE_DRY" = "1" ]; then would "abort here: ComfyUI $cur is below the $want floor"; return 0; fi
  err "ComfyUI $cur is older than $want — the release line this graph was validated against."
  echo "      Nothing was downloaded and the venv is untouched; the running server is unaffected."
  echo "      Repair the checkout, then re-run:"
  echo "        git -C \"$COMFY\" fetch --tags --force --prune origin"
  echo "        git -C \"$COMFY\" checkout -B comfy-base-stable \"\$(git -C \"$COMFY\" tag -l 'v*' --sort=-v:refname | head -1)\""
  BASE_FAILED+=("ComfyUI $cur < required $want — stopped before any download")
  return 1
}
