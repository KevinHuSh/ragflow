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

"""Keyword-driven iterative search graph — v7 (LangGraph).

A deliberately small chain-of-thought baseline. Where v5 fans out into a batch
of sub-questions per round and carries classification, arithmetic, structural
expansion and an exhaustion ledger, v7 keeps exactly ONE question in flight and
loops: ask it, search it, answer it if the chunks allow, otherwise think of the
next question. Every round adds at most one evidence, and the final answer is
composed from the evidence list.

Flow:

    formalize → keywords → retrieve → assess → sufficiency ─(yes)──→ answer → END
                   ↑                             │
                   └────── next_question ←───────┘(no)
                                └─(nothing new | out of rounds)→ answer

Two simplifications are deliberate, and both cost something:

* Keyword generation fills four semantic slots, then issues a narrow and a broad
  formulation. Their result sets are unioned, so fact-type vocabulary improves
  precision without making a guessed phrasing a hard retrieval requirement.
* Retrieval is DOCUMENT-SCOPED. A shallow ranking pass on the BROAD formulation
  alone — entity and aliases, never fact-type vocabulary — picks the top
  ``_DOCS_PER_SUBQ`` documents, and the chunk search is confined to them. Ranking
  on the full term set instead lets words like "coach" and "manager", which are
  dense in every sports page, put an unrelated franchise above the team asked
  about.
* Retrieved chunks are then WIDENED to their document neighbours and prefixed
  with the nearest markdown heading, because a chunk boundary routinely severs a
  value from the only text that names it: the continuation of a truncated list
  carries none of the query's words and is otherwise unreachable.

v5 remains the richer graph; v7 focuses on query formulation and ES recall
without relying on a separate heuristic "needle" detector.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from common.token_utils import num_tokens_from_string
from rag.prompts.generator import citation_prompt, form_message, message_fit_in
from rag.advanced_rag.harness.prompts.report_prompt import FINAL_ANSWER_SYSTEM
from rag.advanced_rag.harness.tools.search import _narrow_by_keywords, _normalize

# Stable pure helpers reused from v1 (no behavioural coupling).
from rag.advanced_rag.keyword_agentic_graph import (
    _chunk_id,
    _doc_aggs_from,
    _extract_json,
    _norm,
    _snip,
)

# Table flattening (including the banner-row handling) is shared with v5.
from rag.advanced_rag.keyword_agentic_graph_v5 import _flatten_chunk_tables

_LOG = logging.getLogger(__name__)

# Tunable caps.
_CHUNKS_PER_QUERY = 6  # chunks retrieved per round (higher than v5: nothing inflates them here)
_DOCS_PER_SUBQ = 3  # ranked candidate documents retained as the retrieval scope
_DOC_TOP_N = 30  # chunk hits fetched when ranking those candidate documents
_DOC_HEAD_FALLBACK_LIMIT = 12  # first chunks scanned when field-style facts miss chunk search
_EXPAND_BEFORE = 1  # neighbour chunks pulled in BEFORE a hit
_EXPAND_AFTER = 1  # neighbour chunks pulled in AFTER a hit
_DOC_ORDERED_LIMIT = 10000  # max chunks fetched when ordering a document

# A markdown heading, so an expanded chunk can say which section it came from.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)

_KEYWORDS_SYSTEM = """You turn ONE question into a search query for a keyword/BM25 search engine.

Fill four slots:
A. ENTITY — the specific thing the fact is about. Use proper nouns, titles and identifiers.
   Keep every multi-word entity as one phrase, for example "Brown County, Kansas".
B. ALIASES — surface forms that may occur in the corpus: full vs. short name, native-language
   and transliterated forms, official vs. common name, acronym and expansion, and qualified
   forms such as "Brown County" and "Brown County, Kansas". Do not invent aliases.
C. FACT-TYPE VOCABULARY — 3–6 optional words for the kind of fact, spread across registers:
   quantity of people -> population, inhabitants, residents, census, demographics, headcount
   time of an event -> founded, established, opened, dated, began
   role of a person -> served, appointed, elected, held, director
   These terms boost precision but must never be required for a hit.
D. QUALIFIERS — optional year, edition, jurisdiction or revision terms. Include date and number
   surface variants when the question supplies them, such as "August 24, 2021", "2021-08-24",
   and "08/24/2021".

The searcher will issue at least two formulations and union their results:
- NARROW: ENTITY + ALIASES as phrase clauses, with FACT-TYPE VOCABULARY and QUALIFIERS.
- BROAD: ENTITY + ALIASES only, for recall when the fact vocabulary guess is wrong.
If the entity is obscure and an alias is useful on its own, set "obscure" to true so the searcher
also issues one alias-only formulation.

DROP question words, relational scaffolding, and generic high-frequency nouns such as "year",
"number", "city", "total", "list" and "information". Do not put those words in FACT-TYPE
VOCABULARY unless they are part of a proper name or a source's actual measure.
Output ONLY JSON, no prose, no code fences:
{"entity": "...", "aliases": ["..."], "fact_type": ["..."], "qualifiers": ["..."], "obscure": false}"""

_ASSESS_SYSTEM = """You are given a QUESTION and a consecutive, numbered batch of chunks.
Judge EACH chunk independently. Do NOT write the answer here.
- "full"    — this chunk ALONE completely answers the question: it states the specific value asked
              for (the date, number, name, ...), OR that value follows from figures stated in THIS
              chunk by a trivial, certain derivation (see below).
- "partial" — this chunk contributes a genuinely relevant fact, but does not state the value asked
              for and does not force it arithmetically.
- "none"    — this chunk does not help answer the question.

A TRIVIAL, CERTAIN derivation is arithmetic forced by the stated figures, never a guess: the
complement of a share of one whole, a unit conversion, or the sum/difference of figures stated in
the chunk. Anything needing an outside fact, an estimate or an assumption is NOT trivial — judge
that "partial".

Do not guess, and do not use outside knowledge. Judge only the chunk in front of you, not what
another chunk might say. Do not assume a list truncated mid-way continues with what you expect.
Output ONLY JSON, no prose, no code fences:
{"statuses": [{"chunk": 1, "status": "full|partial|none"}, ...]}
Return exactly one status for every input chunk, in the same order."""

_BRIEF_ANSWER_SYSTEM = """You are given a QUESTION, the ORIGINAL question that the research serves,
and numbered chunks that DO answer the QUESTION.

Answer the QUESTION from those chunks only — one or two sentences, factual, no speculation, no
outside knowledge, no reasoning shown.
- Keep every detail the ORIGINAL question will later need: exact dates, exact numbers, full names,
  units. A summary that drops the figure is useless to the step that follows.
- If the QUESTION asks for a set or a list, name EVERY member the chunks state, not just the first.
- Answer the question that was ASKED. If the chunks state a closely related figure instead, give the
  asked-for value only when it follows from them by a trivial, certain derivation, and say which
  figure it came from. Otherwise state plainly what the chunks do say.
