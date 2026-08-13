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

"""Keyword-driven, multi-doc iterative search graph — v3 (LangGraph).

Same shape as v2, but a sub-question is no longer bound to a single document:
``retrieve_docs`` keeps the top ``_DOCS_PER_SUBQ`` candidate documents as a RANKED
list, and one merged ``investigate`` node walks that list until the sub-question is
answered.

Flow:

    analyze → keywords → retrieve_docs → investigate → think
        think ─(sufficient | iter ≥ max | no next sub-qs)→ answer → END
              └──────────(else: think's next sub-questions)──────────→ keywords

``investigate`` merges v2's ``retrieve_chunks`` + ``summarize``. Per sub-question it
walks ``sq["ranks"]`` best-first and, for EACH ranked document:

  1. retrieves that doc's chunks with the sub-question's keywords, summarizes them;
  2. if that misses, scans the WHOLE document in overlapping batches;
  3. if it still misses, moves on to the next ranked document.

The walk stops at the first document that answers the sub-question, or when every
ranked document has been checked. Sub-questions are investigated in parallel; the
rank walk inside a sub-question is sequential so the early break saves LLM calls.

Design rules:
* Every LLM node builds a FRESH (system, user) prompt — contexts never accumulate.
* Only chunks the summariser marked relevant enter the citation pool.
* ``answer`` composes a brief answer from the per-sub-question summaries, each
  tagged with the ``[ID:n]`` markers of the chunks backing it.
"""

from __future__ import annotations

import asyncio
import logging
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
_DOC_TOP_N = 30  # chunk hits fetched per sub-q when ranking candidate docs
_DOCS_PER_SUBQ = 1  # ranked candidate docs retained per sub-question
_CHUNKS_PER_DOC = 6  # chunks pulled from each ranked doc on the cheap path
_SUMMARY_CHUNKS = 8  # chunks per summariser call (shown to, and citable by, the LLM)
_DOC_BATCH_SIZE = 3  # chunks per batch when scanning a whole document
_DOC_BATCH_OVERLAP = 1  # chunks shared between consecutive document batches
_MAX_DOC_BATCHES = 18  # max batches scanned per document before giving up on it


# ── Prompts (each a single, self-contained LLM call) ──

_ANALYZE_SYSTEM = """You plan the FIRST round of research for a question. Research runs in several
rounds, so this round does NOT have to answer the whole question.

Emit ONLY the sub-questions that can be searched RIGHT NOW, using facts stated in the original
question itself. Rules:
- SIMPLE and ATOMIC: one fact, one entity, one relation per sub-question. Never bundle several
  facts, or several hops, into one sub-question.
- INDEPENDENT: answerable on its own; never relies on the answer to another sub-question.
- Anything that needs a value you do NOT have yet is deliberately LEFT OUT. Do not guess it and
  do not phrase it indirectly — a later round will ask it once that value is known.
- Never use an unresolved reference such as "that city", "it", "the person", "the former" or
  "the latter". Repeat the full entity name and any ranking, date or location condition.
- Generate AT MOST TWO sub-questions; fewer is better when fewer suffice. Never pad the list.
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
Answer the sub-question ONLY with the facts in the chunks — concise and factual, no speculation,
no outside knowledge.

Return "status", judged strictly:
- "full"    — the chunks COMPLETELY answer the sub-question.
- "partial" — the chunks are on-topic and give some of the answer, but a needed detail is
              missing. In "summary", state what was found AND exactly what is still missing.
- "none"    — the chunks do not answer the sub-question at all.
Do NOT claim "full" when the specific value asked for (a date, number, name, ...) is absent.

Return "relevant": the chunk NUMBERS that support your answer (empty list when status is "none").
Output ONLY JSON, no prose, no code fences:
{"status": "full|partial|none", "summary": "<concise factual answer, or what is missing>", "relevant": [<chunk number>, ...]}"""

