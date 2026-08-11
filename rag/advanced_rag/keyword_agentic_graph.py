#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Sub-question-driven, doc-first iterative agentic-search graph (LangGraph).

Flow (each iteration works over a set of sub-questions):

    keywords → retrieve_docs → retrieve_chunks → summarize
        → think ─(enough | max_iter | no new sub-qs)→ answer
                  └────────(else: think's next sub-questions)────────→ retrieve_docs

Design rules:
* Every LLM node builds a FRESH (system, user) prompt with only the data it
  needs — no chat history is threaded, so contexts never accumulate. Only *data*
  (sub-questions, candidate docs, chunk pool, per-sub-question summaries)
  persists in the graph state.
* ``keywords`` decomposes the question into sub-questions, each with its own
  keywords + synonyms. Everything downstream is paired to a sub-question.
* ``retrieve_docs`` finds candidate documents per sub-question (via ``doc_aggs``).
* ``retrieve_chunks`` pulls chunks with the sub-question's keywords, scoped to
  that sub-question's candidate documents, and — gated by ``enable_snippets`` —
  narrows them to keyword sentences inline (so a sub-q's chunks already hold the
  snippet content).
* ``summarize`` summarizes each sub-question over its own chunks.
* ``think`` judges the accumulated summaries against the ORIGINAL question and,
  if short, emits the missing info as the next sub-questions + keywords.
* The accumulated chunk pool feeds the final answer through ``kb_prompt`` so the
  brief answer still carries ``[ID:n]`` citations.
"""

from __future__ import annotations

import asyncio
import calendar
from collections import defaultdict
import json
import logging
import re
from typing import Any, TypedDict

import json_repair
from langgraph.graph import END, START, StateGraph

from rag.prompts.generator import citation_prompt, form_message, kb_prompt, message_fit_in
from rag.advanced_rag.harness.prompts.report_prompt import FINAL_ANSWER_SYSTEM
from rag.advanced_rag.harness.tools.search import _narrow_by_keywords, _normalize

_LOG = logging.getLogger(__name__)

# Tunable caps.
_MAX_SUBQUESTIONS = 5  # sub-questions kept per iteration
_DOC_TOP_N = 30  # chunk hits fetched per sub-q just to aggregate candidate docs
_DOCS_PER_SUBQ = 6  # candidate docs kept per sub-question
_CHUNKS_PER_DOC = 6  # chunks pulled from each relevant doc
_SUMMARY_SNIPPETS = 12  # snippets fed to each sub-question's summariser


# ── Prompts (each used for a single, self-contained LLM call) ──

_DATE_NORMALIZATION_SYSTEM = """When a question contains dates, expand each unambiguous date into retrieval-friendly synonyms.
Use both natural-language and numeric forms so searches can match whichever variant appears in the sources.
For a date like "August 24, 2021" or "2021-08-24", include forms such as:
2021-08-24, 20210824, 08/24/2021, August 24, 2021, Aug 24, 2021.
Do not guess a day/month order for ambiguous numeric dates."""

_KEYWORDS_SYSTEM = (
    """You break a question into the sub-questions needed to answer it.
For EACH sub-question, list the best search keywords AND their closest / most-likely synonyms.
Each sub-question's "keywords" list MUST contain AT MOST 3 terms — choose the 3 most
distinctive/relevant terms (this cap includes any date variants: pick the 3 forms most
likely to appear in the sources).
A standalone number or serial of digits (an ID, code, year, or numeric date form) is a
strong keyword on its own: keep it as its OWN separate term. Never glue a number together
with other words into a single keyword term (e.g. use "1344259" as one term, not
"Patent 1344259").
Split the sub-questions into two groups:

1. "subquestions" — answerable NOW from the original question alone. Rewrite references
   such as "that city", "the person", "it", "the former" as the full entity name or a
   precise identifying description, and repeat any ranking/date/location condition so the
   sub-question is self-contained. Prefer this whenever the condition can be *described*
   without knowing the answer.
   e.g. "Which US city was 50th most populous by 2023 estimate population?" — answerable now.

2. "pending" — a hop that genuinely needs the ANSWER of a group-1 sub-question (not just a
   description) before it can be searched. Write it as a template with the literal
   placeholder <ANSWER>, and set "depends_on" to the 0-based INDEX (into "subquestions") of
   the sub-question whose answer fills the placeholder.
   e.g. {"question_template": "When was <ANSWER> founded?", "keywords": ["founded"],
         "depends_on": 0}  — resolved after subquestion 0 returns the city name.

Only use a pending hop when the description cannot stand in for the value. Independent
facts (e.g. "When did Frank Fox receive UK Patent 1344259?") stay in group 1.
"""
    + _DATE_NORMALIZATION_SYSTEM
    + """
Output ONLY JSON, no prose, no code fences:
{"subquestions": [{"question": "<sub-question>", "keywords": ["term or synonym", ...]}, ...],
 "pending": [{"question_template": "... <ANSWER> ...", "keywords": ["..."], "depends_on": 0}, ...]}"""
)

_SUMMARIZE_SYSTEM = """You are given a sub-question and snippets retrieved for it.
Summarize ONLY the facts in the snippets that help answer the sub-question; be concise and
factual, do not speculate or add outside knowledge.
Also extract "answer": the SINGLE concrete value this sub-question resolves to (an entity
name, number, date, ...), or null if the snippets do not resolve it. This value may be
substituted into a later dependent sub-question, so keep it short and exact.
Output ONLY JSON, no prose, no code fences:
{"summary": "<concise factual summary, or 'No useful evidence.'>", "answer": "<value or null>"}"""

_THINK_SYSTEM = """You are given the ORIGINAL question and the evidence (with the concrete answer)
summarized for each sub-question so far. Sub-questions whose answer depends on a still-unknown
value are resolved automatically and are NOT your job.
Decide whether the collected evidence is ENOUGH to answer the ORIGINAL question directly.
If it is NOT enough, state what is missing and, ONLY for genuinely new gaps, propose:
- "next_subquestions": self-contained sub-questions answerable from the ORIGINAL question and the
  facts already found (repeat full entity names / conditions; no "that city", "it", "the former"), and
- "next_pending": deferred hops that need a group-1 answer first — a "<ANSWER>" template plus
  "depends_on" = the 0-based INDEX into "next_subquestions".
When any next sub-question contains a date, expand it using the same date-normalization rules as the keyword step.
Output ONLY JSON, no prose, no code fences:
{"sufficient": true/false, "missing": "<what is still missing, or empty>",
 "next_subquestions": [{"question": "<sub-question>", "keywords": ["...", ...]}, ...],
 "next_pending": [{"question_template": "... <ANSWER> ...", "keywords": ["..."], "depends_on": 0}, ...]}"""


class KwAgenticState(TypedDict, total=False):
    question: str  # original Q (never changes)
    subquestions: list  # THIS iteration's answerable sub-questions (enriched in place)
    pending: list  # deferred hops: {id, question_template ("<ANSWER>"), keywords, depends_on: subq_id}
    resolved: dict  # subq_id -> the concrete answer that sub-question resolved to
    chunks: list  # accumulated citation pool (union, dedup by id)
    evidences: list  # accumulated: [{iteration, subq_id, subq, summary}]
    iteration: int
    max_iterations: int
    sufficient: bool
    final_answer: str


def _snip(value: Any, limit: int = 200) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        s = str(value)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + f"...(+{len(s) - limit} chars)"


def _extract_json(text: str) -> dict:
    text = re.sub(r"^.*</think>", "", text or "", flags=re.DOTALL)
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    try:
        parsed = json_repair.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


_MONTH_NAME_TO_NUM = {name.lower(): idx for idx in range(1, 13) for name in (calendar.month_name[idx], calendar.month_abbr[idx]) if name}
_MONTH_NAME_RE = "|".join(sorted((re.escape(name) for name in _MONTH_NAME_TO_NUM), key=len, reverse=True))
_MONTH_DAY_YEAR_RE = re.compile(
    rf"\b(?P<month>{_MONTH_NAME_RE})\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?:in\s+)?)?"
    r"(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_COMPACT_DATE_RE = re.compile(r"\b(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})\b")
_YEAR_MONTH_DAY_SLASH_RE = re.compile(r"\b(?P<year>\d{4})/(?P<month>\d{1,2})/(?P<day>\d{1,2})\b")
_MONTH_DAY_YEAR_SLASH_RE = re.compile(r"\b(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<year>\d{4})\b")


def _date_keyword_variants(year: int, month: int, day: int) -> list[str]:
    try:
        from datetime import date as _date

        _date(year, month, day)
    except ValueError:
        return []

    variants = [
        f"{year:04d}-{month:02d}-{day:02d}",
        f"{year:04d}{month:02d}{day:02d}",
        f"{month:02d}/{day:02d}/{year:04d}",
        f"{calendar.month_name[month]} {day}, {year}",
        f"{calendar.month_abbr[month]} {day}, {year}",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for item in variants:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _date_keyword_hints(text: str) -> list[str]:
    """Return human-readable date synonym hints extracted from ``text``."""
    if not text:
        return []

    hints: list[str] = []
    seen: set[tuple[int, int, int]] = set()

    def _add(source: str, year: int, month: int, day: int) -> None:
        key = (year, month, day)
        if key in seen:
            return
        variants = _date_keyword_variants(year, month, day)
        if not variants:
            return
        seen.add(key)
        hints.append(f"{source} -> " + ", ".join(f'"{variant}"' for variant in variants))

    for match in _MONTH_DAY_YEAR_RE.finditer(text):
        month = _MONTH_NAME_TO_NUM.get(match.group("month").lower())
        if month:
            _add(match.group(0), int(match.group("year")), month, int(match.group("day")))

    for match in _ISO_DATE_RE.finditer(text):
        _add(match.group(0), int(match.group("year")), int(match.group("month")), int(match.group("day")))

    for match in _COMPACT_DATE_RE.finditer(text):
        _add(match.group(0), int(match.group("year")), int(match.group("month")), int(match.group("day")))

    for match in _YEAR_MONTH_DAY_SLASH_RE.finditer(text):
        _add(match.group(0), int(match.group("year")), int(match.group("month")), int(match.group("day")))

    for match in _MONTH_DAY_YEAR_SLASH_RE.finditer(text):
        a = int(match.group("a"))
        b = int(match.group("b"))
        year = int(match.group("year"))
        if a > 12 >= b:
            _add(match.group(0), year, b, a)
        elif b > 12 >= a:
            _add(match.group(0), year, a, b)

    return hints


def _chunk_id(c: dict) -> object:
    return c.get("chunk_id") or c.get("id") or id(c)


def _doc_aggs_from(chunks: list[dict]) -> list[dict]:
    aggs, seen = [], set()
    for c in chunks:
        did = c.get("doc_id")
        if did and did not in seen:
            seen.add(did)
            aggs.append({"doc_id": did, "doc_name": c.get("docnm_kwd") or c.get("document_name") or ""})
    return aggs


def _mk_subquestions(items, iteration: int) -> list[dict]:
    """Build fresh sub-question records from LLM ``[{question, keywords}]`` items."""
    out: list[dict] = []
    seen: set[str] = set()
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or "").strip()
        key = _norm(q)
        if not key or key in seen:
            continue
        seen.add(key)
        kws = ", ".join(str(k).strip() for k in (it.get("keywords") or []) if str(k).strip())
        out.append(
            {
                "id": f"sq{iteration}_{i}",
                "question": q,
                "keywords": kws or q,
                "candidate_docs": [],
                "chunks": [],
                "summary": "",
                "answer": None,  # concrete resolved value, filled by summarize
            }
        )
        if len(out) >= _MAX_SUBQUESTIONS:
            break
    return out


def _mk_pending(items, subqs: list[dict], iteration: int) -> list[dict]:
    """Build deferred-hop records. ``depends_on`` (a 0-based index into ``subqs``)
    is mapped to that sub-question's id; items without a ``<ANSWER>`` placeholder
    or a valid dependency are dropped.
    """
    out: list[dict] = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        tmpl = str(it.get("question_template") or "").strip()
        dep = it.get("depends_on")
        if not tmpl or "<ANSWER>" not in tmpl or not isinstance(dep, int) or not (0 <= dep < len(subqs)):
            continue
        kws = ", ".join(str(k).strip() for k in (it.get("keywords") or []) if str(k).strip())
        out.append(
            {
                "id": f"p{iteration}_{i}",
                "question_template": tmpl,
                "keywords": kws,
                "depends_on": subqs[dep]["id"],
            }
        )
    return out


def _dedupe_subquestions(subquestions: list[dict]) -> list[dict]:
    """Keep the first occurrence of each normalized question in a batch."""
    out: list[dict] = []
    seen: set[str] = set()
    for subquestion in subquestions:
        key = _norm(subquestion.get("question", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(subquestion)
    return out


def _resolve_pending(pending, resolved: dict, iteration: int) -> tuple[list[dict], list[dict]]:
    """Substitute resolved answers into pending hops whose dependency is known.

    Returns ``(ready_subquestions, still_pending)`` — a pending hop becomes a
    concrete, answerable sub-question once ``resolved`` holds its dependency's
    answer; otherwise it stays pending for a later round.
    """
    ready_items: list[dict] = []
    still: list[dict] = []
    for p in pending or []:
        val = resolved.get(p.get("depends_on"))
        if val:
            q = p["question_template"].replace("<ANSWER>", val)
            kws = [k.strip() for k in (p.get("keywords") or "").split(",") if k.strip()]
            kws.append(val)  # the resolved value is itself a strong keyword
            ready_items.append({"question": q, "keywords": kws[:3]})
        else:
            still.append(p)
    return _mk_subquestions(ready_items, iteration), still


def build_keyword_agentic_graph(
    tools,
    token_queue: asyncio.Queue,
    gen_conf: dict | None = None,
    max_iterations: int = 12,
    enable_snippets: bool = True,
):
    """Compile the sub-question-driven iterative search graph.

    :param enable_snippets: when True ``retrieve_chunks`` narrows each chunk to
        its keyword-bearing sentences (dropping keyword-less chunks) before it is
        stored/cited; when False chunks are kept whole. Exposed so the snippet
        step's effect can be A/B compared.
    """
    answer_conf = dict(gen_conf) if gen_conf else {"temperature": 0.3}
    answer_conf.pop("direct_answer", None)

    async def _llm_json(system: str, user: str) -> dict:
        msg = await tools._fit_messages(system, user)  # fresh, self-contained context
        ans = await tools.chat_mdl.async_chat(msg[0]["content"], msg[1:], {"temperature": 0.2})
        if isinstance(ans, tuple):
            ans = ans[0]
        return _extract_json(ans)

    async def _llm_text(system: str, user: str) -> str:
        msg = await tools._fit_messages(system, user)
        ans = await tools.chat_mdl.async_chat(msg[0]["content"], msg[1:], {"temperature": 0.2})
        if isinstance(ans, tuple):
            ans = ans[0]
        return re.sub(r"^.*</think>", "", ans or "", flags=re.DOTALL).strip()

    async def _retrieve(query: str, top_n: int, aggs: bool, doc_ids):
        from common import settings

        vec_weight = 0.3 if tools.embed_mdl else 0
        kbinfos = await settings.retriever.retrieval(
            query,
            tools.embed_mdl,
            tools.tenant_ids,
            tools.kb_ids,
            1,
            top_n,
            0.2,
            vector_similarity_weight=vec_weight,
            aggs=aggs,
            highlight=False,
            doc_ids=doc_ids,
        )
        return _normalize(kbinfos, tools.tenant_ids)

    # ── Node 1: keywords — decompose Q into sub-questions + keywords (LLM) ──
    async def keywords_node(state: KwAgenticState) -> dict:
        q = state.get("question") or ""
        _LOG.info("[Keywords] Decomposing into sub-questions: %s", _snip(q))
        user = f"Question:\n{q}"
        hints = _date_keyword_hints(q)
        if hints:
            user += "\n\nDate normalization hints:\n" + "\n".join(f"- {hint}" for hint in hints)
        parsed = await _llm_json(_KEYWORDS_SYSTEM, user + "\n\nOutput JSON:")
        subqs = _mk_subquestions(parsed.get("subquestions"), 0)
        if not subqs:  # fall back to the whole question as a single sub-q
            subqs = _mk_subquestions([{"question": q, "keywords": [q]}], 0)
        pending = _mk_pending(parsed.get("pending"), subqs, 0)
        _LOG.info("[Keywords] %d sub-question(s), %d deferred hop(s).", len(subqs), len(pending))
        for subq in subqs:
            _LOG.info("[Sub-Q & Keywords]: %s -> %s", subq["question"], subq["keywords"])
        for p in pending:
            _LOG.info("[Pending hop]: %s (needs %s)", p["question_template"], p["depends_on"])

        return {"subquestions": subqs, "pending": pending, "resolved": {}, "iteration": 0, "chunks": [], "evidences": []}

    # ── Node 2: retrieve candidate docs per sub-question (no LLM) ──
    async def retrieve_docs_node(state: KwAgenticState) -> dict:
        subqs = state.get("subquestions") or []
        scoped = tools.scoped_doc_ids(None) if hasattr(tools, "scoped_doc_ids") else None

        async def _one(sq: dict) -> list:
            try:
                kbinfos = await _retrieve(sq["keywords"] or sq["question"], _DOC_TOP_N, aggs=False, doc_ids=scoped)
            except Exception:
                _LOG.exception("[Retrieve docs] failed for sub-q %s", sq["id"])
                return []
            docs = defaultdict(int)
            for ck in (kbinfos.get("chunks") or [])[:_DOCS_PER_SUBQ]:
                did = ck.get("doc_id", "")
                docnm = ck.get("doc_name") or ck.get("docnm_kwd") or ""
                k = did + "\t" + docnm
                if docnm:
                    docs[k] += 1
            return docs

        results = await asyncio.gather(*[_one(sq) for sq in subqs])
        for sq, docs in zip(subqs, results):
            docs = sorted(docs.items(), key=lambda x: x[1] * -1)
            sq["candidate_docs"] = [{"doc_id": d[0].split("\t")[0], "docnm": d[0].split("\t")[1]} for d in docs[:2]]
        _LOG.info("[Retrieve docs] candidate docs per sub-q: %s", [len(sq["candidate_docs"]) for sq in subqs])
        return {"subquestions": subqs}

    # ── Node 3: retrieve chunks per sub-q, scoped to its candidate docs, with
    #    optional snippet narrowing (no LLM) ──
    async def retrieve_chunks_node(state: KwAgenticState) -> dict:
        subqs = state.get("subquestions") or []

        async def _one(sq: dict) -> list:
            doc_ids = [d["doc_id"] for d in (sq.get("candidate_docs") or []) if d.get("doc_id")]
            if not doc_ids:
                return []
            try:
                kbinfos = await _retrieve(sq["keywords"] or sq["question"], _CHUNKS_PER_DOC * len(doc_ids), aggs=False, doc_ids=doc_ids)
                return kbinfos.get("chunks") or []
            except Exception:
                _LOG.exception("[Retrieve chunks] failed for sub-q %s", sq["id"])
                return []

        results = await asyncio.gather(*[_one(sq) for sq in subqs])

        # Narrow (if enabled) and union into the citation pool (dedup by id).
        pool = list(state.get("chunks") or [])
        seen = {_chunk_id(c) for c in pool}
        for sq, chunks in zip(subqs, results):
            if enable_snippets:
                chunks = _narrow_by_keywords(chunks, sq["keywords"])
            sq["chunks"] = chunks
            for c in chunks:
                cid = _chunk_id(c)
                if cid not in seen:
                    seen.add(cid)
                    pool.append(c)
        _LOG.info("[Retrieve chunks] chunks per sub-q: %s (pool=%d, snippets=%s)", [len(sq["chunks"]) for sq in subqs], len(pool), enable_snippets)
        return {"subquestions": subqs, "chunks": pool}

    # ── Node 5: summarize each sub-question over its own chunks (LLM, parallel) ──
    async def summarize_node(state: KwAgenticState) -> dict:
        subqs = state.get("subquestions") or []
        it = state.get("iteration", 0)

        async def _one(sq: dict) -> dict:
            if not sq["chunks"]:
                return {"summary": "", "answer": None}
            snippets_text = "\n\n".join((c.get("content_with_weight") or c.get("content") or "") for c in sq["chunks"][:_SUMMARY_SNIPPETS])
            user = f"Sub-question:\n{sq['question']}\n\nSnippets:\n{snippets_text}\n\nOutput JSON:"
            parsed = await _llm_json(_SUMMARIZE_SYSTEM, user)
            ans = parsed.get("answer")
            ans = str(ans).strip() if ans not in (None, "", "null", "None") else None
            return {"summary": str(parsed.get("summary") or "").strip(), "answer": ans}

        results = await asyncio.gather(*[_one(sq) for sq in subqs])
        evidences = list(state.get("evidences") or [])
        resolved = dict(state.get("resolved") or {})
        for sq, r in zip(subqs, results):
            sq["summary"] = r["summary"]
            sq["answer"] = r["answer"]
            if r["answer"]:
                resolved[sq["id"]] = r["answer"]
            if r["summary"]:
                evidences.append({"iteration": it, "subq_id": sq["id"], "subq": sq["question"], "summary": r["summary"], "answer": r["answer"]})
        _LOG.info("[Summarize] stored %d summary(ies), %d resolved answer(s) at iteration %d.", sum(1 for r in results if r["summary"]), sum(1 for r in results if r["answer"]), it)
        return {"subquestions": subqs, "evidences": evidences, "resolved": resolved}

    # ── Node 6: think — sufficient vs original Q, else next sub-questions (LLM) ──
    async def think_node(state: KwAgenticState) -> dict:
        evidences = state.get("evidences") or []
        resolved = dict(state.get("resolved") or {})
        pending = list(state.get("pending") or [])
        ev_text = "\n\n".join(f"[{e['subq']}] (answer: {e.get('answer') or 'n/a'})\n{e['summary']}" for e in evidences) or "(no evidence yet)"
        user = f"Original question:\n{state.get('question') or ''}\n\nEvidence per sub-question:\n{ev_text}\n\nOutput JSON:"
        parsed = await _llm_json(_THINK_SYSTEM, user)
        sufficient = bool(parsed.get("sufficient"))
        it = state.get("iteration", 0) + 1

        # 1. Substitute newly-resolved answers into pending hops → answerable now.
        ready_subqs, still_pending = _resolve_pending(pending, resolved, it)

        # 2. think's own new sub-questions (deduped) + any new deferred hops it adds.
        asked = {_norm(e["subq"]) for e in evidences}
        fresh = [x for x in (parsed.get("next_subquestions") or []) if isinstance(x, dict) and _norm(x.get("question", "")) not in asked]
        new_subqs = _mk_subquestions(fresh, it)
        new_pending = _mk_pending(parsed.get("next_pending"), new_subqs, it)

        next_subqs = _dedupe_subquestions(ready_subqs + new_subqs)
        next_pending = still_pending + new_pending
        _LOG.info(
            "[Think] iteration %d → sufficient=%s; %d ready-from-pending + %d new sub-q(s), %d hop(s) still pending. Missing: %s",
            it,
            sufficient,
            len(ready_subqs),
            len(new_subqs),
            len(next_pending),
            _snip(parsed.get("missing")),
        )
        for n in next_subqs:
            _LOG.info("[Think] iteration(%d) sub-q → %s", it, n.get("question"))
        for n in next_pending:
            _LOG.info("[Think] iteration(%d) hop → %s", it, n.get("question_template"))
        return {"sufficient": sufficient, "subquestions": next_subqs, "pending": next_pending, "resolved": resolved, "iteration": it}

    # ── Node 7: brief, cited answer (LLM, streamed) ──
    async def answer_node(state: KwAgenticState) -> dict:
        pool = state.get("chunks") or []
        if not pool:
            msg = "I don't have enough information based on the available sources."
            token_queue.put_nowait(msg)
            return {"final_answer": msg}

        tools.kbinfos = {"chunks": pool, "doc_aggs": _doc_aggs_from(pool)}
        evidence_blocks = kb_prompt(tools.kbinfos, tools.chat_mdl.max_length)
        evidence_blocks = evidence_blocks if isinstance(evidence_blocks, list) else [str(evidence_blocks)]

        # Prepend the accumulated per-sub-question findings so the final answer
        # sees the distilled evidence too (uncited context; [ID:n] citations still
        # come from the chunk blocks below).
        findings = []
        for e in state.get("evidences") or []:
            if not e.get("summary"):
                continue
            ans = e.get("answer")
            findings.append(f"- {e['subq']}" + (f" (answer: {ans})" if ans else "") + f": {e['summary']}")
        if findings:
            evidence_blocks = ["Research findings per sub-question:\n" + "\n".join(findings)] + evidence_blocks

        evidence = "\n".join(evidence_blocks)
        rules = citation_prompt(tools.user_defined_prompts).strip()
        system = FINAL_ANSWER_SYSTEM.format(cite_rules=rules)
        user = f"Question:\n{state.get('question') or ''}\n\nEvidence:\n{evidence}"
        _, msg = message_fit_in(form_message(system, user), tools.chat_mdl.max_length)

        _LOG.info("[Answer] Composing a brief, cited answer from %d chunk(s).", len(pool))
        final = ""
        try:
            async for tok in tools.chat_mdl.async_chat_streamly_delta(msg[0]["content"], msg[1:], answer_conf):
                token_queue.put_nowait(tok)
                final += tok
        except Exception:
            _LOG.exception("[Answer] stream failed")
            token_queue.put_nowait("I'm sorry, I encountered an error while composing the answer.")
        return {"final_answer": final}

    def _route_after_think(state: KwAgenticState) -> str:
        if state.get("sufficient") or state.get("iteration", 0) >= max_iterations or not state.get("subquestions"):
            return "answer"
        return "retrieve_docs"

    g = StateGraph(KwAgenticState)
    g.add_node("keywords", keywords_node)
    g.add_node("retrieve_docs", retrieve_docs_node)
    g.add_node("retrieve_chunks", retrieve_chunks_node)
    g.add_node("summarize", summarize_node)
    g.add_node("think", think_node)
    g.add_node("answer", answer_node)

    g.add_edge(START, "keywords")
    g.add_edge("keywords", "retrieve_docs")
    g.add_edge("retrieve_docs", "retrieve_chunks")
    g.add_edge("retrieve_chunks", "summarize")
    g.add_edge("summarize", "think")
    g.add_conditional_edges("think", _route_after_think, {"retrieve_docs": "retrieve_docs", "answer": "answer"})
    g.add_edge("answer", END)

    return g.compile()


async def run_keyword_agentic_rag(
    tools,
    messages: list,
    max_iterations: int = 12,
    enable_snippets: bool = True,
    gen_conf: dict | None = None,
):
    """Drive the sub-question graph, yielding answer-token strings."""
    question = ""
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content"):
            question = m["content"]
            break

    token_queue: asyncio.Queue = asyncio.Queue()
    graph = build_keyword_agentic_graph(
        tools,
        token_queue,
        gen_conf=gen_conf,
        max_iterations=max_iterations,
        enable_snippets=enable_snippets,
    )
    _SENTINEL = object()
    holder: dict[str, Any] = {}

    async def _drive():
        try:
            holder["state"] = await graph.ainvoke(
                {"question": question, "max_iterations": max_iterations, "iteration": 0, "chunks": [], "evidences": [], "resolved": {}, "pending": []},
                {"recursion_limit": max(25, max_iterations * 8 + 10)},
            )
        except Exception:
            _LOG.exception("run_keyword_agentic_rag: graph execution failed")
            holder["error"] = True
        finally:
            token_queue.put_nowait(_SENTINEL)

    task = asyncio.create_task(_drive())
    produced = False
    try:
        while True:
            item = await token_queue.get()
            if item is _SENTINEL:
                break
            produced = True
            yield item
    finally:
        await task

    if not produced and holder.get("error"):
        yield "I couldn't complete the search due to an internal error."
