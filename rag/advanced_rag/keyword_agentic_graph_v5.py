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

"""Keyword-driven iterative search graph — v5 (LangGraph).

Refines v4 with three additions, each behind its own flag so they can be
measured independently:

1. ANSWER-TYPE CLASSIFICATION + COMPUTE. Many questions have no answer anywhere
   in the corpus — it must be counted, ranked or calculated from retrieved
   operands ("divide X by Y to 5 dp", "difference in hours", "who led the team
   in scoring"). ``classify`` decides the answer shape up front; ``summarize``
   extracts machine-usable operands; ``compute`` has the model write an
   arithmetic EXPRESSION which Python evaluates with ``Decimal`` — models are
   unreliable at long division, rounding modes and counting list items.
2. STRUCTURAL EXPANSION. Facts frequently sit at a structural offset from the
   matched text: an actor named on the line ABOVE the role, an award winner on
   the line BELOW the category, a list truncated mid-cell. ``expand`` widens
   every retrieved chunk to its document neighbours and prefixes the nearest
   markdown heading, so a row keeps the section that gives it meaning.
3. PLANNING/RETRIEVAL POLISH. Rarity-weighted keywords, backward planning for
   questions whose subject is unnamed, and document-convention hints in the
   assessor.

Flow:

    classify → analyze → keywords → retrieve_chunks → expand → assess
        → summarize → sufficiency
             sufficiency ─(done | max rounds)→ compute → answer → END
                         └(gap remains)──────→ next_subq ─(new)→ keywords
                                                         └(none)→ compute → answer
"""

from __future__ import annotations

import ast
import asyncio
import logging
import operator
import re
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from html import unescape
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from common.token_utils import num_tokens_from_string
from rag.prompts.generator import citation_prompt, form_message, message_fit_in
from rag.advanced_rag.harness.prompts.report_prompt import FINAL_ANSWER_SYSTEM
from rag.advanced_rag.harness.tools.search import _MD_TABLE, _narrow_by_keywords, _normalize

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

# Deterministic number/word hints are identical to v4's.
from rag.advanced_rag.keyword_agentic_graph_v4 import _number_keyword_hints

_LOG = logging.getLogger(__name__)

# Tunable caps.
_MAX_SUBQUESTIONS = 2  # sub-questions kept per round
_MAX_ANALYZE_SUBQUESTIONS = 2  # sub-questions emitted by the initial analyzer
_DOCS_PER_SUBQ = 3  # documents read end-to-end per sub-question in the optional deep scan
_CHUNKS_PER_SUBQ = 6  # chunks retrieved per sub-question (lower than v4: expansion inflates each ~3x)
_DOC_BATCH_SIZE = 3  # chunks per batch in the optional whole-document scan
_DOC_BATCH_OVERLAP = 1  # chunks shared between consecutive batches
_MAX_DOC_BATCHES = 8  # max batches scanned per document in the optional deep scan
_EXPAND_BEFORE = 1  # neighbour chunks pulled in BEFORE a match (the Kingsley case)
_EXPAND_AFTER = 1  # neighbour chunks pulled in AFTER a match (the award-winner case)
_DOC_ORDERED_LIMIT = 10000  # max chunks fetched when ordering a document
_MAX_STALE_ROUNDS = 2  # consecutive evidence-free rounds tolerated before answering partially

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


# ── Arithmetic evaluated in Python, never by the model ──

_ARITH_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
_ROUNDING = {"up": ROUND_UP, "down": ROUND_DOWN, "nearest": ROUND_HALF_UP}


# ── Table flattening ──────────────────────────────────────────────────────────
#
# A retrieved table row ("| Aurora | CO | 410,053 | ...") only means something
# next to its header, and the header is usually the first line of a DIFFERENT
# chunk. A model reading the row alone has to guess which figure is the 2025
# estimate and which is the 2020 census — which is exactly how a rank-50 lookup
# lands on the wrong city. Rewriting each body row as "<column>: <cell>" pairs
# moves the binding into the row itself, so it survives chunking.

