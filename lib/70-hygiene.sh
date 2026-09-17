# 70-hygiene.sh — the base-owned comfyui_args.txt (state/) edited by flag AND value, ComfyUI-Manager's security level,
# extra_model_paths.yaml registrations. The base owns the preview flags and the API-node switch; packages add their own
# through HYGIENE_ARGS and drop an image's through HYGIENE_ARGS_REMOVE. An image's own args file is imported once.

_base_args_import(){ # the base file is created once — from the image's own args file when there is one, else empty
  [ -f "$ARGS_FILE" ] && return 0
  local hdr="# comfy-base launch args — the only source of ComfyUI's flags on this pod; edit here, never an image's file"
  if [ "$BASE_DRY" = "1" ]; then
    if [ -n "${ARGS_IMPORT:-}" ]; then would "create $ARGS_FILE by importing ${ARGS_IMPORT} (once; that file is never read again)"
    else would "create $ARGS_FILE"; fi
    return 0
  fi
  mkdir -p "$(dirname "$ARGS_FILE")"
  if [ -n "${ARGS_IMPORT:-}" ]; then
    { echo "$hdr"; echo "# imported from $ARGS_IMPORT on $(_base_ts)"; grep -v '^#' "$ARGS_IMPORT" || true; } > "$ARGS_FILE"
    BASE_CHANGED+=("args: imported $ARGS_IMPORT"); ok "args: imported $ARGS_IMPORT into $ARGS_FILE (once; that file is never read again)"
  else
    echo "$hdr" > "$ARGS_FILE"; BASE_CHANGED+=("args: created $ARGS_FILE"); ok "args: created $ARGS_FILE"
  fi
}

_base_args_ensure(){ # <--flag> [value] — present with that value, or made so; idempotent
  local name="$1" val="${2:-}" cur flag="$1${2:+ $2}"
  if [ ! -f "$ARGS_FILE" ]; then
    if [ "$BASE_DRY" = "1" ]; then would "create $ARGS_FILE with '$flag'"; return 0; fi
    mkdir -p "$(dirname "$ARGS_FILE")"; printf '%s\n' "$flag" > "$ARGS_FILE"; BASE_CHANGED+=("created $ARGS_FILE with $flag"); ok "args: created with $flag"; return 0
  fi
  if [ -z "$val" ]; then
    if grep -qE -- "(^|[[:space:]])${name}([[:space:]]|$)" "$ARGS_FILE"; then ok "args: $name present"
    elif [ "$BASE_DRY" = "1" ]; then would "append '$flag' to comfyui_args.txt"
    else printf '%s\n' "$flag" >> "$ARGS_FILE"; BASE_CHANGED+=("comfyui_args.txt: + $flag"); ok "args: + $flag"; fi
    return 0
  fi
  # a flag is its name AND its value: `--preview-method latent2rgb` is not "--preview-method present"
  cur="$(sed -nE "s#.*${name}[[:space:]]+([^[:space:]]+).*#\\1#p" "$ARGS_FILE" | head -1 || true)"
  if [ "$cur" = "$val" ]; then ok "args: $flag present"
  elif [ -n "$cur" ]; then
    if [ "$BASE_DRY" = "1" ]; then would "change $name from '$cur' to '$val' in comfyui_args.txt"
    else sed -E "s#(${name})[[:space:]]+[^[:space:]]+#\\1 ${val}#" "$ARGS_FILE" | _base_replace_file "$ARGS_FILE"; BASE_CHANGED+=("comfyui_args.txt: $name $cur -> $val"); ok "args: $name corrected from '$cur' to '$val'"; fi
  elif [ "$BASE_DRY" = "1" ]; then would "append '$flag' to comfyui_args.txt"
  else printf '%s\n' "$flag" >> "$ARGS_FILE"; BASE_CHANGED+=("comfyui_args.txt: + $flag"); ok "args: + $flag"; fi
}
_base_args_remove(){ # <--flag> [why]
  local name="$1" why="${2:-}"
  [ -f "$ARGS_FILE" ] || return 0
  grep -qE -- "(^|[[:space:]])${name}([[:space:]]|$)" "$ARGS_FILE" || return 0
  if [ "$BASE_DRY" = "1" ]; then would "remove $name from comfyui_args.txt${why:+ ($why)}"; return 0; fi
  sed -E "s/(^|[[:space:]])${name}([[:space:]]|$)/\1\2/g" "$ARGS_FILE" | awk 'NF || /^#/' | _base_replace_file "$ARGS_FILE"
  BASE_CHANGED+=("comfyui_args.txt: removed $name"); ok "args: removed $name${why:+ ($why)}"
}
_base_manager_security(){ # security_level = normal; network_mode and everything else untouched
  local ini="" c
  for c in "$COMFY/user/default/ComfyUI-Manager/config.ini" "$CN/ComfyUI-Manager/config.ini"; do if [ -f "$c" ]; then ini="$c"; break; fi; done
  if [ -z "$ini" ]; then note "ComfyUI-Manager config.ini not found yet (Manager writes it on first start) — security_level is set on the next run"
  elif grep -qE '^security_level[[:space:]]*=[[:space:]]*normal[[:space:]]*$' "$ini"; then ok "Manager security_level = normal ($ini)"
  elif [ "$BASE_DRY" = "1" ]; then would "set security_level = normal in $ini"
  else
    if grep -q '^security_level' "$ini"; then sed -E 's/^security_level[[:space:]]*=.*/security_level = normal/' "$ini" | _base_replace_file "$ini"
    else awk '{print} /^\[default\]/ && !done {print "security_level = normal"; done=1}' "$ini" | _base_replace_file "$ini"; fi
    BASE_CHANGED+=("ComfyUI-Manager config.ini: security_level = normal"); ok "Manager security_level = normal"
  fi
}
_base_yaml_register(){ # <section> <key> <path> — register a folder in extra_model_paths.yaml once; a foreign entry is left alone
  local section="$1" key="$2" path="$3" y="${EXTRA_YAML:-$COMFY/extra_model_paths.yaml}"
  if [ -f "$y" ]; then
    if grep -qE "^[[:space:]]+${key}:" "$y"; then
      if grep -qF -- "$path" "$y"; then ok "extra_model_paths.yaml registers $key"
      else warn "extra_model_paths.yaml already has a $key entry pointing elsewhere — left alone"; fi
    elif [ "$BASE_DRY" = "1" ]; then would "append a $section: section with $key: $path to $y"
    else
      [ -n "$(tail -c1 "$y")" ] && printf '\n' >> "$y"
      printf '\n%s:\n    %s: %s\n' "$section" "$key" "$path" >> "$y"
      BASE_CHANGED+=("extra_model_paths.yaml: + $section.$key"); ok "extra_model_paths.yaml: + $key"
    fi
  elif [ "$BASE_DRY" = "1" ]; then would "create $y with a $section: section ($key)"
  else printf '%s:\n    %s: %s\n' "$section" "$key" "$path" > "$y"; BASE_CHANGED+=("created extra_model_paths.yaml ($section.$key)"); ok "created $y"; fi
}