_SUFFICIENCY_SYSTEM = """You are given the ORIGINAL question and the facts discovered so far.
Decide ONLY whether those facts are ENOUGH to answer the ORIGINAL question directly and completely.
Do NOT propose follow-up research — that is a separate step.

- "sufficient" is true only when EVERY part of the ORIGINAL question can be answered from the facts.
- Each fact is tagged FULL or PARTIAL. PARTIAL means the sources were on-topic but a needed value is
  still missing, so it does NOT make the question answerable unless that missing part is irrelevant
  to the ORIGINAL question.
- Do not assume, infer or fill in a value that no fact states.
- When it is not sufficient, "missing" must state precisely what is still needed.
Output ONLY JSON, no prose, no code fences:
{"sufficient": true/false, "missing": "<what is still missing, or empty>"}"""

_NEXT_SUBQ_SYSTEM = """The facts gathered so far do NOT yet answer the ORIGINAL question. You are given
what is still missing and EVERY sub-question already asked with its outcome. Plan the NEXT round.

- USE THE FACTS ALREADY DISCOVERED. If an earlier round resolved a value (a name, date, number),
  SUBSTITUTE that concrete value into the next sub-question. This is how a multi-hop question
  makes progress across rounds.
  Example — if a discovered fact says the city is "Baltimore", ask "When was Baltimore founded?"
  NOT "When was that city founded?" and NOT "When was the 50th most populous US city founded?".
- Keep each sub-question SIMPLE, ATOMIC and searchable on its own: one fact per question.
- Never use an unresolved reference ("that city", "it", "the former"). If a needed value is still
  unknown, ask for THAT value instead — do not chain two unknowns in one sub-question.
- NEVER repeat or rephrase anything under "Sub-questions ALREADY asked" — those rounds are spent.
  One marked "[searched, no evidence found]" means that angle failed: do not retry it with different
  wording; attack the gap from a genuinely different angle (a different entity, source or attribute).
  One marked "[PARTIAL ...]" is an OPEN gap: ask a NEW, narrowly targeted question for just the
  missing value, naming the entity already discovered.
- If no genuinely NEW and useful sub-question exists — every angle is spent, or the corpus plainly
  cannot supply what is missing — return an EMPTY "next_subquestions" list. An empty list is the
  CORRECT answer in that case; never pad it with a variation of something already asked.
- Generate AT MOST TWO. (Keywords are added by a separate step, so output the questions only.)
Output ONLY JSON, no prose, no code fences:
{"next_subquestions": [{"question": "<sub-question>"}, ...]}"""


class KwV3State(TypedDict, total=False):
    question: str
    subquestions: list  # [{id, question, keywords, ranks: [{doc_id, doc_nm}, ...]}]
    evidences: list  # accumulated [{iteration, subq, summary, chunk_ids}]
    asked: list  # EVERY sub-question attempted so far, answered or not — blocks re-asking
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
        out.append({"id": f"sq{iteration}_{i}", "question": q, "keywords": kws or q, "ranks": []})
        if len(out) >= limit:
            break
    return out


