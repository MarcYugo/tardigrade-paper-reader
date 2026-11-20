# Unlimited-OCR — OpenAI-compatible document-parsing service.
#
# The base is vLLM's official release image for this model. It is used instead
# of a plain `pip install vllm` because two things Unlimited-OCR depends on are
# not in any stable wheel yet:
#   * the `UnlimitedOCRForCausalLM` architecture
#   * `vllm.model_executor.models.unlimited_ocr:NGramPerReqLogitsProcessor`
#
# The `-cu129` tag is the CUDA 12.9 flavour (Hopper/Ada). Omit the suffix to get
# the default CUDA 13.0 image.

ARG BASE_IMAGE=vllm/vllm-openai:unlimited-ocr-cu129
FROM ${BASE_IMAGE}

# `addict`/`matplotlib` are imported by the checkpoint's remote-code files.
# The default launch path never executes them (config parsing is routed to
# vLLM's native `unlimited-ocr` class in the entrypoint), but they are small
# and keep those files a step closer to importable. Do NOT pin Transformers
# here: the vLLM build in this image requires Transformers v5, while the
# checkpoint's remote code targets v4.
RUN uv pip install --system addict matplotlib \
    && python3 -c "import addict, matplotlib"

# Optional: adds the ModelScope Hub client (enable with INSTALL_MODELSCOPE=true).
# Only needed to fetch weights from ModelScope
# (`VLLM_USE_MODELSCOPE=1`) instead of Hugging Face. The dependency-free install
# runs first to avoid disturbing the CUDA/torch pinning of the base image, and
# the import check makes the build fall back to a full install rather than ship
# an image whose ModelScope support is broken. Left off by default because the
# compose setup serves a locally mounted snapshot (`MODEL_ID=/weights`), which
# needs no hub client at all.
ARG INSTALL_MODELSCOPE=false
RUN if [ "${INSTALL_MODELSCOPE}" = "true" ]; then \
        uv pip install --system --no-deps modelscope \
        && python3 -c "from modelscope.hub.snapshot_download import snapshot_download" \
        || uv pip install --system modelscope; \
    fi

COPY docker-entrypoint.sh /usr/local/bin/unlimited-ocr-entrypoint.sh
RUN chmod +x /usr/local/bin/unlimited-ocr-entrypoint.sh

EXPOSE 9010

# Loading the weights and warming up torch.compile takes minutes, hence the
# generous start period; the probe is `urllib` so the image needs no curl.
HEALTHCHECK --interval=30s --timeout=10s --start-period=900s --retries=5 \
    CMD python3 -c "import os,sys,urllib.request;port=os.environ.get('VLLM_PORT','9010');sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=5).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/unlimited-ocr-entrypoint.sh"]
