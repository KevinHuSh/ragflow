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

"""Agentic-RAG capability layer — four small tools instead of one big one.

Where the graph-driven ``RAGTools`` exposed a single terminal ``rag`` tool that
researched a whole question internally, this one hands the model the pieces and
lets it drive:

    find_documents  →  which documents could hold the answer (titles only)
    read_document   →  page-ordered snippets from ONE of them
    compute         →  arithmetic Python performs, not the model
    brief           →  one factual answer from what was just read

The split exists because the terminal-tool shape has a failure mode with no
recovery: whatever the model held back from the call — a count, a sum, a
comparison it meant to finish afterwards — was lost, because nothing ran after
the tool returned. Here every step is a separate call, so the model keeps the
turn and finishes its own reasoning.

``compute`` deliberately duplicates the arithmetic evaluator from
:mod:`rag.advanced_rag.keyword_agentic_graph_v9` rather than importing it: the
two are free to diverge, and this module stays standalone.
"""

import ast
import logging
from typing import List

import json_repair
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from common import settings
from common.misc_utils import thread_pool_exec
from common.token_utils import num_tokens_from_string
from rag.advanced_rag.harness.tools.search import _narrow_content
from rag.app.tag import label_question
from rag.llm.tool_decorator import tool
from rag.prompts.generator import (
    citation_prompt,
    form_message,
    kb_prompt,
    message_fit_in,
)
from api.db.db_models import Document, Knowledgebase
from rag.utils.web_search_conn import WebSearchProvider

_LOG = logging.getLogger(__name__)

# ── Tunable caps ───────────────────────────────────────────────────────────────
_DOC_TOP_N = 30  # chunk hits scanned when ranking candidate documents
_DOC_CANDIDATES = 3  # candidate documents returned per call
_DOC_CHUNK_PAGE = 128  # rows per chunk_list page while walking one document


# ── Arithmetic over what was read ──────────────────────────────────────────────
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
    operands must be provably numeric.
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
        # `len([])` is not a count of zero, it is the caller reporting that it found
        # nothing — and a "0" handed back as a computed fact ends the search at a
        # wrong answer. Same for an empty name or identifier.
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not node.elts:
            return "an empty list is nothing to count, not a result of zero"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and not node.value.strip():
            return "an empty string is nothing to measure"
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
    """Evaluate a model-written arithmetic expression. Returns (rendered, error).

    Exactly one of the two is non-empty.
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


_BRIEF_ANSWER_SYSTEM = """You are given a QUESTION and numbered snippets that were just read from the
knowledge base.

Answer the QUESTION from those snippets only — one or two sentences, factual, no speculation, no
outside knowledge, no reasoning shown.
- Keep every detail a later step will need: exact dates, exact numbers, full names, units. A summary
  that drops the figure is useless to the step that follows.
- If the QUESTION asks for a set or a list, name EVERY member the snippets state, not just the first.
  And if the snippets show that MORE members exist without naming them ("the rest of his career",
  "among others"), say so in the answer. A list presented as complete when it is not is the one error
  the steps after this cannot detect or recover from.
- A question naming part of a DOCUMENT — "the bibliography section", "the infobox" — is asking about
  that part's CONTENT. Chunking routinely severs a heading from what sits under it, so the heading
  being absent says nothing about whether the content is here. Answer from the entries you can see.
- "relevant": the NUMBERS of the snippets your answer rests on.
- "found": false when these snippets do not let you answer the QUESTION, true when they do.

