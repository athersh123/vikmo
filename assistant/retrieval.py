"""Hybrid retrieval engine for catalog spare parts.
Combines BM25 lexical scoring, character trigram fuzzy matching, and Reciprocal Rank Fusion (RRF).
Keeps product descriptions strictly out of the retrieval text to reduce prompt injection surface.
"""
import math
import os
import re
from collections import Counter
from django.db.models import Q
from catalog.models import Product

# Automotive synonyms mapping
SYNONYMS = {
    "brake pad": "brake pad set",
    "brake pads": "brake pad set",
    "brakepads": "brake pad set",
    "brake shoe": "brake shoe set",
    "brake shoes": "brake shoe set",
    "chain kit": "chain sprocket kit",
    "chain": "chain sprocket kit",
    "sprocket": "chain sprocket kit",
    "air filter": "air filter",
    "oil filter": "oil filter",
    "spark plug": "spark plug",
    "clutch plate": "clutch plate set",
    "clutch cable": "clutch cable",
    "throttle cable": "accelerator cable",
    "mirror": "side mirror set",
    "mirrors": "side mirror set",
    "indicators": "led indicator set",
    "indicator": "led indicator set",
}

# Vehicle model normalization regexes
MODEL_NORMALIZATIONS = [
    (re.compile(r"\bpulsar\s*150\b", re.I), "Pulsar 150"),
    (re.compile(r"\bpulsar\s*200\b", re.I), "Pulsar 200"),
    (re.compile(r"\bdominar\s*400\b", re.I), "Dominar 400"),
    (re.compile(r"\bduke\s*200\b", re.I), "Duke 200"),
    (re.compile(r"\bduke\s*390\b", re.I), "Duke 390"),
    (re.compile(r"\bapache\s*160\b", re.I), "Apache RTR 160"),
    (re.compile(r"\bapache\s*200\b", re.I), "Apache RTR 200"),
    (re.compile(r"\bgixxer\s*sf\b", re.I), "Gixxer SF"),
    (re.compile(r"\bgixxer\b", re.I), "Gixxer"),
    (re.compile(r"\bactiva\s*6g\b", re.I), "Activa 6G"),
    (re.compile(r"\bactiva\b", re.I), "Activa"),
]


def normalize_query_text(text: str) -> str:
    """Normalize synonyms and model spellings in query string."""
    if not text:
        return ""
    normalized = text.lower()
    for pattern, target in MODEL_NORMALIZATIONS:
        normalized = pattern.sub(target.lower(), normalized)
    for src, target in SYNONYMS.items():
        if src in normalized:
            normalized = normalized.replace(src, target)
    return normalized


def tokenize(text: str) -> list[str]:
    """Extract lowercased alphanumeric tokens."""
    return re.findall(r"\b[a-zA-Z0-9]+\b", text.lower())


def get_trigrams(text: str) -> set[str]:
    """Extract character 3-grams for typo-tolerant fuzzy matching."""
    clean = re.sub(r"\s+", " ", text.lower().strip())
    if len(clean) < 3:
        return {clean}
    return {clean[i : i + 3] for i in range(len(clean) - 2)}


def trigram_similarity(query_trigrams: set[str], doc_trigrams: set[str]) -> float:
    """Compute Jaccard similarity over character trigrams."""
    if not query_trigrams or not doc_trigrams:
        return 0.0
    intersection = len(query_trigrams & doc_trigrams)
    union = len(query_trigrams | doc_trigrams)
    return intersection / union if union > 0 else 0.0


def compute_bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    avg_dl: float = 8.0,
    k1: float = 1.2,
    b: float = 0.75,
) -> float:
    """Compute BM25 term frequency score for candidate document."""
    if not query_tokens or not doc_tokens:
        return 0.0

    doc_len = len(doc_tokens)
    doc_freq = Counter(doc_tokens)
    score = 0.0

    for token in query_tokens:
        if token in doc_freq:
            tf = doc_freq[token]
            num = tf * (k1 + 1)
            denom = tf + k1 * (1 - b + b * (doc_len / avg_dl))
            score += num / denom

    return score