base_hygiene(){
  hdr "HYGIENE · comfyui_args.txt · ComfyUI-Manager"
  local before=${#BASE_CHANGED[@]} row name val
  _base_args_import
  # previews on, as high as they go: auto uses the family's TAESD decoder when one is installed and falls back
  # to latent2rgb on its own; no API nodes — nothing on this pod calls a third-party inference API
  _base_args_ensure --preview-method auto
  _base_args_ensure --preview-size "${BASE_PREVIEW_SIZE:-1024}"
  _base_args_ensure --disable-api-nodes
  for row in ${HYGIENE_ARGS[@]+"${HYGIENE_ARGS[@]}"}; do
    name="${row%% *}"; val=""; [ "$row" != "$name" ] && val="${row#* }"
    _base_args_ensure "$name" $val
  done
  for name in ${HYGIENE_ARGS_REMOVE[@]+"${HYGIENE_ARGS_REMOVE[@]}"}; do _base_args_remove "$name" "removed by ${PKG_NAME:-the package}"; done
  # the template ships --use-sage-attention; with no sageattention in the venv ComfyUI dies at start (and some
  # models render noise with it). Keep it only when a package's hook installed the module into this venv.
  if [ -x "$PY" ] && "$PY" -c "import sageattention" >/dev/null 2>&1; then
    if [ -f "$ARGS_FILE" ] && grep -q -- '--use-sage-attention' "$ARGS_FILE"; then ok "args: --use-sage-attention kept (sageattention imports in the venv)"; fi
  else _base_args_remove --use-sage-attention "sageattention is not importable in $VENV"; fi
  _base_manager_security
  if declare -F pkg_hygiene >/dev/null; then pkg_hygiene; fi
  if [ "${#BASE_CHANGED[@]}" -gt "$before" ]; then echo "  changed:"; printf '    • %s\n' "${BASE_CHANGED[@]:$before}"; fi
  return 0
}
