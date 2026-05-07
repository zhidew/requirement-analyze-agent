from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
EMBEDDING_DIM = 64
BM25_K1 = 1.2
BM25_B = 0.75
REMOTE_TIMEOUT_SECONDS = 15


class KnowledgeBaseError(RuntimeError):
    pass


def _load_yaml_file(path: Path):
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _tokenize_text(text: str) -> List[str]:
    tokens: List[str] = []
    for raw in TOKEN_RE.findall(text or ""):
        lowered = raw.strip().lower()
        if not lowered:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", lowered):
            tokens.append(lowered)
            if len(lowered) == 1:
                continue
            for idx in range(len(lowered) - 1):
                tokens.append(lowered[idx : idx + 2])
            if len(lowered) > 2:
                for idx in range(len(lowered) - 2):
                    tokens.append(lowered[idx : idx + 3])
        else:
            tokens.append(lowered)
    return tokens


def _build_embedding(text: str, dim: int = EMBEDDING_DIM) -> List[float]:
    vector = [0.0] * dim
    for token in _tokenize_text(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign

    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [round(value / norm, 6) for value in vector]


def _cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _normalize_scores(rows: List[Dict[str, Any]], score_key: str) -> Dict[str, float]:
    values = [max(0.0, float(row.get(score_key, 0.0))) for row in rows]
    if not values:
        return {}
    minimum = min(values)
    maximum = max(values)
    if math.isclose(minimum, maximum):
        if maximum <= 0:
            return {str(row["chunk_id"]): 0.0 for row in rows}
        return {str(row["chunk_id"]): 1.0 for row in rows}
    return {
        str(row["chunk_id"]): (max(0.0, float(row.get(score_key, 0.0))) - minimum) / (maximum - minimum)
        for row in rows
    }


def _normalize_chunk_scores(rows: List[Dict[str, Any]], score_getter) -> Dict[str, float]:
    if not rows:
        return {}
    values = [max(0.0, float(score_getter(row))) for row in rows]
    minimum = min(values)
    maximum = max(values)
    if math.isclose(minimum, maximum):
        if maximum <= 0:
            return {str(row["chunk_id"]): 0.0 for row in rows}
        return {str(row["chunk_id"]): 1.0 for row in rows}
    return {
        str(row["chunk_id"]): (max(0.0, float(score_getter(row))) - minimum) / (maximum - minimum)
        for row in rows
    }


def _chunk_markdown(content: str, target_size: int = 1200, overlap_chars: int = 180) -> List[Dict[str, str]]:
    lines = content.splitlines()
    sections: List[Dict[str, str]] = []
    current_heading_path: List[str] = []
    current_lines: List[str] = []

    def flush_section() -> None:
        if not current_lines:
            return
        section_text = "\n".join(current_lines).strip()
        if not section_text:
            return
        sections.append(
            {
                "heading_path": " > ".join(current_heading_path),
                "content": section_text,
            }
        )

    for line in lines:
        if line.startswith("#"):
            flush_section()
            level = len(line) - len(line.lstrip("#"))
            title = line[level:].strip()
            current_heading_path[:] = current_heading_path[: max(0, level - 1)]
            current_heading_path.append(title)
            current_lines[:] = [line]
        else:
            current_lines.append(line)
    flush_section()

    chunks: List[Dict[str, str]] = []
    for section in sections:
        text = section["content"]
        heading_path = section["heading_path"]
        if len(text) <= target_size:
            chunks.append({"heading_path": heading_path, "content": text})
            continue

        start = 0
        while start < len(text):
            end = min(len(text), start + target_size)
            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append({"heading_path": heading_path, "content": chunk_text})
            if end >= len(text):
                break
            start = max(start + 1, end - overlap_chars)
    return chunks


def _build_excerpt(content: str, keyword: str) -> str:
    lowered = content.lower()
    keyword_lower = keyword.lower()
    offset = lowered.find(keyword_lower)
    if offset < 0:
        return content[:280].strip()
    start = max(0, offset - 80)
    end = min(len(content), offset + 220)
    return content[start:end].strip()


def _extract_line_hits(content: str, keyword: str, limit: int = 5) -> List[Dict[str, Any]]:
    keyword_lower = keyword.lower()
    hits: List[Dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if keyword_lower in line.lower():
            hits.append({"line_number": line_number, "line": line.strip()})
        if len(hits) >= limit:
            break
    return hits


def _weighted_terms(chunk: Dict[str, Any]) -> tuple[Counter, float]:
    weighted_fields = [
        (chunk.get("title"), 3.0),
        (chunk.get("heading_path"), 2.4),
        (" ".join(chunk.get("keywords", [])), 2.2),
        (chunk.get("content"), 1.0),
    ]
    terms: Counter = Counter()
    for text, weight in weighted_fields:
        for token in _tokenize_text(str(text or "")):
            terms[token] += weight
    return terms, float(sum(terms.values()))


def _exact_match_bonus(keyword: str, chunk: Dict[str, Any]) -> float:
    lowered = keyword.lower()
    bonus = 0.0
    if lowered in str(chunk.get("title") or "").lower():
        bonus += 1.4
    if lowered in str(chunk.get("heading_path") or "").lower():
        bonus += 1.0
    keywords = " ".join(chunk.get("keywords", []))
    if lowered in keywords.lower():
        bonus += 0.9
    if lowered in str(chunk.get("content") or "").lower():
        bonus += 0.3
    return bonus


def _requests():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise KnowledgeBaseError("requests library is required for remote knowledge bases.") from exc
    return requests


def _remote_get(index: Dict[str, Any], endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    requests = _requests()
    base_url = str(index["index_url"]).rstrip("/")
    response = requests.get(
        f"{base_url}{endpoint}",
        params={key: value for key, value in (params or {}).items() if value not in (None, "")},
        timeout=REMOTE_TIMEOUT_SECONDS,
    )
    try:
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise KnowledgeBaseError(f"Remote knowledge base request failed: {response.status_code} {response.text[:200]}") from exc

    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        raise KnowledgeBaseError("Remote knowledge base returned non-JSON response.") from exc


def _build_local_doc_chunks(root_path: Path, design_docs: List[Path]) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for path in design_docs:
        content = path.read_text(encoding="utf-8", errors="replace")
        doc_title = path.stem
        section_chunks = _chunk_markdown(content)
        if not section_chunks:
            section_chunks = [{"heading_path": "", "content": content}]

        relative_path = str(path.relative_to(root_path))
        for index, section in enumerate(section_chunks, start=1):
            keywords = set(_tokenize_text(doc_title))
            keywords.update(_tokenize_text(section.get("heading_path", "")))
            chunk = {
                "chunk_id": f"{relative_path}::chunk::{index}",
                "path": str(path),
                "relative_path": relative_path,
                "title": doc_title,
                "heading_path": section.get("heading_path", ""),
                "content": section["content"],
                "matches": [],
                "keywords": sorted(keywords),
                "embedding_model": f"hashing-{EMBEDDING_DIM}d-v1",
            }
            chunk["embedding"] = _build_embedding(
                "\n".join(
                    value
                    for value in (
                        chunk["title"],
                        chunk["heading_path"],
                        chunk["content"],
                        " ".join(chunk["keywords"]),
                    )
                    if value
                )
            )
            chunks.append(chunk)
    return chunks


def load_knowledge_base(
    root_path: Optional[Path] = None,
    includes: Optional[List[str]] = None,
    *,
    kb_type: str = "local",
    index_url: Optional[str] = None,
) -> Dict[str, object]:
    if kb_type == "remote":
        if not index_url:
            raise KnowledgeBaseError("Remote knowledge base is missing index_url.")
        return {
            "type": "remote",
            "index_url": index_url.rstrip("/"),
            "includes": includes or [],
            "embedding_model": f"hashing-{EMBEDDING_DIM}d-v1",
        }

    if root_path is None:
        raise KnowledgeBaseError("Local knowledge base root_path is required.")
    if not root_path.exists():
        raise KnowledgeBaseError(f"Knowledge base path not found: {root_path}")

    include_paths = [root_path / name for name in (includes or [])]
    terminology_path = next((path for path in include_paths if "terminology" in path.name), root_path / "terminology.yaml")
    feature_tree_path = next((path for path in include_paths if "feature-tree" in path.name), root_path / "feature-tree.yaml")

    design_docs: List[Path] = []
    for path in root_path.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".markdown", ".yaml", ".yml", ".json", ".txt"}:
            continue
        if path.name in {terminology_path.name, feature_tree_path.name}:
            continue
        design_docs.append(path)

    return {
        "type": "local",
        "root_path": root_path,
        "terminology": _load_yaml_file(terminology_path) if terminology_path.exists() else {},
        "feature_tree": _load_yaml_file(feature_tree_path) if feature_tree_path.exists() else {},
        "design_docs": design_docs,
        "doc_chunks": _build_local_doc_chunks(root_path, design_docs),
        "embedding_model": f"hashing-{EMBEDDING_DIM}d-v1",
    }


def search_terms(index: Dict[str, object], keyword: str, *, limit: int = 10) -> List[Dict[str, object]]:
    if index.get("type") == "remote":
        payload = _remote_get(index, "/terms", {"q": keyword})
        matches = payload.get("matches") or []
        return matches[:limit]

    terminology = index.get("terminology") or {}
    entries = terminology.get("terms") if isinstance(terminology, dict) else terminology
    entries = entries if isinstance(entries, list) else []
    keyword_lower = keyword.lower()
    matches = []
    for entry in entries:
        name = str(entry.get("term") or entry.get("name") or "")
        definition = str(entry.get("definition") or entry.get("description") or "")
        if keyword_lower in name.lower() or keyword_lower in definition.lower():
            matches.append(entry)
        if len(matches) >= limit:
            break
    return matches


def get_feature_tree(index: Dict[str, object]) -> Dict[str, object]:
    if index.get("type") == "remote":
        payload = _remote_get(index, "/feature-tree")
        if isinstance(payload, dict):
            return payload
        raise KnowledgeBaseError("Remote knowledge base returned invalid feature tree payload.")
    return index.get("feature_tree") or {}


def _local_keyword_search(index: Dict[str, object], keyword: str, *, limit: int = 10) -> List[Dict[str, object]]:
    query_tokens = list(dict.fromkeys(_tokenize_text(keyword)))
    if not query_tokens:
        return []

    prepared: List[Dict[str, Any]] = []
    doc_freq: Counter = Counter()
    for chunk in index.get("doc_chunks", []):
        terms, doc_len = _weighted_terms(chunk)
        matched_terms = [token for token in query_tokens if terms.get(token, 0.0) > 0]
        exact_bonus = _exact_match_bonus(keyword, chunk)
        if not matched_terms and exact_bonus <= 0:
            continue
        prepared.append(
            {
                "chunk": chunk,
                "terms": terms,
                "doc_len": doc_len,
                "matched_terms": matched_terms,
                "keyword_hits": len(matched_terms),
                "exact_match_bonus": exact_bonus,
            }
        )
        for token in set(matched_terms):
            doc_freq[token] += 1

    if not prepared:
        return []

    average_length = sum(item["doc_len"] for item in prepared) / max(len(prepared), 1)
    ranked: List[Dict[str, object]] = []
    total_docs = len(prepared)

    for item in prepared:
        bm25_score = 0.0
        doc_len = item["doc_len"] or 1.0
        for token in query_tokens:
            tf = float(item["terms"].get(token, 0.0))
            if tf <= 0:
                continue
            df = max(int(doc_freq.get(token, 0)), 1)
            idf = math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))
            denom = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * (doc_len / average_length if average_length else 1.0))
            bm25_score += idf * ((tf * (BM25_K1 + 1.0)) / denom)

        keyword_score = item["keyword_hits"] / max(len(query_tokens), 1)
        total_score = bm25_score + keyword_score + item["exact_match_bonus"]
        chunk = dict(item["chunk"])
        chunk["matches"] = _extract_line_hits(chunk["content"], keyword)
        chunk["matched_terms"] = item["matched_terms"]
        chunk["excerpt"] = _build_excerpt(chunk["content"], keyword)
        chunk["scores"] = {
            "bm25_score": round(bm25_score, 6),
            "keyword_hits": item["keyword_hits"],
            "keyword_score": round(keyword_score, 6),
            "exact_match_bonus": round(item["exact_match_bonus"], 6),
            "vector_score": 0.0,
            "hybrid_score": round(total_score, 6),
        }
        chunk["keyword_total_score"] = total_score
        ranked.append(chunk)

    ranked.sort(
        key=lambda row: (
            row["keyword_total_score"],
            row["scores"]["bm25_score"],
            row["scores"]["keyword_hits"],
            row["path"],
            row["chunk_id"],
        ),
        reverse=True,
    )
    return ranked[:limit]


