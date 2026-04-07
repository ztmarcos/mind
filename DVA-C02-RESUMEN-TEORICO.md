# Resumen teórico: Pipeline Obsidian → Bedrock (DVA-C02)

**Proyecto:** Sistema event-driven que procesa archivos markdown en S3, invoca Amazon Bedrock con contexto web opcional (OpenAI), y genera wikis estructurados automáticamente.

---

## 1. Amazon S3 (Simple Storage Service)

### Conceptos clave aplicados

- **Bucket**: contenedor global único (`ow-<account>-<stack-uuid>`). Nombre debe ser único en toda AWS, DNS-compliant.
- **Prefijos como "carpetas"**: `raw/` y `wiki/` son **prefijos de clave**, no directorios reales. S3 es key-value plano.
- **Notificaciones de eventos**: configuración `NotificationConfiguration` en el bucket con:
  - **Event type**: `s3:ObjectCreated:*` (PUT, POST, CompleteMultipartUpload).
  - **Filter rules**: `Prefix: raw/`, `Suffix: .md` → solo archivos `.md` bajo ese prefijo disparan Lambda.
- **Versionado**: `VersioningConfiguration: Enabled` mantiene historial de objetos (recuperación ante borrados accidentales).
- **Cifrado**: `SSE-S3` (AES256) server-side por defecto; alternativa SSE-KMS para control de claves.
- **Public access block**: las 4 opciones en `true` impiden acceso público accidental.

### Para el examen

- **Event notifications** pueden invocar Lambda, SNS o SQS.
- **Condiciones IAM** en `s3:prefix` limitan acceso por ruta (`wiki/*` vs `raw/*`).
- **ARN de bucket** vs **ARN de objeto**: `arn:aws:s3:::bucket` vs `arn:aws:s3:::bucket/key`.
- **Consistencia**: desde dic 2020, S3 es **strongly consistent** para PUT/DELETE de objetos nuevos y sobrescrituras.

---

## 2. AWS Lambda

### Conceptos clave aplicados

- **Event-driven**: función invocada automáticamente por notificación S3; no polling.
- **Runtime**: Python 3.12 (`python3.12`).
- **Handler**: `app.handler` → archivo `lambda/app.py`, función `handler(event, context)`.
- **Event payload**: JSON con `Records[]`, cada record tiene `s3.bucket.name`, `s3.object.key` (URL-encoded).
- **IAM execution role**: política inline con permisos mínimos (S3 Get/Put/List, Bedrock InvokeModel, Secrets Manager GetSecretValue, SQS SendMessage, logs).
- **Environment variables**: `BEDROCK_MODEL_ID`, `SEARCH_PROVIDER`, `WEBSEARCH_ENABLED`, etc. Configuración sin hardcodear en código.
- **Timeout**: 300 s (5 min) para cubrir búsqueda + LLM; default es 3 s (insuficiente).
- **Memory**: 1024 MB; más memoria → más CPU proporcional.
- **Dead Letter Queue (DLQ)**: cola SQS para invocaciones fallidas tras reintentos automáticos (por defecto 2 reintentos).
- **Tracing**: X-Ray activo (`Tracing: Active`) para observabilidad de requests distribuidos.

### Para el examen

- **Concurrency**: account limit regional (1000 concurrent por defecto); reserved concurrency para funciones críticas.
- **Cold start**: primera invocación tarda más (importar librerías, conectar); reutilización del entorno en invocaciones siguientes si container "caliente".
- **Idempotencia**: S3 puede duplicar eventos (at-least-once delivery); código debe manejar reintentos sin efectos secundarios duplicados (en nuestro caso, sobrescribir `wiki/` con misma clave es idempotente).
- **Asynchronous invocation** (S3 → Lambda): event queue interna, reintentos automáticos, DLQ tras fallo.
- **VPC**: por defecto Lambda corre **fuera** de VPC (acceso internet directo); si se mete en VPC privada necesita NAT Gateway o VPC endpoints para AWS APIs.

---

## 3. Amazon Bedrock

### Conceptos clave aplicados

