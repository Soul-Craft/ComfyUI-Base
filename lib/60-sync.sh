# 60-sync.sh — rewrite the workflow's loader widget paths to the canonical library layout (py/sync_workflow.py).
# A package may declare PLACEHOLDERS=( name.safetensors ... ): loader values it ships on purpose with nothing behind
# them (a "put your LoRA here" slot). They are reported, never counted as missing, never rewritten.
# The map is DERIVED from the MODELS table: (category, file) -> Family/Purpose/file. No filename is written twice.

# node type -> the library category its dropdown reads from. Packages extend this with LOADER_CATS rows.
BASE_LOADER_CATS=(
 "UNETLoader|diffusion_models" "UnetLoaderGGUF|unet" "CheckpointLoaderSimple|checkpoints" "CheckpointLoader|checkpoints"
 "CLIPLoader|text_encoders" "DualCLIPLoader|text_encoders" "TripleCLIPLoader|text_encoders" "QuadrupleCLIPLoader|text_encoders" "ClipLoaderGGUF|clip" "DualCLIPLoaderGGUF|clip"
 "VAELoader|vae" "LoraLoader|loras" "LoraLoaderModelOnly|loras" "ControlNetLoader|controlnet" "CLIPVisionLoader|clip_vision" "StyleModelLoader|style_models"
 "UpscaleModelLoader|upscale_models" "LatentUpscaleModelLoader|latent_upscale_models"
 "ModelPreviewOverrideKJ|vae_approx" "PrimitiveString|diffusion_models"
 "SAMLoader|sams" "SAM_SmartInpainter|sams" "SeedVR2LoadDiTModel|SEEDVR2" "SeedVR2LoadVAEModel|SEEDVR2" "DownloadAndLoadDepthAnything|depthanything"
)

base_sync(){ # sets BASE_SYNC_RESULT; fails the run when an ACTIVE loader resolves to nothing
  hdr "WORKFLOW PATHS · $WF_NAME"
  local wf="${PKG_DIR:-.}/${WF_NAME:-}" copy rows cats out line dry=() rc=0
  if [ -z "${WF_NAME:-}" ]; then BASE_SYNC_RESULT="no workflow"; note "this package ships no workflow — nothing to sync"; return 0; fi
  if [ ! -f "$wf" ]; then BASE_SYNC_RESULT="workflow missing"; err "$wf not found"; BASE_FAILED+=("workflow $WF_NAME missing beside the script"); return 0; fi
  copy="$COMFY/user/default/workflows/$WF_NAME"
  _base_tmp; rows="$BASE_TMPD/rows.tsv"; cats="$BASE_TMPD/cats.tsv"
  _base_model_rows 2>/dev/null | awk -F'|' '{print $1"\t"$2"\t"$3"\t"$4}' > "$rows" || true
  printf '%s\n' "${BASE_LOADER_CATS[@]}" ${LOADER_CATS[@]+"${LOADER_CATS[@]}"} | tr '|' '\t' > "$cats"
  [ "$BASE_DRY" = "1" ] && dry=(--dry)
  local ph; for ph in ${PLACEHOLDERS[@]+"${PLACEHOLDERS[@]}"}; do dry+=(--placeholder "$ph"); done   # slots the package ships empty on purpose
  out="$("$SYS_PY" "$BASE_DIR/py/sync_workflow.py" "$wf" "$M" "$rows" "$cats" ${dry[@]+"${dry[@]}"} 2>&1)" || rc=$?
  printf '%s\n' "$out" | grep -v '^SYNC ' | sed 's/^/  /'
  if [ "$rc" -ne 0 ]; then err "the sync tool failed (exit $rc)"; BASE_FAILED+=("workflow sync crashed"); BASE_SYNC_RESULT="crashed"; return 0; fi
  line="$(printf '%s\n' "$out" | grep '^SYNC ' | tail -1)"; BASE_SYNC_RESULT="${line#SYNC }"
  if [ "$BASE_DRY" != "1" ]; then mkdir -p "$(dirname "$copy")"; cp -f "$wf" "$copy"; ok "workflow copied to $copy"; fi
  case "$line" in
    *"active_missing=0"*) ok "every active loader names a file the library knows";;
    *) err "active loader(s) name a file that is neither a MODELS row nor on disk (listed above as ACTIVE)"
       BASE_FAILED+=("workflow sync: $(printf '%s\n' "$out" | grep -c '^  ACTIVE ') active loader(s) unresolved: $(printf '%s\n' "$out" | grep '^  ACTIVE ' | awk '{print $NF}' | head -3 | tr '\n' ' ')");;
  esac
  return 0
}