def _local_vector_search(index: Dict[str, object], keyword: str, *, top_k: int = 5) -> List[Dict[str, object]]:
    query_embedding = _build_embedding(keyword)
    if not any(abs(value) > 0 for value in query_embedding):
        return []

    ranked: List[Dict[str, object]] = []
    for chunk in index.get("doc_chunks", []):
        vector_score = _cosine_similarity(query_embedding, chunk.get("embedding") or [])
        if vector_score <= 0:
            continue
        payload = dict(chunk)
        payload["excerpt"] = _build_excerpt(payload["content"], keyword)
        payload["matches"] = _extract_line_hits(payload["content"], keyword)
        payload["matched_terms"] = []
        payload["scores"] = {
            "bm25_score": 0.0,
            "keyword_hits": 0,
            "keyword_score": 0.0,
            "exact_match_bonus": 0.0,
            "vector_score": round(vector_score, 6),
            "hybrid_score": round(vector_score, 6),
        }
        ranked.append(payload)

    ranked.sort(
        key=lambda row: (row["scores"]["vector_score"], row["path"], row["chunk_id"]),
        reverse=True,
    )
    return ranked[:top_k]


def _local_hybrid_retrieve(
    index: Dict[str, object],
    keyword: str,
    *,
    top_k: int = 5,
) -> List[Dict[str, object]]:
    keyword_ranked = _local_keyword_search(index, keyword, limit=max(top_k * 4, top_k))
    vector_ranked = _local_vector_search(index, keyword, top_k=max(top_k * 4, top_k))
    keyword_normalized = _normalize_scores(keyword_ranked, "keyword_total_score")
    vector_normalized = _normalize_chunk_scores(
        vector_ranked,
        lambda row: (row.get("scores") or {}).get("vector_score", 0.0),
    )

    merged: Dict[str, Dict[str, Any]] = {}
    for row in keyword_ranked + vector_ranked:
        chunk_id = str(row["chunk_id"])
        base = dict(merged.get(chunk_id, {}))
        base.update(row)
        keyword_norm = keyword_normalized.get(chunk_id, 0.0)
        vector_norm = vector_normalized.get(chunk_id, 0.0)
        hybrid_score = 0.72 * keyword_norm + 0.28 * vector_norm
        if chunk_id in keyword_normalized and chunk_id not in vector_normalized:
            hybrid_score = keyword_norm
        if chunk_id in vector_normalized and chunk_id not in keyword_normalized:
            hybrid_score = 0.5 * vector_norm
        scores = dict(base.get("scores") or {})
        scores["hybrid_score"] = round(hybrid_score, 6)
        base["scores"] = scores
        merged[chunk_id] = base

    ranked = sorted(
        merged.values(),
        key=lambda row: (
            row.get("scores", {}).get("hybrid_score", 0.0),
            row.get("scores", {}).get("bm25_score", 0.0),
            row.get("scores", {}).get("vector_score", 0.0),
            row["path"],
            row["chunk_id"],
        ),
        reverse=True,
    )
    return ranked[:top_k]


