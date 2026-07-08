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

"""Agentic-RAG orchestration as a LangGraph state machine.

The graph wraps the existing :class:`rag.advanced_rag.agentic_rag.RAGTools`
methods — it does NOT replace them. Each node calls one or more of those
``@tool`` methods; LangGraph handles the plan → act → observe → replan
loop and, crucially, *checkpoints state at every node boundary* so a
mid-run failure resumes from the last committed node instead of
re-running the whole procedure (per-turn resume, keyed by
``f"{conv_id}:{turn}"``).

Flow (map-reduce over sub-questions for the ``search_kb`` intent):

    START → plan
    plan ─(list[Send])─▶ subq_worker × N    (search_kb: one Send per pending
                                             sub-question, run in PARALLEL)
         ─▶ answer                          (intent == "answer" | budget spent)
         ─▶ select_docs                     (compare)
         ─▶ web_search                      (web_search)
         ─▶ summarize                       (summarize)
         ─▶ structured                      (structured)
         ─▶ others                          (others)

    subq_worker → plan          (B2 loop: replan after each parallel batch;
                                 the planner may emit follow-up sub-questions,
                                 already-answered ones filtered by
                                 ``answered_subqs``)
    select_docs → load_hints → compare → plan
    web_search / summarize / structured → plan
    others → END
    answer → END

Each ``subq_worker`` retrieves into its OWN ``sink`` (via
``RAGTools.retrieve_into``) and composes a per-sub-question sub-answer,
so N sub-questions run concurrently with correct attribution. The
``answer`` node reduces: it merges every worker's chunks into one
citation pool and synthesises a single concise answer to the original
formalized question from the (sub-question, sub-answer) pairs.

The ``sub_results`` / ``answered_subqs`` state fields use an
``operator.add`` reducer so parallel workers append race-free.
LangGraph checkpoints per superstep, so a crash in one worker resumes
re-running ONLY that worker; the others' committed results survive.

Two orthogonal output channels reach the caller through one async queue:
  * ``{"status": <node>}`` frames — cheap step indicators, safe to replay.
  * ``{"answer": <token>}`` frames — streamed by ``answer`` or ``others``
    nodes (the N sub-answers are computed inside workers, not streamed).

LangGraph / langgraph-checkpoint-redis are optional deps: this module is
only imported from the feature-flagged entry point, so the app still
starts without them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from copy import deepcopy
import operator
from typing import Annotated, AsyncIterator, TypedDict

from api.db.services.dialog_service import _extract_visible_answer, _stream_with_think_delta
import json_repair


# --------------------------------------------------------------------
# State
# --------------------------------------------------------------------


class SubResult(TypedDict, total=False):
    """One sub-question's isolated retrieval + sub-answer.

    Produced by a ``subq_worker`` invocation and appended (via the
    ``operator.add`` reducer on ``AgentState.sub_results``) so parallel
    workers concatenate their outputs without racing.
    """
    sub_question: str
    sub_answer: str
    chunks: list          # this sub-question's OWN retrieved chunks
    doc_aggs: list


class AgentState(TypedDict, total=False):
    # Inputs (set once at START)
    messages: list[dict]          # [{role, content}, ...]
    dialog_system_prompt: str
    max_iterations: int

    # Planner outputs
    formalized_question: str
    intent: str                   # answer|search_kb|web_search|summarize|compare|structured|others
    sub_questions: list[str]      # current desired set (last-write-wins; plan may grow it)
    iteration: int

    # Map-reduce over sub-questions (search_kb intent only).
    # Both use ``operator.add`` so N parallel workers append concurrently.
    sub_results: Annotated[list[SubResult], operator.add]
    answered_subqs: Annotated[list[str], operator.add]

    # Retrieval progress (single-shot intents: compare/summarize/web/structured)
    selected_doc_ids: list[str]
    doc_compiled_hints: dict[str, str]     # doc_id → tree/graph outline
    dataset_hint: dict[str, str]           # {skill_outline, dataset_nav}
    # Cumulative citation pool snapshot. For single-shot intents it mirrors
    # RAGTools.kbinfos; for the fan-out path the ``answer`` node rebuilds it
    # by merging every worker's ``chunks`` so a resume never re-retrieves.
    kbinfos: dict[str, list]

    # Final
    final_answer: str

    # Diagnostics
    node_errors: list[dict]


# Intent → downstream routing KEY (resolved to a node / Send list in
# ``route_from_plan``). ``search_kb`` fans out; the rest are single-shot.
_SINGLE_SHOT_ROUTES = {
    "answer": "answer",
    "others": "others",
    "compare": "select_docs",
    "web_search": "web_search",
    "summarize": "summarize",
    "structured": "structured",
}
_VALID_INTENTS = set(_SINGLE_SHOT_ROUTES) | {"search_kb"}
# Path-map for the plan conditional edge. ``search_kb`` returns a Send list
# (bypasses the map); everything else returns one of these keys.
_PLAN_PATH_MAP = {
    "answer": "answer",
    "others": "others",
    "select_docs": "select_docs",
    "web_search": "web_search",
    "summarize": "summarize",
    "structured": "structured",
}


def _messages_to_transcript(messages: list[dict]) -> list[str]:
    """Render ``[{role, content}]`` into the ``["User: ...", ...]`` shape
    ``RAGTools.formalize_question`` expects."""
    out: list[str] = []
    for m in messages or []:
        role = (m.get("role") or "user").capitalize()
        content = m.get("content") or ""
        if content:
            out.append(f"{role}: {content}")
    return out


def _latest_user_text(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if (m.get("role") or "") == "user" and m.get("content"):
            return m["content"]
    return ""


def _parse_json_object(text: str) -> dict:
    """Best-effort JSON-object parse from an LLM answer (strips think tags
    and code fences)."""
    if isinstance(text, tuple):
        text = text[0]
    cleaned = re.sub(r"^.*</think>", "", text or "", flags=re.DOTALL)
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", cleaned).strip()
    try:
        obj = json_repair.loads(cleaned)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


# --------------------------------------------------------------------
# Graph builder
# --------------------------------------------------------------------


def build_graph(tools, checkpointer, token_queue: "asyncio.Queue"):
    """Compile the agentic-RAG graph.

    :param tools: a live ``RAGTools`` instance (its ``@tool`` methods are
        the node bodies; not serializable, so it is captured in the node
        closures rather than stored in graph state).
    :param checkpointer: a LangGraph checkpointer (Redis) for per-turn
        resume.
    :param token_queue: async queue the ``answer`` node streams tokens
        into. Lives outside checkpointed state.
    :returns: a compiled ``StateGraph``.
    """
    from langgraph.graph import StateGraph, START, END

    chat_mdl = tools.chat_mdl

    # ----- plan -----------------------------------------------------
    async def plan_node(state: AgentState) -> dict:
        messages = state.get("messages") or []
        iteration = int(state.get("iteration") or 0)
        max_iters = int(state.get("max_iterations") or 4)

        # Formalize once (iteration 0). ``formalize_question`` resolves
        # follow-up references against the full transcript (spec 1.1).
        formalized = state.get("formalized_question") or ""
        if not formalized:
            try:
                formalized = await tools.formalize_question(_messages_to_transcript(messages))
            except Exception:
                logging.exception("plan_node: formalize failed; using latest message")
                formalized = _latest_user_text(messages)
            formalized = formalized or _latest_user_text(messages)

        # Loop budget exhausted → force an answer from what we have.
        if iteration >= max_iters:
            return {"formalized_question": formalized, "intent": "answer",
                    "iteration": iteration + 1}

        answered = state.get("answered_subqs") or []
        sub_results = state.get("sub_results") or []
        have_evidence = bool(sub_results) or bool((state.get("kbinfos") or {}).get("chunks"))

        # B2 re-plan: when sub-question attempts already exist, summarise them
        # for the planner so it can decide "enough → answer" or "need more
        # fine-grained sub-questions". On the first pass this block is empty.
        answered_digest = ""
        if sub_results:
            def _sub_answer_status(answer: str) -> str:
                answer_lc = (answer or "").strip().lower()
                if not answer_lc:
                    return "not_answered"
                if answer_lc.startswith("not_answered"):
                    return "not_answered"
                if "doesn't answer" in answer_lc or "does not answer" in answer_lc:
                    return "not_answered"
                if "insufficient" in answer_lc or "not enough evidence" in answer_lc:
                    return "not_answered"
                return "answered"

            answered_digest = "\n".join(
                f"- Q: {r.get('sub_question','')}\n"
                f"  Status: {_sub_answer_status(r.get('sub_answer') or '')}\n"
                f"  A: {((r.get('sub_answer') or '').strip() or 'NOT_ANSWERED: no answer was produced')[:500]}"
                for r in sub_results
            )

        planner_system = (
            "You are the planner of a smart agent. Decide the SINGLE next "
            "step for the user's request below. Choose ONE intent:\n"
            "- search_kb: retrieve chunks from the knowledge base (this fans out into parallel sub-questions)\n"
            "- web_search: search the public web (only if KB won't have it)\n"
            "- summarize: the user explicitly asked to summarize one document\n"
            "- compare: the user asked to contrast/diff specific documents\n"
            "- structured: the question is an aggregate/filter over tabular data\n"
            "- others: ordinary conversation or a request that does not need any tool above\n"
            "- answer: enough evidence already gathered — compose the answer\n\n"
            "When you choose search_kb, break the question into the minimal set "
            "of independent sub-questions needed to answer it fully (one element "
            "if it's a single need). The attempted sub-question block may include "
            "failed or partial attempts. If an attempt is empty, marked "
            "NOT_ANSWERED/not_answered, says the evidence is insufficient, or "
            "only partially answers its question, do NOT choose answer just "
            "because it was attempted. Instead choose search_kb and decompose "
            "that unresolved or partial sub-question into smaller, more concrete "
            "follow-up sub-questions. List ONLY NEW narrower questions; do not "
            "repeat an already attempted question verbatim. Choose answer only "
            "when the attempted answers fully cover the original request.\n"
            "Output ONLY a JSON object: "
            '{"intent": "...", "sub_questions": ["..."]}.'
        )
        planner_user = (
            f"Question: {formalized}\n"
            f"Iteration: {iteration + 1}/{max_iters}\n"
            + (f"\nAttempted sub-questions and answers:\n{answered_digest}\n" if answered_digest else "")
            + "\nNext step (JSON):"
        )
        try:
            raw = await chat_mdl.async_chat(
                system=planner_system,
                history=[{"role": "user", "content": planner_user}],
                gen_conf={"temperature": 0.1},
            )
        except Exception:
            logging.exception("plan_node: planner LLM failed; defaulting to search_kb")
            raw = ""
        plan = _parse_json_object(raw)
        intent = str(plan.get("intent") or "").strip()
        if intent not in _VALID_INTENTS:
            intent = "answer" if have_evidence else "search_kb"

        sub_questions = []#list(state.get("sub_questions") or [])
        planner_subs = [s for s in (plan.get("sub_questions") or []) if isinstance(s, str) and s.strip()]
        if intent == "search_kb":
            if not sub_questions and not planner_subs:
                # First pass, planner gave none → decompose the formalized Q.
                try:
                    planner_subs = await tools.decompose_question(formalized)
                except Exception:
                    planner_subs = [formalized]
            for sq in planner_subs:
                if sq not in sub_questions:
                    sub_questions.append(sq)
            if not sub_questions:
                sub_questions = [formalized]

        logging.info(
            f"[PLAN{iteration+1}] -> {intent}: {formalized} -> "
            + "/".join(sub_questions)
        )
        return {
            "formalized_question": formalized,
            "intent": intent,
            "sub_questions": sub_questions,
            "iteration": iteration + 1,
        }

    def route_from_plan(state: AgentState):
        """Route out of ``plan``.

        For ``search_kb`` with un-answered sub-questions, return a list of
        ``Send`` objects — one per pending sub-question — so LangGraph fans
        them out to parallel ``subq_worker`` invocations. Everything else
        returns a single string key resolved by ``_PLAN_PATH_MAP``.
        """
        from langgraph.types import Send

        intent = state.get("intent") or "answer"
        if intent == "search_kb":
            answered = set(state.get("answered_subqs") or [])
            pending = [sq for sq in (state.get("sub_questions") or []) if sq not in answered]
            if pending:
                fq = state.get("formalized_question") or ""
                dataset_hint = state.get("dataset_hint") or {}
                return [
                    Send("subq_worker", {
                        "sub_question": sq,
                        "formalized_question": fq,
                        "dataset_hint": dataset_hint,
                    })
                    for sq in pending
                ]
            # Nothing new to run — synthesise what we have.
            return "answer"
        return _SINGLE_SHOT_ROUTES.get(intent, "answer")

    # ----- select_docs ---------------------------------------------
    async def select_docs_node(state: AgentState) -> dict:
        question = state.get("formalized_question") or ""
        # Spec 4.1: if the KB has a skill tree or dataset nav, let the LLM
        # pick docs from that hierarchical/flat markdown. Otherwise 4.2:
        # fall back to title-based ``select_documents`` (doc_aggs-adjacent).
        from rag.advanced_rag import agentic_rag_hints as hints

        dataset_hint = state.get("dataset_hint") or {}
        if not dataset_hint:
            merged = {"skill_outline": "", "dataset_nav": ""}
            for tenant_id, kb in zip(tools.tenant_ids, tools.kbs):
                h = await hints.gather_dataset_hint(tenant_id, kb.id)
                merged["skill_outline"] = merged["skill_outline"] or h.get("skill_outline", "")
                merged["dataset_nav"] = merged["dataset_nav"] or h.get("dataset_nav", "")
            dataset_hint = merged

        selected: list[str] = []
        hint_md = dataset_hint.get("skill_outline") or dataset_hint.get("dataset_nav") or ""
        if hint_md:
            selected = await _select_docs_from_hint(question, hint_md)

        if not selected:
            try:
                picked = await tools.select_documents(question)
                if isinstance(picked, list):
                    selected = [d for d in picked if isinstance(d, str)]
            except Exception:
                logging.exception("select_docs_node: select_documents failed")

        logging.info(f"[SELECT DOC] -> {selected}: {dataset_hint}")
        return {"selected_doc_ids": selected, "dataset_hint": dataset_hint}

    async def _select_docs_from_hint(question: str, hint_md: str) -> list[str]:
        """Ask the LLM to pick doc ids from the dataset hint markdown.

        The nav/skill markdown carries ``**<doc_id>**`` bullets; we let the
        LLM return the ids it judges relevant, then defensively keep only
        ids that actually appear in the markdown.
        """
        system = (
            "You are given a knowledge base's document navigation outline. "
            "Pick the document IDs whose entries are relevant to the question. "
            "Use ONLY IDs that appear in the outline. Output ONLY a JSON array "
            "of IDs, e.g. [\"abc\",\"def\"]. If none are relevant, output []."
        )
        user = f"Question:\n{question}\n\nOutline:\n{hint_md}\n\nRelevant document IDs (JSON array):"
        try:
            raw = await chat_mdl.async_chat(
                system=system,
                history=[{"role": "user", "content": user}],
                gen_conf={"temperature": 0.1},
            )
        except Exception:
            logging.exception("_select_docs_from_hint: LLM failed")
            return []
        if isinstance(raw, tuple):
            raw = raw[0]
        cleaned = re.sub(r"^.*</think>", "", raw or "", flags=re.DOTALL)
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", cleaned).strip()
        try:
            ids = json_repair.loads(cleaned)
        except Exception:
            return []
        if not isinstance(ids, list):
            return []
        logging.info(f"[SELECT DOC BY HINT] -> {ids}")
        # Keep only ids that literally occur in the outline text.
        return [d for d in ids if isinstance(d, str) and d and d in hint_md]

    # ----- load_hints ----------------------------------------------
    async def load_hints_node(state: AgentState) -> dict:
        from rag.advanced_rag.agentic_rag_hints import gather_doc_hint

        doc_ids = state.get("selected_doc_ids") or []
        hints: dict[str, str] = {}
        for doc_id in doc_ids:
            pair = None
            try:
                from common.misc_utils import thread_pool_exec
                pair = await thread_pool_exec(tools._resolve_doc_tenant, doc_id)
            except Exception:
                pair = None
            if not pair:
                continue
            kb_id, tenant_id = pair
            md = await gather_doc_hint(tenant_id, kb_id, doc_id)
            if md:
                hints[doc_id] = md

        logging.info(f"[LOAD HINT] -> {doc_ids}")
        return {"doc_compiled_hints": hints}

    # ----- subq_worker (map) ---------------------------------------
    async def subq_worker(state: dict) -> dict:
        """Process ONE sub-question end to end, in isolation.

        Runs on a private ``Send`` payload (``sub_question`` +
        ``formalized_question`` + ``dataset_hint``), retrieves into its OWN
        ``sink`` (never the shared ``tools.kbinfos``), composes a
        sub-answer, and appends a :class:`SubResult`. N of these run
        concurrently; the ``operator.add`` reducers on ``sub_results`` /
        ``answered_subqs`` concatenate their outputs race-free.

        Note: ``tools.chat_mdl`` is shared across parallel workers. The LLM
        HTTP calls are stateless; only token-usage accounting may be
        slightly imprecise under concurrency. Retrieval isolation (the part
        that matters for correctness) is guaranteed by the per-worker sink.
        """
        sub_q = state.get("sub_question") or ""
        dataset_hint = state.get("dataset_hint") or {}

        # 1. Doc selection scoped to THIS sub-question.
        selected: list[str] = []
        hint_md = dataset_hint.get("skill_outline") or dataset_hint.get("dataset_nav") or ""
        if hint_md:
            selected = await _select_docs_from_hint(sub_q, hint_md)
        if not selected:
            try:
                picked = await tools.select_documents(sub_q)
                if isinstance(picked, list):
                    selected = [d for d in picked if isinstance(d, str)]
            except Exception:
                logging.exception("subq_worker: select_documents failed for %r", sub_q)

        # 2. Retrieve into a private pool (concurrency-safe).
        sink: dict[str, list] = {"chunks": [], "doc_aggs": []}
        try:
            await tools.retrieve_into(
                question=sub_q, keywords=sub_q,
                docid_scope=selected or None, sink=sink,
            )
        except Exception:
            logging.exception("subq_worker: retrieve_into failed for %r", sub_q)

        # 3. Compose this sub-question's own answer from its own chunks.
        sub_answer = await _compose_sub_answer(sub_q, sink)

        logging.info(f"[SUBQ] {sub_q} -> {len(sink.get('chunks', []))} chunks")
        return {
            "sub_results": [{
                "sub_question": sub_q,
                "sub_answer": sub_answer,
                "chunks": sink.get("chunks", []),
                "doc_aggs": sink.get("doc_aggs", []),
            }],
            "answered_subqs": [sub_q],
        }

    async def _compose_sub_answer(sub_q: str, sink: dict) -> str:
        from rag.prompts.generator import kb_prompt

        try:
            ctx = kb_prompt(sink, chat_mdl.max_length, 0)
        except Exception:
            ctx = []
        ctx_text = "\n\n".join(ctx) if isinstance(ctx, list) else str(ctx)
        if not ctx_text.strip():
            return "NOT_ANSWERED: no relevant evidence was retrieved for this sub-question."
        system = (
            "Answer the single question using ONLY the evidence below. Be "
            "brief and factual — this is an intermediate result that will be "
            "synthesised with others. If the evidence does not directly answer "
            "the question, return exactly `NOT_ANSWERED: <short reason>` so "
            "the planner can decompose the question into smaller follow-ups. "
            "Answer in the question's language.\n\n"
            f"# Evidence\n{ctx_text}"
        )
        try:
            ans = await chat_mdl.async_chat(
                system=system,
                history=[{"role": "user", "content": f"Question: {sub_q}"}],
                gen_conf={"temperature": 0.2},
            )
        except Exception:
            logging.exception("_compose_sub_answer: LLM failed for %r", sub_q)
            return ""
        if isinstance(ans, tuple):
            ans = ans[0]
        return (ans or "").strip()

    # ----- web_search ----------------------------------------------
    async def web_search_node(state: AgentState) -> dict:
        question = state.get("formalized_question") or ""
        try:
            await tools.web_search(question)
        except Exception:
            logging.exception("web_search_node: web_search failed")
        logging.info(f"[WEB SEARCH] -> {question}")
        return {"kbinfos": deepcopy(tools.kbinfos)}

    # ----- summarize -----------------------------------------------
    async def summarize_node(state: AgentState) -> dict:
        doc_ids = state.get("selected_doc_ids") or []
        if not doc_ids:
            # Try to pick the single doc the user meant.
            try:
                picked = await tools.select_documents(state.get("formalized_question") or "")
                if isinstance(picked, list):
                    doc_ids = [d for d in picked if isinstance(d, str)]
            except Exception:
                doc_ids = []
        if doc_ids:
            try:
                await tools.summarize_document(doc_ids[0])
            except Exception:
                logging.exception("summarize_node: summarize_document failed")
        logging.info(f"[SUMMARIZE] -> {doc_ids}")
        return {"selected_doc_ids": doc_ids, "kbinfos": deepcopy(tools.kbinfos)}

    # ----- compare -------------------------------------------------
    async def compare_node(state: AgentState) -> dict:
        doc_ids = state.get("selected_doc_ids") or []
        if len(doc_ids) >= 2:
            try:
                await tools.compare_documents(doc_ids)
            except Exception:
                logging.exception("compare_node: compare_documents failed")
        logging.info(f"[COMPARE] -> {doc_ids}")
        return {"kbinfos": deepcopy(tools.kbinfos)}

    # ----- structured ----------------------------------------------
    async def structured_node(state: AgentState) -> dict:
        question = state.get("formalized_question") or ""
        try:
            await tools.search_structured_data(question)
        except Exception:
            logging.exception("structured_node: search_structured_data failed")
        logging.info(f"[STRUCTURED] -> {question}")
        return {"kbinfos": deepcopy(tools.kbinfos)}

    # ----- others ---------------------------------------------------
    async def others_node(state: AgentState) -> dict:
        """Handle ordinary LLM requests that do not need RAG tools."""
        messages = [m for m in (state.get("messages") or []) if m.get("role") != "system"]
        if not messages:
            messages = [{"role": "user", "content": state.get("formalized_question") or ""}]

        try:
            tools.kbinfos = {"chunks": [], "doc_aggs": []}
        except Exception:
            pass

        system = state.get("dialog_system_prompt") or "You are a helpful assistant."
        full_answer = ""
        stream_iter = chat_mdl.async_chat_streamly_delta(
            system=system,
            history=messages,
            gen_conf={"temperature": 0.3},
        )
        try:
            last_state = None
            async for kind, value, st in _stream_with_think_delta(stream_iter):
                last_state = st
                if kind == "marker":
                    flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
                    token_queue.put_nowait({"answer": "", "reference": {}, "audio_binary": None, "final": False, **flags})
                    continue
                token_queue.put_nowait({"answer": value})
            full_answer = _extract_visible_answer(last_state.full_text if last_state else "")
        except Exception:
            logging.exception("others_node: streaming failed")
            if not full_answer:
                full_answer = "I couldn't compose an answer due to an internal error."
                token_queue.put_nowait({"answer": full_answer})

        logging.info(f"[OTHERS] -> {full_answer[:200]}")
        return {"final_answer": full_answer, "kbinfos": {"chunks": [], "doc_aggs": []}}

    # ----- answer (reduce) -----------------------------------------
    def _merge_kbinfos(state: AgentState) -> dict:
        """Merge every sub-question's chunks + any single-shot kbinfos into
        one deduped citation pool, and publish it onto ``tools.kbinfos`` so
        the outer ``rag_agent`` can build references from it.

        Dedup is by chunk ``id`` (falling back to ``chunk_id``) preserving
        first-seen order so citation indices stay stable.
        """
        merged: dict[str, list] = {"chunks": [], "doc_aggs": []}
        seen_chunks: set = set()
        seen_docs: set = set()

        def _add(pool: dict) -> None:
            for c in (pool or {}).get("chunks", []) or []:
                cid = c.get("id") or c.get("chunk_id") or id(c)
                if cid in seen_chunks:
                    continue
                seen_chunks.add(cid)
                merged["chunks"].append(c)
            for d in (pool or {}).get("doc_aggs", []) or []:
                key = d.get("doc_id") or d.get("doc_name") or id(d)
                if key in seen_docs:
                    continue
                seen_docs.add(key)
                merged["doc_aggs"].append(d)

        for r in state.get("sub_results") or []:
            _add({"chunks": r.get("chunks", []), "doc_aggs": r.get("doc_aggs", [])})
        _add(state.get("kbinfos") or {})

        # Publish for the outer reference builder (single-shot node runs once,
        # answer runs once — no concurrency here).
        try:
            tools.kbinfos = deepcopy(merged)
        except Exception:
            pass
        return merged

    async def answer_node(state: AgentState) -> dict:
        from rag.prompts.generator import kb_prompt

        question = state.get("formalized_question") or _latest_user_text(state.get("messages") or [])
        kbinfos = _merge_kbinfos(state)

        try:
            context = kb_prompt(kbinfos, chat_mdl.max_length, 0)
        except Exception:
            context = []
        context_text = "\n\n".join(context) if isinstance(context, list) else str(context)

        # Gather the per-sub-question Q&A so the synthesis can weave them into
        # one concise, pithy answer to the ORIGINAL formalized question.
        sub_results = state.get("sub_results") or []
        subqa_block = ""
        if sub_results:
            subqa_block = "\n\n".join(
                f"### Sub-question: {r.get('sub_question','')}\n{(r.get('sub_answer') or '').strip()}"
                for r in sub_results if (r.get("sub_answer") or "").strip()
            )

        citation_rules = ""
        try:
            citation_rules = tools.get_citation_guidelines()
        except Exception:
            pass

        system = (
            "You are a RAG assistant composing a FINAL answer. You are given "
            "the original question, the intermediate answers to its "
            "sub-questions, and the underlying evidence chunks. Synthesise a "
            "SINGLE concise, pithy answer to the original question — weave the "
            "sub-answers together, resolve overlaps, and DO NOT just concatenate "
            "them. Answer in the user's language. If the evidence is "
            "insufficient, say so plainly. "
            # "Apply the citation rules verbatim, citing only chunks actually present in the evidence.\n\n"
            #f"# Citation rules\n{citation_rules}\n\n"
            + (f"# Sub-question answers\n{subqa_block}\n\n" if subqa_block else "")
            #+ f"# Evidence\n{context_text}"
        )
        user = f"Original question: {question}\n\nConcise final answer:"

        full_answer = ""
        stream_iter = chat_mdl.async_chat_streamly_delta(system=system, history=[{"role": "user", "content": user}], gen_conf={"temperature": 0.3})
        try:
            last_state = None
            async for kind, value, st in _stream_with_think_delta(stream_iter):
                last_state = st
                if kind == "marker":
                    flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
                    token_queue.put_nowait({"answer": "", "reference": {}, "audio_binary": None, "final": False, **flags})
                    continue
                token_queue.put_nowait({"answer": value})
            full_answer = _extract_visible_answer(last_state.full_text if last_state else "")
        except Exception:
            logging.exception("answer_node: streaming failed")
            if not full_answer:
                full_answer = "I couldn't compose an answer due to an internal error."
                token_queue.put_nowait({"answer": full_answer})

        logging.info(f"[ANSWER] -> {full_answer[:200]}")
        return {"final_answer": full_answer, "kbinfos": kbinfos}

    # ----- wire -----------------------------------------------------
    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("subq_worker", subq_worker)          # search_kb fan-out (map)
    g.add_node("select_docs", select_docs_node)     # compare path only
    g.add_node("load_hints", load_hints_node)        # compare path only
    g.add_node("web_search", web_search_node)
    g.add_node("summarize", summarize_node)
    g.add_node("compare", compare_node)
    g.add_node("structured", structured_node)
    g.add_node("others", others_node)
    g.add_node("answer", answer_node)

    g.add_edge(START, "plan")
    # ``route_from_plan`` returns either a list[Send] (search_kb fan-out) or
    # one of the path-map keys below. Send lists bypass the path-map.
    g.add_conditional_edges("plan", route_from_plan, _PLAN_PATH_MAP)

    # search_kb: every parallel worker loops back to plan (B2). The planner
    # re-evaluates once all workers of the batch have joined and either
    # answers or emits follow-up sub-questions (already-answered ones are
    # filtered by ``answered_subqs``).
    g.add_edge("subq_worker", "plan")

    # compare: select docs → load hints → compare → plan. (search_kb no
    # longer uses this chain; it goes through subq_worker.)
    g.add_edge("select_docs", "load_hints")
    g.add_edge("load_hints", "compare")
    g.add_edge("compare", "plan")

    # Other single-shot intents loop straight back to plan.
    g.add_edge("web_search", "plan")
    g.add_edge("summarize", "plan")
    g.add_edge("structured", "plan")

    g.add_edge("others", END)
    g.add_edge("answer", END)

    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------


async def run_agentic_rag(
    tools,
    messages: list[dict],
    thread_id: str,
    max_iterations: int = 4,
    dialog_system_prompt: str = "",
) -> AsyncIterator[dict]:
    """Drive the agentic-RAG graph, yielding SSE-ready frames.

    Yields dicts of the shape:
      * ``{"status": <node_name>}``  — step indicator
      * ``{"answer": <delta>}``      — streamed answer token(s)
      * ``{"error": <message>}``     — terminal error

    :param tools: a live ``RAGTools`` instance (already ``bind_tools``-ed).
    :param messages: conversation history ``[{role, content}, ...]``.
    :param thread_id: per-turn resume key (e.g. ``f"{conv_id}:{turn}"``).
    :param max_iterations: plan-loop budget.
    :param dialog_system_prompt: resolved dialog prompt for the direct LLM
        ``others`` path.
    """
    from rag.advanced_rag.agentic_rag_checkpoint import open_checkpointer

    token_queue: asyncio.Queue = asyncio.Queue()

    async def _drive(graph, config, init_state):
        try:
            async for update in graph.astream(init_state, config, stream_mode="updates"):
                # ``update`` is {node_name: partial_state}. Emit a status
                # frame per committed node so the UI can render progress.
                if isinstance(update, dict):
                    for node_name in update.keys():
                        token_queue.put_nowait({"status": node_name})
        except Exception as e:
            logging.exception("run_agentic_rag: graph drive failed")
            token_queue.put_nowait({"error": str(e)})
        finally:
            token_queue.put_nowait(None)  # sentinel

    async with open_checkpointer() as checkpointer:
        graph = build_graph(tools, checkpointer, token_queue)
        # ``recursion_limit`` bounds total supersteps. The B2 loop is
        # plan → (fan-out workers) → plan → … capped by ``max_iterations``
        # plan passes; a generous limit avoids tripping on wide fan-outs.
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(25, max_iterations * 8),
        }
        init_state: AgentState = {
            "messages": messages,
            "dialog_system_prompt": dialog_system_prompt,
            "max_iterations": max_iterations,
            "iteration": 0,
            "kbinfos": {"chunks": [], "doc_aggs": []},
            "sub_results": [],
            "answered_subqs": [],
            "sub_questions": [],
        }
        drive_task = asyncio.create_task(_drive(graph, config, init_state))
        try:
            while True:
                item = await token_queue.get()
                if item is None:
                    break
                yield item
        finally:
            if not drive_task.done():
                drive_task.cancel()
                try:
                    await drive_task
                except (asyncio.CancelledError, Exception):
                    pass
