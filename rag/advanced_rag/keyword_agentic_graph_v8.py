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

"""Keyword-driven iterative search graph — v6 (LangGraph).

A deliberately small chain-of-thought baseline. Where v5 fans out into a batch
of sub-questions per round and carries classification, arithmetic, structural
expansion and an exhaustion ledger, v6 keeps exactly ONE question in flight and
loops: ask it, search it, answer it if the chunks allow, otherwise think of the
next question. Every round adds at most one evidence, and the final answer is
composed from the evidence list.

Flow:

    formalize → keywords → retrieve → assess → compute → sufficiency ─(sufficient)→ answer → END
                   ↑                                     │        └──(out of rounds)──┐
                   │                                     ↓(not yet)                   │
                   └──────(new question)────────── next_question ──(nothing new)──────┤
                   ↑                                                                  ↓
                   └────────────(board wiped, attempt left)──────────────────────── retry
                                                                (no attempt left)→ answer

An attempt that ends without an answer is not the end of the run: while a retry is
left, `retry` clears every trace of it — evidence, chunk pool, asked questions, round
counter — and researches the original question again from `keywords`.

Two simplifications are deliberate, and both cost something:

* The search query is UNCAPPED — every keyword, synonym and date/number variant
  goes into one query string. That maximises surface coverage, but BM25 mass is
  spread across the terms, so a rare literal (a patent number, a catalogue id)
  ranks lower than it would as a query on its own.
* There is NO structural expansion. Chunks are assessed exactly as retrieved, so
  a fact sitting one chunk away from its match is not reached, and no section
  heading is attached — meaning v6 cannot tell that a chunk came from a
  ``## References`` list rather than from the body of an article.

v5 remains the richer graph; v6 exists to measure how much of v5's accuracy
comes from its machinery rather than from the underlying retrieval.
"""

from __future__ import annotations

import ast
import asyncio
import logging
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

# Deterministic number/word hints are identical to v4's.

# Table flattening (including the banner-row handling) is shared with v5.
from rag.advanced_rag.keyword_agentic_graph_v5 import _flatten_chunk_tables

_LOG = logging.getLogger(__name__)

# Tunable caps.
_CHUNKS_PER_QUERY = 8  # chunks retrieved per round (higher than v5: nothing inflates them here)
_ENTITY_REPEAT = 3  # copies of each ENTITY term in the query, to weight it up

# Entity terms are what discriminate; fact-type vocabulary and qualifiers are common
# words that would otherwise take an equal share of the query's mass. The fix is to
# weight the entity up inside the SAME query rather than to run a second one.
# ``term_weight.weights`` keeps duplicate tokens and then normalises every weight by
# their sum, so with R the raw weight of everything else, k copies of an entity claim
# k*w / (R + k*w) of the query's mass instead of w / (R + w) — larger for every k > 1
# and rising monotonically toward 1, so no threshold applies.
# The same holds for a multi-word entity's PHRASE clause: quotes cannot be passed in
# (``FulltextQueryer.question`` strips them before tokenising) and the clause is built
# from ADJACENT tokens instead, so contiguity is what makes the phrase and repetition
# is what weights it.
# One query, one ranked result set, no merge step.

# The four aspects the keyword prompt returns, in query order.
_KEYWORD_ASPECTS = ("entity", "aliases", "fact_type", "qualifiers")


# ── Arithmetic over the evidence ───────────────────────────────────────────────
# Some questions ask for a number no source states: the combined population of
# three counties, how many of the listed films won an award, the years between two
# dates. Every input is in the evidence by then, and only the arithmetic is missing
# — which an LLM does by writing digits one at a time and gets wrong often enough
# to matter. So the LLM writes the expression and Python evaluates it.
#
# The expression comes from a language model, so it is NOT trusted. It is parsed
# and every node checked against the whitelist below BEFORE evaluation; anything
# unlisted — an attribute access, a subscript, a lambda, an f-string, a name that
# is not one of the allowed functions — is rejected outright rather than sandboxed
# at run time. Evaluation then runs with no builtins at all.
_COMPUTE_MAX_CHARS = 400  # the whole expression; every figure is inline, none is long
_COMPUTE_MAX_EXPONENT = 64  # so no `**` can be made to allocate its way out
_COMPUTE_MAX_POW_BASE = 10**6