WHEN THE SNIPPETS DO NOT ANSWER THE QUESTION, say nothing at all: set "found" false and leave
"answer" and "relevant" EMPTY. A sentence ABOUT the snippets is NOT an answer — "the provided text
does not contain the bibliography section", "this cannot be determined from the given passages" are
all the empty answer, written the long way. Report what the snippets DO state, or report nothing.
Output ONLY JSON, no prose, no code fences:
{"answer": "<one or two factual sentences, or empty>", "relevant": [<number>, ...], "found": true/false}"""


def _extract_json(text: str) -> dict:
    try:
        parsed = json_repair.loads((text or "").strip())
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _chunk_text(chunk: dict) -> str:
    return str(chunk.get("content_with_weight") or chunk.get("content") or "").strip()


class RAGTools:
    """Retrieval primitives, exposed to the model as four independent tools."""

    def __init__(
        self,
        tenant_ids: list[str],
        chat_mdl: LLMBundle,
        embed_mdl: LLMBundle | None = None,
        kb_ids: List[str] | None = None,
        kbs: list[Knowledgebase] | None = None,
        web_search: WebSearchProvider | None = None,
        meta_data_filter: dict | None = None,
        doc_scope: List[str] | None = None,
        user_defined_prompts: dict | None = None,
        empty_response: str = "",
        do_refer: bool | None = True,
        thinking_mode: str = "medium",
        text_attachments_content: str = "",
        messages: list | None = None,
    ):
        self.tenant_ids = tenant_ids
        self.chat_mdl = chat_mdl.clone()
        self.embed_mdl = embed_mdl
        self.thinking_mode = thinking_mode
        self.field_map = {}
        self.sql_kbs = []
        self.kbs = []
        self.kb_ids = []

        def _exclude_sql_kb(kb):
            if kb.parser_config and "field_map" in kb.parser_config:
                self.field_map.update(kb.parser_config["field_map"])
                self.sql_kbs.append(kb)
            else:
                self.kbs.append(kb)
                self.kb_ids.append(kb.id)

        if kb_ids:
            for kb in KnowledgebaseService.get_by_ids(kb_ids):
                _exclude_sql_kb(kb)
        elif kbs:
            for kb in kbs:
                _exclude_sql_kb(kb)

        self.web_search = web_search
        self.meta_data_filter = meta_data_filter
        self.doc_scope = list(dict.fromkeys(doc_scope)) if doc_scope is not None else None
        self.user_defined_prompts = user_defined_prompts or {}
        self.empty_response = empty_response
        self.do_refer = do_refer

        # Citation pool: every chunk any tool has surfaced this turn, in the SAME
        # order the answer's ``[ID:n]`` markers index, so the caller can resolve
        # references afterwards.
        self.kbinfos: dict[str, list] = {"chunks": [], "doc_aggs": []}

        # What `read_document` last produced. `brief` answers from THIS, implicitly:
        # asking the model to pass snippet ids back is one more thing for it to get
        # wrong, and the working set is unambiguous — it is whatever it just read.
        self._working_set: list[dict] = []
        self._working_question: str = ""

        self.tools = [self.find_documents, self.read_document, self.compute, self.brief]

    # ------------------------------------------------------------------ #
    # Capability flags / cheap introspection
    # ------------------------------------------------------------------ #
    def has_unstructured(self) -> bool:
        return bool(self.kb_ids)

    def has_structured(self) -> bool:
        return bool(self.sql_kbs and self.field_map)

    def has_web(self) -> bool:
        return self.web_search is not None

    def has_llm(self) -> bool:
        return self.chat_mdl is not None

    def scoped_doc_ids(self, doc_scope: List[str] | None = None) -> List[str] | None:
        if self.doc_scope is None:
            return doc_scope
        if not doc_scope:
            return list(self.doc_scope)
        allowed = set(self.doc_scope)
        return [doc_id for doc_id in doc_scope if doc_id in allowed]

    async def _fit_messages(self, system: str, user: str) -> list:
        """Fit system+user messages into the model's context window."""
        _, msg = message_fit_in(form_message(system, user), self.chat_mdl.max_length)
        return msg

    def get_citation_guidelines(self) -> str:
        """Return the citation guidelines the final answer must follow."""
        return citation_prompt(self.user_defined_prompts)

    def sys_prompt(self) -> str:
        """Router prompt for callers that bind ``self.tools``."""
        return (
            "You research questions with four tools. Work one hop at a time.\n"
            "1. `find_documents` — name the entity you need and get back candidate documents "
            "(IDs and titles). It returns no text; it tells you WHERE to read.\n"
            "2. `read_document` — read ONE candidate with the words you expect to see. It returns "
            "the matching passages in document order. Read the most promising candidate first, and "
            "try the next one when it does not hold the answer.\n"
            "3. `compute` — for ANY number you would otherwise work out yourself: a difference of "
            "years, a ratio, a total, a count of letters or digits. Write the arithmetic as a "
            "Python expression with the figures you read substituted in. Never do the arithmetic in "
            "your head.\n"
            "4. `brief` — turn what you just read into one factual sentence before moving to the "
            "next hop, so the detail survives.\n\n"
            "A multi-hop question is several rounds of the above: resolve one fact, substitute it "
            "into the next question, search again. Never invent a document ID, and never state a "
            "fact no passage supports — say plainly what is missing instead."
        )

    # ------------------------------------------------------------------ #
    # Bound tools
    # ------------------------------------------------------------------ #
    @tool
    async def find_documents(self, query: str) -> list[dict]:
        """Find which documents could hold the answer, by ID and title.

        Searches the whole knowledge base and returns the few documents whose
        content matches best. It deliberately returns NO text — use it to decide
        where to read, then call `read_document` on a candidate.

        :param query: the entity and the words you expect the document to use
            ("Ron Hutchinson ice hockey career teams"). Not a sentence.

        :returns: up to three ``{"doc_id", "doc_name"}`` entries, best first.
            Empty when nothing matches.
        """
        if not self.kb_ids or not (query or "").strip():
            return []

        vec_weight = 0.3 if self.embed_mdl else 0
        try:
            kbinfos = await settings.retriever.retrieval(
                query,
                self.embed_mdl,
                self.tenant_ids,
                self.kb_ids,
                1,
                _DOC_TOP_N,
                0.2,
                vector_similarity_weight=vec_weight,
                aggs=False,
                highlight=False,
                doc_ids=self.scoped_doc_ids(None),
                rank_feature=label_question(query, self.kbs),
            )
        except Exception:
            _LOG.exception("[find_documents] retrieval failed for: %s", query[:120])
            return []

        # Rank documents by their BEST chunk: one strong passage is what makes a
        # document worth opening, and a long document would otherwise win on volume.
        best: dict[str, float] = {}
        names: dict[str, str] = {}
        first_seen: dict[str, int] = {}
        for i, ck in enumerate(kbinfos.get("chunks") or []):
            did = ck.get("doc_id")
            if not did:
                continue
            try:
                score = float(ck.get("similarity") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if score <= 0.0:
                # Retrieval is relevance-ordered, so rank decay stands in when the
                # store returns no similarity at all.
                score = 1.0 / (i + 1.0)
            if did not in best or score > best[did]:
                best[did] = score
            names.setdefault(did, ck.get("docnm_kwd", "") or "")
            first_seen.setdefault(did, i)

        ranked = sorted(best.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))[:_DOC_CANDIDATES]
        out = [{"doc_id": did, "doc_name": names.get(did, "")} for did, _ in ranked]
        _LOG.info("[find_documents] %s -> %s", query[:80], [d["doc_name"] or d["doc_id"] for d in out] or "none")
        return out

    @tool
    async def read_document(self, doc_id: str, keywords: str, grep: str = "") -> list[str]:
        """Read one document's passages that mention the words you name.

        Walks the document in reading order (page, then position) and keeps the
        passages carrying your keywords, narrowed to the matching sentences and
        their neighbours. Tables are kept whole, so a row is never cut in half.
        Document order is preserved, which matters when the answer is a table row
        or a section that follows a heading.

        :param doc_id: a document ID that `find_documents` returned. Never invent one.
        :param keywords: comma-separated words that select which passages to keep
            ("Flin Flon Bombers, SJHL, championship"). Matching is case-insensitive
            and tolerates inflection ("nominations" finds "nominated").
        :param grep: optional narrower set used to trim each kept passage down to
            its matching sentences. Defaults to `keywords`. Widen it when you need
            more context around the hit.

        :returns: numbered snippet blocks in document order, ready to quote and
            cite. Empty when the document holds none of the keywords.
        """
        if not self.kb_ids or not (doc_id or "").strip():
            return []
        if self.doc_scope is not None and doc_id not in self.doc_scope:
            _LOG.warning("[read_document] %s is outside the caller's document scope", doc_id)
            return []
        resolved = await thread_pool_exec(self._resolve_doc_tenant, doc_id)
        if resolved is None:
            _LOG.warning("[read_document] doc_id %r is not in any bound KB — refusing to read", doc_id)
            return []
        kb_id, tenant_id = resolved

        kwds = [k.strip() for k in (keywords or "").split(",") if k.strip()]
        grep_kwds = [k.strip() for k in (grep or "").split(",") if k.strip()] or kwds
        if not kwds:
            return []

        # `chunk_list` is the only primitive that orders by page_num_int; retrieval
        # ranks by relevance and its chunk dicts carry no page number at all. So the
        # document is walked in order and the keyword selection happens here — which
        # also keeps selection and snippet-narrowing on the same matcher.
        kept: list[dict] = []
        tokens = 0
        budget = self.chat_mdl.max_length
        for offset in range(0, 10000, _DOC_CHUNK_PAGE):
            try:
                chunks = await thread_pool_exec(
                    settings.retriever.chunk_list,
                    doc_id,
                    tenant_id,
                    [kb_id],
                    max_count=offset + _DOC_CHUNK_PAGE,
                    offset=offset,
                    fields=["content_with_weight", "docnm_kwd", "doc_id"],
                    sort_by_position=True,
                    retrieve_all=False,
                )
            except Exception:
                _LOG.exception("[read_document] chunk_list failed for %s at offset %d", doc_id, offset)
                break
            if not chunks:
                break
            for ck in chunks:
                narrowed = _narrow_content(_chunk_text(ck), grep_kwds)
                if narrowed is None:
                    continue
                num = num_tokens_from_string(narrowed)
                if tokens + num > budget:
                    break
                tokens += num
                ck = dict(ck)
                ck["content_with_weight"] = narrowed
                if "content" in ck:
                    ck["content"] = narrowed
                ck["token_num"] = num
                kept.append(ck)
            if tokens >= budget:
                break

        if not kept:
            _LOG.info("[read_document] %s holds none of: %s", doc_id, ", ".join(kwds))
            return []

        # The working set is what `brief` answers from, and the citation pool is what
        # the caller resolves [ID:n] against.
        self._working_set = kept
        start_idx = len(self.kbinfos.get("chunks", []))
        self.kbinfos["chunks"].extend(kept)
        doc_name = next((c.get("docnm_kwd") or "" for c in kept if c.get("docnm_kwd")), "")
        self.kbinfos["doc_aggs"].append({"doc_name": doc_name, "doc_id": doc_id, "count": len(kept)})

        _LOG.info("[read_document] %s (%s) -> %d passage(s), %d token(s)", doc_id, doc_name, len(kept), tokens)
        blocks = kb_prompt(self.kbinfos, budget)
        blocks = blocks[start_idx:] if start_idx else blocks
        if not self.do_refer:
            return blocks
        header = "# Citation rules\nApply the following rules VERBATIM to your final answer.\n\n" + citation_prompt(self.user_defined_prompts).strip() + "\n\n----\n\n"
        return [header] + blocks

    @tool
    async def compute(self, expression: str) -> str:
        """Work out a number exactly, instead of doing the arithmetic yourself.

        Call this for ANY figure the sources do not state outright but that follows
        from figures they do: a sum, a difference, a count, an average, a
        percentage, a unit conversion, an elapsed span. Substitute every figure you
        read as a literal — the expression is evaluated on its own, with no
        variables, no imports, no attributes and no subscripts.

        Available functions: abs, round, min, max, sum, len, int, float, sorted,
        letters and digit_sum.
          combined population of three  -> 12345 + 6789 + 101112
          how many of the listed items  -> len(["Alpha", "Beta", "Gamma"])
          what percentage one figure is -> 100 * 4523 / 18092
          years between two dates       -> 1998 - 1954
          letters in a set of names     -> letters("Ada Lovelace", "Alan Turing")
          digits of a postcode added up -> digit_sum("L7 7BN")

        `letters` counts alphabetic characters only, ignoring spaces and
        punctuation; never use len() on a name, which counts them as letters.
        `digit_sum` adds each digit separately — digit_sum("2020") is 4, not 2020 —
        and is for digits INSIDE an identifier; whole numbers stated separately are
        added as plain literals.

        Never pass an expression over an empty list: finding nothing to count is not
        a result of zero, and it is refused.

        :param expression: one Python expression, with every figure inline.

        :returns: the number, or a message explaining why the expression was
            refused — read it and send a corrected expression.
        """
        rendered, error = _compute(expression)
        if error:
            _LOG.info("[compute] refused `%s` — %s", (expression or "")[:120], error)
            return f"Refused: {error}. Send a corrected expression."
        _LOG.info("[compute] %s = %s", (expression or "")[:120], rendered)
        return rendered

    @tool
    async def brief(self, question: str) -> str:
        """Turn the passages you just read into one factual answer.

        Answers from the passages the LAST `read_document` call returned — you do
        not pass them back. Use it to fix a hop's answer before moving on, so the
        exact date, number or name survives into the next question.

        :param question: the single question these passages should answer.

        :returns: one or two factual sentences, or a plain statement that the
            passages do not answer it.
        """
        chunks = self._working_set
        if not chunks:
            return "Nothing has been read yet — call `read_document` first."
        if not (question or "").strip():
            return "Ask a question for the passages to answer."

        numbered = "\n\n".join(f"[{i + 1}] Title: {c.get('docnm_kwd') or ''}\n{_chunk_text(c)}" for i, c in enumerate(chunks))
        msg = await self._fit_messages(_BRIEF_ANSWER_SYSTEM, f"QUESTION:\n{question}\n\nSnippets:\n{numbered}\n\nOutput JSON:")
        try:
            ans = await self.chat_mdl.async_chat(msg[0]["content"], msg[1:], {"temperature": 0.2})
        except Exception:
            _LOG.exception("[brief] the model call failed")
            return "The passages could not be summarised."
        if isinstance(ans, tuple):
            ans = ans[0]
        parsed = _extract_json(ans)

        answer = str(parsed.get("answer") or "").strip()
        # `found` is the reliable half: a model with nothing to report still tends to
        # write a sentence explaining that it has nothing, and such a sentence read
        # back as a finding would be treated as something a source stated.
        if not answer or parsed.get("found") is False:
            _LOG.info("[brief] the passages do not answer: %s", question[:120])
            return "These passages do not answer that question."
        self._working_question = question
        _LOG.info("[brief] %s -> %s", question[:80], answer[:120])
        return answer

    # ------------------------------------------------------------------ #
    # Low-level DB helpers (sync — wrap in thread_pool_exec at call sites)
    # ------------------------------------------------------------------ #
    async def _get_cached_metas(self) -> dict:
        cached = getattr(self, "_metas_cache", None)
        if cached is not None:
            return cached
        if not self.kb_ids:
            self._metas_cache = {}
            return self._metas_cache
        self._metas_cache = await thread_pool_exec(DocMetadataService.get_flatted_meta_by_kbs, self.kb_ids)
        return self._metas_cache or {}

    def _filter_known_doc_ids(self, candidate_ids: list[str]) -> set[str]:
        if not candidate_ids or not self.kb_ids:
            return set()
        rows = Document.select(Document.id).where((Document.id.in_(list(candidate_ids))) & (Document.kb_id.in_(self.kb_ids)))
        return {row.id for row in rows}

    def _resolve_doc_tenant(self, doc_id: str) -> tuple[str, str] | None:
        rows = list(Document.select(Document.kb_id).where((Document.id == doc_id) & (Document.kb_id.in_(self.kb_ids))))
        if not rows:
            return None
        kb_id = rows[0].kb_id
        for kb in self.kbs:
            if kb.id == kb_id:
                return kb_id, kb.tenant_id
        return None


__all__ = ["RAGTools"]
