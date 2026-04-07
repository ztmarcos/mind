# Obsidian vault → S3 → Lambda → Bedrock → `wiki/` (event-driven)

Production-minded minimal pipeline: uploading `raw/**/*.md` to S3 triggers Lambda, which optionally runs **web search** (OpenAI Responses `web_search` or Tavily / Brave / Serper), then generates structured Obsidian markdown with **Amazon Bedrock** and writes `wiki/<same-path>.md` plus `wiki/index.md`.

## Architecture

- **S3** prefixes: `raw/` (input), `wiki/` (output).
- **S3 event notification**: `s3:ObjectCreated:*`, prefix `raw/`, suffix `.md` only.
- **Lambda** (Python 3.12): read object → search (optional) → `bedrock:InvokeModel` → `PutObject` to `wiki/` → rebuild `wiki/index.md`.
- **SQS DLQ**: failed invocations after retries are sent to the dead-letter queue.
- **Secrets Manager**: API keys for search providers (not for Bedrock; Bedrock uses execution role).
- **X-Ray tracing**: enabled on the function (`Tracing: Active`) for request tracing.

## Prerequisites

- AWS CLI, **AWS SAM CLI** (`sam`), Docker optional (SAM uses it for some builds; pure-python often builds without).
- In **Amazon Bedrock console**, **enable model access** for the model ID you deploy (default: `anthropic.claude-3-haiku-20240307-v1:0`).
- **IAM** in the target account allowed to create S3, Lambda, SQS, IAM roles, and (if used) Secrets Manager secrets.

## Secret format (Secrets Manager)

Create a secret (plain string or JSON) and pass its ARN as `SearchApiSecretArn` at deploy time.

**JSON (recommended)** — include only the keys you need:

```json
{
  "openai_api_key": "sk-...",
  "tavily_api_key": "tvly-...",
  "brave_api_key": "BSA...",
  "serper_api_key": "..."
}
```

**Plain string** — single API key for the active `SearchProvider`:

- `openai_web` → OpenAI key  
- `tavily` → Tavily key  
- `brave` → Brave key  
- `serper` → Serper key  

### Archivo `.env` en tu máquina (solo local)

- **La Lambda en AWS no lee tu `.env`**. Ahí solo vale **Secrets Manager** (o el valor que inyectes al desplegar).
- En el repo, copia [`.env.example`](.env.example) a `.env`, pon `OPENAI_API_KEY=sk-...`, y mantén **`.env` fuera de git** (ya está en [`.gitignore`](.gitignore)).
- Para subir esa clave a AWS desde la CLI: `chmod +x scripts/push-search-secret.sh && ./scripts/push-search-secret.sh` (crea o actualiza el secreto y muestra el ARN para `sam deploy`).

### Data residency note (OpenAI search)

If `SEARCH_PROVIDER=openai_web`, **queries and prompt text are sent to OpenAI** over HTTPS. They are **not** processed by Bedrock in that step. Bedrock still receives your **note body** and **search snippets** for the final wiki generation.

## Deploy

```bash
cd /path/to/mind
sam build
sam deploy --guided
```

Important parameters:

| Parameter | Meaning |
|-----------|---------|
| `SearchApiSecretArn` | ARN of secret with API keys; leave **empty** to run **without** web search (Bedrock-only). |
| `BedrockModelId` | Bedrock model ID (Claude 3 family recommended). |
| `SearchProvider` | `openai_web` (default), `tavily`, `brave`, or `serper`. |
| `WebsearchEnabled` | Global kill-switch (`true` / `false`). Per-note `websearch: false` still disables search when this is `true`. |
| `OpenAIWebModel` | Model for OpenAI Responses when using `openai_web` (e.g. `gpt-4o-mini`). |

Stack **Outputs**: `BucketName` — use as `S3_BUCKET` for the sync script.

### IAM (Lambda)

The function role allows:

