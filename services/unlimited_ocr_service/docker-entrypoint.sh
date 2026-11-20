#!/usr/bin/env bash
#
# Launches the vLLM OpenAI-compatible server for Unlimited-OCR.
#
# The flags below are the ones Unlimited-OCR requires to produce usable
# markdown; they are not interchangeable with a generic vLLM deployment:
#   * --logits-processors   the model has no chat template and relies on the
#                           no-repeat-n-gram processor, otherwise long
#                           documents loop on <|det|> coordinate tokens
#   * --hf-overrides        the checkpoint declares model_type
#                           "unlimited_ocr" plus auto_map remote code that
#                           targets transformers v4, but the vLLM build in
#                           this image requires transformers v5. Re-keying
#                           the config to vLLM's built-in "unlimited-ocr"
#                           class makes vLLM parse config.json natively and
#                           skip the outdated remote code entirely (so do not
#                           pass --trust-remote-code).
#   * --no-enable-prefix-caching / --mm-processor-cache-gb 0
#                           OCR requests never reuse an image, so both caches
#                           only cost memory
#
set -euo pipefail

MODEL_ID="${MODEL_ID:-baidu/Unlimited-OCR}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-Unlimited-OCR}"
VLLM_HOST="${VLLM_HOST:-0.0.0.0}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_DTYPE="${VLLM_DTYPE:-bfloat16}"
VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-32768}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.90}"
VLLM_LOGITS_PROCESSORS="${VLLM_LOGITS_PROCESSORS:-vllm.model_executor.models.unlimited_ocr:NGramPerReqLogitsProcessor}"
# Set to "{}" to disable the config re-keying (requires a checkpoint whose
# remote code supports the installed Transformers version).
# NOTE: do NOT collapse this into `"${VLLM_HF_OVERRIDES:-{\"model_type\": ...}}"`:
# when the variable IS set, bash ends the ${...} expansion at the JSON's own `}`
# and appends the leftover `}` as literal text (`{"model_type": "unlimited-ocr"}}`),
# which vllm rejects with "cannot be converted to json.loads". An explicit
# if-block with a single-quoted literal is immune to that parsing pitfall.
if [ -z "${VLLM_HF_OVERRIDES:-}" ]; then
    VLLM_HF_OVERRIDES='{"model_type": "unlimited-ocr"}'
fi

args=(
    serve "$MODEL_ID"
    --served-model-name "$SERVED_MODEL_NAME"
    --host "$VLLM_HOST"
    --port "$VLLM_PORT"
    --dtype "$VLLM_DTYPE"
    --max-model-len "$VLLM_MAX_MODEL_LEN"
    --gpu-memory-utilization "$VLLM_GPU_MEMORY_UTILIZATION"
    --logits-processors "$VLLM_LOGITS_PROCESSORS"
    --no-enable-prefix-caching
    --mm-processor-cache-gb 0
)

if [ -n "$VLLM_HF_OVERRIDES" ]; then
    args+=(--hf-overrides "$VLLM_HF_OVERRIDES")
fi

# The checkpoint declares tokenizer_class="LlamaTokenizerFast" ("legacy": true) but
# ships a byte-level BPE vocab; under the Transformers v5 required by this image that
# combination drops spaces/newlines during encode/decode (decoded text comes out with
# literal "Ġ"/"Ċ" characters, or words glued together). Stage a patched copy that
# loads as the plain fast tokenizer and point --tokenizer at it. Set VLLM_TOKENIZER_DIR
# to an empty string to disable; the patch is skipped automatically when MODEL_ID is
# not a local path (e.g. a hub id).
VLLM_TOKENIZER_DIR="${VLLM_TOKENIZER_DIR-/tmp/ocr-tokenizer}"
if [ -n "$VLLM_TOKENIZER_DIR" ] && [ -f "$MODEL_ID/tokenizer_config.json" ]; then
    mkdir -p "$VLLM_TOKENIZER_DIR"
    cp "$MODEL_ID/tokenizer.json" "$MODEL_ID/tokenizer_config.json" "$VLLM_TOKENIZER_DIR"/
    if [ -f "$MODEL_ID/special_tokens_map.json" ]; then
        cp "$MODEL_ID/special_tokens_map.json" "$VLLM_TOKENIZER_DIR"/
    fi
    if [ -f "$MODEL_ID/chat_template.jinja" ]; then
        cp "$MODEL_ID/chat_template.jinja" "$VLLM_TOKENIZER_DIR"/
    fi
    python3 - "$VLLM_TOKENIZER_DIR/tokenizer_config.json" <<'PY'
import json, sys
path = sys.argv[1]
cfg = json.load(open(path))
cfg["tokenizer_class"] = "PreTrainedTokenizerFast"
json.dump(cfg, open(path, "w"), ensure_ascii=False)
PY
    args+=(--tokenizer "$VLLM_TOKENIZER_DIR")
fi

# Multi-page OCR is already unbounded by default (vLLM defaults every modality
# to 999 items per prompt). Set this to cap it, e.g. '{"image": 16}'.
if [ -n "${VLLM_LIMIT_MM_PER_PROMPT:-}" ]; then
    args+=(--limit-mm-per-prompt "$VLLM_LIMIT_MM_PER_PROMPT")
fi

if [ -n "${VLLM_API_KEY:-}" ]; then
    args+=(--api-key "$VLLM_API_KEY")
fi

# Appended last so anything set here is overridable from the command line.
args+=("$@")

echo "[unlimited-ocr] launching: vllm ${args[*]}" >&2
exec vllm "${args[@]}"