- **Foundation models**: modelos pre-entrenados (Claude 3, Titan, Llama, etc.) via API sin gestionar infraestructura.
- **Model access**: hay que **habilitar** cada modelo en la consola Bedrock región por región antes de usar `InvokeModel`.
- **`InvokeModel` API**: POST con `modelId` (ARN foundation model), `body` JSON según formato del proveedor:
  - **Anthropic Claude**: Messages API con `anthropic_version`, `max_tokens`, `system` prompt, `messages[]`.
  - **Amazon Titan**: `inputText`, `textGenerationConfig`.
- **Permisos IAM**: `bedrock:InvokeModel` en recurso `arn:aws:bedrock:<region>::foundation-model/<modelId>`.
- **Sin Knowledge Bases aquí**: invocamos el modelo directamente con contexto en el prompt (grounding manual con snippets de búsqueda).

### Para el examen

- **Regiones**: Bedrock no está en todas (us-east-1, us-west-2, eu-west-1 típicas).
- **Pricing**: por tokens input + output; Claude cobra ~$0.00025/1K tokens input (varía).
- **Streaming**: `InvokeModelWithResponseStream` para responses largas chunk by chunk.
- **Guardrails**: políticas de contenido, PII redaction (no implementadas en el MVP pero existen).
- **Agents**: orquestación automática con herramientas (distinto a nuestro caso de invoke directo).

---

## 4. AWS Secrets Manager

### Conceptos clave aplicados

- **Secret**: almacén de credenciales cifrado (KMS por defecto con clave AWS-managed).
- **JSON structure**: `{"openai_api_key": "sk-...", "tavily_api_key": "..."}` o plain string.
- **ARN con sufijo aleatorio**: `arn:aws:secretsmanager:us-east-1:307657261121:secret:obsidian-vault/search-keys-6ZrlP7` (los últimos 6 chars son únicos).
- **`GetSecretValue` API**: Lambda llama con `SecretId` (ARN o nombre), devuelve `SecretString`.
- **Cache en Lambda**: variable global fuera del handler para reutilizar entre invocaciones (reduce llamadas a Secrets Manager).
- **Rotation**: Secrets Manager puede rotar automáticamente (Lambda de rotación); no implementado aquí pero es feature clave.

### Para el examen

- **vs SSM Parameter Store**: Secrets Manager tiene rotación nativa, más caro (~$0.40/secret/mes + $0.05/10K API calls); Parameter Store SecureString más barato pero sin rotación automática.
- **IAM**: `secretsmanager:GetSecretValue` en el ARN del secreto; condition `secretsmanager:Name` para crear solo ciertos nombres.
- **Cifrado**: KMS key (AWS-managed o customer-managed); `kms:Decrypt` implícito al leer.
- **VPC endpoints**: si Lambda en VPC sin NAT, necesita VPC endpoint de Secrets Manager para acceso privado.

---

## 5. IAM (Identity and Access Management)

### Conceptos clave aplicados

- **Execution role**: rol asumido por Lambda con `AssumeRolePolicyDocument` trust de `lambda.amazonaws.com`.
- **Inline policy**: política directamente en el recurso (en SAM: `Policies:` en la función).
- **Least privilege**: scope ARNs a prefijos específicos (`arn:aws:s3:::bucket/raw/*` solo lectura; `/wiki/*` solo escritura).
- **Condition keys**: `s3:prefix` en `ListBucket` limita a qué prefijos puede listar.
- **Resource-based policy**: el `AWS::Lambda::Permission` permite a S3 (`Principal: s3.amazonaws.com`) invocar la función con `SourceArn` del bucket.

### Para el examen

- **Policy evaluation**: Deny explícito > Allow > Deny implícito (por defecto todo denegado).
- **Service Control Policies (SCP)**: en AWS Organizations; puede bloquear aunque IAM policy diga Allow.
- **Permission boundary**: límite máximo de permisos en un usuario/rol; no puede excederlo aunque tenga Allow policies.
- **PassRole**: para que Lambda/Fargate/etc. asuma un rol, quien crea el recurso necesita `iam:PassRole` en ese rol.
- **Cross-account**: bucket en cuenta A puede dar permiso a rol en cuenta B via bucket policy + assume role.

---

## 6. Amazon SQS (Simple Queue Service)

### Conceptos clave aplicados

