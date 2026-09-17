# 90-ledger.sh — what is installed on this pod and by whom: one flat manifest per package under state/packages/.
# Bash writes it with printf and reads it with read; py/ledger.py renders it. It scopes deletion (a file another
# installed package claims is never offered), drives --latest across packages, and feeds status.

_base_manifest_path(){ echo "$BASE_STATE/packages/${PKG_ID:-base}.manifest"; }

base_ledger_write(){ # after the install stages, before the summary; never under --check
  [ "$BASE_DRY" = "1" ] && return 0
  local f status="ok" py="-" torch="-" wfpath="-" script="-" hooks ts row cat fam purp file url bytes note alts rel entry name dir sha
  f="$(_base_manifest_path)"; mkdir -p "$(dirname "$f")"
  [ "${#BASE_FAILED[@]}" -gt 0 ] && status="failed"
  if [ -x "${PY:-}" ]; then py="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo -)"; fi
  case "$BASE_TORCH_INFO" in *torch\ *) torch="$(printf '%s' "$BASE_TORCH_INFO" | sed -nE 's/.*torch ([^ ·]+).*/\1/p' | head -1)";; esac
  [ -z "$torch" ] && torch="-"
  if [ -n "${WF_NAME:-}" ]; then wfpath="$COMFY/user/default/workflows/$WF_NAME"; fi
  if [ -n "${PKG_SCRIPT:-}" ] && [ -f "$PKG_SCRIPT" ]; then script="$PKG_SCRIPT"; fi
  hooks="$(declare -F | awk '$3 ~ /^pkg_/ {print $3}' | tr '\n' ',' | sed 's/,$//')"; [ -n "$hooks" ] || hooks="-"
  ts="$(_base_ts)"
  {
    printf 'pkg=%s\tname=%s\tversion=%s\tbase=%s\tts=%s\tstatus=%s\n' "${PKG_ID:-base}" "${PKG_NAME:-ComfyUI Base}" "${PKG_VERSION:-$BASE_VERSION}" "$BASE_VERSION" "$ts" "$status"
    printf 'comfy=%s\tvenv=%s\tpython=%s\ttorch=%s\tworkflow=%s\tscript=%s\thooks=%s\n' "${COMFY_NEW:-${COMFY_OLD:-?}}" "${VENV:-?}" "$py" "$torch" "$wfpath" "$script" "$hooks"
    for entry in ${PACK_DIRS[@]+"${PACK_DIRS[@]}"}; do
      name="${entry%%|*}"; dir="${entry#*|}"
      sha="$(_base_git -C "$dir" rev-parse HEAD 2>/dev/null || echo -)"
      url="$(_base_git -C "$dir" remote get-url origin 2>/dev/null || echo -)"
      printf 'pack\t%s\t%s\t%s\t%s\n' "$name" "$sha" "$dir" "$url"
    done
    while IFS='|' read -r cat fam purp file url bytes note alts; do
      [ -n "$cat" ] || continue
      rel="$(_base_dest_rel "$cat|$fam|$purp|$file|$url|$bytes|$note|$alts")"
      printf 'model\t%s\t%s\n' "$rel" "$bytes"
    done < <(_base_model_rows 2>/dev/null || true)
    for row in ${SUPERSEDED[@]+"${SUPERSEDED[@]}"}; do printf 'superseded\t%s\n' "$row"; done
  } > "$f.tmp" && mv "$f.tmp" "$f"
  ok "ledger: $f ($status)"
}
_base_other_manifests(){ # every manifest except this package's own
  local m; for m in "${BASE_STATE:-/nonexistent}"/packages/*.manifest; do
    [ -f "$m" ] || continue
    case "$(basename "$m")" in "${PKG_ID:-base}.manifest") continue;; esac
    echo "$m"
  done
}
_base_ledger_claims(){ # <dest rel> → 0 when another installed package claims that library path
  local rel="$1" m
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    if grep -qF -- "$(printf 'model\t%s\t' "$rel")" "$m"; then return 0; fi
  done < <(_base_other_manifests)
  return 1
}
_base_ledger_pack_claimed(){ # <pack dir name> → 0 when another installed package installs that pack
  local name="$1" m
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    if grep -qF -- "$(printf 'pack\t%s\t' "$name")" "$m"; then return 0; fi
  done < <(_base_other_manifests)
  return 1
}
base_status(){ hdr "STATUS · $BASE_STATE"; "$SYS_PY" "$BASE_DIR/py/ledger.py" "$BASE_STATE" status; _base_host_status; }
_base_host_status(){ # 2.2.0: which host the driver recorded for this volume (state/host.env), if any
  [ -f "$BASE_STATE/host.env" ] || return 0
  hdr "HOST · state/host.env"; sed 's/^/  /' "$BASE_STATE/host.env"
}
base_latest(){ # every installed package's packs + the base packs: what their remotes' HEAD is now
  hdr "LATEST · pinned commit vs remote HEAD"
  local row dir url sha rest new m kind path
  { printf '%s\n' "${BASE_PACKS[@]}" | awk -F'|' '{print "base\t"$1"\t"$3"\t-\t"$2}'
    for m in "${BASE_STATE:-/nonexistent}"/packages/*.manifest; do [ -f "$m" ] && grep '^pack	' "$m" | sed "s/^pack/$(basename "$m" .manifest)/" || true; done
  } | while IFS=$'\t' read -r kind dir sha path url; do
    [ -n "$dir" ] && [ -n "$url" ] && [ "$url" != "-" ] || continue
    new="$(_base_git ls-remote "$url" HEAD 2>/dev/null | awk '{print $1}' | head -1 || true)"
    if [ -z "$new" ]; then echo "  $dir  (could not reach $url)"; elif [ "$new" = "$sha" ]; then echo "  $dir  $sha  (unchanged)  [$kind]"; else echo "  $dir  $new  <- was $sha  [$kind]"; fi
  done
}
