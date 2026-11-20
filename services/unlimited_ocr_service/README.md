# Unlimited-OCR service

Docker packaging for [baidu/Unlimited-OCR](https://www.modelscope.cn/models/PaddlePaddle/Unlimited-OCR),
a long-horizon document-parsing model. The container exposes vLLM's
OpenAI-compatible API, so any OpenAI client can send page images and get
markdown back.

## Files

| File | Purpose |
| --- | --- |
| [Dockerfile](./Dockerfile) | Thin layer over `vllm/vllm-openai:unlimited-ocr-cu129` (CUDA 12.9) adding the entrypoint and a health check. |
| [docker-entrypoint.sh](./docker-entrypoint.sh) | Turns environment variables into the `vllm serve` command line. |
| [docker-compose.yml](./docker-compose.yml) | GPU reservation, port binding, local-weights bind mount. |
| [.env.example](./.env.example) | Every supported setting with its default. |

## Quick start

Download the weights once (~7 GB, they are **not** baked into the image):

```sh
modelscope download --model PaddlePaddle/Unlimited-OCR --local_dir ./models/Unlimited-OCR
```

Then start the service:

```sh
cp .env.example .env          # optional, defaults are fine
docker compose up -d --build
docker compose logs -f        # wait for "Application startup complete"
curl -s localhost:9010/v1/models
```

The container serves `./models/Unlimited-OCR` read-only from `/weights`, so
start-up is just weight loading and `torch.compile` warm-up — no hub download.

Run the PDF smoke test to print the OCR result and save it as UTF-8 JSON next
to the PDF (for example, `paper.pdf` produces `paper.json`):

```sh
python test_unlimited_ocr_service.py paper.pdf
python test_unlimited_ocr_service.py paper.pdf --output results/paper.json
```

### Weights source

* **Local snapshot** (default) — `./models/Unlimited-OCR` bind-mounted at
  `/weights`, with `MODEL_ID=/weights`. Point `LOCAL_WEIGHTS_DIR` at another
  directory to change it.
* **Hugging Face** — `MODEL_ID=baidu/Unlimited-OCR`, e.g. via
  `docker compose run --rm -e MODEL_ID=baidu/Unlimited-OCR ...`.
* **ModelScope** — `MODEL_ID=PaddlePaddle/Unlimited-OCR` plus
  `VLLM_USE_MODELSCOPE=1`, and rebuild with `INSTALL_MODELSCOPE=true` (the
  client is left out of the image by default).

Whichever hub source you use, add back a cache volume so `down`/`up` does not
re-download the weights:

```yaml
    environment:
      HF_HOME: /models/huggingface
      MODELSCOPE_CACHE: /models/modelscope
    volumes:
      - ocr-models:/models
# ...
volumes:
  ocr-models:
```

## Client usage

A single image may be sent in *gundam* (crop) mode; multi-image and PDF requests
automatically fall back to *base* mode, which the server handles for you.

```python
from openai import OpenAI

client = OpenAI(api_key="EMPTY", base_url="http://localhost:9010/v1", timeout=3600)

response = client.chat.completions.create(
    model="Unlimited-OCR",
    messages=[
        {
            "role": "user",
            "content": [
                # Do NOT include a literal "<image>" here: the checkpoint's chat
                # template already emits one "<image>" placeholder per image item,
                # and a duplicate prefix makes the model return an empty response.
                {"type": "text", "text": "document parsing."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }
    ],
    max_tokens=2048,
    temperature=0.0,
    extra_body={
        # Required: keep the <|ref|>/<|det|> grounding tokens in the output.
        "skip_special_tokens": False,
        "vllm_xargs": {"ngram_size": 35, "window_size": 128},  # 1024 for multi-page/PDF
    },
)
print(response.choices[0].message.content)
```

The PDF is sent as rendered page images; it does not need to be converted to
Markdown first. Pages are submitted sequentially, with exactly one image in
each request. The model's output contains text plus layout categories and
bounding boxes. By default, `test_unlimited_ocr_service.py` parses those into
`result` items in the JSON output, each with a page number, type, bbox, and text:

```json
{
  "page": 1,
  "type": "title",
  "bbox": [254, 202, 362, 223],
  "text": "Latex Formular"
}
```

Use `--raw` to store the unprocessed model response instead. The coordinates
are in the model's page-image coordinate system.

## Settings

Everything is read from the environment; see [.env.example](./.env.example).

| Variable | Default | Notes |
| --- | --- | --- |
| `BASE_IMAGE` | `vllm/vllm-openai:unlimited-ocr-cu129` | Build arg. Drop `-cu129` for CUDA 13.0. |
| `INSTALL_MODELSCOPE` | `false` | Build arg. Needed only for `VLLM_USE_MODELSCOPE=1`. |
| `MODEL_ID` | `/weights` | Hub id or in-container path to the mounted snapshot. |
| `LOCAL_WEIGHTS_DIR` | `./models/Unlimited-OCR` | Host path bind-mounted at `/weights`. |
| `SERVED_MODEL_NAME` | `Unlimited-OCR` | Name to pass as `model` in requests. |
| `VLLM_PORT` | `9010` | Published and listened on. |
| `VLLM_DTYPE` | `bfloat16` | The checkpoints are trained in bf16. |
| `VLLM_MAX_MODEL_LEN` | `8192` | Context budget for input (including page images) and generated output. Keep it above `max_tokens` plus the input size. Increase it for larger PDFs if GPU memory allows. |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.90` | Lower it if other processes share the GPU. |
| `VLLM_LIMIT_MM_PER_PROMPT` | `{"image": 16}` | Maximum page images in one prompt. Increase it for PDFs with more pages; lower it if the GPU runs out of memory. |
| `VLLM_HF_OVERRIDES` | `{"model_type": "unlimited-ocr"}` | Routes config parsing to vLLM's native config class so the checkpoint's v4-era remote code is not executed under Transformers v5. Set to `{}` to disable. |
| `VLLM_API_KEY` | unset | vLLM enforces it only when non-empty. |

## Troubleshooting

* **Empty responses** — the request text must NOT repeat the `<image>` prefix: the
  checkpoint's chat template already emits one `<image>` placeholder per image item,
  and a duplicated placeholder makes the model stop immediately (empty response).
  Also keep `skip_special_tokens` as `False`.
* **Decoded text shows literal `Ġ` / `Ċ` characters, or words glued together** —
  the checkpoint's `tokenizer_class` (`LlamaTokenizerFast` + `"legacy": true`) mangles
  its byte-level BPE vocab under Transformers v5. The entrypoint stages a patched
  `PreTrainedTokenizerFast` copy under `/tmp/ocr-tokenizer` and serves with
  `--tokenizer` pointing there; check that flag in the startup log if this reappears.
* **Runaway counting ("1. 2. 3. …") or loops on dense pages** — the page was
  rendered too small. Dense pages need ≥200 dpi (300 recommended; the PDF example
  above uses `dpi=300`); at 150 dpi the model cannot read the page and degenerates.
* **Output loops on `<|det|>` tokens** — the request is missing
  `vllm_xargs`, or the server was started without `--logits-processors` (which
  the entrypoint always adds).
* **`is_torch_fx_available` / `Support for Transformers v4` errors** — the
  checkpoint's remote code targets Transformers v4, while the vLLM build in the
  base image requires v5. The entrypoint therefore serves with
  `--hf-overrides` (vLLM's native `unlimited-ocr` config) instead of
  `--trust-remote-code`; do not add that flag back.
* **`At most 1 image(s) may be provided in one prompt`** — the configured
  `VLLM_LIMIT_MM_PER_PROMPT` is too low for the PDF. Set its `image` limit to
  at least the number of pages being sent, then recreate the service.
* **Health check stays `starting`** — expected for the first few minutes while
  the weights load; check `docker compose logs -f`.
* **`Cannot find model` / empty `/weights`** — `LOCAL_WEIGHTS_DIR` does not
  exist on the host, so Docker creates an empty root-owned directory and mounts
  that. Download the snapshot first.
* **Out of shared memory / NCCL errors** — keep `ipc: host`, or give the service
  a large `shm_size`.

## Extra `vllm serve` flags

Any argument appended to the container command is forwarded after the built-in
ones, so tensor parallelism needs no image change:

```sh
docker compose run --rm unlimited-ocr --tensor-parallel-size 2
```
