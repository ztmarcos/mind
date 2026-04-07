"""
S3-triggered processor: raw/*.md -> Bedrock -> wiki/*.md + wiki/index.md
Optional web search via OpenAI (Responses + web_search) or Tavily / Brave / Serper.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

import boto3
import yaml

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

# Environment variables (read first before creating clients)
USE_BEDROCK = os.environ.get("USE_BEDROCK", "false").lower() == "true"
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
WEBSEARCH_ENABLED = os.environ.get("WEBSEARCH_ENABLED", "true").lower() == "true"
SEARCH_API_SECRET_ARN = os.environ.get("SEARCH_API_SECRET_ARN", "").strip()
MAX_QUERIES = int(os.environ.get("MAX_QUERIES", "3"))
MAX_SNIPPETS = int(os.environ.get("MAX_SNIPPETS", "8"))

# AWS clients (conditionally initialize bedrock)
s3 = boto3.client("s3")
secrets = boto3.client("secretsmanager")
if USE_BEDROCK:
    bedrock = boto3.client("bedrock-runtime")
else:
    bedrock = None

SYSTEM_PROMPT = """You are a careful editor producing Obsidian-ready markdown.

Output ONLY valid markdown with exactly these sections in order:
# Title
## Summary
(bullet list: concise explanation of the SOURCE NOTE; you may incorporate web facts ONLY if they appear in the SEARCH RESULTS block)
## Key Concepts
(bullet list of Obsidian wikilinks: [[concept]] — extract reusable concepts from the source, not generic fluff)
## Insights
(bullet list: distilled learnings; do not invent external facts)
## Related
(bullet list: [[other-notes]] style links; may reference concept names from the note)
## Sources
(ONLY if SEARCH RESULTS below is non-empty: bullet list lines formatted exactly as: `- Title — URL` using ONLY titles and URLs from SEARCH RESULTS. If SEARCH RESULTS is empty or missing, omit the entire ## Sources section.)

Rules:
- Use Obsidian-style [[wikilinks]] in Key Concepts and Related where appropriate.
- For external facts (news, dates, statistics), use ONLY information present in SEARCH RESULTS snippets; if unsure, say so in Insights.
- Do NOT fabricate URLs or citations.
- Be structured and concise.
- Do NOT wrap the output in markdown code fences.
"""

_SECRET_CACHE: dict[str, Any] | None = None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    records = event.get("Records") or []
    results: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("eventSource") != "aws:s3":
            continue
        s3_info = rec.get("s3") or {}
        bucket = (s3_info.get("bucket") or {}).get("name")
        key = _decode_s3_key((s3_info.get("object") or {}).get("key", ""))
        if not bucket or not key:
            LOG.warning("skip record: missing bucket or key")
            continue
        if not key.startswith("raw/") or not key.lower().endswith(".md"):
            LOG.info("skip key (not raw/*.md): %s", key)
            continue
        t0 = time.perf_counter()
        try:
            _process_object(bucket, key)
            results.append({"bucket": bucket, "key": key, "ok": True, "ms": int((time.perf_counter() - t0) * 1000)})
        except Exception as e:
            LOG.exception("failed key=%s", key)
            raise
    return {"processed": results}


def _decode_s3_key(key: str) -> str:
    from urllib.parse import unquote_plus

    return unquote_plus(key)


def _process_object(bucket: str, key: str) -> None:
    LOG.info("process start bucket=%s key=%s", bucket, key)
    obj = s3.get_object(Bucket=bucket, Key=key)
    body_bytes = obj["Body"].read()
    text = body_bytes.decode("utf-8", errors="replace")

    meta, note_body = _parse_frontmatter(text)
    websearch = _coerce_websearch(meta, note_body)

    if USE_BEDROCK:
        search_queries = _normalize_queries(meta.get("search_queries"))
        search_results: list[dict[str, str]] = []
        if websearch and WEBSEARCH_ENABLED and SEARCH_API_SECRET_ARN:
            queries = search_queries if search_queries else _derive_queries(note_body, key)
            queries = queries[:MAX_QUERIES]
            LOG.info("search via openai_web, queries=%s", queries)
            try:
                creds = _load_secrets()
                search_results = _search_openai_web_for_bedrock(queries, creds)
                search_results = search_results[:MAX_SNIPPETS]
                LOG.info("search results count=%s", len(search_results))
            except Exception as e:
                LOG.warning("search failed (degraded to note-only): %s", e)
                search_results = []
        elif websearch and not SEARCH_API_SECRET_ARN:
            LOG.warning("websearch requested but SEARCH_API_SECRET_ARN empty; skipping search")
        wiki_markdown = invoke_bedrock_wiki(note_body, search_results)
    else:
        # OpenAI direct: single call does both search + wiki generation
        creds = _load_secrets()
        wiki_markdown = invoke_openai_wiki(note_body, creds)
    rel = key.removeprefix("raw/")
    wiki_key = f"wiki/{rel}"
    s3.put_object(
        Bucket=bucket,
        Key=wiki_key,
        Body=wiki_markdown.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    LOG.info("wrote wiki key=%s bytes=%s", wiki_key, len(wiki_markdown))
    rebuild_wiki_index(bucket)


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    if not m:
        return {}, raw
    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    rest = raw[m.end() :]
    return meta, rest


def _coerce_websearch(meta: dict[str, Any], body: str) -> bool:
    if not WEBSEARCH_ENABLED:
        return False
    if "websearch" in meta:
        return _truthy(meta["websearch"])
    return bool(SEARCH_API_SECRET_ARN)


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    if s in ("false", "0", "no", "off"):
        return False
    return bool(s)


def _normalize_queries(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        out: list[str] = []
        for x in raw:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    return []


def _derive_queries(body: str, s3_key: str) -> list[str]:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    candidates: list[str] = []
    for ln in lines[:40]:
        if ln.startswith("#"):
            t = ln.lstrip("#").strip()
            if len(t) > 3:
                candidates.append(t[:200])
        elif "?" in ln and len(ln) < 200:
            candidates.append(ln)
    base = os.path.basename(s3_key).replace(".md", "").replace("-", " ").replace("_", " ")
    if base and base not in candidates:
        candidates.insert(0, base[:200])
    snippet = " ".join(body.split())[:400]
    if snippet:
        candidates.append(snippet[:200])
    # dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out[: max(1, MAX_QUERIES)]


def _load_secrets() -> dict[str, str]:
    global _SECRET_CACHE
    if not SEARCH_API_SECRET_ARN:
        return {}
    if _SECRET_CACHE is not None:
        return _SECRET_CACHE
    resp = secrets.get_secret_value(SecretId=SEARCH_API_SECRET_ARN)
    s = resp.get("SecretString") or ""
    data: dict[str, str] = {}
    if s.strip().startswith("{"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                data = {str(k): str(v) for k, v in parsed.items() if v is not None}
                LOG.info("loaded secret keys: %s", list(data.keys()))
        except json.JSONDecodeError as e:
            LOG.error("failed to parse secret JSON: %s", e)
    else:
        # single-key secret: map by provider
        key = s.strip()
        if SEARCH_PROVIDER == "openai_web":
            data["openai_api_key"] = key
        elif SEARCH_PROVIDER == "tavily":
            data["tavily_api_key"] = key
        elif SEARCH_PROVIDER == "brave":
            data["brave_api_key"] = key
        elif SEARCH_PROVIDER == "serper":
            data["serper_api_key"] = key
    _SECRET_CACHE = data
    return data


def _search_openai_web_for_bedrock(queries: list[str], creds: dict[str, str]) -> list[dict[str, str]]:
    """Search via OpenAI Responses for snippets to pass to Bedrock"""
    all_results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for q in queries:
        if not q:
            continue
        batch = _search_openai_web(q, creds)
        for item in batch:
            u = (item.get("url") or "").strip()
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            all_results.append(item)
        if len(all_results) >= MAX_SNIPPETS:
            break
    return all_results


def _http_json(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> Any:
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        LOG.warning("http error %s %s", e.code, err_body[:500])
        raise


def _get_openai_key(creds: dict[str, str]) -> str:
    api_key = creds.get("openai_api_key") or creds.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("secret missing openai_api_key")
    return api_key


def _search_openai_web(query: str, creds: dict[str, str]) -> list[dict[str, str]]:
    api_key = _get_openai_key(creds)
    url = "https://api.openai.com/v1/responses"
    user_prompt = (
        "Use web search to find up-to-date sources for this topic. "
        "Then summarize the top distinct sources as JSON array of objects with keys "
        "title, url, snippet (one sentence each). Topic:\n"
        f"{query}"
    )
    payload: dict[str, Any] = {
        "model": OPENAI_MODEL,
        "tools": [{"type": "web_search"}],
        "input": user_prompt,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = _http_json(url, method="POST", headers=headers, body=payload, timeout=120)
    return _openai_results_from_response(resp)


def _openai_results_from_response(resp: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    # Citations / annotations on output messages
    output = resp.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            contents = item.get("content")
            if isinstance(contents, list):
                for block in contents:
                    if not isinstance(block, dict):
                        continue
                    anns = block.get("annotations")
                    if isinstance(anns, list):
                        for ann in anns:
                            if not isinstance(ann, dict):
                                continue
                            if ann.get("type") in ("url_citation", "citation"):
                                title = str(ann.get("title") or ann.get("name") or "Source")
                                u = str(ann.get("url") or "")
                                snippet = str(ann.get("snippet") or ann.get("text") or "")
                                if u:
                                    out.append({"title": title, "url": u, "snippet": snippet[:500]})
    # Fallback: scrape URLs from plain text
    if not out:
        text = _collect_openai_text(resp)
        for m in re.finditer(r"https?://[^\s)\]>\"']+", text):
            u = m.group(0).rstrip(".,);")
            out.append({"title": "Source", "url": u, "snippet": ""})
    return out[:MAX_SNIPPETS]


def _collect_openai_text(resp: dict[str, Any]) -> str:
    parts: list[str] = []
    output = resp.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") == "message":
                for block in item.get("content") or []:
                    if isinstance(block, dict) and block.get("text"):
                        parts.append(str(block["text"]))
    return "\n".join(parts)


def invoke_openai_wiki(note_body: str, creds: dict[str, str]) -> str:
    """Use OpenAI Chat Completions (with optional web search) for wiki generation"""
    api_key = _get_openai_key(creds)
    url = "https://api.openai.com/v1/chat/completions"
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"SOURCE NOTE (markdown):\n{note_body}"}
    ]
    
    payload: dict[str, Any] = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = _http_json(url, method="POST", headers=headers, body=payload, timeout=120)
    
    choices = resp.get("choices") or []
    if not choices:
        raise ValueError("OpenAI returned no choices")
    
    text = choices[0].get("message", {}).get("content", "")
    return _strip_markdown_fences(text.strip())


def invoke_bedrock_wiki(note_body: str, search_results: list[dict[str, str]]) -> str:
    if not USE_BEDROCK:
        raise RuntimeError("invoke_bedrock_wiki called but USE_BEDROCK=false")
    search_block = _format_search_block(search_results)
    user_content = f"{search_block}\n\n---\n\nSOURCE NOTE (markdown):\n{note_body}"

    model = BEDROCK_MODEL_ID
    if "anthropic" in model.lower() or "claude" in model.lower():
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8192,
            "temperature": 0.2,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_content}]}],
        }
        resp = bedrock.invoke_model(modelId=model, body=json.dumps(body).encode("utf-8"))
        out = json.loads(resp["body"].read())
        parts = out.get("content") or []
        text = ""
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "text":
                text += p.get("text") or ""
        return _strip_markdown_fences(text.strip())

    # Amazon Titan fallback
    body = {
        "inputText": SYSTEM_PROMPT + "\n\n" + user_content,
        "textGenerationConfig": {"maxTokenCount": 4096, "temperature": 0.2},
    }
    resp = bedrock.invoke_model(modelId=model, body=json.dumps(body).encode("utf-8"))
    out = json.loads(resp["body"].read())
    results = out.get("results") or []
    text = results[0].get("outputText", "") if results else ""
    return _strip_markdown_fences(text.strip())


def _search_tavily(query: str, creds: dict[str, str]) -> list[dict[str, str]]:
    key = creds.get("tavily_api_key") or creds.get("TAVILY_API_KEY")
    if not key:
        raise ValueError("secret missing tavily_api_key")
    data = _http_json(
        "https://api.tavily.com/search",
        method="POST",
        body={"api_key": key, "query": query, "max_results": min(10, MAX_SNIPPETS)},
        timeout=60,
    )
    results = data.get("results") if isinstance(data, dict) else None
    out: list[dict[str, str]] = []
    if isinstance(results, list):
        for r in results:
            if not isinstance(r, dict):
                continue
            u = str(r.get("url") or "")
            if not u:
                continue
            out.append(
                {
                    "title": str(r.get("title") or "Source")[:300],
                    "url": u,
                    "snippet": str(r.get("content") or r.get("snippet") or "")[:600],
                }
            )
    return out


def _search_brave(query: str, creds: dict[str, str]) -> list[dict[str, str]]:
    key = creds.get("brave_api_key") or creds.get("BRAVE_API_KEY")
    if not key:
        raise ValueError("secret missing brave_api_key")
    from urllib.parse import quote_plus

    u = "https://api.search.brave.com/res/v1/web/search?q=" + quote_plus(query)
    data = _http_json(u, method="GET", headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=60)
    out: list[dict[str, str]] = []
    web = (data.get("web") or {}) if isinstance(data, dict) else {}
    results = web.get("results")
    if isinstance(results, list):
        for r in results:
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "")
            if not url:
                continue
            title = str(r.get("title") or r.get("profile") or "Source")
            desc = str(r.get("description") or "")
            out.append({"title": title[:300], "url": url, "snippet": desc[:600]})
    return out


def _search_serper(query: str, creds: dict[str, str]) -> list[dict[str, str]]:
    key = creds.get("serper_api_key") or creds.get("SERPER_API_KEY")
    if not key:
        raise ValueError("secret missing serper_api_key")
    data = _http_json(
        "https://google.serper.dev/search",
        method="POST",
        headers={"X-API-KEY": key},
        body={"q": query, "num": min(10, MAX_SNIPPETS)},
        timeout=60,
    )
    out: list[dict[str, str]] = []
    for k in ("organic", "news"):
        block = data.get(k) if isinstance(data, dict) else None
        if not isinstance(block, list):
            continue
        for r in block:
            if not isinstance(r, dict):
                continue
            url = str(r.get("link") or r.get("url") or "")
            if not url:
                continue
            title = str(r.get("title") or "Source")
            snippet = str(r.get("snippet") or "")
            out.append({"title": title[:300], "url": url, "snippet": snippet[:600]})
    return out


def _format_search_block(results: list[dict[str, str]]) -> str:
    if not results:
        return ""
    lines = ["SEARCH RESULTS (use only for external facts; each line is one source):"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. title={r.get('title','')}")
        lines.append(f"   url={r.get('url','')}")
        lines.append(f"   snippet={r.get('snippet','')[:800]}")
    return "\n".join(lines)


def _strip_markdown_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def rebuild_wiki_index(bucket: str) -> None:
    keys: list[str] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": "wiki/"}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            k = obj.get("Key") or ""
            if not k.endswith(".md"):
                continue
            if k == "wiki/index.md":
                continue
            keys.append(k)
        if page.get("IsTruncated"):
            token = page.get("NextContinuationToken")
        else:
            break
    keys.sort()
    lines = ["# Wiki index", "", "Generated pages:", ""]
    for k in keys:
        inner = k.removeprefix("wiki/").removesuffix(".md")
        if inner:
            lines.append(f"- [[{inner}]]")
    body = "\n".join(lines) + "\n"
    s3.put_object(
        Bucket=bucket,
        Key="wiki/index.md",
        Body=body.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    LOG.info("rebuilt wiki/index.md entries=%s", len(keys))