- "relevant": the NUMBERS of the chunks your answer rests on.
Output ONLY JSON, no prose, no code fences:
{"answer": "<one or two factual sentences>", "relevant": [<chunk number>, ...]}"""

_SUFFICIENCY_SYSTEM = """You are given the ORIGINAL question and every fact discovered so far.
Decide ONLY whether those facts are enough to answer the ORIGINAL question completely and directly.
Do NOT propose follow-up research and do NOT answer the question.

- "sufficient" is true only when EVERY part of the ORIGINAL question can be answered from the facts.
- Never assume, infer, or fill in a value that no fact states.
- If the question joins two conditions ("which X did A and also B"), the facts are sufficient only
  when a SINGLE entity is shown to satisfy both. Two facts naming DIFFERENT entities are a
  contradiction to resolve, not an answer.
Output ONLY JSON, no prose, no code fences:
{"sufficient": true/false}"""

_NEXT_QUESTION_SYSTEM = """The facts gathered so far do NOT answer the ORIGINAL question. You are
given the ORIGINAL question, the facts discovered, and every question ALREADY ASKED. Plan the ONE
question to ask next.

- SIMPLE and ATOMIC: one fact, one entity, one relation. Never bundle two hops into one question.
- USE THE FACTS ALREADY DISCOVERED. When an earlier round resolved a value, SUBSTITUTE it into the
  next question — that is how a multi-hop question makes progress. If a fact says the city is
  "Baltimore", ask "When was Baltimore founded?", never "When was that city founded?".
- Never use an unresolved reference ("that city", "it", "the person"). Name the entity in full,
  with any ranking, date or location condition the question attaches to it.
- KEEP THE ORIGINAL QUESTION'S WORDING for the thing being asked. Do not swap in a near-synonym that
  means something else ("make up" is not "own", "worth" is not "earned"): sources tend to state a
  fact in the asker's own phrasing, so re-wording it loses the text that holds the answer.
- NEVER repeat or rephrase anything under "Already asked" — those rounds are spent. Attack the gap
  from a genuinely different angle: a different entity, source or attribute.
- If no genuinely NEW and useful question exists — every angle is spent, or the sources plainly
  cannot supply what is missing — return an EMPTY question. That is the CORRECT answer in that case;
  never pad it with a variation of something already asked.