class CatalogRetriever:
    """
    Hybrid retriever over the 600 catalogue products.
    Uses lexical BM25 + character trigram matching + Reciprocal Rank Fusion (RRF).
    Filters explicitly by vehicle and category.
    Excludes raw product descriptions to keep prompt injection surface minimal.
    """

    @classmethod
    def search(
        cls,
        query: str,
        vehicle: str = None,
        category: str = None,
        limit: int = 5,
    ) -> list[dict]:
        clean_query = normalize_query_text(query or "")
        clean_vehicle = normalize_query_text(vehicle or "") if vehicle else None
        clean_category = category.strip() if category else None

        q_filter = Q()
        query_tokens = tokenize(clean_query)
        query_trigrams = get_trigrams(clean_query)

        # Build candidate filter using tokens
        if query_tokens:
            for token in query_tokens:
                q_filter |= (
                    Q(name__icontains=token)
                    | Q(brand__icontains=token)
                    | Q(category__icontains=token)
                    | Q(vehicle_fitment__icontains=token)
                    | Q(sku__icontains=token)
                )

        if clean_vehicle:
            vehicle_tokens = tokenize(clean_vehicle)
            for v_tok in vehicle_tokens:
                q_filter &= (
                    Q(vehicle_fitment__icontains=v_tok)
                    | Q(vehicle_fitment__iexact="Universal")
                )

        if clean_category:
            q_filter &= Q(category__iexact=clean_category)

        # Retrieve candidate pool
        candidates = list(
            Product.objects.filter(q_filter).select_related("inventory")[:100]
        )

        if not candidates:
            # Fallback relaxation if strict query returned zero candidates
            fallback_filter = Q()
            if query_tokens:
                for token in query_tokens[:2]:
                    fallback_filter |= (
                        Q(name__icontains=token) | Q(category__icontains=token)
                    )
            if clean_vehicle:
                fallback_filter &= (
                    Q(vehicle_fitment__icontains=clean_vehicle)
                    | Q(vehicle_fitment__iexact="Universal")
                )
            candidates = list(
                Product.objects.filter(fallback_filter).select_related("inventory")[:50]
            )

        if not candidates:
            return []

        # 1. Rank by BM25
        bm25_scored = []
        # 2. Rank by Trigram fuzzy similarity
        trigram_scored = []

        for p in candidates:
            # Document representation strictly excludes description
            doc_text = f"{p.sku} {p.name} {p.brand} {p.category} {p.vehicle_fitment}"
            doc_tokens = tokenize(doc_text)
            doc_trigrams = get_trigrams(doc_text)

            bm25 = compute_bm25_score(query_tokens, doc_tokens)
            tri = trigram_similarity(query_trigrams, doc_trigrams)

            # Fitment bonus
            fitment_bonus = 0.0
            if clean_vehicle and clean_vehicle in p.vehicle_fitment.lower():
                fitment_bonus += 2.0
            elif p.vehicle_fitment.lower() == "universal":
                fitment_bonus += 0.5

            # Exact SKU bonus
            if clean_query.strip().upper() == p.sku.upper():
                fitment_bonus += 10.0

            bm25_scored.append((bm25 + fitment_bonus, p))
            trigram_scored.append((tri + fitment_bonus, p))

        # Sort independently
        bm25_scored.sort(key=lambda x: x[0], reverse=True)
        trigram_scored.sort(key=lambda x: x[0], reverse=True)

        # Compute Reciprocal Rank Fusion (RRF)
        # RRF_score(d) = sum(1 / (k + rank(d)))
        k_rrf = 60
        rrf_scores = {}

        for rank, (_, p) in enumerate(bm25_scored, start=1):
            rrf_scores[p.id] = rrf_scores.get(p.id, 0.0) + (1.0 / (k_rrf + rank))

        for rank, (_, p) in enumerate(trigram_scored, start=1):
            rrf_scores[p.id] = rrf_scores.get(p.id, 0.0) + (1.0 / (k_rrf + rank))

        # Final ranking
        id_to_product = {p.id: p for p in candidates}
        fused = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        top_products = [id_to_product[pid] for pid, _ in fused[:limit]]

        results = []
        for p in top_products:
            stock = p.inventory.quantity if hasattr(p, "inventory") else 0
            results.append(
                {
                    "sku": p.sku,
                    "name": p.name,
                    "brand": p.brand,
                    "fitment": p.vehicle_fitment,
                    "vehicle_fitment": p.vehicle_fitment,
                    "category": p.category,
                    "price": f"{p.price:.2f}",
                    "stock": stock,
                }
            )

        return results