_MD_SEPARATOR = re.compile(r"^[ \t]*\|?[ \t]*:?-{1,}:?[ \t]*(?:\|[ \t]*:?-{1,}:?[ \t]*)+\|?[ \t]*$")
_MD_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_HTML_TABLE_EDGE = re.compile(r"<table\b[^>]*>|</table\s*>", re.IGNORECASE)
_HTML_ROW = re.compile(r"<tr\b[^>]*>(.*?)(?:</tr\s*>|(?=<tr\b)|$)", re.IGNORECASE | re.DOTALL)
_HTML_CELL = re.compile(
    r"<(?:th|td)\b[^>]*>(.*?)(?=<(?:th|td)\b|</(?:th|td)\s*>|</tr\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)
_HTML_SPACING_TAG = re.compile(r"<br\b[^>]*>|</(?:p|div|li|h[1-6])\s*>", re.IGNORECASE)
_HTML_ANY_TAG = re.compile(r"<[^>]*>")


def _html_cell_text(raw: str) -> str:
    """Visible text of one cell: spacing tags to blanks, other tags dropped."""
    text = _HTML_SPACING_TAG.sub(" ", raw or "")
    text = _HTML_ANY_TAG.sub("", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _flatten_rows(rows: list[list[str]]) -> str | None:
    """Render parsed rows as ``#n <col>: <cell>, ...`` lines.

    ``None`` means "leave the original alone": no header to label with, or no
    body row to relabel. Empty cells are dropped rather than emitted blank.
    """
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        return None

    # A BANNER row — a single filled cell across an otherwise empty row, such as
    # the "| Thursday 8 June |  |  |  |" date above a festival day's stage table —
    # is a caption, not a header. Treating it as labels binds every value to the
    # date and demotes the real column names ("Apex Stage", "Opus Stage") to data,
    # which is strictly worse than leaving the table alone. Keep it as a caption
    # and promote the row beneath it to header.
    caption = ""
    first = [c.strip() for c in rows[0]]
    if len(rows) >= 3 and len(first) >= 2 and sum(1 for c in first if c) == 1:
        if sum(1 for c in rows[1] if c.strip()) >= 2:
            caption = next(c for c in first if c)
            rows = rows[1:]

    header = [c.strip() for c in rows[0]]
    if not any(header):
        return None
    labels = [h or f"col{i + 1}" for i, h in enumerate(header)]
    lines = []
    for n, row in enumerate(rows[1:], start=1):
        pairs = []
        for i, cell in enumerate(row):
            value = cell.strip()
            if not value:
                continue
            pairs.append(f"{labels[i] if i < len(labels) else f'col{i + 1}'}: {value}")
        if pairs:
            lines.append(f"#{n} " + ", ".join(pairs))
    if not lines:
        return None
    return (f"{caption}\n" if caption else "") + "\n".join(lines)


def _md_table_rows(block: str) -> list[list[str]] | None:
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if len(lines) < 3 or not _MD_SEPARATOR.match(lines[1]):
        return None

    def cells(line: str) -> list[str]:
        s = line.strip()
        s = s[1:] if s.startswith("|") else s
        s = s[:-1] if s.endswith("|") else s
        return [c.replace("\\|", "|").strip() for c in _MD_CELL_SPLIT.split(s)]

    return [cells(lines[0])] + [cells(ln) for ln in lines[2:]]


def _html_table_spans(text: str) -> list[tuple[int, int]]:
    """Outermost ``<table>`` spans, counting depth so nested tables stay whole."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start = 0
    for m in _HTML_TABLE_EDGE.finditer(text):
        if m.group(0).lower().startswith("</"):
            if depth:
                depth -= 1
                if depth == 0:
                    spans.append((start, m.end()))
        else:
            if depth == 0:
                start = m.start()
            depth += 1
    return spans


def _flatten_html_tables(text: str) -> str:
    for lo, hi in reversed(_html_table_spans(text)):
        block = text[lo:hi]
        inner = block[block.find(">") + 1 :]
        # A nested table would interleave its rows with the outer one's; the
        # original markup is less misleading than a scrambled flattening.
        if "<table" in inner.lower():
            continue
        rows = [[_html_cell_text(c) for c in _HTML_CELL.findall(r)] for r in _HTML_ROW.findall(block)]
        flat = _flatten_rows([r for r in rows if r])
        if flat:
            text = text[:lo] + flat + text[hi:]
    return text


def _flatten_markdown_tables(text: str) -> str:
    for m in reversed(list(_MD_TABLE.finditer(text))):
        rows = _md_table_rows(m.group(0))
        flat = _flatten_rows(rows) if rows else None
        if flat:
            text = text[: m.start()] + flat + "\n" + text[m.end() :]
    return text


def _flatten_tables(content: str) -> str:
    """Rewrite every HTML/markdown table body row into labelled phrases."""
    if not content or ("|" not in content and "<table" not in content.lower()):
        return content
    try:
        return _flatten_markdown_tables(_flatten_html_tables(content))
    except Exception:
        _LOG.exception("[Tables] flattening failed; keeping the original content")
        return content


def _flatten_chunk_tables(chunks: list[dict]) -> list[dict]:
    """Apply :func:`_flatten_tables` to each chunk's rendered body."""
    out = []
    changed = 0
    for c in chunks:
        body = c.get("content_with_weight") or ""
        flat = _flatten_tables(body)
        if flat != body:
            c = dict(c)
            c["content_with_weight"] = flat
            changed += 1
        out.append(c)
    if changed:
        _LOG.info("[Tables] flattened table rows in %d of %d chunk(s).", changed, len(chunks))
    return out


def _safe_arith(expression: str, names: dict[str, Decimal]) -> Decimal | None:
    """Evaluate a pure-arithmetic expression with ``Decimal``.

    Only ``+ - * /``, parentheses, numeric literals and the supplied operand
    names are permitted — no attribute access, calls, comprehensions or
    subscripts, so a hostile or confused model cannot execute anything. Returns
    ``None`` when the expression is unparseable, unsupported or divides by zero.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError):
        return None

    def _ev(node):
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("non-numeric constant")
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            if node.id in names:
                return names[node.id]
            raise ValueError(f"unknown operand {node.id}")
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITH_OPS:
            return _ARITH_OPS[type(node.op)](_ev(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in _ARITH_OPS:
            return _ARITH_OPS[type(node.op)](_ev(node.left), _ev(node.right))
        raise ValueError(f"unsupported expression node {type(node).__name__}")

    try:
        return _ev(tree)
    except Exception as exc:
        # A rejected expression is a routine model mistake, not a crash — log the
        # reason without a traceback so the caller can fall back quietly.
        _LOG.warning("[Compute] rejected expression %r: %s", expression, exc)
        return None


def _apply_rounding(value: Decimal, places, mode: str) -> Decimal:
    try:
        places = int(places)
    except (TypeError, ValueError):
        return value
    if places < 0:
        return value
    quant = Decimal(1).scaleb(-places)
    return value.quantize(quant, rounding=_ROUNDING.get(str(mode or "nearest").lower(), ROUND_HALF_UP))


def _as_number(value) -> Decimal | None:
    """Best-effort numeric coercion for an operand harvested from evidence."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, list):
        return Decimal(len(value))
    text = str(value).strip().replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return Decimal(m.group(0)) if m else None


# ── Prompts (each a single, self-contained LLM call) ──

_CLASSIFY_SYSTEM = """You classify what SHAPE the answer to a question takes, before any research runs.

"answer_type":
- "retrieve" — the answer is a fact stated somewhere: a name, place, title, date.
- "derive"   — one retrieved figure must be transformed (a complement, a unit conversion).
- "count"    — the answer is HOW MANY items match a description; the items must be enumerated.
- "rank"     — the answer is the top/leading item of a group by some measure.
- "compute"  — two or more retrieved numbers must be combined (difference, ratio, sum).
- "intersect"— the answer is the ONE entity meeting TWO OR MORE independent constraints, where each
               constraint on its own matches SEVERAL entities. Signals: "which X did A and also B",
               "who was both ... and ...". e.g. "which band was nominated three times for a Grammy
               AND headlined the Opus Stage at Download 2023" — many bands have three nominations and
               four bands headlined that stage; only their overlap answers the question. Choose this
               over "retrieve" whenever the question joins two conditions with "and also"/"both".

Also return:
- "target_unit": the unit the answer must be expressed in ("hours", "percent", ""), if the question names one.
- "rounding": {"places": <int>, "mode": "up"|"down"|"nearest"} when the question specifies rounding, else null.
- "operand_plan": for count/rank/compute, a short description of EACH quantity that must be retrieved
  before the answer can be calculated. These become the research targets — the final calculation is
  done separately, so never make the calculation itself an item here.
  For "intersect", list EACH CONSTRAINT as its own item ("bands that headlined the Opus Stage at
  Download Festival 2023", "bands nominated three times for Best Metal Performance"). The overlap is
  taken mechanically afterwards, so never make "the band satisfying both" an item.
Output ONLY JSON, no prose, no code fences:
{"answer_type": "...", "target_unit": "", "rounding": null, "operand_plan": ["...", "..."]}"""

_ANALYZE_SYSTEM = """You plan the FIRST round of research for a question. Research runs in several
rounds, so this round does NOT have to answer the whole question.

Emit ONLY the sub-questions that can be searched RIGHT NOW, using facts stated in the original
question itself. Rules:
- SIMPLE and ATOMIC: one fact, one entity, one relation per sub-question. Never bundle several
  facts, or several hops, into one sub-question.
- INDEPENDENT: answerable on its own; never relies on the answer to another sub-question.
- START FROM THE MOST UNIQUELY IDENTIFYING CONSTRAINT, even when it appears at the END of the
  question. If the question's subject is unnamed ("I can't recall who...", "the person who..."),
  the leading clause is unsearchable — begin instead with the clause naming a specific event,
  award, year or place, and let a later round walk back to the subject.
- If an "operand plan" is supplied, the sub-questions should retrieve THOSE quantities. Never make
  the final calculation a sub-question: arithmetic is done in a separate step.
- ENUMERATE, DO NOT PICK, when the question joins two conditions ("which X did A and also B").
  Each condition on its own matches SEVERAL entities, so a sub-question phrased "Which band did A?"
  forces an arbitrary choice among many and the two halves will not agree. Ask each condition as a
  LIST instead: "List EVERY band that headlined the Opus Stage at Download Festival 2023, for each
  day of the festival" — not "Which band headlined the Opus Stage at Download Festival 2023?".
  The single answer is found by overlapping the lists in a later step, never by guessing here.
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

PREFER DISCRIMINATIVE TERMS. A keyword is only useful if it is RARE in a general encyclopedia.
Rare proper nouns, identifiers and coined names ("Imdad", "ActRaiser", "1344259", "Keerom") pin a
document immediately. Common words and words that are substrings of unrelated names ("Square",
"Kings", "Butler", "Trail") match hundreds of irrelevant pages — include them only alongside a
rare term, never on their own.

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

DOCUMENT CONVENTIONS — these count as the value being stated:
- In an award or nominee list, the FIRST entry is the winner; the rest are nominees.
- In an infobox listing spouses/terms/positions, an entry with NO end date is the CURRENT one, and
  entries with an end date ("div. 2005", "1998-2004") have ended. A question asking "as of <date>"
  is answered by the entry with no end date.
- A standings "seed"/rank number is the ranking; two teams with the same win-loss record can hold
  different ranks, so a record alone does NOT establish a place.
- A "Section: ..." line prefixed to a chunk names the heading the content sits under. A row only
  answers a question about that category if the section matches.
- Rows of a table can be counted; a list truncated mid-way cannot be counted reliably.
- A pre-aggregated TOTAL ("4 nominations", "career 12 wins", a "most nominated artists" summary) is
  counted as of the DOCUMENT'S latest revision, never as of a date the question names. When the
  sub-question says "as of <date>" and the same document also lists the underlying items with their
  own dates, the dated rows are the authority and the total is NOT. Judge such a chunk on the dated
  rows. An entity filed under "4 nominations" can still be the right answer to "nominated three
  times as of <date>" when one of those nominations is dated after it — so a total that disagrees
  with the question is not grounds for "none".
- A source whose EDITION or VINTAGE differs from the one the question names is still the best
  available evidence: the question asks for "2023 estimate" figures and the table header reads
  "2025 estimate", or the question says "as of 2024" and the source is a 2022 revision. Judge such a
  chunk on whether it states the asked-for value — "full" or "partial" exactly as usual. NEVER judge
  it "none" merely because the year, edition or revision label does not match the question's. The
  matching edition is not in the knowledge base; withholding the one that is there answers nothing.
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

When the sub-question says "as of <date>" and the chunks offer BOTH a pre-aggregated total and the
underlying dated items, count the dated items falling on or before that date and answer with that
count — do not quote the total, which is current as of the document's own latest revision. Say which
items you counted. (An artist summarised as "4 nominations" whose nominations are dated 2016, 2023,
2024 and 2026 has THREE as of August 2024.)

If the chunks' EDITION or VINTAGE differs from the one the sub-question names — it asks for "2023
estimate" figures and the table is headed "2025 estimate" — answer from what is there and name the
difference in "summary" (e.g. "Aurora, CO, per the 2025 estimate table — the closest edition
available"). Still fill "value_number"/"value_items" from it. Refusing to answer because the exact
edition is absent loses the only evidence there is; the caller needs the value plus the caveat.

ALSO extract the answer in machine-usable form, so a later step can calculate with it:
- "value_items": when the sub-question asks HOW MANY, asks for a set, or asks you to LIST every
  match, put EVERY matching item there verbatim, one entity per element — never several names joined
  into one string. Do not report a count you did not enumerate; the items are counted and overlapped
  mechanically later, so this field is the answer, not the prose.
  If the chunk text appears cut off mid-list, say so in "summary" and omit "value_items".
- "value_number": the answer as a plain number when it is numeric. If a TARGET UNIT is given,
  convert to that unit and report the converted number (e.g. target "hours", value "46 days
  11 hours 20 minutes" -> 1115.3333).
- Omit either field when it does not apply. Never invent a number to fill them.
Output ONLY JSON, no prose, no code fences:
{"summary": "<concise factual answer>", "relevant": [<chunk number>, ...],
 "value_number": <number or null>, "value_items": [<item>, ...]}"""

_COMPUTE_SYSTEM = """You are given the ORIGINAL question and the numeric OPERANDS research produced.
Write the arithmetic that turns those operands into the answer. You do NOT do the arithmetic —
Python evaluates your expression — so give the formula, not the result.

- "expression": an arithmetic expression over the operand LABELS only (A, B, ...), using just
  + - * / and parentheses. Never write the numbers' meanings, a computed result, or any function.
- "working": one short sentence naming what each operand is and what the expression produces.
- If the operands cannot answer the question, return an EMPTY expression rather than guessing.
Output ONLY JSON, no prose, no code fences:
{"expression": "A / B", "working": "<one sentence>"}"""

_SUFFICIENCY_SYSTEM = """You are given the ORIGINAL question and the facts discovered so far.
Decide ONLY whether those facts are ENOUGH to answer the ORIGINAL question directly and completely.
Do NOT propose follow-up research.
- "sufficient" is true only when EVERY part of the ORIGINAL question can be answered from the facts.
- Do not assume, infer or fill in a value that no fact states.
- If the question requires a calculation, the facts are sufficient once every OPERAND has been
  found — the calculation itself is performed by a later step, so do not require it here.
- If the question joins TWO conditions ("which X did A and also B"), the facts are sufficient only
  when some SINGLE entity is shown to satisfy BOTH. Two facts naming DIFFERENT entities — one that
  satisfies A, another that satisfies B — are NOT sufficient; that is a contradiction to resolve,
  not an answer. Say false so the next round can enumerate the candidates properly.
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
- An entry marked "[EXHAUSTED ...]" is not pending work — the knowledge base has been shown not to
  hold it. Report it as UNAVAILABLE rather than as something still to look for: say "X is not present
  in the available sources", so the caller answers with what is known instead of searching again.
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
- A relationship named in the question may be recorded differently in the sources ("sister" vs
  "sister-in-law", a changed surname after marriage). If a relationship lookup failed, ask about the
  connecting person instead of repeating the relationship term.
- Never use an unresolved reference. If a needed value is still unknown, ask for THAT value instead —
  do not chain two unknowns in one sub-question.
- VERIFY CANDIDATES rather than enumerating the second set. When the question joins two conditions
  and an earlier round has already listed the candidates for ONE condition, do NOT ask for a full
  list of everything meeting the other condition — that set is usually far larger. Ask instead
  whether the NAMED candidates meet it, one sub-question per candidate, using their actual names:
  "How many times has Ghost been nominated for the Grammy Award for Best Metal Performance?" — not
  "Which bands have three nominations?". Checking a handful of names is reliable; enumerating a long
  list is not.
- NEVER repeat or rephrase anything under "Sub-questions ALREADY asked" — those rounds are spent.
  One marked "[no evidence found]" means that angle failed: attack the gap from a genuinely different
  angle (a different entity, source or attribute) rather than rewording it.
- A sub-question marked "[EXHAUSTED ...]" is FINISHED. Retrieval returned only documents earlier
  rounds had already read, which means the knowledge base does not contain that fact at all. Asking
  it again in ANY wording is guaranteed to fail and wastes the remaining rounds. Treat it as
  permanently unanswerable: either plan a different quantity that the question can still use, or
  return an EMPTY list so the caller answers partially with what is known.
- If no genuinely NEW and useful sub-question exists — every angle is spent, or the corpus plainly
  cannot supply what is missing — return an EMPTY "next_subquestions" list. An empty list is the
  CORRECT answer in that case; never pad it with a variation of something already asked.
- If the facts already contain the core value needed for a usable partial answer, and the only
  remaining gap is a qualifier like an exact date, source date, or formatting detail, STOP.
  Return an EMPTY "next_subquestions" list instead of asking for a finer-grained follow-up.
- Generate AT MOST TWO. (Keywords are added by a separate step, so output the questions only.)
Output ONLY JSON, no prose, no code fences:
{"next_subquestions": [{"question": "<sub-question>"}, ...]}"""

_PARTIAL_PREAMBLE = (
    "The evidence below is INCOMPLETE — research ran out of new angles before every part of the "
    "question could be resolved. Answer the parts that the evidence does support, and state plainly "
    "which part remains unresolved. Do not guess the missing part."
)

_ITERATION_LIMIT_GUESS_PREAMBLE = (
    "Research reached its iteration limit before every part of the question could be verified. "
    "Give the most plausible complete answer supported by the findings, making a best-supported "
    "inference for the unresolved factor. Clearly label that part as a best estimate and state what "
    "remains unverified. Never fabricate a source, citation, exact figure, or date."
)


class KwV5State(TypedDict, total=False):
    question: str
    answer_type: str  # retrieve | derive | count | rank | compute
    target_unit: str  # unit the answer must be expressed in ("hours"), or ""
    rounding: dict  # {"places": int, "mode": "up|down|nearest"} or {}
    operand_plan: list  # quantities that must be retrieved before calculating
    subquestions: list  # [{id, question, keywords, chunks, status, answerable, unavailable}]
    evidences: list  # [{iteration, subq, status, summary, chunk_ids, value_number, value_items}]
    asked: list  # EVERY sub-question attempted so far, answered or not — blocks re-asking
    unavailable: list  # sub-questions the knowledge base demonstrably cannot answer — blocks re-asking
    seen_chunks: list  # chunk_ids already retrieved in an earlier round — used to detect an exhausted search
    stale_rounds: int  # consecutive rounds that produced no new evidence — forces an early stop
    pool: list  # retained (summariser-selected) chunks — the citation set, dedup by id
    computed: dict  # {"expression", "result", "working"} from the compute node
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


_EXHAUSTED_LABEL = "   [EXHAUSTED — already searched; the knowledge base holds no further documents for it. Never ask it again, in any rewording]"


def _tried_listing(asked: list, evidences: list[dict], unavailable: list, partial_label: str) -> str:
    """Render the already-asked ledger, flagging partial and exhausted sub-questions."""
    status_of = {_norm(e["subq"]): e.get("status", "full") for e in evidences}
    exhausted = {_norm(u) for u in (unavailable or [])}
    label = {"full": "", "partial": partial_label}
    lines = []
    for a in asked or []:
        key = _norm(a)
        if key in exhausted:
            lines.append(f"- {a}{_EXHAUSTED_LABEL}")
        else:
            lines.append(f"- {a}" + label.get(status_of.get(key, ""), "   [searched, no evidence found]"))
    return "\n".join(lines)


def _operands_from_evidences(evidences: list[dict]) -> list[dict]:
    """Label the numeric operands the compute step may use (A, B, C, ...).

    ``value_items`` is counted here rather than trusted from the model — counting
    a list is exactly where an LLM slips (37 items reported as 33).
    """
    operands: list[dict] = []
    for e in evidences:
        items = e.get("value_items")
        if isinstance(items, list) and items:
            number = Decimal(len(items))
            kind = f"count of {len(items)} enumerated items"
        else:
            number = _as_number(e.get("value_number"))
            kind = "number"
            if number is None:
                continue
        label = chr(ord("A") + len(operands))
        operands.append({"label": label, "subq": e.get("subq", ""), "number": number, "kind": kind})
        if len(operands) >= 12:
            break
    return operands


_ENTITY_NOISE = re.compile(r"^(the|a|an)\s+|\s*\([^)]*\)\s*$|[.,;:!?\"']")


def _entity_key(item: object) -> str:
    """Loose match key for entity names, so "The Ghost." meets "Ghost"."""
    text = re.sub(r"\s+", " ", str(item or "")).strip().lower()
    return _ENTITY_NOISE.sub("", text).strip()


def _intersect_evidences(evidences: list[dict]) -> tuple[list[str], dict[str, list[str]]]:
    """Overlap the enumerated candidate sets, one set per sub-question.

    Rounds that revisit the same sub-question UNION into that sub-question's set;
    different sub-questions INTERSECT, since each stands for one condition of the
    question. Returns the surviving names (original casing) and the sets they came
    from, so the answer step can show its working.
    """
    sets: dict[str, list[str]] = {}
    keys: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    for e in evidences:
        items = e.get("value_items")
        if not isinstance(items, list) or not items:
            continue
        subq = str(e.get("subq") or "")
        for raw in items:
            key = _entity_key(raw)
            if not key:
                continue
            display.setdefault(key, str(raw).strip())
            if key not in keys.setdefault(subq, set()):
                keys[subq].add(key)
                sets.setdefault(subq, []).append(str(raw).strip())
    if len(keys) < 2:
        return [], sets
    common = set.intersection(*keys.values())
    # Walk the first sub-question's LIST (not its set) so the output order is stable.
    first_subq = next(iter(sets))
    ordered = [name for name in sets[first_subq] if _entity_key(name) in common]
    return ordered, sets


def _operand_listing(operands: list[dict]) -> str:
    return "\n".join(f"{o['label']} = {o['number']}   ({o['kind']}; from: {_snip(o['subq'], 90)})" for o in operands)


def _incomplete_compute(operands: list[dict], plan: list, note: str) -> dict:
    """Payload for a calculation that could not be finished.

    Returning nothing here used to make the answer step fall back to a generic
    "could not be determined". Handing it the operands that WERE established, the
    plan they belong to, and why the arithmetic stopped lets it name the concrete
    values it does have and the one quantity that is genuinely missing.
    """
    return {
        "computed": {
            "result": "",
            "operands": _operand_listing(operands),
            "plan": list(plan or []),
            "found": len(operands),
            "note": note,
        }
    }


def build_keyword_agentic_graph_v5(
    tools,
    token_queue: asyncio.Queue,
    gen_conf: dict | None = None,
    max_iterations: int = 4,
    enable_snippets: bool = False,
    enable_deep_scan: bool = False,
    enable_expand: bool = True,
    enable_compute: bool = True,
):
    """Compile the v5 graph.

    :param enable_snippets: narrow chunks to keyword-bearing sentences before
        assessing. Off by default.
    :param enable_deep_scan: when a sub-question's retrieved chunks are judged
        unanswerable, scan whole documents in overlapping batches. Off by default.
    :param enable_expand: widen each retrieved chunk to its document neighbours
        and prefix the nearest markdown heading. On by default (phase 2).
    :param enable_compute: classify the answer shape and calculate the result in
        Python from retrieved operands. On by default (phase 1).
    """
    answer_conf = dict(gen_conf) if gen_conf else {"temperature": 0.3}
    answer_conf.pop("direct_answer", None)

    # Per-run cache of whole documents in reading order, for structural expansion.
    _doc_order_cache: dict[str, list[dict]] = {}

    async def _llm_json(system: str, user: str) -> dict:
        msg = await tools._fit_messages(system, user)
        ans = await tools.chat_mdl.async_chat(msg[0]["content"], msg[1:], {"temperature": 0.2})
        if isinstance(ans, tuple):
            ans = ans[0]
        return _extract_json(ans)

    async def _retrieve(query: str, top_n: int, doc_ids):
        """Ordinary hybrid retrieval (content + optional vector).

        Table rows in the results are flattened to ``<column>: <cell>`` phrases so
        a row stays interpretable once separated from its header.
        """
        from common import settings

        if not getattr(tools, "tenant_ids", None) or not getattr(tools, "kb_ids", None):
            _LOG.warning("[Retrieve chunks] skipped: no tenant or knowledge-base scope is available.")
            return {"chunks": [], "doc_aggs": []}

        tools.embed_mdl = None
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
        out = _normalize(kbinfos, tools.tenant_ids)
        out["chunks"] = _flatten_chunk_tables(out.get("chunks") or [])
        return out

    # ── Structural expansion (phase 2) ──

    def _position_key(row: dict) -> tuple[int, int]:
        """Sort key for document reading order.

        ``page_num_int`` / ``top_int`` are the fields every backend actually
        carries (``chunk_order_int`` is declared only in the Infinity mapping, so
        ordering by it silently does nothing on Elasticsearch). Both may arrive as
        a list of positions, so take the first entry.
        """

        def _first(value) -> int:
            if isinstance(value, list):
                value = value[0] if value else 0
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        return (_first(row.get("page_num_int")), _first(row.get("top_int")))

    async def _fetch_doc_chunks_ordered(doc_id: str) -> list[dict]:
        """All chunks of ``doc_id`` in reading order, memoized for this run.

        Ordering mirrors ``TaskHandler._load_chunks_for_doc``: ascending
        ``page_num_int`` then ``top_int``. The doc store applies that order, and
        the same key is re-applied locally so a backend that ignores one of the
        fields still yields a stable sequence.
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
        ordered = await _fetch_doc_chunks_ordered(doc_id)
        if not ordered:
            return chunk
        idx = next((i for i, c in enumerate(ordered) if _chunk_id(c) == cid), None)
        if idx is None:
            return chunk
        lo = max(0, idx - _EXPAND_BEFORE)
        hi = min(len(ordered), idx + _EXPAND_AFTER + 1)
        body = "\n".join((ordered[i].get("content_with_weight") or "") for i in range(lo, hi)).strip()
        # Flatten AFTER joining: a table split across chunks is whole again here, so
        # a header stranded in the previous chunk can still label these rows. This
        # also re-applies the flattening _retrieve did, which the join just replaced.
        # body = _flatten_tables(body)
        # heading = _nearest_heading(ordered, idx)
        out = dict(chunk)
        out["content_with_weight"] = body  # (f"Section: {heading}\n" if heading else "") + body
        return out

    # ── Assessment (batched, budget-bounded) ──

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
        return max(1, int(max_length * 0.5))

    def _assessment_batches(question: str, chunks: list[dict]) -> list[list[tuple[int, dict, str]]]:
        """Group adjacent chunks while keeping the full assessment prompt below 20% of context."""
        budget = _assessment_budget()
        batches: list[list[tuple[int, dict, str]]] = []
        current: list[tuple[int, dict, str]] = []

        for index, chunk in enumerate(chunks):
            body = _assessment_body(chunk)
            candidate_bodies = [item[2] for item in current] + [body]
            candidate_tokens = num_tokens_from_string(_ASSESS_SYSTEM + _assessment_prompt(question, candidate_bodies))

            if current and candidate_tokens > budget:
                batches.append(current)
                current = []
                candidate_tokens = num_tokens_from_string(_ASSESS_SYSTEM + _assessment_prompt(question, [body]))

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

        Returns ``(status, useful chunks)``: ``"full"`` when some chunk answered
        outright, ``"partial"`` when only partial chunks were found, ``"none"``
        otherwise. An answer built only from partials is NOT settled — the planner
        must revisit it.
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

    async def _summarize(question: str, chunks: list[dict], target_unit: str) -> dict:
        """Summarize from the USEFUL chunks, and extract machine-usable operands."""
        shown, listing = await _shown_listing(chunks)
        if not shown:
            return {"summary": "", "relevant": [], "value_number": None, "value_items": []}
        head = f"Chunks:\n{listing}\n\nSub-question:\n{question}\n"
        if target_unit:
            head += f"\nTARGET UNIT for any numeric value: {target_unit}\n"
        parsed = await _llm_json(_SUMMARIZE_SYSTEM, head + "\nOutput JSON:")
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
        items = parsed.get("value_items")
        return {
            "summary": summary,
            "relevant": relevant,
            "value_number": parsed.get("value_number"),
            "value_items": [str(x) for x in items] if isinstance(items, list) else [],
        }

    async def _deep_scan(sq: dict) -> list[dict]:
        """Optional fallback: scan whole documents in overlapping batches."""
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

    # ── Node 0: classify — what SHAPE is the answer? (LLM, once) ──
    async def classify_node(state: KwV5State) -> dict:
        q = state.get("question") or ""
        base = {"answer_type": "retrieve", "target_unit": "", "rounding": {}, "operand_plan": []}
        if not enable_compute:
            return base
        parsed = await _llm_json(_CLASSIFY_SYSTEM, f"Question:\n{q}\n\nOutput JSON:")
        at = str(parsed.get("answer_type") or "").strip().lower()
        if at not in ("retrieve", "derive", "count", "rank", "compute", "intersect"):
            at = "retrieve"
        rounding = parsed.get("rounding")
        rounding = rounding if isinstance(rounding, dict) else {}
        plan = [str(p).strip() for p in (parsed.get("operand_plan") or []) if str(p).strip()]
        _LOG.info("[Classify] answer_type=%s unit=%r rounding=%s operands=%d", at, parsed.get("target_unit") or "", rounding or "-", len(plan))
        return {"answer_type": at, "target_unit": str(parsed.get("target_unit") or "").strip(), "rounding": rounding, "operand_plan": plan}

    # ── Node 1: analyze — decompose Q into independent sub-questions (LLM) ──
    async def analyze_node(state: KwV5State) -> dict:
        q = state.get("question") or ""
        _LOG.info("[Analyze] Planning round 1 — simple, independent sub-questions for: %s", _snip(q))
        user = f"Question:\n{q}"
        plan = state.get("operand_plan") or []
        if plan:
            user += "\n\nOperand plan (retrieve each of these; do NOT ask for the final calculation):\n" + "\n".join(f"- {p}" for p in plan)
        parsed = await _llm_json(_ANALYZE_SYSTEM, user + "\n\nOutput JSON:")
        subqs = _mk_subqs(parsed.get("subquestions"), 0, limit=_MAX_ANALYZE_SUBQUESTIONS) or _mk_subqs([{"question": q}], 0, limit=_MAX_ANALYZE_SUBQUESTIONS)
        for sq in subqs:
            _LOG.info("[Sub-Q]: %s", sq["question"])
        return {
            "subquestions": subqs,
            "iteration": 0,
            "pool": [],
            "evidences": [],
            "asked": [],
            "unavailable": [],
            "seen_chunks": [],
            "stale_rounds": 0,
            "partial": False,
            "computed": {},
        }

    # ── Node 2: keywords — discriminative terms + synonyms + variants (LLM) ──
    async def keywords_node(state: KwV5State) -> dict:
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

    # ── Node 3: retrieve_chunks — KB-wide top chunks (no LLM) ──
    async def retrieve_chunks_node(state: KwV5State) -> dict:
        subqs = state.get("subquestions") or []
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
            # Recorded before assess narrows the list, so the exhaustion check in
            # summarize_node sees every chunk retrieval surfaced — not just the useful ones.
            sq["retrieved_chunks"] = sorted({str(c.get("chunk_id")) for c in chunks if c.get("chunk_id")})
        _LOG.info("[Retrieve chunks] chunks per sub-q: %s (snippets=%s)", str([len(sq["chunks"]) for sq in subqs]), enable_snippets)
        return {"subquestions": subqs}

    # ── Node 4: expand — neighbours + section heading (no LLM, phase 2) ──
    async def expand_node(state: KwV5State) -> dict:
        subqs = state.get("subquestions") or []
        if not enable_expand:
            return {"subquestions": subqs}
        for sq in subqs:
            chunks = sq.get("chunks") or []
            if not chunks:
                continue
            sq["chunks"] = await asyncio.gather(*[_expand_chunk(c) for c in chunks])
        _LOG.info("[Expand] widened chunks by -%d/+%d with section headings.", _EXPAND_BEFORE, _EXPAND_AFTER)
        return {"subquestions": subqs}

    # ── Node 5: assess — can each sub-question be answered? (LLM) ──
    async def assess_node(state: KwV5State) -> dict:
        subqs = state.get("subquestions") or []

        async def _one(sq: dict) -> str:
            if sq["chunks"]:
                status, useful = await _assess_chunks(sq["question"], sq["chunks"])
                if status != "none":
                    sq["chunks"] = useful
                    return status
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

    # ── Node 6: summarize — evidence + operands from answerable pairs (LLM) ──
    async def summarize_node(state: KwV5State) -> dict:
        subqs = state.get("subquestions") or []
        it = state.get("iteration", 0)
        unit = state.get("target_unit") or ""
        answerable = [sq for sq in subqs if sq.get("answerable")]

        results = await asyncio.gather(*[_summarize(sq["question"], sq["chunks"], unit) for sq in answerable])

        asked = list(state.get("asked") or [])
        asked_keys = {_norm(a) for a in asked}
        for sq in subqs:
            key = _norm(sq["question"])
            if key and key not in asked_keys:
                asked_keys.add(key)
                asked.append(sq["question"])

        # ── Exhaustion ledger ──
        # A sub-question that answered nothing AND surfaced no chunk the earlier rounds
        # had not already retrieved has nothing left to find: the knowledge base simply
        # does not hold it. Recording that stops later rounds from re-asking the same
        # thing in new words until the iteration budget runs out. Round 0 is exempt so
        # every sub-question gets at least one retry with different keywords.
        # NOTE: chunk-level granularity is deliberately conservative — one new chunk
        # anywhere keeps a sub-question alive, so the stale-round brake in
        # sufficiency_node is what bounds the truly hopeless cases.
        seen_chunks = set(state.get("seen_chunks") or [])
        unavailable = list(state.get("unavailable") or [])
        unavailable_keys = {_norm(u) for u in unavailable}
        round_chunks: set[str] = set()
        for sq in subqs:
            sq_chunks = set(sq.get("retrieved_chunks") or [])
            round_chunks |= sq_chunks
            if it < 1 or sq.get("answerable"):
                continue
            if sq_chunks - seen_chunks:
                continue
            sq["unavailable"] = True
            key = _norm(sq["question"])
            if key and key not in unavailable_keys:
                unavailable_keys.add(key)
                unavailable.append(sq["question"])
                _LOG.info("[Summarize] exhausted — no unseen chunks remain for: %s", _snip(sq["question"]))
        seen_chunks |= round_chunks

        evidences = list(state.get("evidences") or [])
        pool = list(state.get("pool") or [])
        seen = {_chunk_id(c) for c in pool}
        for sq, res in zip(answerable, results):
            if not res["summary"]:
                continue
            for c in res["relevant"]:
                cid = _chunk_id(c)
                if cid not in seen:
                    seen.add(cid)
                    pool.append(c)
            evidences.append(
                {
                    "iteration": it,
                    "subq": sq["question"],
                    "status": sq.get("status", "full"),
                    "summary": res["summary"],
                    "chunk_ids": [_chunk_id(c) for c in res["relevant"]],
                    "value_number": res["value_number"],
                    "value_items": res["value_items"],
                }
            )
        _LOG.info("[Summarize] +%d evidence (%d total), pool=%d chunk(s) at round %d.", len([r for r in results if r["summary"]]), len(evidences), len(pool), it)
        return {
            "subquestions": subqs,
            "evidences": evidences,
            "pool": pool,
            "asked": asked,
            "unavailable": unavailable,
            "seen_chunks": sorted(seen_chunks),
        }

    # ── Node 7: sufficiency — original Q vs all evidence (LLM, two steps) ──
    async def sufficiency_node(state: KwV5State) -> dict:
        evidences = state.get("evidences") or []
        ev_text = "\n\n".join(f"(round {e.get('iteration', 0)}) {e['subq']}\n-> {e['summary']}" for e in evidences) or "(nothing discovered yet)"
        prompt = f"Facts discovered so far:\n{ev_text}\n\nOriginal question:\n{state.get('question') or ''}\n\nOutput JSON:"
        verdict = await _llm_json(_SUFFICIENCY_SYSTEM, prompt)
        sufficient = bool(verdict.get("sufficient"))

        # For a two-condition question the verdict is decidable mechanically, and the
        # model is unreliable here: given "Tool has three nominations" and "Metallica
        # headlined the stage" it happily called the pair sufficient and answered with
        # a contradiction. Overlap the enumerated candidate sets instead.
        forced = ""
        if (state.get("answer_type") or "") == "intersect":
            hits, sets = _intersect_evidences(evidences)
            if len(sets) >= 2 and not hits:
                sufficient = False
                forced = (
                    "No single entity satisfies BOTH conditions yet: "
                    + "; ".join(f"[{_snip(q, 60)}] -> {', '.join(v[:12])}" for q, v in sets.items())
                    + ". Check the named candidates from one list against the other condition, "
                    "one candidate at a time, instead of enumerating a second long list."
                )
                _LOG.info("[Sufficiency] intersect: %d set(s) enumerated, no overlap — forcing another round.", len(sets))
            elif len(hits) == 1:
                sufficient = True
                _LOG.info("[Sufficiency] intersect: exactly one entity satisfies every condition (%s).", hits[0])

        missing = ""
        if not sufficient:
            tried = _tried_listing(
                state.get("asked") or [],
                evidences,
                state.get("unavailable") or [],
                "   [PARTIAL — the specific value was never stated]",
            )
            missing_prompt = prompt
            if tried:
                missing_prompt += f"\n\nSub-questions ALREADY asked:\n{tried}"
            missing_verdict = await _llm_json(
                _MISSING_SYSTEM,
                missing_prompt + f"\n\nOriginal question:\n{state.get('question') or ''}\n\nIn order to answer the original question. Tell me what is missing.",
            )
            missing = str(missing_verdict.get("missing") or "").strip()
            if forced:
                # The mechanical finding outranks the model's prose: it names the actual
                # candidates, which is what the next round has to work from.
                missing = f"{forced}\n{missing}" if missing else forced

        # A round that added no evidence at all made no progress. Two of those in a row
        # means the remaining gap is not reachable from this knowledge base, so stop and
        # answer partially rather than spending the rest of the iteration budget on it.
        prev = state.get("iteration", 0)
        gained = any(e.get("iteration") == prev for e in evidences)
        stale = 0 if gained else int(state.get("stale_rounds") or 0) + 1

        it = prev + 1
        out_of_rounds = (not sufficient) and (it >= max_iterations or stale >= _MAX_STALE_ROUNDS)
        if not sufficient and stale >= _MAX_STALE_ROUNDS:
            _LOG.info("[Sufficiency] %d consecutive round(s) added no evidence — stopping early with a partial answer.", stale)
        _LOG.info("[Sufficiency] round %d → sufficient=%s. Missing: %s", it, sufficient, _snip(missing))
        return {"sufficient": sufficient, "missing": missing, "iteration": it, "partial": out_of_rounds, "stale_rounds": stale}

    # ── Node 8: next_subq — plan the next round from the asked records (LLM) ──
    async def next_subq_node(state: KwV5State) -> dict:
        evidences = state.get("evidences") or []
        asked = state.get("asked") or []
        it = state.get("iteration", 0)
        unavailable = state.get("unavailable") or []
        ev_text = "\n\n".join(f"(round {e.get('iteration', 0)}, {e.get('status', 'full').upper()}) {e['subq']}\n-> {e['summary']}" for e in evidences) or "(nothing discovered yet)"

        parts = [
            f"Original question:\n{state.get('question') or ''}",
            f"Facts discovered so far:\n{ev_text}",
            f"Still missing:\n{state.get('missing') or '(not stated)'}",
        ]
        if asked:
            tried = _tried_listing(
                asked,
                evidences,
                unavailable,
                "   [PARTIAL — answered only from partial chunks; the specific value was never stated]",
            )
            parts.append(f"Sub-questions ALREADY asked (never repeat or rephrase these):\n{tried}")
        if unavailable:
            parts.append(
                "The knowledge base has been shown NOT to contain the following. Searching for them "
                "again — in any wording — cannot succeed, so plan around them or return an empty list:\n" + "\n".join(f"- {u}" for u in unavailable)
            )
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

    # ── Node 9: compute — the model writes the formula, Python evaluates it ──
    async def compute_node(state: KwV5State) -> dict:
        answer_type = state.get("answer_type") or "retrieve"
        if not enable_compute or answer_type == "retrieve":
            return {}
        evidences = state.get("evidences") or []
        plan = state.get("operand_plan") or []

        # An intersect question is resolved by overlapping the enumerated candidate
        # sets, not by arithmetic — done here in Python so the answer step is handed a
        # settled result rather than being asked to eyeball two lists.
        if answer_type == "intersect":
            hits, sets = _intersect_evidences(evidences)
            listing = "\n".join(f"  [{_snip(q, 80)}] -> {', '.join(v[:25])}" for q, v in sets.items())
            if len(sets) < 2:
                _LOG.info("[Intersect] only %d candidate set(s) enumerated; answering from summaries.", len(sets))
                return _incomplete_compute([], plan, "the candidate list for at least one of the conditions was never enumerated")
            if not hits:
                _LOG.info("[Intersect] %d sets, no entity satisfies all of them.", len(sets))
                return {"computed": {"result": "", "sets": listing, "intersection": [], "note": "no entity appears in every candidate list"}}
            _LOG.info("[Intersect] %d set(s) -> %d entity satisfying all: %s", len(sets), len(hits), ", ".join(hits[:5]))
            return {"computed": {"result": hits[0] if len(hits) == 1 else "", "sets": listing, "intersection": hits}}

        operands = _operands_from_evidences(evidences)
        if not operands:
            _LOG.info("[Compute] no numeric operands were extracted; reporting the gap instead of a bare failure.")
            return _incomplete_compute(operands, plan, "research produced no numeric value for any of the required quantities")

        listing = _operand_listing(operands)
        rounding = state.get("rounding") or {}
        user = f"Original question:\n{state.get('question') or ''}\n\nOperands:\n{listing}\n"
        if state.get("target_unit"):
            user += f"\nAnswer unit: {state['target_unit']}\n"
        user += "\nOutput JSON:"

        parsed = await _llm_json(_COMPUTE_SYSTEM, user)
        expression = str(parsed.get("expression") or "").strip()
        working = str(parsed.get("working") or "").strip()
        if not expression:
            _LOG.info("[Compute] model produced no expression; reporting the %d operand(s) found instead.", len(operands))
            return _incomplete_compute(operands, plan, "the quantities found so far are not enough to form the calculation")

        names = {o["label"]: o["number"] for o in operands}
        value = _safe_arith(expression, names)
        if value is None:
            _LOG.info("[Compute] expression rejected or unevaluable: %r", expression)
            return _incomplete_compute(operands, plan, f"the proposed calculation ({expression}) could not be evaluated")
        if rounding:
            value = _apply_rounding(value, rounding.get("places"), rounding.get("mode"))
        result = format(value.normalize() if value == value.to_integral_value() else value, "f")
        _LOG.info("[Compute] %s = %s  (%s)", expression, result, _snip(working, 120))
        return {"computed": {"expression": expression, "result": result, "working": working, "operands": listing}}

    # ── Node 10: answer — brief cited answer, full or partial (LLM, streamed) ──
    async def answer_node(state: KwV5State) -> dict:
        evidences = [e for e in (state.get("evidences") or []) if e.get("summary")]
        partial = bool(state.get("partial"))
        missing = str(state.get("missing") or "").strip()
        best_effort = state.get("iteration", 0) >= max_iterations and bool(missing)
        computed = state.get("computed") or {}
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
        if best_effort:
            system += (
                "\n\n# Iteration-Limit Best Estimate\n"
                "The research limit has been reached with an unresolved factor. The normal instruction "
                "not to guess is overridden only for a clearly labeled best-supported inference. Base it "
                "on the supplied findings, distinguish evidence from inference, and never invent citations."
            )
        head = f"Question:\n{state.get('question') or ''}\n\n"
        if best_effort:
            head += _ITERATION_LIMIT_GUESS_PREAMBLE + f"\n\nStill unverified:\n{missing}\n\n"
        elif partial:
            head += _PARTIAL_PREAMBLE + "\n\n"
        body = (
            "Answer from the findings below. Each finding shows the [ID:n] citation markers that "
            "support it — reuse those exact markers in your answer; do not invent IDs.\n\n"
            "Findings:\n" + "\n".join(findings)
        )
        if best_effort:
            body = "For the unverified factor, make one explicit best estimate only when the findings make it plausible; label it as an inference rather than a confirmed fact.\n\n" + body
        if computed.get("intersection") is not None and computed.get("sets"):
            hits = computed.get("intersection") or []
            body += "\n\nCANDIDATE LISTS (enumerated by the research above):\n" + computed["sets"] + "\n"
            if len(hits) == 1:
                body += f"Exactly one entity appears in EVERY list: {hits[0]}\nThat overlap was computed mechanically and is the answer — state it directly, and cite the findings the lists came from."
            elif hits:
                body += (
                    f"These appear in every list: {', '.join(hits)}\n"
                    "Use the question's remaining wording to choose between them; if it does not "
                    "separate them, say which ones remain rather than picking arbitrarily."
                )
            else:
                body += (
                    "NO entity appears in every list, so the conditions are not yet reconciled. Do "
                    "NOT answer with an entity that satisfies only one of them, and do NOT pair two "
                    "different entities as though they were one answer. Say which condition each "
                    "candidate meets and that no single entity has been shown to meet them all."
                )
        elif computed.get("result"):
            body += (
                f"\n\nCALCULATED RESULT (computed exactly from the findings above; use this value "
                f"verbatim as the answer, do not recompute it):\n"
                f"  {computed['expression']} = {computed['result']}\n"
                f"  {computed.get('working', '')}"
            )
        elif computed.get("note"):
            body += "\n\nINCOMPLETE CALCULATION — this question asks for a calculated value, but the arithmetic could not be completed.\n"
            if computed.get("operands"):
                body += f"Quantities that WERE established:\n{computed['operands']}\n"
            else:
                body += "No quantity was established numerically.\n"
            if computed.get("plan"):
                body += "The calculation required:\n" + "\n".join(f"  - {p}" for p in computed["plan"]) + "\n"
            body += (
                f"Why it stopped: {computed['note']}.\n"
                "Report every quantity that WAS established, with its value and citation, then name "
                "precisely which quantity is missing and state that the available sources do not "
                "contain it. A bare 'the answer could not be determined' is not acceptable when some "
                "of the required values are known — give those values."
            )
        _, msg = message_fit_in(form_message(system, head + body), tools.chat_mdl.max_length)

        answer_mode = "BEST-EFFORT" if best_effort else ("PARTIAL" if partial else "Full")
        _LOG.info("[Answer] %s answer from %d finding(s), citing a pool of %d chunk(s).", answer_mode, len(findings), len(pool))
        final = ""
        try:
            async for tok in tools.chat_mdl.async_chat_streamly_delta(msg[0]["content"], msg[1:], answer_conf):
                token_queue.put_nowait(tok)
                final += tok
        except Exception:
            _LOG.exception("[Answer] stream failed")
            token_queue.put_nowait("I'm sorry, I encountered an error while composing the answer.")
        return {"final_answer": final}

    def _route_after_sufficiency(state: KwV5State) -> str:
        # "partial" also covers the early stop after consecutive evidence-free rounds.
        if state.get("sufficient") or state.get("iteration", 0) >= max_iterations or state.get("partial"):
            return "compute"
        return "next_subq"

    def _route_after_next_subq(state: KwV5State) -> str:
        return "keywords" if state.get("subquestions") else "compute"

    g = StateGraph(KwV5State)
    g.add_node("classify", classify_node)
    g.add_node("analyze", analyze_node)
    g.add_node("keywords", keywords_node)
    g.add_node("retrieve_chunks", retrieve_chunks_node)
    g.add_node("expand", expand_node)
    g.add_node("assess", assess_node)
    g.add_node("summarize", summarize_node)
    g.add_node("sufficiency", sufficiency_node)
    g.add_node("next_subq", next_subq_node)
    g.add_node("compute", compute_node)
    g.add_node("answer", answer_node)

    g.add_edge(START, "classify")
    g.add_edge("classify", "analyze")
    g.add_edge("analyze", "keywords")
    g.add_edge("keywords", "retrieve_chunks")
    g.add_edge("retrieve_chunks", "expand")
    g.add_edge("expand", "assess")
    g.add_edge("assess", "summarize")
    g.add_edge("summarize", "sufficiency")
    g.add_conditional_edges("sufficiency", _route_after_sufficiency, {"compute": "compute", "next_subq": "next_subq"})
    g.add_conditional_edges("next_subq", _route_after_next_subq, {"keywords": "keywords", "compute": "compute"})
    g.add_edge("compute", "answer")
    g.add_edge("answer", END)

    return g.compile()


async def run_keyword_agentic_rag_v5(
    tools,
    messages: list,
    max_iterations: int = 4,
    enable_snippets: bool = False,
    enable_deep_scan: bool = False,
    enable_expand: bool = False,
    enable_compute: bool = True,
    gen_conf: dict | None = None,
):
    """Drive the v5 graph, yielding answer-token strings."""
    question = ""
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content"):
            question = m["content"]
            break

    token_queue: asyncio.Queue = asyncio.Queue()
    graph = build_keyword_agentic_graph_v5(
        tools,
        token_queue,
        gen_conf=gen_conf,
        max_iterations=max_iterations,
        enable_snippets=enable_snippets,
        enable_deep_scan=enable_deep_scan,
        enable_expand=enable_expand,
        enable_compute=enable_compute,
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
                    "pool": [],
                    "evidences": [],
                    "asked": [],
                    "partial": False,
                    "computed": {},
                },
                {"recursion_limit": max(25, max_iterations * 8 + 10)},
            )
        except Exception:
            _LOG.exception("run_keyword_agentic_rag_v5: graph execution failed")
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