- **Dead Letter Queue (DLQ)**: cola separada para mensajes/eventos fallidos.
- **DeadLetterQueue** en Lambda: tras N reintentos asíncronos, evento va a la DLQ.
- **MessageRetentionPeriod**: 1209600 s (14 días) máximo; después mensaje se borra.
- **Visibility timeout**: cuando consumer lee mensaje, queda "invisible" para otros; si no se borra, reaparece (no aplicado aquí, DLQ solo recibe sin re-process).

### Para el examen

- **Standard vs FIFO**: Standard = throughput alto, at-least-once, orden best-effort. FIFO = exactamente-una-vez, orden estricto, max 300 TPS (3000 con batching).
- **Long polling**: `ReceiveMessageWaitTimeSeconds > 0` reduce llamadas vacías; más eficiente que short polling.
- **Batch operations**: `SendMessageBatch`, `DeleteMessageBatch` hasta 10 mensajes.
- **DLQ con Lambda**: configurar `MaximumRetryAttempts` y `OnFailure` destination (SQS o SNS).
- **Encryption**: SSE-SQS (AWS-managed) o SSE-KMS.

---

## 7. AWS CloudFormation & SAM (Serverless Application Model)

### Conceptos clave aplicados

- **Infrastructure as Code (IaC)**: `template.yaml` declara recursos; CloudFormation los crea/actualiza.
- **SAM Transform**: `Transform: AWS::Serverless-2016-10-31` expande recursos shorthand (`AWS::Serverless::Function` → Lambda + rol + logs).
- **Stack**: unidad de deploy con lifecycle (CREATE, UPDATE, DELETE, ROLLBACK).
- **Parameters**: `SearchApiSecretArn`, `BedrockModelId`, etc. Valores en deploy time sin cambiar template.
- **Outputs**: `BucketName`, `ProcessorFunctionArn` exportables para otros stacks o CLI.
- **Conditions**: `HasSearchSecret: !Not [!Equals [!Ref SearchApiSecretArn, '']]` para lógica condicional.
- **Intrinsic functions**: `!Sub`, `!Ref`, `!GetAtt`, `!Select`, `!Split`, `Fn::If`.
- **DependsOn**: controla orden de creación; `ContentBucket` depende de `ProcessorPermissionForS3`.
- **Circular dependency**: error fatal; roto usando ARNs calculados (`!Sub` con `AWS::AccountId` + `AWS::StackId`) sin `!Ref` al recurso físico.

### Para el examen

- **Change sets**: preview de cambios antes de aplicar UPDATE; útil en prod.
- **Drift detection**: detecta cambios manuales en recursos vs lo que dice el template.
- **Rollback**: automático en fallo; `--disable-rollback` para debug (deja recursos a medias).
- **Nested stacks**: template referencia otros templates (`AWS::CloudFormation::Stack`); modularidad.
- **Stack policies**: JSON que previene updates en ciertos recursos (ej. no borrar DB).
- **Capabilities**: `CAPABILITY_IAM` obligatorio si creas roles; `CAPABILITY_NAMED_IAM` si tienen nombres custom.
- **sam build**: empaqueta dependencias (pip/npm) en `.aws-sam/build/`; `sam deploy`: sube a S3 + crea/actualiza stack.

---

## 8. Integraciones de terceros & HTTPS desde Lambda

### Conceptos clave aplicados

- **OpenAI Responses API**: POST a `https://api.openai.com/v1/responses` con `Authorization: Bearer <key>`, body JSON con `model`, `tools: [{"type": "web_search"}]`, `input`.
- **Sin librerías pesadas**: `urllib.request` de stdlib para HTTP (evita dependencias grandes en Lambda package).
- **Secrets en headers**: clave desde Secrets Manager → header `Authorization`.
- **Parsing de respuestas**: JSON con `output[]` que incluye `annotations` (citations) con URLs.
- **Fallback**: si API de búsqueda falla (HTTP 5xx, timeout, etc.), degradar a solo nota local sin tumbar el pipeline (try/except, log warning, continuar).

### Para el examen

