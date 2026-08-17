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

"""Keyword-driven iterative search graph — v4 (LangGraph).

Refines v3. There is no per-sub-question document shortlist: each sub-question
retrieves across the whole knowledge base, and its judgement is split into a
cheap answerability gate followed by a summarisation of only the pairs that pass.

Flow:

    analyze → keywords → retrieve_chunks → assess → summarize → sufficiency
        sufficiency ─(sufficient)──────────────────────────────→ answer(partial=False) → END
                    └(insufficient)→ next_subq ─(no new sub-qs)→ answer(partial=True)  → END
                                                └(new sub-qs)──→ keywords   (next round)

Design rules:
* Every LLM node builds a FRESH (system, user) prompt — contexts never accumulate.
* ``retrieve_chunks`` searches the whole KB (only the caller-level ``doc_scope``,
  when set, still narrows it) and optionally narrows hits to keyword sentences.
* ``assess`` is a cheap yes/no gate; ``summarize`` only ever sees material that
  already passed it, and picks the chunk numbers that back its summary.
* ``answer`` composes from the evidence summaries, each tagged with the ``[ID:n]``
  markers of the chunks backing it; ``pool`` is the citation backing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from common.token_utils import num_tokens_from_string, truncate
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
_MAX_SUBQUESTIONS = 2  # sub-questions kept per round
_MAX_ANALYZE_SUBQUESTIONS = 2  # sub-questions emitted by the initial analyzer
_DOCS_PER_SUBQ = 3  # documents read end-to-end per sub-question in the optional deep scan
_CHUNKS_PER_SUBQ = 10  # chunks retrieved per sub-question (KB-wide)
_DOC_BATCH_SIZE = 3  # chunks per batch in the optional whole-document scan
_DOC_BATCH_OVERLAP = 1  # chunks shared between consecutive batches
_MAX_DOC_BATCHES = 8  # max batches scanned per document in the optional deep scan


# ── Number / date synonym hints (deterministic, fed to the keyword step) ──

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORD_ONES = [
    "zeroth",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
    "eleventh",
    "twelfth",
    "thirteenth",
    "fourteenth",
    "fifteenth",
    "sixteenth",
    "seventeenth",
    "eighteenth",
    "nineteenth",
]
_ORD_TENS = ["", "", "twentieth", "thirtieth", "fortieth", "fiftieth", "sixtieth", "seventieth", "eightieth", "ninetieth"]
_SCALES = {"thousand": 1000, "million": 10**6, "billion": 10**9, "trillion": 10**12}
_WORD_NUM = {w: i for i, w in enumerate(_ONES)}
_WORD_NUM.update({w: i * 10 for i, w in enumerate(_TENS) if w})

_ORDINAL_DIGIT_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_DIGIT_SCALE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s+(thousand|million|billion|trillion)s?\b", re.IGNORECASE)
_WORD_SCALE_RE = re.compile(r"\b([a-z]+)\s+(thousand|million|billion|trillion)s?\b", re.IGNORECASE)
_COMMA_NUM_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")


def _int_to_ordinal_words(n: int) -> str:
    if 0 <= n < 20:
        return _ORD_ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _ORD_TENS[tens] if ones == 0 else f"{_TENS[tens]}-{_ORD_ONES[ones]}"
    return ""


def _number_keyword_hints(text: str) -> list[str]:
    """Numeric <-> written-form synonyms found in ``text``.

    Mirrors :func:`_date_keyword_hints`: a deterministic assist so the keyword step
    does not have to invent ``21st -> twenty-first`` / ``two million -> 2000000``
    on its own. Returns human-readable hint lines; never raises.
    """
    if not text:
        return []
    hints: list[str] = []
    seen: set[str] = set()

    def _add(source: str, *variants: str) -> None:
        vs: list[str] = []
        for v in variants:  # dedup, keep order (e.g. "third" == "third".replace("-", " "))
            if v and v != source and v not in vs:
                vs.append(v)
        if not vs or source in seen:
            return
        seen.add(source)
        hints.append(f"{source} -> " + ", ".join(f'"{v}"' for v in vs))

    for m in _ORDINAL_DIGIT_RE.finditer(text):
        n = int(m.group(1))
        word = _int_to_ordinal_words(n)
        if word:
            _add(m.group(0), word, f"{word.replace('-', ' ')}")

    for m in _DIGIT_SCALE_RE.finditer(text):
        try:
            value = float(m.group(1)) * _SCALES[m.group(2).lower()]
        except (ValueError, KeyError):
            continue
        _add(m.group(0), f"{int(value)}", f"{int(value):,}")

    for m in _WORD_SCALE_RE.finditer(text):
        word = m.group(1).lower()
        if word not in _WORD_NUM:
            continue
        value = _WORD_NUM[word] * _SCALES[m.group(2).lower()]
        _add(m.group(0), f"{value}", f"{value:,}")

    for m in _COMMA_NUM_RE.finditer(text):
        plain = m.group(0).replace(",", "")
        _add(m.group(0), plain)

    return hints


# ── Prompts (each a single, self-contained LLM call) ──

_ANALYZE_SYSTEM = """You plan the FIRST round of research for a question. Research runs in several
rounds, so this round does NOT have to answer the whole question.

