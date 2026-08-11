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

"""Keyword-driven, doc-first iterative search graph — v2 (LangGraph).

A flatter redesign of :mod:`rag.advanced_rag.keyword_agentic_graph`: all
sub-questions are INDEPENDENT (no dependency/pending machinery), each sub-question
is answered from a SINGLE best document, and the summariser itself curates which
chunk ids become citations.

Flow:

    analyze → keywords → retrieve_docs → retrieve_chunks → summarize → think
        think ─(sufficient | iter ≥ max | no next sub-qs)→ answer → END
              └──────────(else: think's next sub-questions)──────────→ keywords

``analyze`` only decomposes the question into independent sub-questions; a
separate ``keywords`` node turns each sub-question into search keywords, close
synonyms and date-format variants (so ``think``'s follow-ups get keyworded too).

Design rules:
* Every LLM node builds a FRESH (system, user) prompt — contexts never accumulate.
* ``retrieve_docs`` picks the TOP-1 doc per sub-question by hit count within the
  top-N retrieved chunks. ``retrieve_chunks`` re-retrieves scoped to that doc.
* ``summarize`` returns, per sub-question, a summary plus the chunk NUMBERS that
  support it; only those chunks are retained into the citation pool. If a
  sub-question yields no supporting chunk, its top chunks' document-adjacent
  neighbours (one before + one after, deduped) are loaded and it is re-summarized.
* ``answer`` composes a brief, ``[ID:n]``-cited answer from the retained pool,
  with the per-sub-question summaries prepended as uncited context.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
import math
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from rag.prompts.generator import citation_prompt, form_message, message_fit_in
from rag.advanced_rag.harness.prompts.report_prompt import FINAL_ANSWER_SYSTEM
from rag.advanced_rag.harness.tools.search import _narrow_by_keywords, _normalize

# Stable pure helpers reused from v1 (no behavioural coupling).
from rag.advanced_rag.keyword_agentic_graph import (
    _DATE_NORMALIZATION_SYSTEM,
    _chunk_id,
    _date_keyword_hints,
    _doc_aggs_from,
    _extract_json,
    _norm,
    _snip,
)

_LOG = logging.getLogger(__name__)

# Tunable caps.
_MAX_SUBQUESTIONS = 2  # sub-questions kept per iteration
_MAX_ANALYZE_SUBQUESTIONS = 2  # independent sub-questions emitted by the initial analyzer
_DOC_TOP_N = 6  # chunk hits fetched per sub-q to pick the top-1 doc
_CHUNKS_PER_DOC = 6  # chunks pulled from the chosen doc
_SUMMARY_CHUNKS = 3  # chunks per summariser batch (shown to, and citable by, the LLM)
_DOC_ORDERED_LIMIT = 10000  # max chunks fetched when ordering a doc
_MAX_DOC_BATCHES = 8  # max overlapping batches scanned per sub-q when the retrieved chunks miss


# ── Prompts (each a single, self-contained LLM call) ──

_ANALYZE_SYSTEM = """You break a question into the INDEPENDENT sub-questions needed to answer it — each
answerable on its own from the original question, without the answer to another sub-question.
Generate AT MOST TWO sub-questions. Generate fewer when fewer are sufficient; never pad the
list. The sub-questions must be distinct, non-overlapping, and independently searchable.
Do not generate dependent follow-ups, multi-step chains, or questions containing unresolved
references such as "that city", "it", "the person", "the former", or "the latter". Repeat
the full entity name and any ranking, date, or location condition in every sub-question.
Output ONLY JSON, no prose, no code fences:
{"subquestions": [{"question": "<sub-question>"}, ...]}"""

_KEYWORDS_SYSTEM = (
    """You are given several sub-questions. For EACH sub-question, produce search-friendly
keywords AND their closest / most-likely synonyms (AT MOST 3 terms; a standalone number or
serial of digits is its OWN term, never glued to other words). Include alternative
date-format expression variants whenever a sub-question mentions a date.
"""
    + _DATE_NORMALIZATION_SYSTEM
    + """
