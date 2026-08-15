import argparse
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import sys

import httpx
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db.session import SessionLocal

RXNAV_BASE = "https://rxnav.nlm.nih.gov/REST"

DOSAGE_NOISE = {
    "inj",
    "injection",
    "tab",
    "tablet",
    "tablets",
    "cap",
    "capsule",
    "caps",
    "syrup",
    "drop",
    "drops",
    "iv",
    "im",
    "po",
    "od",
    "bd",
    "tid",
    "qid",
    "hs",
    "stat",
    "sr",
    "xr",
    "xl",
    "mr",
    "er",
    "cr",
    "dt",
    "ds",
    "forte",
    "plus",
    "neo",
    "vial",
    "amp",
    "ampoule",
    "ml",
    "mg",
    "mcg",
    "gm",
    "g",
}

KEEP_COMPONENTS_IF_BAD = {
    "supportive care",
    "-",
    "",
}


@dataclass
class RowDecision:
    id: int
    old_name: str
    old_components: str
    new_name: str
    new_components: str
    score: float
    changed_name: bool
    changed_components: bool
    reason: str


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def compact_space(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def tokenize_alpha(value: str) -> list[str]:
    return [t for t in re.findall(r"[a-z]+", (value or "").lower()) if t]


def clean_query_terms(name: str) -> list[str]:
    base = compact_space(name)
    if not base:
        return []

    terms: list[str] = []
    terms.append(base)

    no_brackets = re.sub(r"\([^)]*\)", " ", base)
    no_brackets = compact_space(no_brackets)
    if no_brackets and no_brackets not in terms:
        terms.append(no_brackets)

    tokens = re.split(r"[^A-Za-z0-9]+", no_brackets)
    cleaned_tokens: list[str] = []
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        low = t.lower()
        if low in DOSAGE_NOISE:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", low):
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?(?:mg|mcg|g|gm|ml)", low):
            continue
        cleaned_tokens.append(t)

    cleaned = compact_space(" ".join(cleaned_tokens))
    if cleaned and cleaned not in terms:
        terms.append(cleaned)

    if "/" in cleaned:
        parts = [compact_space(p) for p in cleaned.split("/") if compact_space(p)]
        terms.extend([p for p in parts if p not in terms])

    if "-" in cleaned:
        parts = [compact_space(p) for p in cleaned.split("-") if compact_space(p)]
        terms.extend([p for p in parts if p not in terms])

    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        k = normalize_key(term)
        if not k or k in seen:
            continue
        seen.add(k)
        unique.append(term)
    return unique[:5]


def similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return float(SequenceMatcher(None, normalize_key(a), normalize_key(b)).ratio())


def score_candidate(input_name: str, query_term: str, candidate_name: str) -> float:
    s1 = similarity(input_name, candidate_name)
    s2 = similarity(query_term, candidate_name)

    input_tokens = set(tokenize_alpha(input_name))
    cand_tokens = set(tokenize_alpha(candidate_name))
    overlap = 0.0
    if input_tokens:
        overlap = len(input_tokens & cand_tokens) / max(1, len(input_tokens))

    return (0.55 * s1) + (0.30 * s2) + (0.15 * overlap)


def extract_ingredients_from_related(related_payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    rg = (related_payload or {}).get("relatedGroup") or {}
    for group in rg.get("conceptGroup") or []:
        tty = str(group.get("tty") or "").upper()
        if tty not in {"IN", "MIN", "PIN"}:
            continue
        for cp in group.get("conceptProperties") or []:
            name = compact_space(str(cp.get("name") or ""))
            if not name:
                continue
            if name.lower() not in {x.lower() for x in out}:
                out.append(name)
    return out


def extract_candidates_from_drugs(drugs_payload: dict[str, Any]) -> list[dict[str, str]]:
    cands: list[dict[str, str]] = []
    dg = (drugs_payload or {}).get("drugGroup") or {}
    for group in dg.get("conceptGroup") or []:
        for cp in group.get("conceptProperties") or []:
            rxcui = str(cp.get("rxcui") or "").strip()
            name = compact_space(str(cp.get("name") or ""))
            if rxcui and name:
                cands.append({"rxcui": rxcui, "name": name})
    return cands


def extract_candidates_from_approx(approx_payload: dict[str, Any], client: httpx.Client, cache_rxcui_name: dict[str, str]) -> list[dict[str, str]]:
    cands: list[dict[str, str]] = []
    group = (approx_payload or {}).get("approximateGroup") or {}
    for cand in group.get("candidate") or []:
        rxcui = str(cand.get("rxcui") or "").strip()
        if not rxcui:
            continue
        name = cache_rxcui_name.get(rxcui, "")
        if not name:
            try:
                props = client.get(f"{RXNAV_BASE}/rxcui/{rxcui}/properties.json", timeout=12.0)
                if props.status_code == 200:
                    pobj = props.json().get("properties") or {}
                    name = compact_space(str(pobj.get("name") or ""))
            except Exception:
                name = ""
            if name:
                cache_rxcui_name[rxcui] = name
        if name:
            cands.append({"rxcui": rxcui, "name": name})
    return cands


def best_rxnav_match(name: str, client: httpx.Client, query_cache: dict[str, dict[str, Any]], rxcui_name_cache: dict[str, str], rel_cache: dict[str, list[str]]) -> dict[str, Any] | None:
    terms = clean_query_terms(name)
    if not terms:
        return None

    best: dict[str, Any] | None = None
    seen = set()

    for term in terms:
        term_key = normalize_key(term)
        if not term_key:
            continue

        if term_key in query_cache:
            candidates = query_cache[term_key]
        else:
            candidates: dict[str, Any] = {"items": []}
            items: list[dict[str, str]] = []
            try:
                approx_resp = client.get(f"{RXNAV_BASE}/approximateTerm.json", params={"term": term, "maxEntries": 6}, timeout=12.0)
                if approx_resp.status_code == 200:
                    items.extend(extract_candidates_from_approx(approx_resp.json(), client, rxcui_name_cache))
            except Exception:
                pass
            try:
                drugs_resp = client.get(f"{RXNAV_BASE}/drugs.json", params={"name": term}, timeout=12.0)
                if drugs_resp.status_code == 200:
                    items.extend(extract_candidates_from_drugs(drugs_resp.json()))
            except Exception:
                pass

            uniq: list[dict[str, str]] = []
            seen_local: set[str] = set()
            for it in items:
                k = f"{it.get('rxcui','')}::{normalize_key(it.get('name',''))}"
                if k in seen_local:
                    continue
                seen_local.add(k)
                uniq.append(it)
            candidates["items"] = uniq
            query_cache[term_key] = candidates

        for cand in candidates.get("items") or []:
            rxcui = str(cand.get("rxcui") or "")
            cname = str(cand.get("name") or "")
            if not rxcui or not cname:
                continue
            k = (rxcui, normalize_key(cname))
            if k in seen:
                continue
            seen.add(k)

            score = score_candidate(name, term, cname)
            if (best is None) or (score > best["score"]):
                best = {"rxcui": rxcui, "name": cname, "score": score}

    if not best:
        return None

    rxcui = best["rxcui"]
    if rxcui in rel_cache:
        ingredients = rel_cache[rxcui]
    else:
        ingredients = []
        try:
            rel_resp = client.get(f"{RXNAV_BASE}/rxcui/{rxcui}/related.json", params={"tty": "IN+MIN+PIN"}, timeout=12.0)
            if rel_resp.status_code == 200:
                ingredients = extract_ingredients_from_related(rel_resp.json())
        except Exception:
            ingredients = []
        rel_cache[rxcui] = ingredients

    best["ingredients"] = ingredients
    return best


def choose_new_name(old_name: str, match_name: str, score: float) -> str:
    old_clean = compact_space(old_name)
    cand_clean = compact_space(match_name)
    if not old_clean or not cand_clean:
        return old_clean or cand_clean

    old_key = normalize_key(old_clean)
    cand_key = normalize_key(cand_clean)

    # Strong match: accept canonical RxNorm name.
    if score >= 0.92:
        return cand_clean

    # If old looks noisy and candidate is clearly better.
    noisy = bool(re.search(r"\b(inj|injection|tab|tablet|capsule|syrup|iv|im|od|bd|tid|qid|vial|amp)\b", old_clean, re.I))
    if noisy and score >= 0.86:
        return cand_clean

    # If almost same key (likely typo/plural variation), normalize.
    if old_key and cand_key and (old_key in cand_key or cand_key in old_key) and score >= 0.84:
        return cand_clean

    return old_clean


def choose_new_components(old_components: str, ingredients: list[str], fallback_name: str) -> str:
    old_clean = compact_space(old_components)
    if ingredients:
        return " + ".join(ingredients)
    if old_clean.lower() not in KEEP_COMPONENTS_IF_BAD:
        return old_clean
    return compact_space(fallback_name)


def dump_backup(db, backup_table: str) -> None:
    db.execute(text(f"DROP TABLE IF EXISTS {backup_table}"))
    db.execute(text(f"CREATE TABLE {backup_table} AS SELECT * FROM medicine_component_lookup"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Internet-backed correction for medicine_component_lookup")
    parser.add_argument("--apply", action="store_true", help="Apply updates to DB")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit rows")
    parser.add_argument("--min-score", type=float, default=0.82, help="Minimum candidate score to update components")
    parser.add_argument("--sleep-ms", type=int, default=40, help="Sleep between rows in milliseconds")
    parser.add_argument("--report", type=str, default="", help="Path to save JSON report")
    args = parser.parse_args()

    with SessionLocal() as db:
        rows_sql = """
            SELECT id, medicine_key, medicine_name, components, subclassification, is_high_end_antibiotic, source
            FROM medicine_component_lookup
            ORDER BY id ASC
        """
        params: dict[str, Any] = {}
        if args.limit and args.limit > 0:
            rows_sql += " LIMIT :limit"
            params["limit"] = int(args.limit)

        rows = db.execute(text(rows_sql), params).mappings().all()

        key_owner: dict[str, int] = {}
        for r in rows:
            key_owner[str(r.get("medicine_key") or "")] = int(r.get("id"))

        decisions: list[RowDecision] = []
        query_cache: dict[str, dict[str, Any]] = {}
        rxcui_name_cache: dict[str, str] = {}
        rel_cache: dict[str, list[str]] = {}

        started = time.time()
        with httpx.Client(headers={"User-Agent": "verifai-medicine-rectifier/1.0"}, timeout=15.0) as client:
            for idx, row in enumerate(rows, start=1):
                rid = int(row.get("id"))
                old_name = compact_space(str(row.get("medicine_name") or ""))
                old_comp = compact_space(str(row.get("components") or ""))

                match = best_rxnav_match(old_name, client, query_cache, rxcui_name_cache, rel_cache)
                if not match:
                    decisions.append(RowDecision(rid, old_name, old_comp, old_name, old_comp, 0.0, False, False, "no_match"))
                    if args.sleep_ms > 0:
                        time.sleep(args.sleep_ms / 1000.0)
                    continue

                score = float(match.get("score") or 0.0)
                if score < float(args.min_score):
                    decisions.append(RowDecision(rid, old_name, old_comp, old_name, old_comp, score, False, False, "low_confidence"))
                    if args.sleep_ms > 0:
                        time.sleep(args.sleep_ms / 1000.0)
                    continue

                cand_name = compact_space(str(match.get("name") or ""))
                ing = match.get("ingredients") or []
                new_name = choose_new_name(old_name, cand_name, score)
                new_comp = choose_new_components(old_comp, ing, cand_name)

                changed_name = normalize_key(new_name) != normalize_key(old_name)
                changed_comp = compact_space(new_comp).lower() != old_comp.lower()

                # Avoid medicine_key uniqueness conflicts.
                if changed_name:
                    new_key = normalize_key(new_name)
                    owner = key_owner.get(new_key)
                    if owner is not None and owner != rid:
                        new_name = old_name
                        changed_name = False

                reason = "matched"
                if changed_name and changed_comp:
                    reason = "name_and_components_updated"
                elif changed_name:
                    reason = "name_updated"
                elif changed_comp:
                    reason = "components_updated"
                else:
                    reason = "already_good"

                decisions.append(RowDecision(rid, old_name, old_comp, new_name, new_comp, score, changed_name, changed_comp, reason))

                if idx % 200 == 0:
                    elapsed = round(time.time() - started, 1)
                    print(f"processed {idx}/{len(rows)} elapsed={elapsed}s")

                if args.sleep_ms > 0:
                    time.sleep(args.sleep_ms / 1000.0)

        to_update = [d for d in decisions if d.changed_name or d.changed_components]

        backup_table = ""
        if args.apply:
            backup_table = f"medicine_component_lookup_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            dump_backup(db, backup_table)

            for d in to_update:
                payload = {
                    "id": d.id,
                    "medicine_name": d.new_name,
                    "medicine_key": normalize_key(d.new_name),
                    "components": d.new_components,
                    "source": "internet_rxnav_corrected",
                }
                db.execute(
                    text(
                        """
                        UPDATE medicine_component_lookup
                        SET medicine_name = :medicine_name,
                            medicine_key = :medicine_key,
                            components = :components,
                            source = :source,
                            last_checked_at = NOW(),
                            updated_at = NOW()
                        WHERE id = :id
                        """
                    ),
                    payload,
                )
            db.commit()

        stats = defaultdict(int)
        for d in decisions:
            stats[d.reason] += 1

        result = {
            "total_rows": len(decisions),
            "candidate_updates": len(to_update),
            "name_updates": sum(1 for d in to_update if d.changed_name),
            "component_updates": sum(1 for d in to_update if d.changed_components),
            "backup_table": backup_table,
            "mode": "apply" if args.apply else "dry_run",
            "stats": dict(stats),
            "sample_updates": [
                {
                    "id": d.id,
                    "old_name": d.old_name,
                    "new_name": d.new_name,
                    "old_components": d.old_components,
                    "new_components": d.new_components,
                    "score": round(d.score, 4),
                    "reason": d.reason,
                }
                for d in to_update[:120]
            ],
        }

        report_path = args.report.strip()
        if not report_path:
            report_dir = Path("artifacts")
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = str(report_dir / f"medicine_rectify_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{result['mode']}.json")

        Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: result[k] for k in ["total_rows", "candidate_updates", "name_updates", "component_updates", "backup_table", "mode", "report_path"] if k in result} | {"report_path": report_path}))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