Emit ONLY the sub-questions that can be searched RIGHT NOW, using facts stated in the original
question itself. Rules:
- SIMPLE and ATOMIC: one fact, one entity, one relation per sub-question. Never bundle several
  facts, or several hops, into one sub-question.
- INDEPENDENT: answerable on its own; never relies on the answer to another sub-question.
- Anything that needs a value you do NOT have yet is deliberately LEFT OUT — a later round will
  ask it once that value is known.
- Never use an unresolved reference such as "that city", "it", "the person" or "the former".
  Repeat the full entity name and any ranking, date or location condition.
- Generate AT MOST TWO sub-questions; fewer is better when fewer suffice. Never pad the list.
Output ONLY JSON, no prose, no code fences:
{"subquestions": [{"question": "<sub-question>"}, ...]}"""

_KEYWORDS_SYSTEM = (
    """You are given several sub-questions. For EACH sub-question, produce search-friendly keywords
AND their closest / most-likely synonyms (AT MOST 3 terms; a standalone number or serial of digits
is its OWN term, never glued to other words).

Always include the alternative surface forms a source might use, in BOTH directions:
- Ordinals:  "21st" <-> "twenty-first";  "3rd" <-> "third"; "6" <-> "six"(including other languages).
- Magnitudes: "two million" <-> "2000000" <-> "2,000,000".
- Dates: "Aug 2nd" <-> "08-02" <-> "2024-08-02" <-> "August 2, 2024".
Pick the forms most likely to appear in the sources.
"""
    + _DATE_NORMALIZATION_SYSTEM
    + """
Output ONLY JSON, no prose, no code fences:
{"keywords": [{"id": "<sub-question id>", "keywords": ["term or synonym", ...]}, ...]}"""
)

_ASSESS_SYSTEM = """You are given a sub-question and a consecutive, numbered batch of chunks.
Judge EACH chunk independently. Do not write the answer.
- "full"    — this chunk ALONE completely answers the sub-question: it states the specific value
              asked for (the date, number, name, ...), OR that value follows from figures stated in
              THIS chunk by a trivial, certain derivation (see below).
- "partial" — this chunk contributes a genuinely relevant fact, but does not state the specific
              value asked for and does not force it arithmetically.
- "none"    — this chunk does not help answer the sub-question.

A TRIVIAL, CERTAIN derivation is arithmetic that is forced by the stated figures — never a guess:
- the complement of a share of one whole: the question asks what share A is, the chunk states B's
  share of the same whole, so A = 100% - B. e.g. asked "what percentage did Enix make up" and the
  chunk says "80% of staff were former Square employees" -> Enix is 20% -> "full".
- a unit conversion, or the sum/difference of figures stated in the chunk.
Anything needing an outside fact, an estimate, or an assumption is NOT trivial — judge that "partial".
Do not guess or use outside knowledge. Judge only the corresponding chunk, not what other chunks might say.
Output ONLY JSON, no prose, no code fences:
{"statuses": [{"chunk": 1, "status": "full|partial|none"}, ...]}
Return exactly one status for every input chunk, in the same order."""

_SUMMARIZE_SYSTEM = """You are given a sub-question and NUMBERED chunks that DO answer it.
Answer the sub-question from those chunks only — concise and factual, no speculation, no outside
knowledge. Also return "relevant": the chunk NUMBERS that support your answer.

Answer the question that was ASKED. When the chunks state a related figure rather than the one asked
for, you MAY complete a trivial, certain derivation and give the asked-for value, showing the figure
it came from:
- complement of a share of one whole: asked "what percentage did Enix make up", chunk says "80% of
  staff were former Square employees" -> answer "20% (the chunks state 80% were former Square staff)".