Output ONLY JSON, no prose, no code fences:
{"question": "<the next question, or an empty string>"}"""

_NO_REASONING_RULE = (
    "\n\n# Output Discipline\n"
    "Emit ONLY the finished answer. The reader sees your entire reply, so it must contain no "
    "deliberation: no numbered walk-through of the steps, no 'Let me work through this', no "
    "'the evidence tells us', no narration of what you decided to state or withhold, and no "
    "restatement of the question. Lead with the answer itself, then any brief supporting detail. "
    "If part of the question is unresolved, say so in one sentence — do not explain how you "
    "arrived at that conclusion."
)

_BEST_EFFORT_RULE = (
    "\n\n# Iteration-Limit Best Estimate\n"
    "Research reached its limit with a factor unresolved. The instruction not to guess is relaxed "
    "ONLY for a single clearly labelled best-supported inference. Base it on the findings, mark it "
    "plainly as an inference rather than a confirmed fact, and never invent a source, citation, "
    "figure or date."
)

_BEST_EFFORT_PREAMBLE = (
    "Research reached its iteration limit before every part of the question could be verified. Give "
    "the most plausible complete answer the findings support, making ONE best-supported inference "
    "for the unresolved factor. Label that part explicitly as a best estimate and state what remains "
    "unverified. Never fabricate a source, citation, exact figure, or date."
)

_PARTIAL_PREAMBLE = (
    "The evidence below is INCOMPLETE — research ran out of new angles before every part of the "
    "question could be resolved. Answer the parts that the evidence does support, and state plainly "
    "which part remains unresolved. Do not guess the missing part."
)


class KwV7State(TypedDict, total=False):
    question: str  # the raw last user message
    formalized: str  # the standalone question rebuilt from the conversation
    current: str  # the question being researched THIS round
    keywords: str  # slot terms used by local chunk narrowing
    keyword_slots: dict  # {entity, aliases, fact_type, qualifiers, obscure}
    lookup_formulations: list  # [{label, query, terms}]
    doc_scope: list  # [{doc_id, doc_nm}] — the ranked documents chunk retrieval is confined to
    chunks: list  # chunks retrieved for `current`
    evidences: list  # [{iteration, question, answer, chunk_ids}]
    asked: list  # questions attempted that produced no evidence
    pool: list  # retained chunks — the citation set, dedup by id
    iteration: int
    max_iterations: int
    global_iteration: int
    max_global_iter: int
    sufficient: bool
    partial: bool
    final_answer: str


def _numbered(chunks: list[dict]) -> str:
    """Render chunks as a 1-based numbered listing for the assessor."""
    return "\n\n".join(f"[{i + 1}] Title: {c.get('docnm_kwd') or ''}\n{(c.get('content_with_weight') or c.get('content') or '').strip()}" for i, c in enumerate(chunks))


def _facts_listing(evidences: list[dict]) -> str:
    """Render the evidence for the planner, flagging facts built only from partials.

    A brief assembled from "partial" chunks answered around the question rather
    than stating the value, so the planner must be able to tell it apart from a
    settled fact — otherwise it moves on as though the hop were closed.
    """
    lines = []
    for e in evidences:
        mark = "   [PARTIAL — no chunk stated the value outright]" if e.get("status") == "partial" else ""
        lines.append(f"(round {e.get('iteration', 0)}) {e['question']}{mark}\n-> {e['answer']}")
    return "\n\n".join(lines) or "(nothing discovered yet)"


def _pick(chunks: list[dict], numbers: object) -> list[dict]:
    """Resolve 1-based chunk numbers from an LLM response to chunk dicts."""
    out, seen = [], set()
    for n in numbers if isinstance(numbers, list) else []:
        try:
            idx = int(n) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(chunks) and idx not in seen:
            seen.add(idx)
            out.append(chunks[idx])
    return out


def _clean_terms(values: object, limit: int) -> list[str]:
    """Keep non-empty, distinct surface forms while preserving LLM order."""
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else []:
        term = str(value or "").strip()
        key = _norm(term)
        if term and key not in seen:
            seen.add(key)
            out.append(term)
        if len(out) >= limit:
            break
    return out


def _compose_lookup_query(parts: list[str]) -> str:
    """Join slot values while keeping each multi-word value contiguous."""
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        value = str(part or "").strip()
        key = _norm(value)
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return ",".join(out)


def _normalise_keyword_slots(parsed: dict, fallback: str) -> dict:
    """Validate the four LLM slots and cap noisy expansions."""
    if not isinstance(parsed, dict):
        parsed = {}
    entity = str(parsed.get("entity") or "").strip() or fallback.strip()
    aliases = _clean_terms(parsed.get("aliases"), 6)
    fact_type = _clean_terms(parsed.get("fact_type") or parsed.get("fact_vocab"), 6)
    qualifiers = _clean_terms(parsed.get("qualifiers"), 8)
    aliases = [alias for alias in aliases if _norm(alias) != _norm(entity)]
    return {
        "entity": entity,
        "aliases": aliases,
        "fact_type": fact_type,
        "qualifiers": qualifiers,
        "obscure": bool(parsed.get("obscure")),
    }


def _lookup_formulations(slots: dict, fallback: str) -> list[dict]:
    """Build narrow, broad, and optional alias-only ES lookups."""
    entity = str(slots.get("entity") or "").strip()
    aliases = _clean_terms(slots.get("aliases"), 6)
    fact_type = _clean_terms(slots.get("fact_type"), 6)
    qualifiers = _clean_terms(slots.get("qualifiers"), 8)
    names = [value for value in [entity, *aliases] if value]
    broad = _compose_lookup_query(names) or fallback
    narrow = _compose_lookup_query([*names, *fact_type, *qualifiers]) or broad

    forms = [
        {"label": "narrow", "query": narrow, "terms": [*names, *fact_type, *qualifiers]},
        {"label": "broad", "query": broad, "terms": names},
    ]
    if slots.get("obscure") and aliases:
        forms.append({"label": "alias-only", "query": aliases[0], "terms": [aliases[0]]})
    return forms


_MONTH_TO_NUM = {name.lower(): idx for idx, name in enumerate(calendar.month_name) if name}
_MONTH_TO_NUM.update({name.lower(): idx for idx, name in enumerate(calendar.month_abbr) if name})
_EXACT_DATE_PATTERNS = (
    re.compile(r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<year>\d{4})\b", re.I),
    re.compile(r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\b", re.I),
    re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b"),
)
_MONTH_RANGE_PATTERN = re.compile(
    r"\b(?:between|from|during)\s+(?P<m1>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?:and|to)\s+(?P<m2>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<year>\d{4})\b",
    re.I,
)


def _parse_exact_date(text: str) -> date | None:
    """Parse a few explicit date spellings from model output."""
    if not text:
        return None
    for pattern in _EXACT_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        year = int(match.group("year"))
        month_raw = match.group("month")
        month = int(month_raw) if month_raw.isdigit() else _MONTH_TO_NUM.get(month_raw.lower(), 0)
        day = int(match.group("day"))
        if year and month and day:
            try:
                return date(year, month, day)
            except ValueError:
                continue
    return None


def _parse_month_range(text: str) -> tuple[date, date] | None:
    """Parse a month range like 'between April and July 1887'."""
    if not text:
        return None
    match = _MONTH_RANGE_PATTERN.search(text)
    if not match:
        return None
    year = int(match.group("year"))
    m1 = _MONTH_TO_NUM.get(match.group("m1").lower(), 0)
    m2 = _MONTH_TO_NUM.get(match.group("m2").lower(), 0)
    if not year or not m1 or not m2:
        return None
    try:
        start = date(year, m1, 1)
        end = date(year, m2, calendar.monthrange(year, m2)[1])
    except ValueError:
        return None
    return start, end


def _age_on(birth: date, when: date) -> int:
    """Compute age in full years at a specific date."""
    age = when.year - birth.year
    if (when.month, when.day) < (birth.month, birth.day):
        age -= 1
    return age


def _maybe_compose_age_answer(question: str, evidences: list[dict]) -> tuple[str, list[int]] | None:
    """Deterministically answer age questions when the necessary dates are present.

    Returns the answer plus the indexes of the evidence entries that supplied the
    needed dates, so the caller can cite them directly.
    """
    q = _norm(question)
    if "how old" not in q and "age" not in q:
        return None

    birth_date: date | None = None
    event_window: tuple[date, date] | None = None
    birth_sources: list[int] = []
    event_sources: list[int] = []
    birth_hints = ("born", "birth", "birthday", "date of birth")
    event_hints = ("perform", "conduct", "experiment", "founded", "established", "opened", "began", "held", "while", "during", "between")

    for idx, ev in enumerate(evidences):
        blob = f"{ev.get('question') or ''} {ev.get('answer') or ''}"
        low = blob.lower()
        if birth_date is None and any(hint in low for hint in birth_hints):
            parsed = _parse_exact_date(blob)
            if parsed:
                birth_date = parsed
                birth_sources = [idx]
        if event_window is None and any(hint in low for hint in event_hints):
            exact = _parse_exact_date(blob)
            if exact:
                event_window = (exact, exact)
                event_sources = [idx]
            else:
                rng = _parse_month_range(blob)
                if rng:
                    event_window = rng
                    event_sources = [idx]

    if not birth_date or not event_window:
        return None

    start_age = _age_on(birth_date, event_window[0])
    end_age = _age_on(birth_date, event_window[1])
    relevant = sorted(set(birth_sources + event_sources))
    if start_age == end_age:
        return f"{start_age} years old.", relevant
    return f"{start_age}–{end_age} years old.", relevant


_CHILD_COUNT_NUM_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_CHILD_COUNT_ROSTER_RE = re.compile(r"\bInclude\s+(?P<items>.+?)(?:[.?!]\s*$|$)", re.I)
_CHILD_COUNT_NAME_RE = re.compile(
    r"\b(?:How many|How much)\s+children\s+did\s+(?P<name>.+?)\s+have\b",
    re.I,
)


def _parse_child_count(text: str) -> int | None:
    low = (text or "").lower()
    if not low:
        return None
    if re.search(r"\b(no|not|never|without)\s+children\b", low) or re.search(r"\bchildren?\s*:\s*0\b", low):
        return 0
    m = re.search(r"\b(\d+)\s+children?\b", low)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(" + "|".join(_CHILD_COUNT_NUM_WORDS) + r")\s+children?\b", low)
    if m:
        return _CHILD_COUNT_NUM_WORDS.get(m.group(1), None)
    return None


def _extract_child_count_name(question: str) -> str:
    match = _CHILD_COUNT_NAME_RE.search(question or "")
    if not match:
        return ""
    name = match.group("name").strip()
    name = re.sub(r"\s+as of\b.*$", "", name, flags=re.I).strip(" ?,.")
    return name


def _extract_child_count_roster(question: str) -> list[str]:
    match = _CHILD_COUNT_ROSTER_RE.search(question or "")
    if not match:
        return []
    items = match.group("items")
    items = re.sub(r"\band\b", ",", items, flags=re.I)
    roster = []
    for raw in items.split(","):
        name = raw.strip(" .;")
        if not name:
            continue
        if re.search(r"\b(academy|best|winner|nominee|nominees|actor|actress|award|awards|oscar|oscars)\b", name, re.I):
            continue
        roster.append(name)
    return roster


def _maybe_compose_child_count_answer(question: str, evidences: list[dict], asked: list[str] | None = None) -> tuple[str, list[int]] | None:
    """Deterministically sum child counts when the question asks for a combined total."""
    q = _norm(question)
    if "children" not in q or not any(word in q for word in ("combined", "total", "all of", "sum", "together")):
        return None

    roster = _extract_child_count_roster(question)
    asked_names = {_norm(_extract_child_count_name(a)) for a in (asked or []) if _extract_child_count_name(a)}

    seen: dict[str, tuple[int, int]] = {}
    missing: list[str] = []
    for idx, ev in enumerate(evidences):
        qtext = str(ev.get("question") or "")
        name = _extract_child_count_name(qtext)
        if not name:
            continue
        count = _parse_child_count(str(ev.get("answer") or ""))
        if count is None:
            if name not in missing:
                missing.append(name)
            continue
        seen[name] = (count, idx)

    if roster:
        for name in roster:
            if name in seen:
                continue
            if _norm(name) in asked_names or name in missing:
                if name not in missing:
                    missing.append(name)

    if not seen:
        return None

    subtotal = sum(count for count, _ in seen.values())
    if roster and len(seen) == len(roster) and not missing:
        parts = []
        evidence_idxs: list[int] = []
        for name in roster:
            count, idx = seen[name]
            parts.append(f"{name}: {count}")
            evidence_idxs.append(idx)
        return f"{subtotal} children combined ({'; '.join(parts)}).", evidence_idxs

    known = ", ".join(f"{name}: {count}" for name, (count, _) in seen.items())
    missing_text = ", ".join(missing) if missing else ("some nominees remain unverified" if roster else "some children counts remain unverified")
    if roster and all(_norm(name) in asked_names or name in seen for name in roster):
        return f"{subtotal} children could be verified from the evidence ({known}). Missing counts: {missing_text}.", [idx for _, idx in seen.values()]
    return f"{subtotal} children could be verified from the evidence ({known}). Missing counts: {missing_text}.", [idx for _, idx in seen.values()]


def build_keyword_agentic_graph_v7(
    tools,
    token_queue: asyncio.Queue,
    messages: list | None = None,
    gen_conf: dict | None = None,
    max_iterations: int = 5,
    max_global_iter: int = 2,
):
    """Compile the v7 graph.

    :param messages: the full conversation, used once to rebuild a standalone
        question. Falls back to the raw question when unavailable.
    :param max_iterations: rounds of (keywords → retrieve → assess) before the
        graph answers with whatever evidence it has.
    :param max_global_iter: full graph attempts before returning the best answer.
    """
    max_global_iter = max(1, int(max_global_iter or 1))
    answer_conf = dict(gen_conf) if gen_conf else {"temperature": 0.3}
    # Whole documents in reading order, memoized for this run.
    _doc_order_cache: dict[str, list[dict]] = {}
    answer_conf.pop("direct_answer", None)

    async def _llm_json(system: str, user: str) -> dict:
        msg = await tools._fit_messages(system, user)
        ans = await tools.chat_mdl.async_chat(msg[0]["content"], msg[1:], {"temperature": 0.2})
        if isinstance(ans, tuple):
            ans = ans[0]
        return _extract_json(ans)

    # ── Node 1: formalize — rebuild a standalone question from the chat history ──
    async def formalize_node(state: KwV7State) -> dict:
        raw = state.get("question") or ""
        formalized = raw
        """
        if messages:
            try:
                rebuilt = await full_question(messages=messages, chat_mdl=tools.chat_mdl)
                if rebuilt and rebuilt.strip():
                    formalized = rebuilt.strip()
            except Exception:
                _LOG.exception("[Formalize] failed; falling back to the raw question")
        """
        if _norm(formalized) != _norm(raw):
            _LOG.info("[Formalize] %s  ->  %s", _snip(raw), _snip(formalized))
        else:
            _LOG.info("[Formalize] unchanged: %s", _snip(formalized))
        return {
            "formalized": formalized,
            "current": formalized,
            "iteration": 0,
            "evidences": [],
            "asked": [],
            "pool": [],
            "global_iteration": state.get("global_iteration", 0),
            "max_global_iter": max_global_iter,
            "partial": False,
        }

    # ── Node 2: keywords — four slots compiled into narrow/broad lookups ──
    async def keywords_node(state: KwV7State) -> dict:
        current = state.get("current") or ""
        user = f"Question:\n{current}"
        parsed = await _llm_json(_KEYWORDS_SYSTEM, user + "\n\nOutput JSON:")
        slots = _normalise_keyword_slots(parsed, current)
        forms = _lookup_formulations(slots, current)
        terms = _clean_terms(
            [slots["entity"], *slots["aliases"], *slots["fact_type"], *slots["qualifiers"]],
            24,
        )
        keywords = ",".join(terms) or current
        _LOG.info(
            "[Keywords] %s -> entity=%r aliases=%d fact_type=%d qualifiers=%d formulations=%s",
            _snip(current),
            _snip(slots["entity"], 60),
            len(slots["aliases"]),
            len(slots["fact_type"]),
            len(slots["qualifiers"]),
            ", ".join(form["label"] for form in forms),
        )
        return {"keywords": keywords, "keyword_slots": slots, "lookup_formulations": forms}

    # ── Structural expansion ──
    #
    # A chunk boundary routinely severs a value from the only text that names it.
    # The Thailand coaching list is the archetype: the retrieved chunk ends seven
    # entries in, and the continuation is a bare run of names and year ranges with
    # no "coach", no "manager", no year the query mentions — lexically unreachable
    # by ANY query the planner could write. Only its heading, stranded in the
    # previous chunk, says what it is. Pulling neighbours and prefixing that
    # heading is the only way such a chunk is ever read.

    def _position_key(row: dict) -> tuple[int, int]:
        """Sort key for document reading order: page, then vertical position."""

        def _first(value) -> int:
            if isinstance(value, list):
                value = value[0] if value else 0
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        return (_first(row.get("page_num_int")), _first(row.get("top_int")))

    async def _doc_in_order(doc_id: str) -> list[dict]:
        """Every chunk of ``doc_id`` in reading order, memoized for this run.

        Ordering mirrors ``TaskHandler._load_chunks_for_doc``: ascending
        ``page_num_int`` then ``top_int``. The same key is re-applied locally so a
        backend that ignores one of those fields still yields a stable sequence.
        """
        if doc_id in _doc_order_cache:
            return _doc_order_cache[doc_id]
        from common import settings
        from common.doc_store.doc_store_base import OrderByExpr
        from common.misc_utils import thread_pool_exec
        from rag.nlp import search as _rag_search

        ordered: list[dict] = []
        try:
            index_names = [_rag_search.index_name(t) for t in tools.tenant_ids]
            fields = ["id", "content_with_weight", "docnm_kwd", "doc_id", "page_num_int", "top_int"]
            order = OrderByExpr()
            try:
                order.asc("page_num_int")
                order.asc("top_int")
            except Exception:
                order = OrderByExpr()
            res = await thread_pool_exec(settings.docStoreConn.search, fields, [], {"doc_id": [doc_id]}, [], order, 0, _DOC_ORDERED_LIMIT, index_names, tools.kb_ids)
            rows = settings.docStoreConn.get_fields(res, fields) or {}
            for cid, row in rows.items():
                ordered.append(
                    {
                        "chunk_id": cid,
                        "content_with_weight": row.get("content_with_weight") or "",
                        "docnm_kwd": row.get("docnm_kwd") or "",
                        "doc_id": row.get("doc_id") or doc_id,
                        "_order": _position_key(row),
                    }
                )
            ordered.sort(key=lambda c: c["_order"])
        except Exception:
            _LOG.exception("[Expand] ordered fetch failed for doc=%s", doc_id)
            ordered = []
        _doc_order_cache[doc_id] = ordered
        return ordered

    def _nearest_heading(ordered: list[dict], idx: int) -> str:
        """The closest markdown heading at or before ``idx``."""
        for i in range(idx, -1, -1):
            found = _HEADING_RE.findall(ordered[i].get("content_with_weight") or "")
            if found:
                return found[-1][1].strip()
        return ""

    async def _expand_chunk(chunk: dict) -> dict:
        """Widen a chunk to its neighbours and prefix its section heading.

        Keeps the ORIGINAL ``chunk_id`` so citations still resolve to the matched
        chunk; only the rendered content grows.
        """
        doc_id = chunk.get("doc_id")
        cid = _chunk_id(chunk)
        if not doc_id or not cid:
            return chunk
        ordered = await _doc_in_order(doc_id)
        if not ordered:
            return chunk
        idx = next((i for i, c in enumerate(ordered) if _chunk_id(c) == cid), None)
        if idx is None:
            return chunk
        lo = max(0, idx - _EXPAND_BEFORE)
        hi = min(len(ordered), idx + _EXPAND_AFTER + 1)
        body = "\n".join((ordered[i].get("content_with_weight") or "") for i in range(lo, hi)).strip()
        heading = _nearest_heading(ordered, idx)
        out = dict(chunk)
        out["content_with_weight"] = (f"Section: {heading}\n" if heading else "") + body
        return out

    async def _doc_head_chunks(doc_id: str) -> list[dict]:
        """Return the leading chunks of a document for field-style fallback reads."""
        ordered = await _doc_in_order(doc_id)
        if not ordered:
            return []
        head = ordered[:_DOC_HEAD_FALLBACK_LIMIT]
        chunks = []
        for chunk in head:
            chunks.append(
                {
                    "chunk_id": chunk.get("chunk_id"),
                    "content_with_weight": chunk.get("content_with_weight") or "",
                    "docnm_kwd": chunk.get("docnm_kwd") or "",
                    "doc_id": chunk.get("doc_id") or doc_id,
                }
            )
        return chunks

    # ── Node 3: retrieve_docs — rank candidate documents from the keywords ──
    async def retrieve_docs_node(state: KwV7State) -> dict:
        """Pick the top ``_DOCS_PER_SUBQ`` documents this round's chunks may come from.

        A wide, shallow pass first: one query over ``_DOC_TOP_N`` hits, scored per
        DOCUMENT by its best chunk. Confining the chunk search to those documents
        stops a handful of near-identical passages in one irrelevant file from
        filling the whole result set, which is how a lineup table from the wrong
        year, or a page's citation list, crowds out the page that answers.

        Ranking unions BROAD and NARROW formulations. BROAD protects recall when
        fact vocabulary is noisy, while NARROW recovers category/list pages where
        words such as "Best Actor" and "nominee" are the only signal that separates
        the target document from a family of near-neighbour award pages.
        """
        from common import settings

        if not getattr(tools, "tenant_ids", None) or not getattr(tools, "kb_ids", None):
            return {"doc_scope": []}

        forms = state.get("lookup_formulations") or []
        queries: list[tuple[str, str]] = []
        seen_queries: set[str] = set()
        for label in ("narrow", "broad", "alias-only"):
            for form in forms:
                if form.get("label") != label:
                    continue
                query = str(form.get("query") or "").strip()
                key = _norm(query)
                if query and key not in seen_queries:
                    seen_queries.add(key)
                    queries.append((label, query))
        if not queries:
            query = state.get("keywords") or state.get("current") or ""
            queries = [("fallback", query)]
        scoped = tools.scoped_doc_ids(None) if hasattr(tools, "scoped_doc_ids") else None
        tools.embed_mdl = None

        best: dict[str, float] = {}
        names: dict[str, str] = {}
        first_seen: dict[str, int] = {}
        for qidx, (label, query) in enumerate(queries):
            try:
                kbinfos = await settings.retriever.retrieval(
                    query,
                    tools.embed_mdl,
                    tools.tenant_ids,
                    tools.kb_ids,
                    1,
                    _DOC_TOP_N,
                    0.2,
                    vector_similarity_weight=0.3 if tools.embed_mdl else 0,
                    aggs=False,
                    highlight=False,
                    doc_ids=scoped,
                )
            except Exception:
                _LOG.exception("[Retrieve docs] %s failed for: %s", label, _snip(query))
                continue
            weight = 1.1 if label == "narrow" else 1.0
            for i, ck in enumerate((_normalize(kbinfos, tools.tenant_ids) or {}).get("chunks") or []):
                did = ck.get("doc_id")
                if not did:
                    continue
                # Retrieval is relevance-ordered; use similarity when present and
                # fall back to a rank decay so a document's BEST chunk sets score.
                try:
                    score = float(ck.get("similarity") or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                if score <= 0.0:
                    score = 1.0 / (i + 1.0)
                score *= weight
                if did not in best or score > best[did]:
                    best[did] = score
                names.setdefault(did, ck.get("docnm_kwd", "") or "")
                first_seen.setdefault(did, qidx * _DOC_TOP_N + i)

        ranked = sorted(best.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))[:_DOCS_PER_SUBQ]
        scope = [{"doc_id": did, "doc_nm": names.get(did, "")} for did, _ in ranked]
        _LOG.info("[Retrieve docs] ranked on %s -> %s", [label for label, _ in queries], [d["doc_nm"] or d["doc_id"] for d in scope] or "none")
        return {"doc_scope": scope}

    # ── Node 4: retrieve — union narrow/broad searches, tables flattened (no LLM) ──
    async def retrieve_node(state: KwV7State) -> dict:
        from common import settings

        if not getattr(tools, "tenant_ids", None) or not getattr(tools, "kb_ids", None):
            _LOG.warning("[Retrieve] skipped: no tenant or knowledge-base scope is available.")
            return {"chunks": []}

        fallback = state.get("current") or ""
        forms = state.get("lookup_formulations") or _lookup_formulations({}, fallback)
        doc_ids = tools.scoped_doc_ids(None) if hasattr(tools, "scoped_doc_ids") else None
        # Confine this round to the ranked documents. An empty scope means the
        # ranking pass found nothing, so fall back to the caller's scope rather
        # than searching an empty set and guaranteeing zero chunks.
        ranked_ids = [d["doc_id"] for d in (state.get("doc_scope") or []) if d.get("doc_id")]
        if ranked_ids:
            doc_ids = [d for d in ranked_ids if d in doc_ids] if doc_ids else ranked_ids
            if not doc_ids:
                _LOG.info("[Retrieve] ranked documents fall outside the caller's scope; keeping the caller's.")
                doc_ids = tools.scoped_doc_ids(None) if hasattr(tools, "scoped_doc_ids") else None
        tools.embed_mdl = None

        async def _one(form: dict) -> list[dict]:
            query = form.get("query") or fallback
            try:
                kbinfos = await settings.retriever.retrieval(
                    query,
                    tools.embed_mdl,
                    tools.tenant_ids,
                    tools.kb_ids,
                    1,
                    _CHUNKS_PER_QUERY,
                    0.2,
                    vector_similarity_weight=0.3 if tools.embed_mdl else 0,
                    aggs=False,
                    highlight=False,
                    doc_ids=doc_ids,
                )
            except Exception:
                _LOG.exception("[Retrieve] %s failed for: %s", form.get("label"), _snip(query))
                return []
            return _flatten_chunk_tables((_normalize(kbinfos, tools.tenant_ids) or {}).get("chunks") or [])

        results = await asyncio.gather(*[_one(form) for form in forms])
        chunks: list[dict] = []
        seen: set[str] = set()
        for batch in results:
            for chunk in batch:
                chunk_id = _chunk_id(chunk)
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                chunks.append(chunk)

        # Widen BEFORE narrowing: expansion reaches the text a chunk boundary cut
        # off, narrowing then trims whatever of it is irrelevant. Doing it the
        # other way round would re-read full neighbours and discard the trim.
        if chunks:
            expanded = await asyncio.gather(*[_expand_chunk(c) for c in chunks])
            grew = sum(1 for before, after in zip(chunks, expanded) if after.get("content_with_weight") != before.get("content_with_weight"))
            if grew:
                _LOG.info("[Expand] widened %d of %d chunk(s) by -%d/+%d with section headings.", grew, len(chunks), _EXPAND_BEFORE, _EXPAND_AFTER)
            chunks = list(expanded)

        narrowing_terms = ",".join(
            _clean_terms(
                [term for form in forms for term in (form.get("terms") or [])],
                32,
            )
        )
        narrowed = _narrow_by_keywords(chunks, narrowing_terms)
        if narrowed:
            _LOG.info("[Retrieve] narrowed %d chunk(s) to %d keyword-bearing(%d). [%s]", len(chunks), len(narrowed), sum([n["token_num"] for n in narrowed]), narrowing_terms)
            chunks = narrowed
        elif chunks:
            # Keyword matching is verbatim substring; retrieval matches tokens. A
            # chunk can be genuinely relevant without containing any keyword as
            # written, so an empty narrowing means the filter was too strict here,
            # not that the results were worthless.
            _LOG.info("[Retrieve] narrowing removed every chunk; keeping the %d retrieved as-is.", len(chunks))

        _LOG.info(
            "[Retrieve] %d chunk(s) from %d formulation(s) for: %s",
            len(chunks),
            len(forms),
            _snip(state.get("current") or ""),
        )
        return {"chunks": chunks}

    # ── Assessment (batched, budget-bounded) ──

    def _assessment_body(chunk: dict) -> str:
        title = str(chunk.get("docnm_kwd") or "")
        content = str(chunk.get("content_with_weight") or chunk.get("content") or "").strip()
        return "Title: " + title + "\n--------------------------\n" + content

    def _assessment_prompt(question: str, bodies: list[str]) -> str:
        listing = "\n\n".join(f"[{i + 1}] {body}" for i, body in enumerate(bodies))
        return f"Question:\n{question}\n\nConsecutive chunks:\n{listing}\n\nOutput JSON:"

    def _assessment_batches(question: str, chunks: list[dict]) -> list[list[tuple[int, dict, str]]]:
        """Group adjacent chunks while keeping each assessment prompt inside the budget."""
        budget = max(1, int(int(getattr(tools.chat_mdl, "max_length", 8192) or 8192) * 0.3))
        batches: list[list[tuple[int, dict, str]]] = []
        current: list[tuple[int, dict, str]] = []
        for index, chunk in enumerate(chunks):
            body = _assessment_body(chunk)
            bodies = [item[2] for item in current] + [body]
            if current and num_tokens_from_string(_ASSESS_SYSTEM + _assessment_prompt(question, bodies)) > budget:
                batches.append(current)
                current = []
            current.append((index, chunk, body))
        if current:
            batches.append(current)
        return batches

    async def _assess_batch(question: str, batch: list[tuple[int, dict, str]]) -> list[str]:
        """Judge one batch, returning a ``full|partial|none`` status per chunk."""
        parsed = await _llm_json(_ASSESS_SYSTEM, _assessment_prompt(question, [item[2] for item in batch]))
        raw = parsed.get("statuses") if isinstance(parsed, dict) else []
        statuses = ["none"] * len(batch)
        if not isinstance(raw, list):
            return statuses
        for position, item in enumerate(raw):
            slot = position
            if isinstance(item, dict):
                try:
                    slot = int(item.get("chunk", item.get("index", position + 1))) - 1
                except (TypeError, ValueError):
                    slot = position
                status = str(item.get("status") or "").strip().lower()
            else:
                status = str(item or "").strip().lower()
            if 0 <= slot < len(statuses) and status in ("full", "partial", "none"):
                statuses[slot] = status
        return statuses

    async def _assess_chunks(question: str, chunks: list[dict]) -> tuple[str, list[dict]]:
        """Assess chunks in bounded batches, keeping the useful ones.

        Returns ``(status, useful chunks)``. Judging one chunk at a time stops a
        single answer-bearing chunk from being outvoted by the bulk around it, and
        the batch loop stops as soon as some chunk answers outright — later batches
        cannot improve on "full". A result built only from partials is NOT settled:
        the caller records the question as asked so the planner revisits it.
        """
        useful: list[dict] = []
        picked: list[str] = []
        status_out = "none"
        for batch in _assessment_batches(question, chunks):
            statuses = await _assess_batch(question, batch)
            for (index, chunk, _), status in zip(batch, statuses):
                if status == "none":
                    continue
                useful.append(chunk)
                picked.append(f"{index + 1}:{status}")
                if status == "full":
                    status_out = "full"
                elif status_out != "full":
                    status_out = "partial"
            if status_out == "full":
                break
        if picked:
            _LOG.info("[Assess] %s — useful chunk(s) %s of %d for: %s", status_out.upper(), ", ".join(picked), len(chunks), _snip(question))
        return status_out, useful

    # ── Node 4: assess — judge each chunk, then brief the useful ones ──
    async def assess_node(state: KwV7State) -> dict:
        current = state.get("current") or ""
        chunks = state.get("chunks") or []
        doc_scope = state.get("doc_scope") or []
        it = state.get("iteration", 0)
        asked = list(state.get("asked") or [])
        evidences = list(state.get("evidences") or [])
        pool = list(state.get("pool") or [])

        def _give_up() -> dict:
            if current and not any(_norm(a) == _norm(current) for a in asked):
                asked.append(current)
            return {"evidences": evidences, "asked": asked, "pool": pool}

        if not chunks:
            _LOG.info("[Assess] no chunks retrieved — recording the question as asked.")
            return _give_up()

        status, useful = await _assess_chunks(current, chunks)
        if status == "none" or not useful:
            if doc_scope:
                fallback_chunks: list[dict] = []
                for doc in doc_scope[:_DOCS_PER_SUBQ]:
                    fallback_chunks.extend(await _doc_head_chunks(doc.get("doc_id") or ""))
                fallback_chunks = [c for c in fallback_chunks if _chunk_id(c)]
                if fallback_chunks:
                    _LOG.info("[Assess] retrying with %d head chunk(s) from ranked docs.", len(fallback_chunks))
                    status, useful = await _assess_chunks(current, fallback_chunks)
                    if status != "none" and useful:
                        chunks = fallback_chunks
            if status == "none" or not useful:
                _LOG.info("[Assess] INSUFFICIENT for: %s", _snip(current))
                return _give_up()

        # Brief only the USEFUL chunks. Judging and answering are separate jobs, and
        # the brief is written knowing the ORIGINAL question it has to serve, so it
        # keeps the exact date/number/name the later steps will need.
        parsed = await _llm_json(
            _BRIEF_ANSWER_SYSTEM,
            f"Question:\n{current}\n\nORIGINAL question this serves:\n{state.get('formalized') or ''}\n\nChunks:\n{_numbered(useful)}\n\nOutput JSON:",
        )
        answer = str(parsed.get("answer") or "").strip()
        if not answer:
            _LOG.info("[Assess] useful chunks found but no brief was produced — recording the question as asked.")
            return _give_up()

        cited = _pick(useful, parsed.get("relevant")) or useful
        seen = {_chunk_id(c) for c in pool}
        for c in cited:
            cid = _chunk_id(c)
            if cid not in seen:
                seen.add(cid)
                pool.append(c)
        evidences.append(
            {
                "iteration": it,
                "question": current,
                "answer": answer,
                "status": status,
                "chunk_ids": [_chunk_id(c) for c in cited],
            }
        )
        _LOG.info("[Assess] ANSWERED (%s): %s -> %s", status, _snip(current), _snip(answer))
        return {"evidences": evidences, "asked": asked, "pool": pool}

    # ── Node 5: sufficiency — all evidence vs the original question ──
    async def sufficiency_node(state: KwV7State) -> dict:
        evidences = state.get("evidences") or []
        question = state.get("formalized") or state.get("question") or ""
        composed_children = _maybe_compose_child_count_answer(question, evidences, state.get("asked") or [])
        composed_age = _maybe_compose_age_answer(question, evidences)
        facts = _facts_listing(evidences)
        verdict = await _llm_json(
            _SUFFICIENCY_SYSTEM,
            f"Facts discovered so far:\n{facts}\n\nOriginal question:\n{question}\n\nOutput JSON:",
        )
        sufficient = bool(verdict.get("sufficient")) or (composed_age is not None and "years old" in composed_age[0]) or (composed_children is not None and "children combined" in composed_children[0])
        it = state.get("iteration", 0) + 1
        _LOG.info("[Sufficiency] round %d → sufficient=%s (%d evidence).", it, sufficient, len(evidences))
        return {"sufficient": sufficient, "iteration": it, "partial": (not sufficient) and it >= max_iterations}

    # ── Node 6: next_question — one simple, atomic follow-up ──
    async def next_question_node(state: KwV7State) -> dict:
        evidences = state.get("evidences") or []
        asked = state.get("asked") or []
        facts = _facts_listing(evidences)
        formalized = state.get("formalized") or ""
        # Everything attempted blocks a repeat: the failures in `asked`, and the
        # questions that succeeded, which live on their evidence records.
        attempted = list(asked) + [e["question"] for e in evidences]

        roster = _extract_child_count_roster(formalized)
        if roster:
            attempted_names = {_norm(_extract_child_count_name(q)) for q in attempted if _extract_child_count_name(q)}
            next_name = next((name for name in roster if _norm(name) not in attempted_names), "")
            if next_name:
                m = re.search(r"\bas of\s+([^?.;]+)", formalized, re.I)
                suffix = f" as of {m.group(1).strip()}" if m else ""
                nxt = f"How many children did {next_name} have{suffix}?"
                _LOG.info("[Next question] roster mode → %s", _snip(nxt))
                return {
                    "current": nxt,
                    "keywords": "",
                    "keyword_slots": {},
                    "lookup_formulations": [],
                    "doc_scope": [],
                    "chunks": [],
                    "partial": False,
                }
            _LOG.info("[Next question] roster mode exhausted; answering with what is known.")
            return {
                "current": "",
                "keywords": "",
                "keyword_slots": {},
                "lookup_formulations": [],
                "doc_scope": [],
                "chunks": [],
                "partial": True,
            }

        parts = [
            f"Original question:\n{formalized}",
            f"Facts discovered so far:\n{facts}",
        ]
        if attempted:
            parts.append("Already asked (never repeat or rephrase any of these):\n" + "\n".join(f"- {a}" for a in attempted))
        parts.append("Output JSON:")

        parsed = await _llm_json(_NEXT_QUESTION_SYSTEM, "\n\n".join(parts))
        nxt = str(parsed.get("question") or "").strip()
        if nxt and any(_norm(a) == _norm(nxt) for a in attempted):
            _LOG.info("[Next question] discarded a repeat of an earlier question: %s", _snip(nxt))
            nxt = ""
        _LOG.info("[Next question] round %d → %s", state.get("iteration", 0), _snip(nxt) if nxt else "(none — answering with what is known)")
        return {
            "current": nxt,
            "keywords": "",
            "keyword_slots": {},
            "lookup_formulations": [],
            "doc_scope": [],
            "chunks": [],
            "partial": not nxt,
        }

    # ── Node 7: answer — brief cited answer, full or partial (streamed) ──
    async def answer_node(state: KwV7State) -> dict:
        evidences = [e for e in (state.get("evidences") or []) if e.get("answer")]
        question = state.get("formalized") or state.get("question") or ""
        asked = state.get("asked") or []
        composed = _maybe_compose_child_count_answer(question, evidences, asked) or _maybe_compose_age_answer(question, evidences)
        if composed:
            answer, evidence_idxs = composed
            pool = state.get("pool") or []
            tools.kbinfos = {"chunks": pool, "doc_aggs": _doc_aggs_from(pool)}
            id_of = {_chunk_id(c): i for i, c in enumerate(pool)}
            cited_chunk_ids: list[int] = []
            for idx in evidence_idxs:
                if 0 <= idx < len(evidences):
                    cited_chunk_ids.extend(id_of[cid] for cid in (evidences[idx].get("chunk_ids") or []) if cid in id_of)
            cite = " ".join(f"[ID:{i}]" for i in sorted(set(cited_chunk_ids)))
            final = f"{answer}" + (f" {cite}" if cite else "")
            _LOG.info("[Answer] deterministic answer from %d finding(s).", len(evidence_idxs))
            token_queue.put_nowait(final)
            return {"final_answer": final}

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
            findings.append(f"- {e['question']}: {e['answer']}" + (f"  (cite: {cite})" if cite else ""))

        system = FINAL_ANSWER_SYSTEM.format(cite_rules=citation_prompt(tools.user_defined_prompts).strip())
        system += _NO_REASONING_RULE
        head = f"Question:\n{state.get('formalized') or state.get('question') or ''}\n\n"
        partial = bool(state.get("partial")) or not state.get("sufficient")
        # Out of rounds with findings in hand is a different situation from having
        # run out of angles: the graph stops asking, but a labelled inference from
        # what was found beats discarding it.
        best_effort = partial and state.get("iteration", 0) >= max_iterations
        if best_effort:
            system += _BEST_EFFORT_RULE
            head += _BEST_EFFORT_PREAMBLE + "\n\n"
        elif partial:
            head += _PARTIAL_PREAMBLE + "\n\n"
        body = (
            "Answer from the findings below. Each finding shows the [ID:n] citation markers that "
            "support it — reuse those exact markers in your answer; do not invent IDs.\n\n"
            "Findings:\n" + "\n".join(findings)
        )
        if best_effort:
            body += "\n\nMake ONE explicit best estimate for the unresolved factor, and only where the findings make it plausible. Present it as an inference, not a confirmed fact."
        _, msg = message_fit_in(form_message(system, head + body), tools.chat_mdl.max_length)

        mode = "BEST-EFFORT" if best_effort else ("PARTIAL" if partial else "Full")
        _LOG.info("[Answer] %s answer from %d finding(s), citing a pool of %d chunk(s).", mode, len(findings), len(pool))
        final = ""
        try:
            async for tok in tools.chat_mdl.async_chat_streamly_delta(msg[0]["content"], msg[1:], answer_conf):
                token_queue.put_nowait(tok)
                final += tok
        except Exception:
            _LOG.exception("[Answer] stream failed")
            token_queue.put_nowait("I'm sorry, I encountered an error while composing the answer.")
        return {"final_answer": final}

    # ── Node 8: restart — second chance after the per-attempt iteration budget ──
    async def restart_node(state: KwV7State) -> dict:
        attempt = state.get("global_iteration", 0) + 1
        question = state.get("formalized") or state.get("question") or ""
        _LOG.info(
            "[Restart] attempt %d/%d exhausted after %d round(s); clearing research state for attempt %d/%d: %s",
            attempt,
            max_global_iter,
            state.get("iteration", 0),
            attempt + 1,
            max_global_iter,
            _snip(question),
        )
        return {
            "current": question,
            "keywords": "",
            "keyword_slots": {},
            "lookup_formulations": [],
            "doc_scope": [],
            "chunks": [],
            "evidences": [],
            "asked": [],
            "pool": [],
            "iteration": 0,
            "global_iteration": attempt,
            "sufficient": False,
            "partial": False,
            "final_answer": "",
        }

    def _route_after_sufficiency(state: KwV7State) -> str:
        if state.get("sufficient"):
            return "answer"
        if state.get("iteration", 0) >= max_iterations:
            if state.get("global_iteration", 0) + 1 < max_global_iter:
                return "restart"
            return "answer"
        return "next_question"

    def _route_after_next_question(state: KwV7State) -> str:
        return "keywords" if state.get("current") else "answer"

    g = StateGraph(KwV7State)
    g.add_node("formalize", formalize_node)
    g.add_node("keywords", keywords_node)
    g.add_node("retrieve_docs", retrieve_docs_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("assess", assess_node)
    g.add_node("sufficiency", sufficiency_node)
    g.add_node("next_question", next_question_node)
    g.add_node("answer", answer_node)
    g.add_node("restart", restart_node)

    g.add_edge(START, "formalize")
    g.add_edge("formalize", "keywords")
    g.add_edge("keywords", "retrieve_docs")
    g.add_edge("retrieve_docs", "retrieve")
    g.add_edge("retrieve", "assess")
    g.add_edge("assess", "sufficiency")
    g.add_conditional_edges("sufficiency", _route_after_sufficiency, {"next_question": "next_question", "restart": "restart", "answer": "answer"})
    g.add_conditional_edges("next_question", _route_after_next_question, {"keywords": "keywords", "answer": "answer"})
    g.add_edge("restart", "keywords")
    g.add_edge("answer", END)
    return g.compile()


async def run_keyword_agentic_rag_v7(
    tools,
    messages: list,
    max_iterations: int = 13,
    max_global_iter: int = 2,
    gen_conf: dict | None = None,
):
    """Drive the v7 graph, yielding answer-token strings."""
    max_global_iter = max(1, int(max_global_iter or 1))
    question = ""
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content"):
            question = m["content"]
            break

    token_queue: asyncio.Queue = asyncio.Queue()
    graph = build_keyword_agentic_graph_v7(
        tools,
        token_queue,
        messages=messages,
        gen_conf=gen_conf,
        max_iterations=max_iterations,
        max_global_iter=max_global_iter,
    )
    _SENTINEL = object()
    holder: dict[str, Any] = {}

    async def _drive():
        try:
            holder["state"] = await graph.ainvoke(
                {
                    "question": question,
                    "max_iterations": max_iterations,
                    "max_global_iter": max_global_iter,
                    "global_iteration": 0,
                    "iteration": 0,
                    "evidences": [],
                    "asked": [],
                    "pool": [],
                    "partial": False,
                },
                {"recursion_limit": max(25, max_global_iter * (max_iterations * 8 + 10))},
            )
        except Exception:
            _LOG.exception("run_keyword_agentic_rag_v7: graph execution failed")
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