- **API Gateway**: para exponer Lambda vía HTTP (aquí no hay; S3 dispara directo).
- **Retry con backoff**: best practice para APIs externas rate-limited.
- **Timeouts**: Lambda timeout debe > timeout HTTP; si HTTP tarda 60s y Lambda timeout 30s → falla.
- **Egress data transfer**: tráfico saliente a internet desde Lambda cuesta (primeros GB free, luego ~$0.09/GB según región).
- **VPC**: si Lambda en VPC, internet saliente requiere NAT Gateway o IGW (Internet Gateway solo para subnets públicas).

---

## 9. Observabilidad: CloudWatch Logs & X-Ray

### Conceptos clave aplicados

- **CloudWatch Logs**: cada invocación Lambda escribe stdout/stderr a log stream `/aws/lambda/<function-name>`.
- **Structured logging**: `LOG.info("key=%s val=%s", k, v)` mejor que `print` para parsear.
- **Log groups**: creado automáticamente por Lambda; retention configurable (14 días en el template via parameter, pero SAM no setea retention por defecto; se puede añadir `AWS::Logs::LogGroup`).
- **X-Ray tracing**: `Tracing: Active` inyecta trace ID en invocaciones; segmentos para llamadas a Bedrock, S3, Secrets (si usas AWS X-Ray SDK; con boto3 plain requiere `aws-xray-sdk` wrapper).

### Para el examen

- **Metrics**: Lambda publica automáticamente Invocations, Errors, Duration, Throttles, ConcurrentExecutions a CloudWatch.
- **Custom metrics**: `PutMetricData` en código para business metrics.
- **Alarms**: `AWS::CloudWatch::Alarm` en template con `Threshold`, `EvaluationPeriods`, SNS action.
- **Logs Insights**: query language SQL-like para analizar logs (`fields @timestamp, @message | filter @message like /error/ | sort @timestamp desc`).
- **X-Ray service map**: visualiza dependencias entre Lambda, S3, Bedrock; identifica cuellos de botella.
- **Sampling**: X-Ray muestrea % de requests (configurable); 100% sampling caro en alto volumen.

---

## 10. Despliegue & DevOps

### Conceptos clave aplicados

- **sam build**: instala `requirements.txt` en `.aws-sam/build/ProcessorFunction/`, copia código.
- **sam deploy**: empaqueta ZIP, sube a S3 (bucket de artifacts), crea changeset, ejecuta.
- **`--guided`**: wizard interactivo; guarda `samconfig.toml` para deploys futuros sin preguntas.
- **`--resolve-s3`**: SAM crea bucket de artifacts automáticamente (nombre generado).
- **Stack lifecycle**: primer deploy = CREATE; siguientes = UPDATE (changeset); `delete-stack` borra todos los recursos (excepto S3 bucket con objetos si no tiene `DeletionPolicy: Retain`).

### Para el examen

- **CI/CD con CodePipeline**: Source (CodeCommit/GitHub) → Build (CodeBuild ejecuta `sam build && sam package`) → Deploy (CloudFormation deploy changeset).
- **Blue/Green deploys en Lambda**: alias + versiones ($LATEST, v1, v2) con weighted routing; SAM soporta `AutoPublishAlias` + `DeploymentPreference`.
- **Canary**: `DeploymentPreference: Type: Canary10Percent5Minutes` rutas 10% tráfico a nueva versión, espera 5 min, checa alarmas, si OK rutas 100%.
- **Rollback automático**: si alarma CloudWatch se dispara durante deploy, CloudFormation rollback a versión anterior.
- **AWS SAM CLI local**: `sam local invoke` / `sam local start-api` para testing local con Docker (no usado aquí pero útil en dev).

---

## 11. Seguridad (defense in depth)

### Aplicado en el proyecto

- **Encryption at rest**: S3 SSE-S3, Secrets Manager con KMS.
- **Encryption in transit**: HTTPS para llamadas a OpenAI, Bedrock API (TLS 1.2+).
- **Least privilege IAM**: políticas acotadas a prefijos, acciones mínimas.
- **No secrets en código**: `OPENAI_API_KEY` en Secrets Manager, no hardcoded.
- **Public access block en S3**: previene bucket público accidental.
- **Versioning**: S3 versionado para recuperación ante borrado/sobrescritura maliciosa.
- **DLQ**: eventos fallidos no se pierden, investigables.

### Para el examen