- a unit conversion, or the sum/difference of figures stated in the chunks.
Do NOT estimate, assume, or bring in any figure the chunks do not state. If the asked-for value is
not stated and not arithmetically forced, say plainly what the chunks do state instead.
Output ONLY JSON, no prose, no code fences:
{"summary": "<concise factual answer>", "relevant": [<chunk number>, ...]}"""

_SUFFICIENCY_SYSTEM = """You are given the ORIGINAL question and the facts discovered so far.
Decide ONLY whether those facts are ENOUGH to answer the ORIGINAL question directly and completely.
Do NOT propose follow-up research.
- "sufficient" is true only when EVERY part of the ORIGINAL question can be answered from the facts.
- Do not assume, infer or fill in a value that no fact states.
- If the core entity/value is already known but a qualifier like exact date, source date,
  or formatting detail is missing, prefer "sufficient": false and let the caller answer partially
  from the known value instead of chasing a finer-grained fact.
Output ONLY JSON, no prose, no code fences:
{"sufficient": true/false}"""

_MISSING_SYSTEM = """You are given the ORIGINAL question, the facts discovered so far, and every
sub-question ALREADY ASKED (each marked with whether it found evidence).
The evidence was already judged insufficient.

State precisely what is still missing for a complete answer. Do NOT answer the question and do NOT
invent facts.
- USE THE ORIGINAL QUESTION'S OWN WORDING for the unknown. Do NOT paraphrase it into a different
  concept: "make up" is not "own", "worth" is not "earned", "led" is not "founded". Re-wording the
  unknown sends every later round chasing a different fact than the one that was asked for.
- If that wording is AMBIGUOUS (e.g. "what percentage did X make up" could mean share of staff,
  of ownership, of revenue, or of output), do NOT pick one reading. List the plausible readings so
  the next step can try them, and keep the question's original phrase in your answer.