Output ONLY JSON, no prose, no code fences:
{"keywords": [{"id": "<sub-question id>", "keywords": ["term or synonym", ...]}, ...]}"""
)

_SUMMARIZE_SYSTEM = """You are given a sub-question and NUMBERED chunks retrieved for it.
Answer the sub-question ONLY with the facts in the chunks — concise and factual,
no speculation, no outside knowledge. Also return "relevant": the list of chunk NUMBERS that
actually support your answer (empty list if none of the chunks help or the sub-question can't be answered with these chunks).
Output ONLY JSON, no prose, no code fences:
{"summary": "<concise factual answer, or 'No useful evidence.'>", "relevant": [<chunk number>, ...]}"""

_THINK_SYSTEM = """You are given the ORIGINAL question and the evidence summarized for each sub-question so far.
Decide whether the collected evidence is ENOUGH to answer the ORIGINAL question directly.
If it is NOT enough, state what is missing and propose the next INDEPENDENT sub-questions that would
fill the gap. Every next sub-question must be searchable on its own from the ORIGINAL question —
repeat full entity names / conditions, never "that city", "it", "the former". (Keywords are added by a
separate step, so output the questions only.)
Output ONLY JSON, no prose, no code fences:
{"sufficient": true/false, "missing": "<what is still missing, or empty>",
 "next_subquestions": [{"question": "<sub-question>"}, ...]}"""


class KwV2State(TypedDict, total=False):
    question: str
    subquestions: list  # [{id, question, keywords, doc_id, chunks}]
    evidences: list  # accumulated [{iteration, subq, summary, chunk_ids}]
    pool: list  # retained (summariser-selected) chunks — the citation set, dedup by id
    iteration: int
    max_iterations: int
    sufficient: bool
    final_answer: str


def _mk_subqs(items, iteration: int, limit: int = _MAX_SUBQUESTIONS) -> list[dict]:
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
        out.append({"id": f"sq{iteration}_{i}", "question": q, "keywords": kws or q, "doc_id": "", "chunks": []})
        if len(out) >= limit:
            break
    return out


def build_keyword_agentic_graph_v2(
    tools,
    token_queue: asyncio.Queue,
    gen_conf: dict | None = None,
    max_iterations: int = 3,
    enable_snippets: bool = False,
):
    """Compile the v2 graph.

    :param enable_snippets: optional extra — when True, ``retrieve_chunks`` narrows
        chunks to keyword sentences before summarising. Off by default because the
        summariser already curates the relevant chunks.
    """
    answer_conf = dict(gen_conf) if gen_conf else {"temperature": 0.3}
    answer_conf.pop("direct_answer", None)

    async def _llm_json(system: str, user: str) -> dict:
        msg = await tools._fit_messages(system, user)
        ans = await tools.chat_mdl.async_chat(msg[0]["content"], msg[1:], {"temperature": 0.2})
        if isinstance(ans, tuple):
            ans = ans[0]
        return _extract_json(ans)

    async def _retrieve(query: str, top_n: int, doc_ids):
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
            aggs=False,
            highlight=False,
            doc_ids=doc_ids,
        )
        return _normalize(kbinfos, tools.tenant_ids)

    async def _fetch_doc_chunks_ordered(doc_id: str) -> list[dict]:
        """All chunks of ``doc_id`` in document order (by ``chunk_order_int``)."""
        from common import settings
        from common.doc_store.doc_store_base import OrderByExpr
        from common.misc_utils import thread_pool_exec
        from rag.nlp import search as _rag_search

        index_names = [_rag_search.index_name(t) for t in tools.tenant_ids]
        fields = ["id", "content_with_weight", "docnm_kwd", "doc_id", "chunk_order_int"]
        order = OrderByExpr()
        try:
            order.asc("chunk_order_int")
        except Exception:
            order = OrderByExpr()
        try:
            res = await thread_pool_exec(settings.docStoreConn.search, fields, [], {"doc_id": [doc_id]}, [], order, 0, _DOC_ORDERED_LIMIT, index_names, tools.kb_ids)
            rows = settings.docStoreConn.get_fields(res, fields) or {}
        except Exception:
            _LOG.exception("[Adjacent] ordered doc fetch failed for %s", doc_id)
            return []
        out = []
        for cid, row in rows.items():
            try:
                order_int = int(row.get("chunk_order_int") or 0)
            except (TypeError, ValueError):
                order_int = 0
            out.append(
                {
                    "id": cid,
                    "chunk_id": cid,
                    "content_with_weight": row.get("content_with_weight") or "",
                    "docnm_kwd": row.get("docnm_kwd") or "",
                    "doc_id": row.get("doc_id") or doc_id,
                    "_order": order_int,
                }
            )
        out.sort(key=lambda c: c["_order"])
        return out

    async def _summarize_chunks(question: str, chunks: list[dict]) -> tuple[str, list[dict]]:
        """LLM summary + the chunk objects it marked relevant (by 1-based number)."""
        shown = chunks[:_SUMMARY_CHUNKS]
        if not shown:
            return "", []
        listing = "\n\n".join(f"[{i + 1}] " + (c.get("content_with_weight") or c.get("content") or "") for i, c in enumerate(shown))
        user = f"Chunks:\n{listing}\n\nSub-question:\n{question}\n\nOutput JSON:"
        parsed = await _llm_json(_SUMMARIZE_SYSTEM, user)
        sub_answer = str(parsed.get("summary") or "").strip()
        relevant: list[dict] = []
        for r in parsed.get("relevant") or []:
            try:
                i = int(r) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(shown):
                relevant.append(shown[i])
        return sub_answer, relevant

    async def _answer_by_batches(sq: dict) -> tuple[str, list[dict]]:
        """Scan the sub-question's document in overlapping batches until answered.

        The doc's chunks are read in document order and summarized ``_SUMMARY_CHUNKS``
        at a time; consecutive batches overlap by ONE chunk (the last chunk of a
        batch starts the next), so an answer straddling a boundary is never split.
        Stops as soon as a batch yields a relevant chunk, or after ``_MAX_DOC_BATCHES``.
        Returns ``(summary, relevant chunks)``.
        """
        ordered = await _fetch_doc_chunks_ordered(sq["doc_id"])
        n = len(ordered)
        _LOG.info("[Read file]: %s(%d) -> %s", sq["doc_nm"], n, sq["question"])
        if not n:
            return "", []
        step = max(1, _SUMMARY_CHUNKS - 1)  # 1-chunk overlap between batches
        start = 0
        for _scan in range(_MAX_DOC_BATCHES):
            batch = ordered[start : start + _SUMMARY_CHUNKS]
            if not batch:
                break
            summary, relevant = await _summarize_chunks(sq["question"], batch)
            if relevant:
                _LOG.info("[Summarize] sub-q %s answered from doc batch [%d:%d].", sq["id"], start, start + len(batch))
                return summary, relevant
            if start + _SUMMARY_CHUNKS >= n:  # this batch already reached the doc end
                break
            start += step
        _LOG.info("[Read file] failure: %s(%d) -> %s", sq["doc_nm"], n, sq["question"])
        return "", []

    # ── Node 1: analyze — decompose Q into independent sub-questions (LLM) ──
    async def analyze_node(state: KwV2State) -> dict:
        q = state.get("question") or ""
        _LOG.info("[Analyze] Decomposing into independent sub-questions: %s", _snip(q))
        parsed = await _llm_json(_ANALYZE_SYSTEM, f"Question:\n{q}\n\nOutput JSON:")
        subqs = _mk_subqs(parsed.get("subquestions"), 0, limit=_MAX_ANALYZE_SUBQUESTIONS) or _mk_subqs([{"question": q}], 0, limit=_MAX_ANALYZE_SUBQUESTIONS)
        for sq in subqs:
            _LOG.info("[Sub-Q]: %s", sq["question"])
        return {"subquestions": subqs, "iteration": 0, "pool": [], "evidences": []}

    # ── Node 1b: keywords — search-friendly keywords + synonyms per sub-question,
    #    including date-format variants (batched LLM call) ──
    async def keywords_node(state: KwV2State) -> dict:
        subqs = state.get("subquestions") or []
        if not subqs:
            return {"subquestions": subqs}
        listing = "\n".join(f"[{sq['id']}] {sq['question']}" for sq in subqs)
        user = "Sub-questions:\n" + listing
        hints = _date_keyword_hints("\n".join(sq["question"] for sq in subqs))
        if hints:
            user += "\n\nDate normalization hints:\n" + "\n".join(f"- {h}" for h in hints)
        parsed = await _llm_json(_KEYWORDS_SYSTEM, user + "\n\nOutput JSON:")
        by_id = {sq["id"]: sq for sq in subqs}
        for e in parsed.get("keywords") or []:
            sq = by_id.get(e.get("id"))
            if not sq:
                continue
            kws = ", ".join(str(k).strip() for k in (e.get("keywords") or []) if str(k).strip())
            if kws:
                sq["keywords"] = kws
        for sq in subqs:
            if not sq.get("keywords"):
                sq["keywords"] = sq["question"]
            _LOG.info("[Keywords]: %s -> %s", sq["question"], sq["keywords"])
        return {"subquestions": subqs}

    # ── Node 2: retrieve the top-1 doc per sub-question (no LLM) ──
    async def retrieve_docs_node(state: KwV2State) -> dict:
        subqs = state.get("subquestions") or []
        scoped = tools.scoped_doc_ids(None) if hasattr(tools, "scoped_doc_ids") else None

        async def _one(sq: dict) -> str:
            try:
                kbinfos = await _retrieve(sq["keywords"] or sq["question"], _DOC_TOP_N, doc_ids=scoped)
            except Exception:
                _LOG.exception("[Retrieve docs] failed for sub-q %s", sq["id"])
                return ""
            counts: dict[str, int] = defaultdict(int)
            sc = 1
            for i, ck in enumerate(kbinfos.get("chunks") or []):
                did = ck.get("doc_id")
                nm = ck.get("docnm_kwd", "")
                if did:
                    counts[did + "\t" + nm] += math.pow(ck.get("similarity", sc) * 100.0, 2.0)
                sc /= i + 1.0
            print(sq, max(counts.items(), key=lambda x: x[1]), "JJJJJJJJJJJJJJJJJJJJJ", flush=True)
            return max(counts.items(), key=lambda x: x[1])[0] if counts else ""

        docs = await asyncio.gather(*[_one(sq) for sq in subqs])
        for sq, did in zip(subqs, docs):
            did, nm = did.split("\t")
            sq["doc_id"] = did
            sq["doc_nm"] = nm
            _LOG.info("[Retrieve docs] %s -> %s", sq["question"], nm)
        return {"subquestions": subqs}

    # ── Node 3: retrieve chunks scoped to the chosen doc (no LLM) ──
    async def retrieve_chunks_node(state: KwV2State) -> dict:
        subqs = state.get("subquestions") or []

        async def _one(sq: dict) -> list:
            if not sq["doc_id"]:
                return []
            try:
                kbinfos = await _retrieve(sq["keywords"] or sq["question"], _CHUNKS_PER_DOC, doc_ids=[sq["doc_id"]])
                chunks = kbinfos.get("chunks") or []
            except Exception:
                _LOG.exception("[Retrieve chunks] failed for sub-q %s", sq["id"])
                return []
            if enable_snippets:
                chunks = _narrow_by_keywords(chunks, sq["keywords"])
            return chunks

        per_sq = await asyncio.gather(*[_one(sq) for sq in subqs])
        for sq, chunks in zip(subqs, per_sq):
            sq["chunks"] = chunks
        _LOG.info("[Retrieve chunks] chunks per sub-q: %s", [len(sq["chunks"]) for sq in subqs])
        return {"subquestions": subqs}

    # ── Node 4: summarize + curate citations, with adjacency retry (LLM) ──
    async def summarize_node(state: KwV2State) -> dict:
        subqs = state.get("subquestions") or []
        it = state.get("iteration", 0)

        async def _one(sq: dict) -> dict:
            summary, relevant = ("", [])
            if sq["chunks"]:
                summary, relevant = await _summarize_chunks(sq["question"], sq["chunks"])
            if not relevant and sq.get("doc_id"):
                # Retrieved chunks missed → scan the whole doc in overlapping batches
                # until the sub-question is answered.
                summary, relevant = await _answer_by_batches(sq)
            return {"summary": summary, "relevant": relevant}

        results = await asyncio.gather(*[_one(sq) for sq in subqs])

        evidences = list(state.get("evidences") or [])
        pool = list(state.get("pool") or [])
        seen = {_chunk_id(c) for c in pool}
        for sq, r in zip(subqs, results):
            for c in r["relevant"]:
                cid = _chunk_id(c)
                if cid not in seen:
                    seen.add(cid)
                    pool.append(c)
            if r["summary"]:
                evidences.append({"iteration": it, "subq": sq["question"], "summary": r["summary"], "chunk_ids": [_chunk_id(c) for c in r["relevant"]]})
        _LOG.info("[Summarize] %d evidence(s), pool=%d chunk(s) at iteration %d.", len(evidences), len(pool), it)
        return {"subquestions": subqs, "evidences": evidences, "pool": pool}

    # ── Node 5: think — sufficient vs original Q, else next sub-questions (LLM) ──
    async def think_node(state: KwV2State) -> dict:
        evidences = state.get("evidences") or []
        ev_text = "\n\n".join(f"[{e['subq']}]\n{e['summary']}" for e in evidences) or "(no evidence yet)"
        user = f"Original question:\n{state.get('question') or ''}\n\nEvidence per sub-question:\n{ev_text}\n\nOutput JSON:"
        parsed = await _llm_json(_THINK_SYSTEM, user)
        sufficient = bool(parsed.get("sufficient"))
        it = state.get("iteration", 0) + 1
        asked = {_norm(e["subq"]) for e in evidences}
        fresh = [x for x in (parsed.get("next_subquestions") or []) if isinstance(x, dict) and _norm(x.get("question", "")) not in asked]
        next_subqs = _mk_subqs(fresh, it)
        _LOG.info("[Think] iteration %d → sufficient=%s; %d new sub-question(s). Missing: %s", it, sufficient, len(next_subqs), _snip(parsed.get("missing")))
        return {"sufficient": sufficient, "subquestions": next_subqs, "iteration": it}

    # ── Node 6: brief, cited answer — composed from the per-sub-question
    #    summaries (evidences), NOT the raw chunks. Each summary is tagged with the
    #    [ID:n] markers of the chunks that support it; the pool stays as the
    #    citation backing (kbinfos) so those markers resolve to real sources. ──
    async def answer_node(state: KwV2State) -> dict:
        evidences = [e for e in (state.get("evidences") or []) if e.get("summary")]
        if not evidences:
            msg = "I don't have enough information based on the available sources."
            token_queue.put_nowait(msg)
            return {"final_answer": msg}

        # Citation backing: [ID:n] == position of a chunk in the pool. We do NOT
        # render chunk content to the model — only the summaries below — but the
        # pool must stay on kbinfos so downstream [ID:n] resolution finds sources.
        pool = state.get("pool") or []
        tools.kbinfos = {"chunks": pool, "doc_aggs": _doc_aggs_from(pool)}
        id_of = {_chunk_id(c): i for i, c in enumerate(pool)}

        # One finding per sub-question: its summary + the citation ids that back it.
        findings = []
        for e in evidences:
            ids = [id_of[cid] for cid in (e.get("chunk_ids") or []) if cid in id_of]
            cite = " ".join(f"[ID:{i}]" for i in ids)
            findings.append(f"- {e['subq']}: {e['summary']}" + (f"  (cite: {cite})" if cite else ""))
        evidence = "\n".join(findings)

        rules = citation_prompt(tools.user_defined_prompts).strip()
        system = FINAL_ANSWER_SYSTEM.format(cite_rules=rules)
        user = (
            f"Question:\n{state.get('question') or ''}\n\n"
            "Answer ONLY from the findings below. Each finding shows the [ID:n] citation markers that "
            "support it — reuse those exact markers in your answer; do not invent IDs.\n\n"
            f"Findings:\n{evidence}"
        )
        _, msg = message_fit_in(form_message(system, user), tools.chat_mdl.max_length)

        _LOG.info("[Answer] Composing a brief answer from %d finding(s), citing a pool of %d chunk(s).", len(findings), len(pool))
        final = ""
        try:
            async for tok in tools.chat_mdl.async_chat_streamly_delta(msg[0]["content"], msg[1:], answer_conf):
                token_queue.put_nowait(tok)
                final += tok
        except Exception:
            _LOG.exception("[Answer] stream failed")
            token_queue.put_nowait("I'm sorry, I encountered an error while composing the answer.")
        return {"final_answer": final}

    def _route_after_think(state: KwV2State) -> str:
        if state.get("sufficient") or state.get("iteration", 0) >= max_iterations or not state.get("subquestions"):
            return "answer"
        return "keywords"

    g = StateGraph(KwV2State)
    g.add_node("analyze", analyze_node)
    g.add_node("keywords", keywords_node)
    g.add_node("retrieve_docs", retrieve_docs_node)
    g.add_node("retrieve_chunks", retrieve_chunks_node)
    g.add_node("summarize", summarize_node)
    g.add_node("think", think_node)
    g.add_node("answer", answer_node)

    g.add_edge(START, "analyze")
    g.add_edge("analyze", "keywords")
    g.add_edge("keywords", "retrieve_docs")
    g.add_edge("retrieve_docs", "retrieve_chunks")
    g.add_edge("retrieve_chunks", "summarize")
    g.add_edge("summarize", "think")
    g.add_conditional_edges("think", _route_after_think, {"keywords": "keywords", "answer": "answer"})
    g.add_edge("answer", END)

    return g.compile()


async def run_keyword_agentic_rag_v2(
    tools,
    messages: list,
    max_iterations: int = 12,
    enable_snippets: bool = False,
    gen_conf: dict | None = None,
):
    """Drive the v2 graph, yielding answer-token strings."""
    question = ""
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content"):
            question = m["content"]
            break

    token_queue: asyncio.Queue = asyncio.Queue()
    graph = build_keyword_agentic_graph_v2(tools, token_queue, gen_conf=gen_conf, max_iterations=max_iterations, enable_snippets=enable_snippets)
    _SENTINEL = object()
    holder: dict[str, Any] = {}

    async def _drive():
        try:
            holder["state"] = await graph.ainvoke(
                {"question": question, "max_iterations": max_iterations, "iteration": 0, "pool": [], "evidences": []},
                {"recursion_limit": max(25, max_iterations * 6 + 10)},
            )
        except Exception:
            _LOG.exception("run_keyword_agentic_rag_v2: graph execution failed")
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