def search_design_docs(
    index: Dict[str, object],
    keyword: str,
    *,
    limit: int = 10,
    feature_id: Optional[str] = None,
) -> List[Dict[str, object]]:
    if index.get("type") == "remote":
        payload = _remote_get(index, "/search", {"q": keyword, "limit": limit, "feature_id": feature_id})
        matches = payload.get("matches") or []
        return matches[:limit]

    matches = _local_keyword_search(index, keyword, limit=limit)
    if not feature_id:
        return matches
    feature_lower = feature_id.lower()
    return [
        item
        for item in matches
        if feature_lower in json.dumps(item, ensure_ascii=False).lower()
    ][:limit]


def vector_search_design_docs(
    index: Dict[str, object],
    keyword: str,
    *,
    top_k: int = 5,
    feature_id: Optional[str] = None,
) -> List[Dict[str, object]]:
    if index.get("type") == "remote":
        payload = _remote_get(index, "/vector-search", {"q": keyword, "top_k": top_k, "feature_id": feature_id})
        matches = payload.get("matches") or []
        return matches[:top_k]

    matches = _local_vector_search(index, keyword, top_k=top_k)
    if not feature_id:
        return matches
    feature_lower = feature_id.lower()
    return [
        item
        for item in matches
        if feature_lower in json.dumps(item, ensure_ascii=False).lower()
    ][:top_k]


def retrieve_design_context(
    index: Dict[str, object],
    keyword: str,
    *,
    top_k: int = 5,
    feature_id: Optional[str] = None,
) -> List[Dict[str, object]]:
    if index.get("type") == "remote":
        payload = _remote_get(index, "/retrieve", {"q": keyword, "top_k": top_k, "feature_id": feature_id})
        matches = payload.get("hits") or []
        return matches[:top_k]

    matches = _local_hybrid_retrieve(index, keyword, top_k=top_k)
    if not feature_id:
        return matches
    feature_lower = feature_id.lower()
    return [
        item
        for item in matches
        if feature_lower in json.dumps(item, ensure_ascii=False).lower()
    ][:top_k]


def get_related_designs(
    index: Dict[str, object],
    feature_id: str,
    *,
    limit: int = 10,
) -> List[Dict[str, object]]:
    if index.get("type") == "remote":
        payload = _remote_get(index, "/retrieve", {"q": feature_id, "top_k": limit, "feature_id": feature_id})
        matches = payload.get("hits") or []
        return matches[:limit]
    return search_design_docs(index, feature_id, limit=limit, feature_id=feature_id)