def _letters(*texts: object) -> int:
    """Count the alphabetic characters across the given names.

    "How many letters are in these names" is a real question, and every plain-Python
    way to answer it needs machinery this evaluator refuses — an attribute call
    (``"".join``, ``str.isalpha``) or a comprehension. So it is a function instead.

    Spaces, hyphens, apostrophes, digits and punctuation do NOT count; letters
    carrying diacritics DO ("José" is 4), because they are letters of the name.
    Takes any number of names, or a single list of them.
    """
    total = 0
    for text in texts:
        for item in text if isinstance(text, (list, tuple, set)) else [text]:
            if not isinstance(item, str):
                raise TypeError(f"letters() takes names, not {type(item).__name__}")
            total += sum(1 for ch in item if ch.isalpha())
    return total


def _digit_sum(*texts: object) -> int:
    """Add up the decimal digits inside the given values.

    "What do you get when you add up the numbers in the postcode" is a real
    question, and the plain-Python answer needs the comprehension this evaluator
    refuses — so, like ``letters``, it is a function instead of an expression.

    Every digit is added SEPARATELY: digit_sum("L7 7BN") is 14 and digit_sum("2020")
    is 4. A question that means whole numbers added together ("66 + 12") is written
    with those literals instead, because the value of a multi-digit number is not
    the sum of anything.

    Only ASCII digits count — a superscript or a fraction glyph is not a digit of
    the postcode. Takes any number of strings or whole numbers, or a list of them.
    """
    total = 0
    for text in texts:
        for item in text if isinstance(text, (list, tuple, set)) else [text]:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                raise TypeError(f"digit_sum() takes text or whole numbers, not {type(item).__name__}")
            total += sum(int(ch) for ch in str(item) if "0" <= ch <= "9")
    return total


# Pure arithmetic on literals. Nothing here can reach an object, a module or a name.
_COMPUTE_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "len": len,
    "int": int,
    "float": float,
    "sorted": sorted,
    "letters": _letters,
    "digit_sum": _digit_sum,
}