- **KMS**: Customer-Managed Keys (CMK) para control de rotación, auditoría (CloudTrail registra `Decrypt`).
- **S3 bucket policies**: denegar HTTP (`aws:SecureTransport: false`), forzar cifrado en PUT.
- **IAM policy conditions**: `aws:SourceIp`, `aws:CurrentTime`, `aws:SecureTransport`.
- **Cognito**: autenticación de usuarios; no aplicado aquí (backend sin UI).
- **API Gateway + Lambda authorizer**: validar JWT tokens antes de invocar Lambda.
- **VPC security groups**: si Lambda en VPC, SG controla tráfico; egress 443 a Bedrock/Secrets.
- **AWS Secrets Manager rotation**: Lambda automática cada N días para rotar credenciales (ej. RDS passwords).

---

## 12. Costes & Escalabilidad

### Proyecto actual

- **S3**: $0.023/GB-mes (standard); operaciones GET/PUT ~$0.0004/1K requests.
- **Lambda**: primeros 1M requests/mes free, luego $0.20/1M; $0.0000166667/GB-s compute.
- **Bedrock Claude 3 Haiku**: ~$0.00025/1K tokens input, $0.00125/1K output (varía por modelo).
- **Secrets Manager**: $0.40/secret/mes + $0.05/10K API calls.
- **OpenAI (si se usa)**: facturación externa; ej. GPT-4o-mini ~$0.15/1M input tokens.
- **Data transfer**: primeros 100 GB/mes saliente free, luego ~$0.09/GB (a internet desde Lambda).

### Escalabilidad considerada

- **Concurrencia Lambda**: 1000 concurrent por defecto; si `sync:up` sube 5000 archivos simultáneos, algunos throttled → reintento automático.
- **S3 throughput**: ~5500 GET/s, ~3500 PUT/s **por prefijo**; layout inteligente con prefijos si millones de objetos.
- **Bedrock quotas**: requests/min por modelo (ej. 100 req/min Claude Haiku); si excede → HTTP 429, Lambda reintenta.
- **DLQ como backpressure**: eventos fallidos no bloquean nuevos; procesar DLQ async o alarma si crece.

### Para el examen

- **Reserved Concurrency en Lambda**: garantiza N invocaciones simultáneas para función crítica (resta del pool compartido).
- **Provisioned Concurrency**: pre-warm containers para eliminar cold starts (coste 24/7).
- **S3 Intelligent-Tiering**: mueve objetos entre access tiers automáticamente; ahorra si archivos viejos no se leen.
- **CloudWatch Logs retention**: logs sin retention infinita → coste sube; setear 7-30 días en prod.
- **X-Ray sampling**: 1 request/s + 5% resto (default) balancea coste vs visibilidad.
- **Bedrock batch inference**: para datasets grandes offline; más barato que real-time (no usado aquí).

---

## 13. Troubleshooting & Debugging (escenarios reales del proyecto)

### Problemas encontrados y solución

1. **`InvalidClientTokenId` en AWS CLI**  
   **Causa**: credenciales caducadas o mal configuradas.  
   **Fix**: `aws configure` con Access Key válida; o `aws sso login` si SSO.

2. **`AccessDeniedException` en `CreateSecret`**  
   **Causa**: usuario IAM sin `secretsmanager:CreateSecret`.  
   **Fix**: adjuntar política `SecretsManagerReadWrite` o inline custom.

3. **`Circular dependency between resources`**  
   **Causa**: rol Lambda referencia bucket (`!GetAtt ContentBucket.Arn`) y bucket referencia Lambda (notification).  
   **Fix**: usar ARN calculado con `!Sub` + `AWS::AccountId` + `AWS::StackId` sin `!Ref` al recurso físico; separar `AWS::Lambda::Permission` con `DependsOn`.

4. **Bedrock `AccessDeniedException` en `InvokeModel`**  
   **Causa**: modelo no habilitado en región.  
   **Fix**: consola Bedrock → Model access → enable Claude 3 Haiku.

5. **Lambda timeout tras 3 segundos**  
   **Causa**: default 3s; búsqueda OpenAI + Bedrock tarda más.  
   **Fix**: `Timeout: 300` en template.