- If the facts already contain a usable core value, say the missing part is only the qualifier.
- Take the already-asked list into account. If the missing item was already searched for and found
  nothing, say so explicitly ("X is missing; it was already searched for via <sub-question> and no
  evidence was found"), so the next planning step tries a DIFFERENT angle instead of repeating it.
Output ONLY JSON, no prose, no code fences:
{"missing": "<what is still missing, or empty>"}"""

_NEXT_SUBQ_SYSTEM = """The facts gathered so far do NOT yet answer the ORIGINAL question. You are given
what is still missing and EVERY sub-question already asked. Plan the NEXT round.

- USE THE FACTS ALREADY DISCOVERED. If an earlier round resolved a value (a name, date, number),
  SUBSTITUTE that concrete value into the next sub-question. This is how a multi-hop question makes
  progress across rounds. Example — if a fact says the city is "Baltimore", ask "When was Baltimore
  founded?", NOT "When was that city founded?".
- Keep each sub-question SIMPLE, ATOMIC and INDEPENDENT: one fact per question, searchable on its own.
- KEEP THE ORIGINAL QUESTION'S WORDING for the thing being asked. Do NOT swap in a near-synonym that
  means something different ("make up" -> "own", "worth" -> "earned"): sources tend to state the fact
  using the asker's own phrasing, so re-wording it loses the very text that holds the answer.
- A sub-question marked "[PARTIAL ...]" was NOT settled — the sources were on-topic but never stated
  the value. Do not move on to a different topic as if it were answered: either ask for that same
  value in the ORIGINAL question's wording, or try a DIFFERENT READING of the ambiguous term. Do not
  simply drill into a finer-grained version of the same failed reading.
- Never use an unresolved reference. If a needed value is still unknown, ask for THAT value instead —
  do not chain two unknowns in one sub-question.
- NEVER repeat or rephrase anything under "Sub-questions ALREADY asked" — those rounds are spent.
  One marked "[no evidence found]" means that angle failed: attack the gap from a genuinely different
  angle (a different entity, source or attribute) rather than rewording it.
- If no genuinely NEW and useful sub-question exists — every angle is spent, or the corpus plainly
  cannot supply what is missing — return an EMPTY "next_subquestions" list. An empty list is the
  CORRECT answer in that case; never pad it with a variation of something already asked.
- If the facts already contain the core value needed for a usable partial answer, and the only
  remaining gap is a qualifier like an exact date, source date, or formatting detail, STOP.
  Return an EMPTY "next_subquestions" list instead of asking for a finer-grained follow-up.
- Do not keep narrowing a resolved birthplace from prefecture/state/country to a city/town unless
  the question itself explicitly asked for that smaller unit.
- Generate AT MOST TWO. (Keywords are added by a separate step, so output the questions only.)
Output ONLY JSON, no prose, no code fences:
{"next_subquestions": [{"question": "<sub-question>"}, ...]}"""

_PARTIAL_PREAMBLE = (
    "The evidence below is INCOMPLETE — research ran out of new angles before every part of the "
    "question could be resolved. Answer the parts that the evidence does support, and state plainly "
    "which part remains unresolved. Do not guess the missing part."
)


class KwV4State(TypedDict, total=False):
    question: str
    subquestions: list  # [{id, question, keywords, chunks, answerable}]
    evidences: list  # accumulated [{iteration, subq, summary, chunk_ids}]
    asked: list  # EVERY sub-question attempted so far, answered or not — blocks re-asking
    pool: list  # retained (summariser-selected) chunks — the citation set, dedup by id
    iteration: int
    max_iterations: int
    sufficient: bool
    partial: bool
    missing: str
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
        out.append({"id": f"sq{iteration}_{i}", "question": q, "keywords": kws or q, "chunks": [], "answerable": False})
        if len(out) >= limit:
            break
    return out


def build_keyword_agentic_graph_v4(
    tools,
    token_queue: asyncio.Queue,
    gen_conf: dict | None = None,
    max_iterations: int = 3,
    enable_snippets: bool = False,
    enable_deep_scan: bool = False,
):
    """Compile the v4 graph.

    :param enable_snippets: when True, ``retrieve_chunks`` narrows chunks to their
        keyword-bearing sentences before the assess/summarize steps. Off by default.
    :param enable_deep_scan: when True, a sub-question whose retrieved chunks are
        judged unanswerable falls back to scanning its ranked documents in
        overlapping batches. Off by default.
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
        """Ordinary hybrid retrieval (content + optional vector)."""
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

    async def _shown_listing(chunks: list[dict]) -> tuple[list[dict], str]:
        shown = chunks
        listing = "\n\n".join(f"[{i + 1}] " + (c.get("content_with_weight") or c.get("content") or "") for i, c in enumerate(shown))
        return shown, listing

    def _assessment_body(chunk: dict) -> str:
        title = str(chunk.get("docnm_kwd") or "")
        content = str(chunk.get("content_with_weight") or chunk.get("content") or "").strip()
        return "Title: " + title + "\n--------------------------\n" + content

    def _assessment_prompt(question: str, bodies: list[str]) -> str:
        listing = "\n\n".join(f"[{i + 1}] {body}" for i, body in enumerate(bodies))
        return f"Sub-question:\n{question}\n\nConsecutive chunks:\n{listing}\n\nOutput JSON:"

    def _assessment_budget() -> int:
        max_length = int(getattr(tools.chat_mdl, "max_length", 8192) or 8192)
        return max(1, int(max_length * 0.2))

    def _assessment_batches(question: str, chunks: list[dict]) -> list[list[tuple[int, dict, str]]]:
        """Group adjacent chunks while keeping the full assessment prompt below 20% of context."""
        budget = _assessment_budget()
        batches: list[list[tuple[int, dict, str]]] = []
        current: list[tuple[int, dict, str]] = []

        for index, chunk in enumerate(chunks):
            body = _assessment_body(chunk)
            candidate_bodies = [item[2] for item in current] + [body]
            candidate_prompt = _assessment_prompt(question, candidate_bodies)
            candidate_tokens = num_tokens_from_string(_ASSESS_SYSTEM + candidate_prompt)

            if current and candidate_tokens > budget:
                batches.append(current)
                current = []
                candidate_bodies = [body]
                candidate_prompt = _assessment_prompt(question, candidate_bodies)
                candidate_tokens = num_tokens_from_string(_ASSESS_SYSTEM + candidate_prompt)

            if not current and candidate_tokens > budget:
                fixed_prompt = _assessment_prompt(question, [""])
                body_budget = max(1, budget - num_tokens_from_string(_ASSESS_SYSTEM + fixed_prompt))
                body = truncate(body, body_budget)
                candidate_tokens = num_tokens_from_string(_ASSESS_SYSTEM + _assessment_prompt(question, [body]))
                if candidate_tokens > budget:
                    _LOG.warning(
                        "[Assess] single chunk prompt exceeds assessment budget: %d > %d tokens",
                        candidate_tokens,
                        budget,
                    )

            current.append((index, chunk, body))

        if current:
            batches.append(current)
        return batches

    async def _assess_batch(question: str, batch: list[tuple[int, dict, str]]) -> list[str]:
        """Judge a consecutive batch and return one ``full|partial|none`` status per chunk."""
        prompt = _assessment_prompt(question, [item[2] for item in batch])
        parsed = await _llm_json(_ASSESS_SYSTEM, prompt)
        raw_statuses = parsed.get("statuses") if isinstance(parsed, dict) else []
        statuses = ["none"] * len(batch)
        if not isinstance(raw_statuses, list):
            return statuses

        for position, item in enumerate(raw_statuses):
            status_position = position
            if isinstance(item, dict):
                try:
                    status_position = int(item.get("chunk", item.get("index", position + 1))) - 1
                except (TypeError, ValueError):
                    status_position = position
                status = str(item.get("status") or "").strip().lower()
            else:
                status = str(item or "").strip().lower()
            if 0 <= status_position < len(statuses) and status in ("full", "partial", "none"):
                statuses[status_position] = status
        return statuses

    async def _assess_chunks(question: str, chunks: list[dict]) -> tuple[str, list[dict]]:
        """Assess adjacent chunks in bounded batches, keeping the useful ones.

        Each batch contains consecutive chunks and is sized from the selected LLM's
        context window. Chunks judged ``partial`` are collected and the walk
        continues, so several partials can together support an answer. The batch
        containing the first ``full`` result is the last batch assessed.

        Returns ``(status, useful chunks)`` where status is ``"full"`` when some
        chunk answered outright, ``"partial"`` when only partial chunks were found,
        and ``"none"`` otherwise. The status is carried all the way to the planner:
        an answer built only from partials is NOT done, and must be revisited rather
        than treated as settled. Only the useful chunks are handed to the summariser.
        """
        shown = chunks
        useful: list[dict] = []
        picked: list[str] = []
        status_out = "none"
        for batch in _assessment_batches(question, shown):
            statuses = await _assess_batch(question, batch)
            for (index, chunk, _), status in zip(batch, statuses, strict=True):
                if status == "none":
                    continue
                useful.append(chunk)
                picked.append(f"{index + 1}:{status}")
                if status == "full":
                    status_out = "full"
                else:
                    status_out = "partial" if status_out != "full" else status_out
            if status_out == "full":
                break
        if picked:
            _LOG.info("[Assess] %s — useful chunk(s) %s of %d for: %s", status_out.upper(), ", ".join(picked), len(shown), _snip(question))
        return status_out, useful

    async def _summarize(question: str, chunks: list[dict]) -> tuple[str, list[dict]]:
        """Summarize a pair from its USEFUL chunks only (as picked by ``_assess_chunks``).

        Returns ``(summary, cited chunks)``.
        """
        shown, listing = await _shown_listing(chunks)
        if not shown:
            return "", []
        parsed = await _llm_json(_SUMMARIZE_SYSTEM, f"Chunks:\n{listing}\n\nSub-question:\n{question}\n\nOutput JSON:")
        summary = str(parsed.get("summary") or "").strip()
        relevant: list[dict] = []
        for r in parsed.get("relevant") or []:
            try:
                i = int(r) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(shown):
                relevant.append(shown[i])
        if summary and not relevant:
            relevant = shown[:1]  # never lose the citation backing for a real answer
        return summary, relevant

    async def _deep_scan(sq: dict) -> list[dict]:
        """Optional fallback: scan whole documents in overlapping batches.

        The documents come from the sub-question's own retrieved chunks (best
        first, deduped) — retrieval is KB-wide now, so those hits are the only
        signal for which documents are worth reading end to end. Returns the
        USEFUL chunks of the first batch that yields any, or ``[]``. Only runs
        when ``enable_deep_scan``.
        """
        from rag.svr.task_executor_refactor.task_handler import TaskHandler

        docs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for ck in sq.get("chunks") or []:
            did = ck.get("doc_id")
            if did and did not in seen:
                seen.add(did)
                docs.append((did, ck.get("docnm_kwd") or did))
            if len(docs) >= _DOCS_PER_SUBQ:
                break

        for doc_id, doc_nm in docs:
            resolved = tools._resolve_doc_tenant(doc_id)
            if not resolved:
                continue
            kb_id, tenant_id = resolved
            batch_count = 0
            async for batch in TaskHandler._load_chunks_for_doc(tenant_id, kb_id, doc_id, batch_size=_DOC_BATCH_SIZE, overlap=_DOC_BATCH_OVERLAP):
                if batch_count >= _MAX_DOC_BATCHES:
                    break
                batch_count += 1
                status, useful = await _assess_chunks(sq["question"], batch)
                if status != "none":
                    _LOG.info("[Deep scan] '%s' batch %d answers with %d chunk(s): %s", doc_nm, batch_count, len(useful), _snip(sq["question"]))
                    return useful
        return []

    # ── Node 1: analyze — decompose Q into independent sub-questions (LLM) ──
    async def analyze_node(state: KwV4State) -> dict:
        q = state.get("question") or ""
        _LOG.info("[Analyze] Planning round 1 — simple, independent sub-questions for: %s", _snip(q))
        parsed = await _llm_json(_ANALYZE_SYSTEM, f"Question:\n{q}\n\nOutput JSON:")
        subqs = _mk_subqs(parsed.get("subquestions"), 0, limit=_MAX_ANALYZE_SUBQUESTIONS) or _mk_subqs([{"question": q}], 0, limit=_MAX_ANALYZE_SUBQUESTIONS)
        for sq in subqs:
            _LOG.info("[Sub-Q]: %s", sq["question"])
        return {"subquestions": subqs, "iteration": 0, "pool": [], "evidences": [], "asked": [], "partial": False}

    # ── Node 2: keywords — search terms + synonyms + number/date variants (LLM) ──
    async def keywords_node(state: KwV4State) -> dict:
        subqs = state.get("subquestions") or []
        if not subqs:
            return {"subquestions": subqs}
        listing = "\n".join(f"[{sq['id']}] {sq['question']}" for sq in subqs)
        blob = "\n".join(sq["question"] for sq in subqs)
        user = "Sub-questions:\n" + listing
        hints = _date_keyword_hints(blob) + _number_keyword_hints(blob)
        if hints:
            user += "\n\nNormalization hints:\n" + "\n".join(f"- {h}" for h in hints)
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

    # ── Node 3+4: retrieve_chunks — top chunks across the whole KB, then narrow (no LLM) ──
    async def retrieve_chunks_node(state: KwV4State) -> dict:
        subqs = state.get("subquestions") or []
        # Only the caller-level document scope still applies (None when unset);
        # there is no per-sub-question document shortlist any more.
        scoped = tools.scoped_doc_ids(None) if hasattr(tools, "scoped_doc_ids") else None

        async def _one(sq: dict) -> list[dict]:
            try:
                kbinfos = await _retrieve(sq["keywords"] or sq["question"], _CHUNKS_PER_SUBQ, doc_ids=scoped)
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
        _LOG.info("[Retrieve chunks] chunks per sub-q: %s (snippets=%s)", str([len(sq["chunks"]) for sq in subqs]), enable_snippets)
        return {"subquestions": subqs}

    # ── Node 6: assess — can each sub-question be answered from its chunks? (LLM) ──
    async def assess_node(state: KwV4State) -> dict:
        subqs = state.get("subquestions") or []

        async def _one(sq: dict) -> str:
            if sq["chunks"]:
                status, useful = await _assess_chunks(sq["question"], sq["chunks"])
                sq["chunks"] = useful
                if status != "none":
                    return status
            # Deep scan still reads the ORIGINAL chunks above to pick which
            # documents to open, so only overwrite them once it succeeds.
            if enable_deep_scan:
                useful = await _deep_scan(sq)
                if useful:
                    sq["chunks"] = useful
                    return "partial"
            return "none"

        statuses = await asyncio.gather(*[_one(sq) for sq in subqs])
        for sq, status in zip(subqs, statuses):
            sq["status"] = status
            sq["answerable"] = status != "none"
        _LOG.info("[Assess] %d full, %d partial, %d unanswerable of %d sub-q(s).", statuses.count("full"), statuses.count("partial"), statuses.count("none"), len(subqs))
        return {"subquestions": subqs}

    # ── Node 7: summarize — evidence from the answerable pairs only (LLM) ──
    async def summarize_node(state: KwV4State) -> dict:
        subqs = state.get("subquestions") or []
        it = state.get("iteration", 0)
        answerable = [sq for sq in subqs if sq.get("answerable")]

        results = await asyncio.gather(*[_summarize(sq["question"], sq["chunks"]) for sq in answerable])

        # Record every sub-question ATTEMPTED this round — answered or not — so the
        # planner can never re-propose one that already came up empty.
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
        for sq, (summary, relevant) in zip(answerable, results):
            if not summary:
                continue
            for c in relevant:
                cid = _chunk_id(c)
                if cid not in seen:
                    seen.add(cid)
                    pool.append(c)
            evidences.append(
                {
                    "iteration": it,
                    "subq": sq["question"],
                    # "full" == answered outright; "partial" == built only from partial
                    # chunks, so the planner must treat it as an OPEN gap, not settled.
                    "status": sq.get("status", "full"),
                    "summary": summary,
                    "chunk_ids": [_chunk_id(c) for c in relevant],
                }
            )
        _LOG.info("[Summarize] +%d evidence (%d total), pool=%d chunk(s) at round %d.", len([r for r in results if r[0]]), len(evidences), len(pool), it)
        return {"subquestions": subqs, "evidences": evidences, "pool": pool, "asked": asked}

    # ── Node 8: sufficiency — original Q vs all evidence (LLM) ──
    async def sufficiency_node(state: KwV4State) -> dict:
        evidences = state.get("evidences") or []
        ev_text = "\n\n".join(f"(round {e.get('iteration', 0)}) {e['subq']}\n-> {e['summary']}" for e in evidences) or "(nothing discovered yet)"
        prompt = f"Facts discovered so far:\n{ev_text}\n\nOriginal question:\n{state.get('question') or ''}\n\nOutput JSON:"
        # Step 1: sufficiency only — the prompt returns just {"sufficient": bool}.
        verdict = await _llm_json(_SUFFICIENCY_SYSTEM, prompt)
        sufficient = bool(verdict.get("sufficient"))

        # Step 2: only when insufficient, ask what is missing over the SAME prompt —
        # plus the already-asked list, so the gap is described in terms of what has
        # actually been tried instead of being restated identically every round.
        missing = ""
        if not sufficient:
            asked = state.get("asked") or []
            status_of = {_norm(e["subq"]): e.get("status", "full") for e in evidences}
            _label = {"full": "", "partial": "   [PARTIAL — the specific value was never stated]"}
            tried = "\n".join(f"- {a}" + _label.get(status_of.get(_norm(a), ""), "   [searched, no evidence found]") for a in asked)
            missing_prompt = prompt
            if tried:
                missing_prompt += f"\n\nSub-questions ALREADY asked:\n{tried}"
            missing_verdict = await _llm_json(
                _MISSING_SYSTEM,
                missing_prompt + f"\n\nOriginal question:\n{state.get('question') or ''}\n\nIn order to answer the original question. Tell me what is missing.",
            )
            missing = str(missing_verdict.get("missing") or "").strip()
        it = state.get("iteration", 0) + 1
        _LOG.info("[Sufficiency] round %d → sufficient=%s. Missing: %s", it, sufficient, _snip(missing))
        return {"sufficient": sufficient, "missing": missing, "iteration": it}

    # ── Node 10: next_subq — plan the next round from the asked records (LLM) ──
    async def next_subq_node(state: KwV4State) -> dict:
        evidences = state.get("evidences") or []
        asked = state.get("asked") or []
        it = state.get("iteration", 0)
        ev_text = "\n\n".join(f"(round {e.get('iteration', 0)}, {e.get('status', 'full').upper()}) {e['subq']}\n-> {e['summary']}" for e in evidences) or "(nothing discovered yet)"
        status_of = {_norm(e["subq"]): e.get("status", "full") for e in evidences}
        _label = {"full": "", "partial": "   [PARTIAL — answered only from partial chunks; the specific value was never stated]"}

        parts = [
            f"Original question:\n{state.get('question') or ''}",
            f"Facts discovered so far:\n{ev_text}",
            f"Still missing:\n{state.get('missing') or '(not stated)'}",
        ]
        if asked:
            tried = "\n".join(f"- {a}" + _label.get(status_of.get(_norm(a), ""), "   [no evidence found]") for a in asked)
            parts.append(f"Sub-questions ALREADY asked (never repeat or rephrase these):\n{tried}")
        parts.append("Output JSON:")

        parsed = await _llm_json(_NEXT_SUBQ_SYSTEM, "\n\n".join(parts))
        asked_keys = {_norm(a) for a in asked}
        fresh = []
        for x in parsed.get("next_subquestions") or []:
            if not isinstance(x, dict):
                continue
            key = _norm(x.get("question", ""))
            if not key or key in asked_keys:
                continue
            asked_keys.add(key)
            fresh.append(x)
        next_subqs = _mk_subqs(fresh, it)
        _LOG.info("[Next sub-Q] round %d → %d new sub-question(s).", it, len(next_subqs))
        return {"subquestions": next_subqs, "partial": not next_subqs}

    # ── Node 9/11: answer — brief cited answer, full or partial (LLM, streamed) ──
    async def answer_node(state: KwV4State) -> dict:
        evidences = [e for e in (state.get("evidences") or []) if e.get("summary")]
        partial = bool(state.get("partial"))
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

        rules = citation_prompt(tools.user_defined_prompts).strip()
        system = FINAL_ANSWER_SYSTEM.format(cite_rules=rules)
        head = f"Question:\n{state.get('question') or ''}\n\n"
        if partial:
            head += _PARTIAL_PREAMBLE + "\n\n"
        user = (
            head + "Answer ONLY from the findings below. Each finding shows the [ID:n] citation markers that "
            "support it — reuse those exact markers in your answer; do not invent IDs.\n\n" + "Findings:\n" + "\n".join(findings)
        )
        _, msg = message_fit_in(form_message(system, user), tools.chat_mdl.max_length)

        _LOG.info("[Answer] %s answer from %d finding(s), citing a pool of %d chunk(s).", "PARTIAL" if partial else "Full", len(findings), len(pool))
        final = ""
        try:
            async for tok in tools.chat_mdl.async_chat_streamly_delta(msg[0]["content"], msg[1:], answer_conf):
                token_queue.put_nowait(tok)
                final += tok
        except Exception:
            _LOG.exception("[Answer] stream failed")
            token_queue.put_nowait("I'm sorry, I encountered an error while composing the answer.")
        return {"final_answer": final}

    def _route_after_sufficiency(state: KwV4State) -> str:
        if state.get("sufficient"):
            return "answer"
        if state.get("iteration", 0) >= max_iterations:
            return "answer_partial"
        return "next_subq"

    def _route_after_next_subq(state: KwV4State) -> str:
        return "keywords" if state.get("subquestions") else "answer_partial"

    async def answer_partial_node(state: KwV4State) -> dict:
        return await answer_node({**state, "partial": True})

    g = StateGraph(KwV4State)
    g.add_node("analyze", analyze_node)
    g.add_node("keywords", keywords_node)
    g.add_node("retrieve_chunks", retrieve_chunks_node)
    g.add_node("assess", assess_node)
    g.add_node("summarize", summarize_node)
    g.add_node("sufficiency", sufficiency_node)
    g.add_node("next_subq", next_subq_node)
    g.add_node("answer", answer_node)
    g.add_node("answer_partial", answer_partial_node)

    g.add_edge(START, "analyze")
    g.add_edge("analyze", "keywords")
    g.add_edge("keywords", "retrieve_chunks")
    g.add_edge("retrieve_chunks", "assess")
    g.add_edge("assess", "summarize")
    g.add_edge("summarize", "sufficiency")
    g.add_conditional_edges("sufficiency", _route_after_sufficiency, {"answer": "answer", "answer_partial": "answer_partial", "next_subq": "next_subq"})
    g.add_conditional_edges("next_subq", _route_after_next_subq, {"keywords": "keywords", "answer_partial": "answer_partial"})
    g.add_edge("answer", END)
    g.add_edge("answer_partial", END)

    return g.compile()


async def run_keyword_agentic_rag_v4(
    tools,
    messages: list,
    max_iterations: int = 4,
    enable_snippets: bool = False,
    enable_deep_scan: bool = False,
    gen_conf: dict | None = None,
):
    """Drive the v4 graph, yielding answer-token strings."""
    question = ""
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content"):
            question = m["content"]
            break

    token_queue: asyncio.Queue = asyncio.Queue()
    graph = build_keyword_agentic_graph_v4(
        tools,
        token_queue,
        gen_conf=gen_conf,
        max_iterations=max_iterations,
        enable_snippets=enable_snippets,
        enable_deep_scan=enable_deep_scan,
    )
    _SENTINEL = object()
    holder: dict[str, Any] = {}

    async def _drive():
        try:
            holder["state"] = await graph.ainvoke(
                {"question": question, "max_iterations": max_iterations, "iteration": 0, "pool": [], "evidences": [], "asked": [], "partial": False},
                {"recursion_limit": max(25, max_iterations * 8 + 10)},
            )
        except Exception:
            _LOG.exception("run_keyword_agentic_rag_v4: graph execution failed")
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