_COMPUTE_NODES = (
    ast.Expression,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Load,
    ast.Name,
    ast.Call,
    ast.IfExp,
    ast.UnaryOp,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


# Functions whose result is a number whatever they are handed. `min`, `max` and
# `sum` are absent on purpose — min("b", "a") is a string — and `sorted` returns a
# list, so neither may stand where a number is required.
_COMPUTE_ALWAYS_NUMERIC = {"abs", "round", "int", "float", "len", "letters", "digit_sum"}


def _is_numeric(node: ast.AST) -> bool:
    """True when ``node`` can ONLY evaluate to a number.

    Multiplication is the one operator that turns a short expression into an
    arbitrarily large object — ``"a" * 10**9``, ``[1] * 10**9`` — so both its
    operands must be provably numeric. Everything else in the whitelist either
    cannot grow (``+`` on literals is bounded by the expression length) or is
    already constrained (``**`` takes plain numbers only).
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float))  # bool included: it IS an int
    if isinstance(node, ast.UnaryOp):
        return _is_numeric(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_numeric(node.left) and _is_numeric(node.right)
    if isinstance(node, ast.IfExp):
        return _is_numeric(node.body) and _is_numeric(node.orelse)
    if isinstance(node, ast.Compare):
        return True  # a comparison is a bool, and a bool is an int
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _COMPUTE_ALWAYS_NUMERIC:
            return True
        if node.func.id in {"sum", "min", "max"}:
            return all(_is_numeric(arg) or _is_numeric_sequence(arg) for arg in node.args)
    return False


def _is_numeric_sequence(node: ast.AST) -> bool:
    """True for a literal list/tuple/set whose every element is provably numeric."""
    return isinstance(node, (ast.List, ast.Tuple, ast.Set)) and all(_is_numeric(element) for element in node.elts)


def _check_expression(tree: ast.AST) -> str:
    """Reject anything outside the arithmetic whitelist. Returns "" when clean."""
    for node in ast.walk(tree):
        if not isinstance(node, _COMPUTE_NODES):
            return f"{type(node).__name__} is not allowed"
        if isinstance(node, ast.Name) and node.id not in _COMPUTE_FUNCTIONS:
            return f"unknown name {node.id!r}"
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _COMPUTE_FUNCTIONS:
                return "only the listed functions may be called"
            if node.keywords:
                return "keyword arguments are not allowed"
            # `len("Ada Lovelace")` is 12 and the answer is 11. The gap is silent, so
            # the expression is refused rather than counted.
            if node.func.id == "len" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                return "len() of a name counts spaces and punctuation — use letters() for letters"
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool, str)):
            return f"{type(node.value).__name__} literals are not allowed"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult) and not (_is_numeric(node.left) and _is_numeric(node.right)):
            return "multiplication needs a number on both sides"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            # Both sides must be plain numbers, and both bounded: chained powers are
            # how a short expression turns into an unbounded computation.
            for side, limit in ((node.left, _COMPUTE_MAX_POW_BASE), (node.right, _COMPUTE_MAX_EXPONENT)):
                inner = side.operand if isinstance(side, ast.UnaryOp) else side
                if not (isinstance(inner, ast.Constant) and isinstance(inner.value, (int, float)) and not isinstance(inner.value, bool)):
                    return "** needs a plain number on both sides"
                if abs(inner.value) > limit:
                    return f"** operand {inner.value} exceeds {limit}"
    return ""


def _format_number(value: float | int) -> str:
    """Render a computed number without float noise ("3.0" -> "3", 0.1+0.2 -> "0.3")."""
    if isinstance(value, int):
        return str(value)
    if value == int(value) and abs(value) < 10**15:
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _compute(expression: str) -> tuple[str, str]:
    """Evaluate an LLM-written arithmetic expression. Returns (rendered, error).

    Exactly one of the two is non-empty. Every rejection is a normal outcome — the
    graph simply carries on without the computed evidence.
    """
    expression = (expression or "").strip()
    if not expression:
        return "", "empty expression"
    if len(expression) > _COMPUTE_MAX_CHARS:
        return "", f"expression is longer than {_COMPUTE_MAX_CHARS} characters"
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return "", f"does not parse ({exc.msg})"
    problem = _check_expression(tree)
    if problem:
        return "", problem
    try:
        value = eval(compile(tree, "<evidence-arithmetic>", "eval"), {"__builtins__": {}}, dict(_COMPUTE_FUNCTIONS))  # noqa: S307 — whitelisted arithmetic only, see _check_expression
    except Exception as exc:
        return "", f"failed to evaluate ({type(exc).__name__}: {exc})"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "", f"result is {type(value).__name__}, not a number"
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return "", "result is not a finite number"
    return _format_number(value), ""


_KEYWORDS_SYSTEM = """You turn ONE question into search terms for a keyword/BM25 search engine.

Emit the terms that would appear VERBATIM in a document that answers the question, sorted into FOUR
categories. Every term must come from the question itself or be a surface form of something in it.

A. "entity" — the specific thing the fact is ABOUT: proper nouns, titles, identifiers. Keep a
   multi-word entity whole, as ONE term ("Brown County", "Treaty of Versailles"); split across
   several terms its tokens match independently and drag in noise. A bare identifier — a serial,
   patent, catalogue or case number — is a complete entity on its own; never glue it to the words
   around it.
B. "aliases" — the engine matches ONLY the surface forms you supply, so emit the plausible variants
   of A: full vs. short name, native-language and transliterated forms, official vs. common name,
   acronym and its expansion, and the qualified form ("Brown County" -> "Brown County, Kansas").
C. "fact_type" — 3 to 6 words the corpus might use for this KIND of fact, since you cannot know how
   it is phrased. Spread them across registers:
     quantity of people -> population, inhabitants, residents, census, demographics, headcount
     time of an event   -> founded, established, opened, dated, began
     role of a person   -> served, appointed, elected, held, director
   SOURCES TABULATE WHAT QUESTIONS SPELL OUT: a statistic named in prose is usually written in a
   table as a column abbreviation, and the prose wording may not appear in the document at all. So
   include the abbreviation a table would use — "points per game" -> "PPG", "PTS"; "earnings per
   share" -> "EPS"; "games played" -> "GP" — and reach a superlative through its plain column too:
   "leading scorer" is found by looking for "PTS" and "PPG", not for the phrase itself.
D. "qualifiers" — year, edition, jurisdiction, revision. Worth emitting even when it looks
   redundant: the qualifier often sits in a table header or a document title that chunking has
   severed from the value. Include EVERY alternative expression of a DATE or NUMBER in the
   question — ordinals and their words ("21st" -> "twenty-first"), digits and their words
   ("2000000" -> "two million", "2 million"), and each common date format ("Aug 2nd" -> "August 2",
   "2 August", "08-02").

A and B are what FINDS the document; C and D only boost the ranking. So never withhold an entity
because you are unsure of it, and never pad C or D to reach a count.

DROP entirely: question words ("which", "who", "when", "how many"), relational scaffolding, and
generic high-frequency nouns ("year", "number", "city", "total", "list", "information"). They cost
ranking quality and retrieve nothing.

Output ONLY JSON, no prose, no code fences:
{"entity": ["<term>", ...], "aliases": ["<term>", ...], "fact_type": ["<term>", ...], "qualifiers": ["<term>", ...]}
Any category may be empty; leave "entity" empty only when the question truly names nothing."""

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
outside knowledge, no reasoning shown (except for common public knowledge like a country's capital, etc.).
- Keep every detail the ORIGINAL question will later need: exact dates, exact numbers, full names,
  units. A summary that drops the figure is useless to the step that follows.
- If the QUESTION asks for a set or a list, name EVERY member the chunks state, not just the first.
- Answer the question that was ASKED. If the chunks state a closely related figure instead, give the
  asked-for value only when it follows from them by a trivial, certain derivation, and say which
  figure it came from. Otherwise state plainly what the chunks do say.
- "relevant": the NUMBERS of the chunks your answer rests on.
- If nothing useful detail can support answer the QUESTION, keep "answer" and "relevant" empty.
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

_COMPUTE_SYSTEM = """You are given the ORIGINAL question and every fact discovered so far. Decide
whether that question asks for a NUMBER that NO fact states outright but that FOLLOWS ARITHMETICALLY
from figures the facts DO state — a sum, a difference, a count, an average, a percentage, a unit
conversion, an elapsed span.

If it does, compute it by writing ONE Python expression with every figure substituted as a literal.
The expression is evaluated on its own: no variables, no assignments, no imports, no attributes, no
subscripts. The only functions available are abs, round, min, max, sum, len, int, float, sorted,
letters and digit_sum.
  combined population of three  -> 12345 + 6789 + 101112
  how many of the listed items  -> len(["Alpha", "Beta", "Gamma"])
  what percentage one figure is -> 100 * 4523 / 18092
  years between two dates       -> 1998 - 1954
  letters in a set of names     -> letters("Ada Lovelace", "Alan Turing")
  digits of a postcode added up -> digit_sum("L7 7BN")

ADDING UP THE DIGITS of a postcode, a house number, a serial number, a year or an address: use
digit_sum(...), and never read the digits out by hand. It adds each digit separately, which is what
such a question means — digit_sum("L7 7BN") is 7+7 = 14, digit_sum("2020") is 2+0+2+0 = 4. Pass the
identifier EXACTLY as the facts write it, letters and spaces included; they are ignored. It is the
WRONG tool for whole numbers the facts state separately — two populations, two prices, two years are
added as plain literals (12345 + 6789), not fed to digit_sum.

COUNTING LETTERS: use letters(...), NEVER len(...) on a name. len counts spaces, hyphens and
apostrophes as though they were letters, so it is wrong by exactly the amount nobody notices
(len("Ada Lovelace") is 12; the name has 11 letters). letters(...) takes any number of names, or one
list of them, and counts alphabetic characters only. Spell each name EXACTLY as the facts give it,
including any middle name or accent — and if the facts do not show a name in full, that figure is
missing, so return "needed": false rather than counting a partial name.

Return "needed": false, with an empty expression, whenever ANY of these holds:
- the ORIGINAL question does not ask for a number;
- a fact already states that number outright — a value you would only be restating is not a
  calculation;
- any figure the calculation needs is missing from the facts, or a list the count depends on is not
  shown to be complete. NEVER invent, estimate, recall or infer a figure. Missing input means
  "needed": false, and that is the CORRECT answer in that case — a wrong number is worse than none.

"label" names what the number IS, as a short noun phrase ("combined population of the three
counties"), so a later step can use the result without re-deriving it.
"uses" lists the ROUND NUMBERS of the facts whose figures you substituted.
Output ONLY JSON, no prose, no code fences:
{"needed": true/false, "expression": "<one Python expression, or empty>", "label": "<short noun phrase>", "uses": [<round number>, ...]}"""

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


class KwV6State(TypedDict, total=False):
    question: str  # the raw last user message
    formalized: str  # the standalone question rebuilt from the conversation
    current: str  # the question being researched THIS round
    keywords: str  # search terms for `current`, one copy each, comma-separated
    query: str  # the string actually searched: `keywords` with the entity weighted up
    chunks: list  # chunks retrieved for `current`
    evidences: list  # [{iteration, question, answer, chunk_ids}]
    asked: list  # questions attempted that produced no evidence
    pool: list  # retained chunks — the citation set, dedup by id
    iteration: int
    max_iterations: int
    retries: int  # attempts abandoned so far — a retry wipes every field above it
    sufficient: bool
    partial: bool
    final_answer: str


def _numbered(chunks: list[dict]) -> str:
    """Render chunks as a 1-based numbered listing for the assessor."""
    return "\n\n".join(f"[{i + 1}] Title: {c.get('docnm_kwd') or ''}\n{(c.get('content_with_weight') or c.get('content') or '').strip()}" for i, c in enumerate(chunks))


def _facts_listing(evidences: list[dict]) -> str:
    """Render the evidence for the planner, flagging what is not a settled fact.

    A brief assembled from "partial" chunks answered around the question rather
    than stating the value, so the planner must be able to tell it apart from a
    settled fact — otherwise it moves on as though the hop were closed. A
    "computed" fact is flagged for the opposite reason: no source states it, so a
    reader must not go looking for the sentence it came from.
    """
    lines = []
    for e in evidences:
        mark = ""
        if e.get("status") == "computed":
            mark = "   [COMPUTED — arithmetic over the facts above, not stated by any source]"
        elif e.get("status") == "partial":
            mark = "   [PARTIAL — no chunk stated the value outright]"
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


def build_keyword_agentic_graph_v8(
    tools,
    token_queue: asyncio.Queue,
    messages: list | None = None,
    gen_conf: dict | None = None,
    max_iterations: int = 5,
    max_retry_number: int = 1,
):
    """Compile the v6 graph.

    :param messages: the full conversation, used once to rebuild a standalone
        question. Falls back to the raw question when unavailable.
    :param max_iterations: rounds of (keywords → retrieve → assess) before the
        attempt ends with whatever evidence it has.
    :param max_retry_number: extra attempts at the whole question after an attempt
        ends without an answer. Each one wipes the board — evidence, chunk pool,
        asked questions, round counter — and researches the question from scratch,
        so 0 restores the single-attempt behaviour and 1 allows two attempts.
    """
    answer_conf = dict(gen_conf) if gen_conf else {"temperature": 0.3}
    answer_conf.pop("direct_answer", None)

    async def _llm_json(system: str, user: str) -> dict:
        msg = await tools._fit_messages(system, user)
        ans = await tools.chat_mdl.async_chat(msg[0]["content"], msg[1:], {"temperature": 0.2})
        if isinstance(ans, tuple):
            ans = ans[0]
        return _extract_json(ans)

    # ── Node 1: formalize — rebuild a standalone question from the chat history ──
    async def formalize_node(state: KwV6State) -> dict:
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
            "partial": False,
        }

    # ── Node 2: keywords — four aspects of the question, the entity weighted up ──
    async def keywords_node(state: KwV6State) -> dict:
        current = state.get("current") or ""
        parsed = await _llm_json(_KEYWORDS_SYSTEM, f"Question:\n{current}\n\nOutput JSON:")

        # ONE dedup set across all four categories: a term the model emits as both an
        # entity and an alias must not collect a second share of the query's mass on
        # the strength of having been named twice.
        aspects: dict[str, list[str]] = {}
        seen: set[str] = set()
        for aspect in _KEYWORD_ASPECTS:
            terms: list[str] = []
            for k in parsed.get(aspect) or []:
                term = str(k).strip()
                key = _norm(term)
                if term and key and key not in seen:
                    seen.add(key)
                    terms.append(term)
            aspects[aspect] = terms

        # `keywords` is the plain union, one copy each — it is what narrows retrieved
        # chunks to their keyword-bearing sentences, where a repeated term would only
        # be deduped again. Fact-type vocabulary belongs in it: the sentence carrying
        # the value ("The population was 5,432") often does not name the entity.
        keywords = ", ".join(t for aspect in _KEYWORD_ASPECTS for t in aspects[aspect]) or current
        weighted = [t for t in aspects["entity"] for _ in range(_ENTITY_REPEAT)]
        weighted += [t for aspect in _KEYWORD_ASPECTS[1:] for t in aspects[aspect]]
        query = ", ".join(weighted) or keywords

        _LOG.info(
            "[Keywords] %s -> entity x%d: %s | aliases: %s | fact-type: %s | qualifiers: %s",
            _snip(current),
            _ENTITY_REPEAT,
            "; ".join(aspects["entity"]) or "-",
            "; ".join(aspects["aliases"]) or "-",
            "; ".join(aspects["fact_type"]) or "-",
            "; ".join(aspects["qualifiers"]) or "-",
        )
        return {"keywords": keywords, "query": query}

    # ── Node 3: retrieve — one search, tables flattened (no LLM) ──
    async def retrieve_node(state: KwV6State) -> dict:
        from common import settings

        if not getattr(tools, "tenant_ids", None) or not getattr(tools, "kb_ids", None):
            _LOG.warning("[Retrieve] skipped: no tenant or knowledge-base scope is available.")
            return {"chunks": []}

        query = state.get("query") or state.get("keywords") or state.get("current") or ""
        doc_ids = tools.scoped_doc_ids(None) if hasattr(tools, "scoped_doc_ids") else None

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
            _LOG.exception("[Retrieve] failed for: %s", _snip(query))
            return {"chunks": []}

        chunks = _flatten_chunk_tables((_normalize(kbinfos, tools.tenant_ids) or {}).get("chunks") or [])

        # Narrow each chunk to the sentences carrying a keyword, plus their
        # neighbours, and drop chunks holding no keyword at all. Block-level HTML and
        # markdown tables are protected spans, so a table stays whole rather than
        # being cut mid-row. Uses the plain keyword list, not the entity-weighted
        # query, whose repeated terms would only be split back out and deduped.
        narrowed = _narrow_by_keywords(chunks, state.get("keywords") or "")
        if narrowed:
            _LOG.info("[Retrieve] narrowed %d chunk(s) to %d keyword-bearing(%d).", len(chunks), len(narrowed), sum([c["token_num"] for c in narrowed]))
            chunks = narrowed
        elif chunks:
            # Keyword matching is verbatim substring; retrieval matches tokens. A
            # chunk can be genuinely relevant without containing any keyword as
            # written, so an empty narrowing means the filter was too strict here,
            # not that the results were worthless.
            _LOG.info("[Retrieve] narrowing removed every chunk; keeping the %d retrieved as-is.", len(chunks))

        _LOG.info("[Retrieve] %d chunk(s) for: %s", len(chunks), _snip(state.get("current") or ""))
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
    async def assess_node(state: KwV6State) -> dict:
        current = state.get("current") or ""
        chunks = state.get("chunks") or []
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

    # ── Node 5: compute — the one number the sources never state outright ──
    async def compute_node(state: KwV6State) -> dict:
        evidences = list(state.get("evidences") or [])
        if not evidences:
            return {}

        parsed = await _llm_json(
            _COMPUTE_SYSTEM,
            f"Facts discovered so far:\n{_facts_listing(evidences)}\n\nOriginal question:\n{state.get('formalized') or ''}\n\nOutput JSON:",
        )
        if not parsed.get("needed"):
            return {}
        expression = str(parsed.get("expression") or "").strip()
        if not expression:
            return {}
        # The same facts are re-examined every round, so a still-unanswered question
        # yields the same expression every round. Record it once.
        if any(_norm(str(e.get("expression") or "")) == _norm(expression) for e in evidences):
            return {}

        rendered, error = _compute(expression)
        if error:
            # A refused expression is not a failure of the round: the facts stand on
            # their own and sufficiency judges them unchanged.
            _LOG.info("[Compute] refused `%s` — %s", _snip(expression), error)
            return {}

        # Cite what the arithmetic was built from, so the computed value carries the
        # same [ID:n] markers as the figures that produced it.
        rounds = set()
        for n in parsed.get("uses") or []:
            try:
                rounds.add(int(n))
            except (TypeError, ValueError):
                continue
        sources = [e for e in evidences if e.get("iteration") in rounds] or evidences
        chunk_ids, seen = [], set()
        for e in sources:
            for cid in e.get("chunk_ids") or []:
                if cid not in seen:
                    seen.add(cid)
                    chunk_ids.append(cid)

        label = str(parsed.get("label") or "").strip() or "Value calculated from the facts found"
        evidences.append(
            {
                "iteration": state.get("iteration", 0),
                "question": label,
                "answer": f"{rendered}  (calculated as {expression})",
                "status": "computed",
                "expression": expression,
                "chunk_ids": chunk_ids,
            }
        )
        _LOG.info("[Compute] %s = %s  (%s)", _snip(expression), rendered, _snip(label))
        return {"evidences": evidences}

    # ── Node 6: sufficiency — all evidence vs the original question ──
    async def sufficiency_node(state: KwV6State) -> dict:
        evidences = state.get("evidences") or []
        facts = _facts_listing(evidences)
        verdict = await _llm_json(
            _SUFFICIENCY_SYSTEM,
            f"Facts discovered so far:\n{facts}\n\nOriginal question:\n{state.get('formalized') or ''}\n\nOutput JSON:",
        )
        sufficient = bool(verdict.get("sufficient"))
        it = state.get("iteration", 0) + 1
        _LOG.info("[Sufficiency] round %d → sufficient=%s (%d evidence).", it, sufficient, len(evidences))
        _LOG.info("[Evidences]: %s", facts)
        return {"sufficient": sufficient, "iteration": it, "partial": (not sufficient) and it >= max_iterations}

    # ── Node 7: next_question — one simple, atomic follow-up ──
    async def next_question_node(state: KwV6State) -> dict:
        evidences = state.get("evidences") or []
        asked = state.get("asked") or []
        facts = _facts_listing(evidences)
        # Everything attempted blocks a repeat: the failures in `asked`, and the
        # questions that succeeded, which live on their evidence records.
        attempted = list(asked) + [e["question"] for e in evidences]
        parts = [
            f"Original question:\n{state.get('formalized') or ''}",
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
        return {"current": nxt, "keywords": "", "chunks": [], "partial": not nxt}

    # ── Node 8: answer — brief cited answer, full or partial (streamed) ──
    async def answer_node(state: KwV6State) -> dict:
        evidences = [e for e in (state.get("evidences") or []) if e.get("answer")]
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

    # ── Node 9: retry — wipe the board and research the question again ──
    async def retry_node(state: KwV6State) -> dict:
        """Reset to what ``formalize`` produced, keeping only the question itself.

        An attempt that ends unanswered chose its questions, its keywords and its
        chunks in one connected trajectory; the second and third question were
        built on the evidence the first one found. Preserving any of it would steer
        the retry down the same path, so the board is wiped and only the standalone
        question survives.
        """
        retries = state.get("retries", 0) + 1
        formalized = state.get("formalized") or state.get("question") or ""
        _LOG.info(
            "[Retry] attempt %d of %d ended unanswered after %d round(s) — discarding %d evidence and %d pooled chunk(s), restarting: %s",
            retries,
            max_retry_number + 1,
            state.get("iteration", 0),
            len(state.get("evidences") or []),
            len(state.get("pool") or []),
            _snip(formalized),
        )
        return {
            "current": formalized,
            "keywords": "",
            "query": "",
            "chunks": [],
            "evidences": [],
            "asked": [],
            "pool": [],
            "iteration": 0,
            "sufficient": False,
            "partial": False,
            "retries": retries,
        }

    def _retry_exhausted(state: KwV6State) -> bool:
        return state.get("retries", 0) >= max_retry_number

    def _route_after_sufficiency(state: KwV6State) -> str:
        if state.get("sufficient"):
            return "answer"
        if state.get("iteration", 0) >= max_iterations:
            return "answer" if _retry_exhausted(state) else "retry"
        return "next_question"

    def _route_after_next_question(state: KwV6State) -> str:
        if state.get("current"):
            return "keywords"
        # Out of angles rather than out of rounds. It is the same dead end — the
        # graph is about to answer a question it never resolved — so it takes the
        # same remedy while an attempt is left.
        return "answer" if _retry_exhausted(state) else "retry"

    g = StateGraph(KwV6State)
    g.add_node("formalize", formalize_node)
    g.add_node("keywords", keywords_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("assess", assess_node)
    g.add_node("sufficiency", sufficiency_node)
    g.add_node("next_question", next_question_node)
    g.add_node("retry", retry_node)
    g.add_node("compute", compute_node)
    g.add_node("answer", answer_node)

    g.add_edge(START, "formalize")
    g.add_edge("formalize", "keywords")
    g.add_edge("keywords", "retrieve")
    g.add_edge("retrieve", "assess")
    g.add_edge("assess", "sufficiency")
    g.add_conditional_edges("sufficiency", _route_after_sufficiency, {"next_question": "next_question", "retry": "retry", "answer": "compute"})
    g.add_conditional_edges("next_question", _route_after_next_question, {"keywords": "keywords", "retry": "retry", "answer": "compute"})
    g.add_edge("retry", "keywords")
    g.add_edge("compute", "answer")
    g.add_edge("answer", END)
    return g.compile()


async def run_keyword_agentic_rag_v8(
    tools,
    messages: list,
    max_iterations: int = 9,
    gen_conf: dict | None = None,
    max_retry_number: int = 1,
):
    """Drive the v6 graph, yielding answer-token strings."""
    question = ""
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content"):
            question = m["content"]
            break

    token_queue: asyncio.Queue = asyncio.Queue()
    graph = build_keyword_agentic_graph_v8(
        tools,
        token_queue,
        messages=messages,
        gen_conf=gen_conf,
        max_iterations=max_iterations,
        max_retry_number=max_retry_number,
    )
    _SENTINEL = object()
    holder: dict[str, Any] = {}

    async def _drive():
        try:
            holder["state"] = await graph.ainvoke(
                {
                    "question": question,
                    "max_iterations": max_iterations,
                    "iteration": 0,
                    "evidences": [],
                    "asked": [],
                    "pool": [],
                    "partial": False,
                    "retries": 0,
                },
                # Every retry re-walks the whole graph, so the step budget covers
                # all attempts, not one.
                {"recursion_limit": max(25, max_iterations * 8 * (max_retry_number + 1) + 10)},
            )
        except Exception:
            _LOG.exception("run_keyword_agentic_rag_v6: graph execution failed")
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
