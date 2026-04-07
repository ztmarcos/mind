#!/usr/bin/env bash
# Crea o actualiza el secreto en AWS leyendo OPENAI_API_KEY del .env en la raíz del repo.
# Uso: ./scripts/push-search-secret.sh [SECRET_ID] [REGION]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRET_ID="${1:-obsidian-vault/search-keys}"
REGION="${2:-${AWS_REGION:-us-east-1}}"

ENV_FILE="$ROOT/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "No existe $ENV_FILE — copia .env.example a .env" >&2
  exit 1
fi

OPENAI_API_KEY=""
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${line// }" ]] && continue
  if [[ "$line" =~ ^OPENAI_API_KEY=(.*)$ ]]; then
    OPENAI_API_KEY="${BASH_REMATCH[1]}"
    OPENAI_API_KEY="${OPENAI_API_KEY#\"}"
    OPENAI_API_KEY="${OPENAI_API_KEY%\"}"
    OPENAI_API_KEY="${OPENAI_API_KEY#\'}"
    OPENAI_API_KEY="${OPENAI_API_KEY%\'}"
    break
  fi
done < "$ENV_FILE"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY no encontrado o vacío en .env (usa: OPENAI_API_KEY=sk-...)" >&2
  exit 1
fi

export OPENAI_API_KEY
JSON="$(python3 -c 'import json, os; print(json.dumps({"openai_api_key": os.environ["OPENAI_API_KEY"]}))')"

if aws secretsmanager describe-secret --secret-id "$SECRET_ID" --region "$REGION" &>/dev/null; then
  aws secretsmanager put-secret-value \
    --secret-id "$SECRET_ID" \
    --secret-string "$JSON" \
    --region "$REGION"
  echo "Secreto actualizado: $SECRET_ID"
else
  aws secretsmanager create-secret \
    --name "$SECRET_ID" \
    --description "OpenAI key for Obsidian wiki pipeline (search only)" \
    --secret-string "$JSON" \
    --region "$REGION"
  echo "Secreto creado: $SECRET_ID"
fi

ARN="$(aws secretsmanager describe-secret --secret-id "$SECRET_ID" --region "$REGION" --query ARN --output text)"
echo "ARN para sam deploy (SearchApiSecretArn): $ARN"