def build_keyword_agentic_graph_v3(
    tools,
    token_queue: asyncio.Queue,
    gen_conf: dict | None = None,
    max_iterations: int = 3,
    enable_snippets: bool = False,
):
    """Compile the v3 graph.

    :param enable_snippets: optional extra — when True, the cheap retrieval path
        narrows chunks to keyword sentences before summarising. Off by default
        because the summariser already curates the relevant chunks.
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

    async def _summarize_chunks(question: str, chunks: list[dict]) -> tuple[str, str, list[dict]]:
        """Summarize chunks for a sub-question.

        Returns ``(status, summary, relevant chunks)`` where status is
        ``"full"`` / ``"partial"`` / ``"none"``. The status is what drives the
        ranked-doc walk and the recorded evidence, so it is sanity-checked here:
        a claimed answer with no supporting chunk is downgraded to ``"none"``.
        """
        shown = chunks[:_SUMMARY_CHUNKS]
        if not shown:
            return "none", "", []
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

        status = str(parsed.get("status") or "").strip().lower()
        if status not in ("full", "partial", "none"):
            status = "full" if relevant else "none"  # reply omitted/garbled the status
        if not relevant:
            status = "none"  # nothing can be "answered" without a supporting chunk
        return status, sub_answer, relevant

    async def _answer_by_batches(question: str, doc_id: str, doc_nm: str) -> tuple[str, str, list[dict]]:
        """Scan ONE document in overlapping batches until the sub-question is answered.

        The document is streamed in document order, ``_DOC_BATCH_SIZE`` chunks at a
        time with ``_DOC_BATCH_OVERLAP`` chunks shared between consecutive batches,
        so an answer straddling a boundary is never split. Stops at the first batch
        that yields a relevant chunk, or after ``_MAX_DOC_BATCHES``.
        """
        resolved = tools._resolve_doc_tenant(doc_id)
        if not resolved:
            return "none", "", []
        kb_id, tenant_id = resolved
        from rag.svr.task_executor_refactor.task_handler import TaskHandler

        _LOG.info("[Investigate] %s", doc_nm)
        best_partial: tuple[str, str, list[dict]] | None = None
        batch_count = 0
        async for loaded_batch in TaskHandler._load_chunks_for_doc(
            tenant_id,
            kb_id,
            doc_id,
            batch_size=_DOC_BATCH_SIZE,
            overlap=_DOC_BATCH_OVERLAP,
        ):
            if batch_count >= _MAX_DOC_BATCHES:
                break
            batch_count += 1
            status, summary, relevant = await _summarize_chunks(question, loaded_batch)
            if status == "full":
                _LOG.info("[Investigate] FULLY answered from '%s' batch %d (%d chunk(s)): %s", doc_nm, batch_count, len(loaded_batch), _snip(question))
                return status, summary, relevant
            if status == "partial" and best_partial is None:
                best_partial = (status, summary, relevant)  # hold it, keep scanning for a full answer
        if best_partial:
            _LOG.info("[Investigate] only PARTIAL in '%s' after %d batch(es): %s", doc_nm, batch_count, _snip(question))
            return best_partial
        _LOG.info("[Investigate] '%s' exhausted after %d batch(es): %s", doc_nm, batch_count, _snip(question))
        return "none", "", []

    # ── Node 1: analyze — decompose Q into independent sub-questions (LLM) ──
    async def analyze_node(state: KwV3State) -> dict:
        q = state.get("question") or ""
        _LOG.info("[Analyze] Planning round 1 — simple, independent sub-questions for: %s", _snip(q))
        parsed = await _llm_json(_ANALYZE_SYSTEM, f"Question:\n{q}\n\nOutput JSON:")
        subqs = _mk_subqs(parsed.get("subquestions"), 0, limit=_MAX_ANALYZE_SUBQUESTIONS) or _mk_subqs([{"question": q}], 0, limit=_MAX_ANALYZE_SUBQUESTIONS)
        for sq in subqs:
            _LOG.info("[Sub-Q]: %s", sq["question"])
        return {"subquestions": subqs, "iteration": 0, "pool": [], "evidences": []}

    # ── Node 2: keywords — search-friendly keywords + synonyms per sub-question,
    #    including date-format variants (batched LLM call) ──
    async def keywords_node(state: KwV3State) -> dict:
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

    # ── Node 3: rank the top-N candidate docs per sub-question (no LLM) ──
    async def retrieve_docs_node(state: KwV3State) -> dict:
        subqs = state.get("subquestions") or []
        scoped = tools.scoped_doc_ids(None) if hasattr(tools, "scoped_doc_ids") else None

        async def _one(sq: dict) -> list[dict]:
            try:
                kbinfos = await _retrieve(sq["keywords"] or sq["question"], _DOC_TOP_N, doc_ids=scoped)
            except Exception:
                _LOG.exception("[Retrieve docs] failed for sub-q %s", sq["id"])
                return []
            best: dict[str, float] = {}
            names: dict[str, str] = {}
            first_seen: dict[str, int] = {}
            for i, ck in enumerate(kbinfos.get("chunks") or []):
                did = ck.get("doc_id")
                if not did:
                    continue
                # Retrieval is relevance-ordered; use similarity when present and
                # fall back to a rank decay so a doc's BEST chunk sets its score.
                try:
                    score = float(ck.get("similarity") or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                if score <= 0.0:
                    score = 1.0 / (i + 1.0)
                if did not in best or score > best[did]:
                    best[did] = score
                names.setdefault(did, ck.get("docnm_kwd", "") or "")
                first_seen.setdefault(did, i)
            ranked = sorted(best.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))[:_DOCS_PER_SUBQ]
            return [{"doc_id": did, "doc_nm": names.get(did, "")} for did, _ in ranked]

        per_sq = await asyncio.gather(*[_one(sq) for sq in subqs])
        for sq, ranks in zip(subqs, per_sq):
            sq["ranks"] = ranks
            _LOG.info("[Retrieve docs] %s -> %s", _snip(sq["question"]), [d["doc_nm"] or d["doc_id"] for d in ranks] or "none")
        return {"subquestions": subqs}

    # ── Node 4: investigate — walk the ranked docs until answered (LLM) ──
    #    Merges v2's retrieve_chunks + summarize. Sub-questions run in parallel;
    #    the rank walk inside one sub-question is sequential so the early break
    #    actually saves retrievals and LLM calls.
    async def investigate_node(state: KwV3State) -> dict:
        subqs = state.get("subquestions") or []
        it = state.get("iteration", 0)

        async def _cheap_path(sq: dict, doc_id: str) -> tuple[str, str, list[dict]]:
            """Keyword-retrieved chunks from one doc, summarized."""
            try:
                kbinfos = await _retrieve(sq["keywords"] or sq["question"], _CHUNKS_PER_DOC, doc_ids=[doc_id])
                chunks = kbinfos.get("chunks") or []
            except Exception:
                _LOG.exception("[Investigate] chunk retrieval failed for sub-q %s doc %s", sq["id"], doc_id)
                return "none", "", []
            if enable_snippets:
                chunks = _narrow_by_keywords(chunks, sq["keywords"])
            if not chunks:
                return "none", "", []
            return await _summarize_chunks(sq["question"], chunks)

        async def _one(sq: dict) -> dict:
            """Walk this sub-question's ranked docs until it is FULLY answered.

            Only a ``full`` status stops the walk. A ``partial`` is held as a
            fallback and the next ranked document is still tried, so an on-topic
            document that misses the specific value can't mask a better one.
            """
            best_partial: dict | None = None

            def _result(status, summary, relevant, doc_nm, rank) -> dict:
                return {"status": status, "summary": summary, "relevant": relevant, "doc_nm": doc_nm, "rank": rank}

            for rank, doc in enumerate(sq.get("ranks") or [], start=1):
                doc_id = doc.get("doc_id")
                doc_nm = doc.get("doc_nm") or doc_id or ""
                if not doc_id:
                    continue

                # (1) cheap path — this doc's keyword-retrieved chunks.
                status, summary, relevant = await _cheap_path(sq, doc_id)
                if status == "full":
                    _LOG.info("[Investigate] FULLY answered from '%s' (rank %d, retrieved chunks): %s", doc_nm, rank, _snip(sq["question"]))
                    return _result(status, summary, relevant, doc_nm, rank)
                if status == "partial" and best_partial is None:
                    best_partial = _result(status, summary, relevant, doc_nm, rank)

                # (2) deep path — scan this whole doc in overlapping batches.
                status, summary, relevant = await _answer_by_batches(sq["question"], doc_id, doc_nm)
                if status == "full":
                    return _result(status, summary, relevant, doc_nm, rank)
                if status == "partial" and best_partial is None:
                    best_partial = _result(status, summary, relevant, doc_nm, rank)

                # (3) not fully answered here — try the next ranked doc.
            if best_partial:
                _LOG.info("[Investigate] only PARTIAL after %d ranked doc(s): %s", len(sq.get("ranks") or []), _snip(sq["question"]))
                return best_partial
            _LOG.info("[Investigate] unanswered after %d ranked doc(s): %s", len(sq.get("ranks") or []), _snip(sq["question"]))
            return _result("none", "", [], "", 0)

        results = await asyncio.gather(*[_one(sq) for sq in subqs])

        # Record every sub-question ATTEMPTED this round — answered or not — so
        # think can never re-propose one that already came up empty.
        asked = list(state.get("asked") or [])
        asked_keys = {_norm(a) for a in asked}
        for sq in subqs:
            key = _norm(sq["question"])
            if key and key not in asked_keys:
                asked_keys.add(key)
                asked.append(sq["question"])

        evidences = list(state.get("evidences") or [])
        pool = list(state.get("pool") or [])
        seen = {_chunk_id(c) for c in pool}
        full_n = partial_n = 0
        for sq, r in zip(subqs, results):
            if r["status"] == "none" or not r["relevant"]:
                # Unanswered — record nothing, so an empty "No useful evidence."
                # never pollutes think's input or the final answer.
                continue
            if r["status"] == "full":
                full_n += 1
            else:
                partial_n += 1
            for c in r["relevant"]:
                cid = _chunk_id(c)
                if cid not in seen:
                    seen.add(cid)
                    pool.append(c)
            evidences.append(
                {
                    "iteration": it,
                    "subq": sq["question"],
                    "status": r["status"],  # "full" | "partial" — the real record
                    "summary": r["summary"],
                    "chunk_ids": [_chunk_id(c) for c in r["relevant"]],
                    "doc_nm": r["doc_nm"],
                }
            )
        _LOG.info(
            "[Investigate] iteration %d: %d full, %d partial, %d unanswered of %d sub-q(s); evidence=%d, pool=%d chunk(s).",
            it,
            full_n,
            partial_n,
            len(subqs) - full_n - partial_n,
            len(subqs),
            len(evidences),
            len(pool),
        )
        return {"subquestions": subqs, "evidences": evidences, "pool": pool, "asked": asked}

    # ── Node 5: think — two focused LLM steps (LLM) ──
    #    (1) is the evidence sufficient? (2) if not, what should the next round ask?
    #    Splitting them keeps the sufficiency judgment from being distracted by the
    #    planning task, and skips the planning call entirely once we are done.
    async def think_node(state: KwV3State) -> dict:
        evidences = state.get("evidences") or []
        asked = state.get("asked") or []
        question = state.get("question") or ""
        it = state.get("iteration", 0) + 1

        ev_text = "\n\n".join(f"(round {e.get('iteration', 0)}, {e.get('status', 'full').upper()}) {e['subq']}\n-> {e['summary']}" for e in evidences) or "(nothing discovered yet)"

        # ── Step 1: sufficiency check over ALL evidence ──
        verdict = await _llm_json(
            _SUFFICIENCY_SYSTEM,
            f"Original question:\n{question}\n\nFacts discovered so far:\n{ev_text}\n\nOutput JSON:",
        )
        sufficient = bool(verdict.get("sufficient"))
        missing = str(verdict.get("missing") or "").strip()

        # ── Step 2: already sufficient → stop here, no planning call needed ──
        if sufficient:
            _LOG.info("[Think] iteration %d → sufficient=True; answering now.", it)
            return {"sufficient": True, "subquestions": [], "iteration": it}

        # ── Step 3: not sufficient → plan the next round from the asked records ──
        asked_keys = {_norm(a) for a in asked}
        status_of = {_norm(e["subq"]): e.get("status", "full") for e in evidences}

        parts = [
            f"Original question:\n{question}",
            f"Facts discovered so far:\n{ev_text}",
            f"Still missing:\n{missing or '(not stated)'}",
        ]
        if asked:
            # Show outcomes too: a question that already came up empty should make the
            # model pivot to a different angle, not rephrase the same failed search.
            _label = {"full": "", "partial": "   [PARTIAL — ask a NEW question for the missing detail]"}
            tried = "\n".join(f"- {a}" + _label.get(status_of.get(_norm(a), ""), "   [searched, no evidence found]") for a in asked)
            parts.append(f"Sub-questions ALREADY asked (never repeat or rephrase these):\n{tried}")
        parts.append("Output JSON:")

        parsed = await _llm_json(_NEXT_SUBQ_SYSTEM, "\n\n".join(parts))

        # Hard guard: drop anything already attempted in ANY previous round.
        fresh = []
        for x in parsed.get("next_subquestions") or []:
            if not isinstance(x, dict):
                continue
            key = _norm(x.get("question", ""))
            if not key or key in asked_keys:
                continue
            asked_keys.add(key)  # also dedups within this batch
            fresh.append(x)
        next_subqs = _mk_subqs(fresh, it)
        if not next_subqs:
            # No new angle left — route to answer with whatever was gathered.
            _LOG.info("[Think] iteration %d → not sufficient but no new sub-question available; answering with what we have. Missing: %s", it, _snip(missing))
        else:
            _LOG.info("[Think] iteration %d → not sufficient; %d new sub-question(s). Missing: %s", it, len(next_subqs), _snip(missing))
        return {"sufficient": False, "subquestions": next_subqs, "iteration": it}

    # ── Node 6: brief, cited answer — composed from the per-sub-question
    #    summaries (evidences), NOT the raw chunks. Each summary is tagged with the
    #    [ID:n] markers of the chunks that support it; the pool stays as the
    #    citation backing (kbinfos) so those markers resolve to real sources. ──
    async def answer_node(state: KwV3State) -> dict:
        evidences = [e for e in (state.get("evidences") or []) if e.get("summary")]
        if not evidences:
            msg = "I don't have enough information based on the available sources."
            token_queue.put_nowait(msg)
            return {"final_answer": msg}

        pool = state.get("pool") or []
        tools.kbinfos = {"chunks": pool, "doc_aggs": _doc_aggs_from(pool)}
        id_of = {_chunk_id(c): i for i, c in enumerate(pool)}

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

    def _route_after_think(state: KwV3State) -> str:
        if state.get("sufficient") or state.get("iteration", 0) >= max_iterations or not state.get("subquestions"):
            return "answer"
        return "keywords"

    g = StateGraph(KwV3State)
    g.add_node("analyze", analyze_node)
    g.add_node("keywords", keywords_node)
    g.add_node("retrieve_docs", retrieve_docs_node)
    g.add_node("investigate", investigate_node)
    g.add_node("think", think_node)
    g.add_node("answer", answer_node)

    g.add_edge(START, "analyze")
    g.add_edge("analyze", "keywords")
    g.add_edge("keywords", "retrieve_docs")
    g.add_edge("retrieve_docs", "investigate")
    g.add_edge("investigate", "think")
    g.add_conditional_edges("think", _route_after_think, {"keywords": "keywords", "answer": "answer"})
    g.add_edge("answer", END)

    return g.compile()


async def run_keyword_agentic_rag_v3(
    tools,
    messages: list,
    max_iterations: int = 4,
    enable_snippets: bool = True,
    gen_conf: dict | None = None,
):
    """Drive the v3 graph, yielding answer-token strings."""
    question = ""
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content"):
            question = m["content"]
            break

    token_queue: asyncio.Queue = asyncio.Queue()
    graph = build_keyword_agentic_graph_v3(tools, token_queue, gen_conf=gen_conf, max_iterations=max_iterations, enable_snippets=enable_snippets)
    _SENTINEL = object()
    holder: dict[str, Any] = {}

    async def _drive():
        try:
            holder["state"] = await graph.ainvoke(
                {"question": question, "max_iterations": max_iterations, "iteration": 0, "pool": [], "evidences": [], "asked": []},
                {"recursion_limit": max(25, max_iterations * 6 + 10)},
            )
        except Exception:
            _LOG.exception("run_keyword_agentic_rag_v3: graph execution failed")
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