6. **S3 event no dispara Lambda**  
   **Causa**: falta `AWS::Lambda::Permission` con `Principal: s3.amazonaws.com`.  
   **Fix**: crear permiso explícito con `SourceArn` del bucket.

### Para el examen

- **CloudWatch Logs**: primera parada para errores Lambda; buscar `ERROR`, `Exception`, `Traceback`.
- **X-Ray traces**: si timeout, ver qué subsegmento tardó más (Bedrock? Secrets Manager?).
- **Test events en consola Lambda**: invocar función manualmente con JSON de S3 event sample.
- **CloudTrail**: auditoría de llamadas API; ver quién creó/borró recursos, permisos denegados (útil en troubleshoot IAM).
- **VPC Flow Logs**: si Lambda en VPC y falla conectar a Bedrock → revisar si hay NAT, rutas, Security Group egress 443.

---

## 14. Mejoras & Extensiones (mencionadas en el proyecto pero no implementadas)

- **SQS entre S3 y Lambda**: desacoplar; S3 → SQS → Lambda poll SQS (evita thundering herd en sync masivo).
- **Step Functions**: orquestar multi-paso (búsqueda → Bedrock → validación → escritura); retry policies granulares, parallel branches.
- **DynamoDB para metadatos**: trackear estado de procesamiento (`raw/file.md` → `processing` → `done`), evitar re-process.
- **EventBridge rules**: filtrar eventos S3 con content-based routing (ej. solo `.md` con tag específico).
- **AppConfig**: feature flags (activar/desactivar websearch sin redeploy).
- **Secrets rotation Lambda**: auto-rotar OpenAI key cada 90 días.
- **CloudFront + S3**: si se quisiera servir `wiki/` vía web pública (signed URLs).

---

## Resumen de skills DVA-C02 cubiertas

| Dominio | Skill | Implementado |
|---------|-------|--------------|
| **Development** | Event-driven architecture (S3 → Lambda) | ✅ |
| | SDK usage (boto3: S3, Secrets, Bedrock) | ✅ |
| | Environment variables & config | ✅ |
| | Error handling & graceful degradation | ✅ |
| **Security** | IAM least privilege (scoped ARNs, conditions) | ✅ |
| | Secrets Manager for credentials | ✅ |
| | Encryption at rest (S3 SSE, Secrets KMS) | ✅ |
| | Encryption in transit (HTTPS) | ✅ |
| **Deployment** | IaC with CloudFormation/SAM | ✅ |
| | CI/CD concepts (no pipeline físico pero diseño para CodePipeline) | 📖 |
| | Rollback on failure | ✅ |
| **Troubleshooting** | CloudWatch Logs structured logging | ✅ |
| | X-Ray tracing active | ✅ (básico) |
| | DLQ for failed invocations | ✅ |
| | Debugging circular dependencies | ✅ |

**Nota:** El proyecto cubre ~70-80% de los temas core del DVA-C02. Faltan: API Gateway, DynamoDB transacciones, Step Functions, CodePipeline desplegado (aunque el template es CI/CD-ready), Cognito, ElastiCache.

---

## Comandos útiles para recordar (cheatsheet)

```bash
# Ver identidad AWS CLI
aws sts get-caller-identity

# Crear secreto
aws secretsmanager create-secret --name X --secret-string '{"key":"val"}'

# Deploy SAM
sam build && sam deploy --guided

# Subir a S3 (dispara Lambda)
aws s3 cp file.md s3://bucket/raw/file.md

# Ver logs Lambda
aws logs tail /aws/lambda/FUNCTION --follow

# Borrar stack
aws cloudformation delete-stack --stack-name X

# Listar modelos Bedrock
aws bedrock list-foundation-models --region us-east-1
```

---

**Conclusión:** Este proyecto es un **caso práctico end-to-end** que integra servicios core del Developer Associate: compute (Lambda), storage (S3), ML (Bedrock), secrets (Secrets Manager), observability (CloudWatch/X-Ray), IaC (SAM), IAM, SQS. La arquitectura event-driven, least-privilege IAM y manejo de dependencias circulares en CloudFormation son **temas frecuentes en el examen**. Estudiar este diseño + troubleshooting real prepara bien para escenarios de preguntas tipo "¿Cómo implementarías X de forma serverless/segura/escalable?".
