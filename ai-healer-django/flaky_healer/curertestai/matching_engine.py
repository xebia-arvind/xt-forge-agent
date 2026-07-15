from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import pandas as pd
import re, json, os

# ---------------- Try imports ----------------
_USE_SBERT = False
_USE_FAISS = False

try:
    from sentence_transformers import SentenceTransformer
    _USE_SBERT = True
except:
    _USE_SBERT = False

try:
    import faiss
    _USE_FAISS = True
except:
    _USE_FAISS = False

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.neighbors import NearestNeighbors


# ---------- Helper utilities ----------
def normalize_text(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()

def safe_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, (list, tuple)):
        return " ".join([safe_str(i) for i in x])
    if isinstance(x, dict):
        return " ".join([f"{k} {v}" for k, v in x.items() if v])
    return str(x)

def overlap_ratio(a: str, b: str) -> float:
    a_tokens = set(normalize_text(a).lower().split())
    b_tokens = set(normalize_text(b).lower().split())
    if not a_tokens or not b_tokens:
        return 0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


# ---------- Flatten DOM into semantic text ----------
def flatten_element(el: Dict[str, Any]) -> str:
    parts = [f"tag: {el.get('tag','')}"]
    text = el.get("text") or el.get("accessible_name") or ""
    parts.append(f"text: {normalize_text(text)[:400]}")

    attrs = el.get("attributes", {})
    for k in ["id", "class", "aria-label", "href", "role", "type"]:
        if attrs.get(k):
            parts.append(f"{k}: {normalize_text(str(attrs[k]))}")

    parts.append(f"selector: {normalize_text(el.get('selector',''))}")
    parts.append(f"xpath: {normalize_text(el.get('xpath',''))}")

    ctx = el.get("context", {})
    parts.append(f"parent: {normalize_text(safe_str(ctx.get('parent')))}")
    parts.append(f"parent_class: {normalize_text(safe_str(ctx.get('parent_class')))}")

    return " ||| ".join(parts)


# ---------- Selector generation ----------
def escape_attr(val: str) -> str:
    return val.replace('"', '\\"')

def build_css_from_element(el: Dict[str, Any]) -> str:
    attrs = el.get("attributes", {})
    tag = el.get("tag", "a")

    # ID → strongest selector
    if attrs.get("id"):
        return f"#{attrs['id']}"

    # aria-label
    if attrs.get("aria-label"):
        return f'{tag}[aria-label="{escape_attr(attrs["aria-label"])}"]'

    # href
    if attrs.get("href"):
        href = attrs["href"]
        if len(href) > 40:
            tail = href.rstrip("/").split("/")[-1]
            return f'{tag}[href$="{escape_attr(tail)}"]'
        return f'{tag}[href="{escape_attr(href)}"]'

    # class token
    cls = attrs.get("class") or ""
    if cls:
        parts = cls.split()
        return f"{tag}.{parts[0]}"

    # role
    if el.get("role"):
        return f'{tag}[role="{escape_attr(el["role"])}"]'

    # fallback
    if el.get("xpath"):
        return f'XPATH: {el["xpath"]}'

    return tag


# ---------- Matching Engine ----------
class MatchingEngine:
    def __init__(self, elements: List[Dict[str, Any]], use_sbert=True, use_faiss=True):
        self.elements = elements
        self.use_sbert = use_sbert and _USE_SBERT
        self.use_faiss = use_faiss and _USE_FAISS

        # Build corpus
        self.corpus_texts = [flatten_element(el) for el in elements]
        self.ready = True

        if not self.corpus_texts:
            # No retrievable DOM content.
            self.ready = False
            self.embeddings = np.zeros((0, 0))
            self.dim = 0
            self.faiss_ok = False
            return

        # Embeddings
        if self.use_sbert:
            self.model = SentenceTransformer("all-MiniLM-L6-v2")
            self.embeddings = self.model.encode(
                self.corpus_texts, show_progress_bar=False, convert_to_numpy=True
            )
        else:
            try:
                self.vectorizer = TfidfVectorizer(ngram_range=(1, 2))
                self.embeddings = self.vectorizer.fit_transform(self.corpus_texts).toarray()
            except ValueError:
                # Fallback when vocabulary cannot be built from sparse/noisy corpus.
                self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5))
                try:
                    self.embeddings = self.vectorizer.fit_transform(self.corpus_texts).toarray()
                except ValueError:
                    self.ready = False
                    self.embeddings = np.zeros((0, 0))
                    self.dim = 0
                    self.faiss_ok = False
                    return

        self.dim = self.embeddings.shape[1]
        if self.dim == 0:
            self.ready = False
            self.faiss_ok = False
            return

        # Retrieval index
        if self.use_faiss:
            try:
                self.index = faiss.IndexFlatIP(self.dim)
                faiss.normalize_L2(self.embeddings)
                self.index.add(np.array(self.embeddings).astype("float32"))
                self.faiss_ok = True
            except:
                self.faiss_ok = False
                self.nn = NearestNeighbors(metric="cosine").fit(self.embeddings)
        else:
            self.faiss_ok = False
            self.nn = NearestNeighbors(metric="cosine").fit(self.embeddings)

    def embed_query(self, selector: str, semantic: str) -> np.ndarray:
        q = f"selector: {selector} ||| semantic: {semantic}"
        if self.use_sbert:
            vec = self.model.encode([q], convert_to_numpy=True)[0]
            if self.use_faiss:
                faiss.normalize_L2(vec.reshape(1, -1))
            return vec
        else:
            return self.vectorizer.transform([q]).toarray()[0]

    def retrieve(self, qvec, top_k):
        if self.faiss_ok:
            D, I = self.index.search(qvec.reshape(1, -1).astype("float32"), top_k)
            return I[0], D[0]
        else:
            dist, idxs = self.nn.kneighbors([qvec], n_neighbors=top_k)
            sim = 1 - dist[0]
            return idxs[0], sim

    def attribute_score(self, el, selector, semantic):
        attrs = el.get("attributes", {})
        score = 0

        # id match
        found = re.search(r"#([\w\-]+)", selector or "")
        if found and attrs.get("id") == found.group(1):
            score += 0.6

        # aria-label meaning
        if attrs.get("aria-label"):
            score += 0.3 * overlap_ratio(attrs["aria-label"], semantic)

        # href overlap
        if attrs.get("href"):
            score += 0.2 * overlap_ratio(attrs["href"], semantic)

        # class overlap
        cls_tokens = set((attrs.get("class") or "").split())
        sel_cls = set(re.findall(r"\.([\w\-]+)", selector or ""))
        if cls_tokens & sel_cls:
            score += 0.2

        return min(1, score)

    def rank(self, selector: str, semantic: str, top_k=10):
        if not self.ready or not self.elements:
            return []

        qvec = self.embed_query(selector, semantic)
        idxs, sims = self.retrieve(qvec, top_k)

        results = []
        for i, idx in enumerate(idxs):
            el = self.elements[idx]
            base = float(sims[i])
            attr = self.attribute_score(el, selector, semantic)
            final = 0.7 * base + 0.3 * attr

            results.append(
                dict(
                    index=idx,
                    score=final,
                    base=base,
                    attr=attr,
                    element=el,
                    suggested=build_css_from_element(el),
                )
            )

        results.sort(key=lambda x: -x["score"])
        return results
