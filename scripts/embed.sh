#!/usr/bin/env bash
# Provider-agnostic embedding wrapper.
# Reads text from $1 or stdin; emits a pgvector literal on stdout.
# Call: scripts/embed.sh "text to embed"
# Output: '[0.01,-0.02,...]'::vector(1536)
set -euo pipefail

: "${EMBEDDINGS_PROVIDER:?EMBEDDINGS_PROVIDER must be set}"
: "${EMBEDDINGS_MODEL:?EMBEDDINGS_MODEL must be set}"
: "${EMBEDDINGS_DIMENSIONS:?EMBEDDINGS_DIMENSIONS must be set}"
: "${CLOUD_PROVIDER:=aws}"

text="${1:-$(cat)}"

# bedrock is AWS-only; openai/voyage/gemini/mistral work on aws and gcp.
if [[ "$EMBEDDINGS_PROVIDER" == "bedrock" && "$CLOUD_PROVIDER" != "aws" ]]; then
    echo "ERROR: bedrock embeddings require CLOUD_PROVIDER=aws (got ${CLOUD_PROVIDER})" >&2
    exit 1
fi

embed_openai() {
    : "${OPENAI_API_KEY:?OPENAI_API_KEY must be set for openai provider}"
    curl -fsS https://api.openai.com/v1/embeddings \
        -H "Authorization: Bearer $OPENAI_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$(jq -cn --arg m "$EMBEDDINGS_MODEL" --arg t "$text" --argjson d "$EMBEDDINGS_DIMENSIONS" \
              '{model:$m, input:$t, dimensions:$d}')" \
    | jq -c '.data[0].embedding'
}

embed_voyage() {
    : "${VOYAGE_API_KEY:?VOYAGE_API_KEY must be set for voyage provider}"
    curl -fsS https://api.voyageai.com/v1/embeddings \
        -H "Authorization: Bearer $VOYAGE_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$(jq -cn --arg m "$EMBEDDINGS_MODEL" --arg t "$text" \
              '{model:$m, input:[$t]}')" \
    | jq -c '.data[0].embedding'
}

embed_gemini() {
    : "${GEMINI_API_KEY:?GEMINI_API_KEY must be set for gemini provider}"
    curl -fsS "https://generativelanguage.googleapis.com/v1beta/models/${EMBEDDINGS_MODEL}:embedContent?key=${GEMINI_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "$(jq -cn --arg t "$text" --argjson d "$EMBEDDINGS_DIMENSIONS" \
              '{content:{parts:[{text:$t}]}, outputDimensionality:$d}')" \
    | jq -c '.embedding.values'
}

embed_mistral() {
    : "${MISTRAL_API_KEY:?MISTRAL_API_KEY must be set for mistral provider}"
    curl -fsS https://api.mistral.ai/v1/embeddings \
        -H "Authorization: Bearer $MISTRAL_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$(jq -cn --arg m "$EMBEDDINGS_MODEL" --arg t "$text" \
              '{model:$m, input:[$t]}')" \
    | jq -c '.data[0].embedding'
}

embed_bedrock() {
    aws bedrock-runtime invoke-model \
        --model-id "$EMBEDDINGS_MODEL" \
        --body "$(jq -cn --arg t "$text" --argjson d "$EMBEDDINGS_DIMENSIONS" \
                   '{inputText:$t, dimensions:$d}')" \
        --cli-binary-format raw-in-base64-out \
        /dev/stdout \
    | jq -c '.embedding'
}

embed_local() {
    echo "ERROR: EMBEDDINGS_PROVIDER=local is not supported; use openai, voyage, gemini, mistral or bedrock" >&2
    exit 1
}

case "$EMBEDDINGS_PROVIDER" in
    openai)  vec=$(embed_openai)  ;;
    voyage)  vec=$(embed_voyage)  ;;
    gemini)  vec=$(embed_gemini)  ;;
    mistral) vec=$(embed_mistral) ;;
    bedrock) vec=$(embed_bedrock) ;;
    local)   vec=$(embed_local)   ;;
    *)
        echo "ERROR: Unknown EMBEDDINGS_PROVIDER: $EMBEDDINGS_PROVIDER" >&2
        exit 1
        ;;
esac

if [[ -z "$vec" || "$vec" == "null" ]]; then
    echo "ERROR: Embedding returned null or empty for provider $EMBEDDINGS_PROVIDER" >&2
    exit 1
fi

printf "'%s'::vector(%d)\n" "$vec" "$EMBEDDINGS_DIMENSIONS"