- `s3:GetObject` on `bucket/raw/*`
- `s3:PutObject` on `bucket/wiki/*`
- `s3:ListBucket` with prefix `wiki/`
- `bedrock:InvokeModel` on `arn:aws:bedrock:<region>::foundation-model/<BedrockModelId>`
- `sqs:SendMessage` to the DLQ
- `secretsmanager:GetSecretValue` on the configured secret ARN **only if** `SearchApiSecretArn` is non-empty at deploy time

Re-deploy if you add a secret ARN later so IAM updates.

## Frontmatter (per note)

Optional YAML frontmatter:

```yaml
---
websearch: true
search_queries:
  - "exact search phrase"
  - "another query"
---
```

- If **`WEBSEARCH_ENABLED`** is `false` at deploy time, search never runs.
- If **`websearch: false`**, search is skipped for that note.
- If **`websearch`** is omitted and global search is on and a secret is configured, search runs with **heuristic queries** from the note title/lines.

## Local sync

Requires Python 3 and `boto3`:

```bash
pip install -r scripts/requirements.txt
```

```bash
export OBSIDIAN_VAULT_DIR="/absolute/path/to/vault"
export S3_BUCKET="your-bucket-from-stack-output"
export AWS_REGION="us-east-1"   # optional
python scripts/sync.py up       # upload vault *.md -> s3://$S3_BUCKET/raw/...
python scripts/sync.py down     # download raw/ + wiki/ -> $VAULT/_sync/raw and _sync/wiki
```

`sync.py up` skips anything under `_sync/` so you do not re-upload downloaded trees.

## Example input / output

See [examples/raw-sample.md](examples/raw-sample.md). After upload to `raw/...`, Lambda writes `wiki/...` with sections:

- `# Title`
- `## Summary`
- `## Key Concepts` (`[[wikilinks]]`)
- `## Insights`
- `## Related`
- `## Sources` (only when search results exist; bullets `Title — URL` from search results only)

## CloudFormation: circular dependency (S3 + Lambda)

If deploy failed with `Circular dependency between resources: [ProcessorFunction, ContentBucket, ...]`, the template avoids that by giving the bucket a **deterministic name** (`ow-<account>-<stack-uuid-suffix>`) and using the **same pattern in IAM** without `!GetAtt ContentBucket.Arn` on the function role. Redeploy with an updated [template.yaml](template.yaml).

## OpenAI Responses `web_search` troubleshooting

- If the API returns **400** about tools, check OpenAI docs for the current tool name (`web_search` vs preview variants) and adjust the payload in [lambda/app.py](lambda/app.py) (`_search_openai_web`).
- Citations are parsed from response **annotations** when present; otherwise URLs are heuristically extracted from model text (weaker grounding — prefer annotation-capable models).

## DVA-C02 mapping (study angles)

| Topic | This project |
|-------|----------------|
| S3 events, prefix/suffix filters | Notification on `raw/*.md` only |
| Lambda, event-driven design | S3 → Lambda handler |
| IAM least privilege | Scoped S3 + Bedrock + optional Secrets + SQS DLQ |
| CloudWatch Logs | Lambda standard logging |
| X-Ray | `Tracing: Active` on function |
| Bedrock | `InvokeModel` with Claude message schema |
| Secrets Manager | Search API keys |
| Third-party HTTPS from Lambda | Search providers / OpenAI |
| Resilience | DLQ, Lambda retries, degraded mode if search fails |
| IaC / deployment | AWS SAM (CloudFormation) |

## Costs / scale

- Each `PutObject` to `raw/…` triggers **one** Lambda run per object.
- Each run may call **search** (N queries) plus **one** Bedrock invocation.
- Bulk `sync.py up` on large vaults triggers many concurrent Lambdas (account concurrency limits apply).
- **Mitigation**: set `WebsearchEnabled=false`, use `websearch: false` in notes, or batch uploads.

## License

Use and modify freely for your vault and certification study.
