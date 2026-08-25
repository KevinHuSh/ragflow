from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


DEFAULT_CONFIG_PATH = Path("configs/frame_benchmark_conf.json")


class BenchmarkError(RuntimeError):
    pass


class JsonHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        max_retries: int,
        backoff_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.session = requests.Session()

    def clone(self) -> "JsonHttpClient":
        return JsonHttpClient(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            backoff_seconds=self.backoff_seconds,
        )

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, json=body)

    def post_eventstream(self, path: str, body: dict[str, Any]) -> Any:
        stream_body = dict(body)
        stream_body["stream"] = True
        return self._request_eventstream("POST", path, json=stream_body)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    **kwargs,
                )
                payload = _decode_response(response)
                if response.status_code >= 400:
                    raise BenchmarkError(f"HTTP {response.status_code} from {url}: {_compact(payload)}")
                if isinstance(payload, dict) and payload.get("code", 0) not in (0, None):
                    raise BenchmarkError(f"RAGFlow code {payload.get('code')} from {url}: {payload.get('message') or _compact(payload)}")
                return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
            except (requests.RequestException, BenchmarkError) as exc:
                last_error = exc
                if attempt >= self.max_retries or not _should_retry(exc):
                    break
                time.sleep(self.backoff_seconds * (2**attempt))

        raise BenchmarkError(str(last_error))

    def _request_eventstream(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {self.api_key}",
        }
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    stream=True,
                    **kwargs,
                )
                if response.status_code >= 400:
                    payload = _decode_response(response)
                    raise BenchmarkError(f"HTTP {response.status_code} from {url}: {_compact(payload)}")
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    payload = _decode_response(response)
                    if isinstance(payload, dict) and payload.get("code", 0) not in (0, None):
                        raise BenchmarkError(f"RAGFlow code {payload.get('code')} from {url}: {payload.get('message') or _compact(payload)}")
                    return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
                return _collect_eventstream_answer(response.iter_lines(decode_unicode=True))
            except (requests.RequestException, BenchmarkError) as exc:
                last_error = exc
                if attempt >= self.max_retries or not _should_retry(exc):
                    break
                time.sleep(self.backoff_seconds * (2**attempt))

        raise BenchmarkError(str(last_error))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FRAMES questions against one RAGFlow chat and judge answers with another RAGFlow chat.")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("retry", "repair"),
        help="Maintenance command on an existing output directory: 'retry' re-runs errored rows, 'repair' re-runs rows that scored 0",
    )
    parser.add_argument("command_output_dir", nargs="?", help="Output directory used by the retry/repair command")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to frame_benchmark_conf.json")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output JSONL files instead of resuming")
    parser.add_argument("--dry-run", action="store_true", help="Load config and questions, then print the planned run without calling APIs")
    parser.add_argument("--skip-answers", action="store_true", help="Skip RAGFlow answering and judge an existing answers JSONL")
    parser.add_argument("--skip-judge", action="store_true", help="Only collect RAGFlow answers; do not call the judge chat")
    parser.add_argument(
        "--bad-cases",
        help="UTF-8 file containing question<TAB>RAGFlow answer rows; run only matching questions from the frames mapping",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = _load_json(config_path)
    base_dir = config_path.parent

    if args.command in ("retry", "repair"):
        if not args.command_output_dir:
            parser.error(f"{args.command} requires an output directory")
        output_dir = Path(args.command_output_dir).expanduser()
        if not output_dir.is_dir():
            parser.error(f"{args.command} output directory does not exist: {output_dir}")
        answers_path = output_dir / cfg.get("output", {}).get("answers_jsonl", "answers.jsonl")
        judged_path = output_dir / cfg.get("output", {}).get("judged_jsonl", "judged_answers.jsonl")
        report_path = output_dir / cfg.get("output", {}).get("report_json", "report.json")
        if args.command == "retry":
            asyncio.run(
                retry_failed_items(
                    client_cfg=cfg,
                    answers_path=answers_path,
                    judged_path=judged_path,
                )
            )
        else:
            asyncio.run(
                repair_zero_scores(
                    client_cfg=cfg,
                    answers_path=answers_path,
                    judged_path=judged_path,
                )
            )
        report = build_report(_read_jsonl(judged_path))
        _write_json(report_path, report)
        print(f"Report written to {report_path}")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    output_dir = _resolve_output_dir(cfg.get("output", {}).get("output_dir", "outputs/frame_benchmark_<timestamp>"))
    answers_path = output_dir / cfg.get("output", {}).get("answers_jsonl", "answers.jsonl")
    judged_path = output_dir / cfg.get("output", {}).get("judged_jsonl", "judged_answers.jsonl")
    report_path = output_dir / cfg.get("output", {}).get("report_json", "report.json")

    mapping_path = _resolve_path(cfg["frames_mapping_path"], base_dir)
    questions = _load_frames_questions(mapping_path)
    if args.bad_cases:
        bad_cases_path = _resolve_path(args.bad_cases, base_dir)
        selected = _load_bad_case_questions(
            bad_cases_path,
            questions,
            sample_count=cfg.get("question_sample_count"),
            strategy=cfg.get("sample_strategy", "random"),
            seed=cfg.get("sample_seed", 42),
        )
        print(f"Bad-case file: {bad_cases_path}")
    else:
        selected = _select_questions(
            questions,
            sample_count=cfg.get("question_sample_count"),
            strategy=cfg.get("sample_strategy", "random"),
            seed=cfg.get("sample_seed", 42),
        )

    print(f"Loaded {len(questions)} questions from {mapping_path}")
    print(f"Selected {len(selected)} questions")
    print(f"Output directory: {output_dir}")

    if args.dry_run:
        for question in selected[:5]:
            print(f"- {question['question_id']}: {question['question'][:100]}")
        if len(selected) > 5:
            print(f"... {len(selected) - 5} more")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_answers:
        if args.overwrite and answers_path.exists():
            answers_path.unlink()
        ragflow_client = _make_client(cfg)
        asyncio.run(
            run_answer_phase(
                client=ragflow_client,
                cfg=cfg,
                questions=selected,
                answers_path=answers_path,
            )
        )

    if args.skip_judge:
        print(f"Answers written to {answers_path}")
        return 0

    if args.overwrite and judged_path.exists():
        judged_path.unlink()

    judge_client = _make_client(cfg)
    asyncio.run(
        run_judge_phase(
            client=judge_client,
            cfg=cfg,
            answers_path=answers_path,
            judged_path=judged_path,
        )
    )
    report = build_report(_read_jsonl(judged_path))
    _write_json(report_path, report)
    print(f"Judged rows written to {judged_path}")
    print(f"Report written to {report_path}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _replace_jsonl_rows(path: Path, replacements: dict[str, dict[str, Any]]) -> None:
    """Substitute rows by ``question_id`` in place, preserving file order."""
    if not replacements:
        return
    rows = _read_jsonl(path)
    out = []
    for row in rows:
        question_id = row.get("question_id")
        key = str(question_id) if question_id is not None else None
        out.append(replacements.get(key, row) if key else row)
    _write_jsonl(path, out)


async def repair_zero_scores(
    *,
    client_cfg: dict[str, Any],
    answers_path: Path,
    judged_path: Path,
) -> list[dict[str, Any]]:
    """Re-run every judged row that scored 0, then re-judge and rewrite both files.

    Non-destructive: the original answer/judged rows are snapshotted first, and any
    question whose repair attempt errored (or produced no answer) is restored
    exactly as it was. A repair can therefore only improve a row or leave it
    unchanged — it never turns a scored 0 into an excluded error row, which would
    silently shrink the report's denominator.

    Returns the score-0 rows that were targeted.
    """
    if not await asyncio.to_thread(answers_path.exists):
        raise FileNotFoundError(f"Missing answers file: {answers_path}")
    if not await asyncio.to_thread(judged_path.exists):
        raise FileNotFoundError(f"Missing judged file: {judged_path}")

    judged_rows = _read_jsonl(judged_path)
    zero_rows = [row for row in judged_rows if row.get("question_id") is not None and _as_float(row.get("accuracy")) < 4]
    if not zero_rows:
        print("[repair] no score-0 rows found; nothing to do")
        return []

    target_ids = {str(row["question_id"]) for row in zero_rows}
    print(f"[repair] {len(target_ids)} question(s) scored 0:")
    for row in zero_rows:
        print(f"  - {row['question_id']}: {str(row.get('question', ''))[:110]}")

    # Snapshot originals so a failed repair can be rolled back.
    orig_answers = {qid: row for qid, row in _read_jsonl_by_id(answers_path).items() if qid in target_ids}
    orig_judged = {qid: row for qid, row in _read_jsonl_by_id(judged_path).items() if qid in target_ids}

    questions = [
        {
            "question_id": str(row["question_id"]),
            "question": row.get("question", ""),
            "gold_answer": row.get("gold_answer", ""),
            "reasoning_types": row.get("reasoning_types") or [],
        }
        for row in zero_rows
    ]

    # Re-ask RAGFlow, then re-judge. Both phases resume-append, so drop the old
    # rows first and let them be regenerated.
    _remove_jsonl_ids(answers_path, target_ids)
    await run_answer_phase(
        client=_make_client(client_cfg),
        cfg=client_cfg,
        questions=questions,
        answers_path=answers_path,
    )
    _remove_jsonl_ids(judged_path, target_ids)
    await run_judge_phase(
        client=_make_client(client_cfg),
        cfg=client_cfg,
        answers_path=answers_path,
        judged_path=judged_path,
    )
    _deduplicate_jsonl(answers_path)
    _deduplicate_jsonl(judged_path)

    # Roll back any question whose repair failed outright.
    new_judged = _read_jsonl_by_id(judged_path)
    rolled_back = {qid for qid in target_ids if qid not in new_judged or _row_has_error(new_judged[qid])}
    if rolled_back:
        _replace_jsonl_rows(answers_path, {qid: orig_answers[qid] for qid in rolled_back if qid in orig_answers})
        _replace_jsonl_rows(judged_path, {qid: orig_judged[qid] for qid in rolled_back if qid in orig_judged})
        print(f"[repair] {len(rolled_back)} repair(s) failed and were rolled back to the original row")

    improved = 0
    for qid in target_ids - rolled_back:
        score = _as_float((new_judged.get(qid) or {}).get("accuracy"))
        if score is not None and score > 0:
            improved += 1
    print(f"[repair] {improved}/{len(target_ids)} question(s) now score above 0")
    return zero_rows


async def retry_failed_items(
    *,
    client_cfg: dict[str, Any],
    answers_path: Path,
    judged_path: Path,
) -> None:
    """Retry failed answer/judge rows in an existing benchmark output directory.

    The judge file is optional because an answer-only run may not have reached
    the judge phase yet. In that case, the normal judge resume behavior treats
    every answer row as pending.
    """
    if not await asyncio.to_thread(answers_path.exists):
        raise FileNotFoundError(f"Missing answers file: {answers_path}")

    answer_rows = _read_jsonl(answers_path)
    judged_rows = _read_jsonl(judged_path)
    answer_retry_ids = {str(row["question_id"]) for row in answer_rows if row.get("question_id") is not None and _answer_row_has_error(row)}
    judge_retry_ids = {str(row["question_id"]) for row in judged_rows if row.get("question_id") is not None and _row_has_error(row)}
    judge_retry_ids.update(answer_retry_ids)

    answer_questions_by_id = {
        str(row["question_id"]): {
            "question_id": str(row["question_id"]),
            "question": row.get("question", ""),
            "gold_answer": row.get("gold_answer", ""),
            "reasoning_types": row.get("reasoning_types") or [],
        }
        for row in answer_rows
        if row.get("question_id") is not None and _answer_row_has_error(row)
    }
    print(f"[retry] answer errors: {len(answer_retry_ids)}")
    print(f"[retry] judge errors: {len(judge_retry_ids)}")

    if answer_retry_ids:
        _remove_jsonl_ids(answers_path, answer_retry_ids)
        await run_answer_phase(
            client=_make_client(client_cfg),
            cfg=client_cfg,
            questions=list(answer_questions_by_id.values()),
            answers_path=answers_path,
        )

    _remove_jsonl_ids(judged_path, judge_retry_ids)
    await run_judge_phase(
        client=_make_client(client_cfg),
        cfg=client_cfg,
        answers_path=answers_path,
        judged_path=judged_path,
    )

    _deduplicate_jsonl(answers_path)
    _deduplicate_jsonl(judged_path)


async def run_answer_phase(
    *,
    client: JsonHttpClient,
    cfg: dict[str, Any],
    questions: list[dict[str, Any]],
    answers_path: Path,
) -> None:
    completed_ids = set(_read_jsonl_by_id(answers_path))
    chat_cfg = cfg.get("ragflow_chat", {})
    chat_id = cfg["chat_id"]
    shared_session_id = None
    concurrency = max(1, int(chat_cfg.get("concurrency", cfg.get("answer_concurrency", 4))))

    if not chat_cfg.get("fresh_session_per_question", True):
        shared_session_id = await asyncio.to_thread(create_session, client.clone(), chat_id)
        concurrency = 1

    pending = [(index, question) for index, question in enumerate(questions, start=1) if question["question_id"] not in completed_ids]
    for index, question in enumerate(questions, start=1):
        if question["question_id"] in completed_ids:
            print(f"[answers] skip existing {index}/{len(questions)} {question['question_id']}")
    if not pending:
        return

    semaphore = asyncio.Semaphore(concurrency)

    async def _answer_one(index: int, question: dict[str, Any]) -> dict[str, Any]:
        question_id = question["question_id"]
        print(f"[answers] {index}/{len(questions)} {question_id}")
        row = {
            "question_id": question_id,
            "question": question["question"],
            "gold_answer": question["gold_answer"],
            "reasoning_types": question["reasoning_types"],
            "ragflow_answer": "",
            "ragflow_error": "",
        }
        try:
            session_id = None if chat_cfg.get("fresh_session_per_question", True) else shared_session_id
            async with semaphore:
                answer_payload = await ask_ragflow(client.clone(), cfg, question["question"], session_id=session_id)
            answer = extract_answer_text(answer_payload)
            row["ragflow_answer"] = answer
            row["ragflow_session_id"] = _extract_session_id(answer_payload)
            if not answer:
                row["ragflow_error"] = "empty_ragflow_answer"
            elif answer.lstrip().startswith("**ERROR**"):
                row["ragflow_error"] = answer.strip()
        except Exception as exc:  # noqa: BLE001
            row["ragflow_error"] = str(exc)
        return row

    tasks = [asyncio.create_task(_answer_one(index, question)) for index, question in pending]
    with answers_path.open("a", encoding="utf-8") as handle:
        for task in asyncio.as_completed(tasks):
            row = await task
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()


async def run_judge_phase(
    *,
    client: JsonHttpClient,
    cfg: dict[str, Any],
    answers_path: Path,
    judged_path: Path,
) -> None:
    if not await asyncio.to_thread(answers_path.exists):
        raise FileNotFoundError(f"Missing answers file: {answers_path}")

    rows = _read_jsonl(answers_path)
    completed_ids = set(_read_jsonl_by_id(judged_path))
    judge_cfg = cfg.get("judge", {})
    concurrency = max(1, int(judge_cfg.get("concurrency", cfg.get("judge_concurrency", 4))))

    pending = [(index, row) for index, row in enumerate(rows, start=1) if str(row.get("question_id")) not in completed_ids]
    for index, row in enumerate(rows, start=1):
        question_id = str(row.get("question_id"))
        if question_id in completed_ids:
            print(f"[judge] skip existing {index}/{len(rows)} {question_id}")
    if not pending:
        return

    semaphore = asyncio.Semaphore(concurrency)

    async def _judge_one(index: int, row: dict[str, Any]) -> dict[str, Any]:
        question_id = str(row.get("question_id"))
        print(f"[judge] {index}/{len(rows)} {question_id}")
        judged = dict(row)
        judged["accuracy"] = None
        judged["accuracy_level"] = None
        judged["judge_answer"] = ""
        judged["judge_error"] = ""

        if row.get("ragflow_error"):
            judged["judge_error"] = "excluded_due_to_ragflow_error"
            return judged

        try:
            async with semaphore:
                judge_payload = await ask_judge(client.clone(), cfg, row)
            judge_answer = extract_answer_text(judge_payload)
            accuracy, level, parsed = parse_accuracy(judge_answer)
            judged["judge_answer"] = judge_answer
            judged["judge_parsed"] = parsed
            judged["accuracy"] = accuracy
            judged["accuracy_level"] = level
        except Exception as exc:  # noqa: BLE001
            judged["judge_error"] = str(exc)
        return judged

    tasks = [asyncio.create_task(_judge_one(index, row)) for index, row in pending]
    with judged_path.open("a", encoding="utf-8") as handle:
        for task in asyncio.as_completed(tasks):
            judged = await task
            handle.write(json.dumps(judged, ensure_ascii=False) + "\n")
            handle.flush()


def create_session(client: JsonHttpClient, chat_id: str) -> str:
    payload = client.post(
        f"/api/v1/chats/{quote(chat_id, safe='')}/sessions",
        {"name": f"frame-benchmark-{datetime.now().strftime('%Y%m%d-%H%M%S')}"},
    )
    session_id = _extract_session_id(payload)
    if not session_id:
        raise BenchmarkError(f"Could not create chat session: {_compact(payload)}")
    return session_id


async def ask_ragflow(client: JsonHttpClient, cfg: dict[str, Any], question: str, *, session_id: str | None) -> Any:
    return await asyncio.to_thread(_ask_ragflow_sync, client, cfg, question, session_id=session_id)


def _ask_ragflow_sync(client: JsonHttpClient, cfg: dict[str, Any], question: str, *, session_id: str | None) -> Any:
    chat_cfg = cfg.get("ragflow_chat", {})
    body: dict[str, Any] = {
        "chat_id": cfg["chat_id"],
        "question": question,
        "stream": bool(chat_cfg.get("stream", False)),
    }
    if session_id:
        body["session_id"] = session_id
    if chat_cfg.get("llm_id"):
        body["llm_id"] = chat_cfg["llm_id"]
    for key in ("quote", "refine_multiturn", "temperature", "top_p", "max_tokens", "reasoning"):
        if key in chat_cfg:
            body[key] = chat_cfg[key]
    body["reasoning"] = 2
    res = post_chat_completion(client, body)
    # print(res)
    return res


async def ask_judge(client: JsonHttpClient, cfg: dict[str, Any], row: dict[str, Any]) -> Any:
    return await asyncio.to_thread(_ask_judge_sync, client, cfg, row)


def _ask_judge_sync(client: JsonHttpClient, cfg: dict[str, Any], row: dict[str, Any]) -> Any:
    judge_cfg = cfg["judge"]
    prompt = render_prompt(judge_cfg["judgement_prompt"], row)
    body: dict[str, Any] = {
        "chat_id": judge_cfg["chat_id"],
        "question": prompt,
        "stream": bool(judge_cfg.get("stream", False)),
    }
    res = post_chat_completion(client, body)
    return res


def post_chat_completion(client: JsonHttpClient, body: dict[str, Any]) -> Any:
    if body.get("stream"):
        return client.post_eventstream("/api/v1/chat/completions", body)
    res = client.post("/api/v1/chat/completions", body)
    return res


def render_prompt(template: str, row: dict[str, Any]) -> str:
    return (
        template.replace("{question}", str(row.get("question", "")))
        .replace("{gold_answer}", str(row.get("gold_answer", "")))
        .replace("{ragflow_answer}", re.sub(r"<think>.*?</think>", "", str(row.get("ragflow_answer", ""))))
    )


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    error_rows = [row for row in rows if _row_has_error(row)]
    scored_rows = [row for row in rows if not _row_has_error(row) and _as_float(row.get("accuracy")) is not None]

    by_type_scores: dict[str, list[float]] = defaultdict(list)
    by_type_levels: dict[str, list[float]] = defaultdict(list)
    for row in scored_rows:
        score = _as_float(row.get("accuracy"))
        if score is None:
            continue
        level = _as_float(row.get("accuracy_level"))
        reasoning_types = row.get("reasoning_types") or ["__missing_reasoning_type__"]
        for reasoning_type in reasoning_types:
            by_type_scores[str(reasoning_type)].append(score)
            if level is not None:
                by_type_levels[str(reasoning_type)].append(level)

    error_breakdown = Counter(_error_label(row) for row in error_rows)
    level_distribution = Counter(str(int(row["accuracy_level"])) for row in scored_rows if _as_float(row.get("accuracy_level")) is not None)
    return {
        "overall_accuracy": _average([_as_float(row.get("accuracy")) for row in scored_rows]),
        "overall_accuracy_level": _average([_as_float(row.get("accuracy_level")) for row in scored_rows]),
        "reasoning_type_accuracy": {
            reasoning_type: {
                "accuracy": _average(scores),
                "accuracy_level": _average(by_type_levels.get(reasoning_type, [])),
                "scored_number": len(scores),
            }
            for reasoning_type, scores in sorted(by_type_scores.items())
        },
        "total_number": total,
        "error_number": len(error_rows),
        "scored_number": len(scored_rows),
        "excluded_error_number": len(error_rows),
        "accuracy_level_distribution": dict(sorted(level_distribution.items())),
        "error_breakdown": dict(sorted(error_breakdown.items())),
    }


def parse_accuracy(text: str) -> tuple[float, int | None, dict[str, Any]]:
    text = re.sub(r"<think>.*</think>", "", text)
    parsed = _parse_jsonish(text)
    if parsed is not None:
        level = _extract_level(parsed)
        accuracy = _extract_score(parsed)
        if accuracy is not None:
            return accuracy, level, parsed
        if level is not None:
            return level / 4.0, level, parsed
    match = re.search(r"(?:accuracy|score)\s*[:=]\s*([0-9]+(?:\.\d+)?)", text, flags=re.IGNORECASE)
    if match:
        accuracy = float(match.group(1))
        if accuracy in (0.0, 2.0, 4.0):
            accuracy = accuracy / 4.0
        elif 1.0 < accuracy <= 100.0:
            accuracy = accuracy / 100.0
        return accuracy, None, {"accuracy": accuracy}
    raise ValueError(f"Could not parse judge accuracy from: {text[:500]}")


def extract_answer_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("answer", "content", "text", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = extract_answer_text(value)
                if nested:
                    return nested
        if "data" in payload:
            return extract_answer_text(payload["data"])
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            return extract_answer_text(choices[0])
    return ""


def _extract_session_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("session_id", "conversation_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if isinstance(payload.get("data"), dict):
        return _extract_session_id(payload["data"])
    return None


def _load_frames_questions(mapping_path: Path) -> list[dict[str, Any]]:
    mapping = _load_json(mapping_path)
    questions = []
    for question_id, payload in sorted(mapping.items(), key=lambda item: _natural_key(item[0])):
        questions.append(
            {
                "question_id": str(question_id),
                "question": payload.get("question", ""),
                "gold_answer": payload.get("gold_answer", ""),
                "reasoning_types": payload.get("reasoning_types") or [],
            }
        )
    return questions


def _load_bad_case_questions(
    bad_cases_path: Path,
    questions: list[dict[str, Any]],
    *,
    sample_count: Any,
    strategy: str,
    seed: int,
) -> list[dict[str, Any]]:
    """Select mapping questions listed as ``question<TAB>answer`` bad cases."""
    if not bad_cases_path.exists():
        raise FileNotFoundError(f"Missing bad-cases file: {bad_cases_path}")

    questions_by_text: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for question in questions:
        questions_by_text[question["question"]].append(question)

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    missing: list[str] = []
    for line_number, line in enumerate(bad_cases_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        question_text, separator, _ragflow_answer = line.partition("\t")
        if not separator or not question_text:
            raise ValueError(f"Invalid bad-case row at {bad_cases_path}:{line_number}; expected question<TAB>RAGFlow answer")

        matches = questions_by_text.get(question_text, [])
        if not matches:
            for k in questions_by_text.keys():
                if k.find(question_text[:-2]) >= 0:
                    matches = questions_by_text.get(k)
                    break

        if not matches:
            missing.append(question_text)
            continue

        for question in matches:
            question_id = str(question["question_id"])
            if question_id not in selected_ids:
                selected_ids.add(question_id)
                selected.append(question)

    if missing:
        preview = ", ".join(repr(question) for question in missing[:3])
        suffix = "" if len(missing) <= 3 else f" ... ({len(missing)} total)"
        raise ValueError(f"Bad-case questions not found in frames mapping: {preview}{suffix}")
    if not selected:
        raise ValueError(f"Bad-cases file contains no questions: {bad_cases_path}")
    return _select_questions(
        selected,
        sample_count=sample_count,
        strategy=strategy,
        seed=seed,
    )


def _select_questions(
    questions: list[dict[str, Any]],
    *,
    sample_count: Any,
    strategy: str,
    seed: int,
) -> list[dict[str, Any]]:
    if sample_count in (None, "", 0, "all"):
        return questions
    count = int(sample_count)
    if count >= len(questions):
        return questions
    if count < 0:
        raise ValueError("question_sample_count must be null, 0, 'all', or a positive integer")
    if strategy == "first":
        return questions[:count]
    if strategy != "random":
        raise ValueError("sample_strategy must be 'random' or 'first'")
    rng = random.Random(seed)
    return rng.sample(questions, count)


def _make_client(cfg: dict[str, Any]) -> JsonHttpClient:
    api_key = cfg.get("ragflow_api_key") or os.getenv(cfg.get("ragflow_api_key_env_var", "RAGFLOW_API_KEY"), "")
    if not api_key:
        raise ValueError("ragflow_api_key is required, or set ragflow_api_key_env_var")
    backend = cfg.get("backend", {})
    retry = cfg.get("retry", {})
    return JsonHttpClient(
        base_url=_base_url(backend),
        api_key=api_key,
        timeout_seconds=int(cfg.get("timeout_seconds", 300)),
        max_retries=int(retry.get("max_retries", 2)),
        backoff_seconds=float(retry.get("backoff_seconds", 2.0)),
    )


def _base_url(backend: dict[str, Any]) -> str:
    if backend.get("base_url"):
        return str(backend["base_url"]).rstrip("/")
    scheme = backend.get("scheme", "http")
    host = backend.get("host", "127.0.0.1")
    port = backend.get("port", 80)
    return f"{scheme}://{host}:{port}"


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(content, encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def _read_jsonl_by_id(path: Path) -> dict[str, dict[str, Any]]:
    return {str(row.get("question_id")): row for row in _read_jsonl(path) if row.get("question_id") is not None}


def _remove_jsonl_ids(path: Path, question_ids: set[str]) -> None:
    if not question_ids:
        return
    rows = [row for row in _read_jsonl(path) if str(row.get("question_id")) not in question_ids]
    _write_jsonl(path, rows)


def _deduplicate_jsonl(path: Path) -> None:
    rows = _read_jsonl(path)
    latest_by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    rows_without_id: list[dict[str, Any]] = []
    for row in rows:
        question_id = row.get("question_id")
        if question_id is None:
            rows_without_id.append(row)
            continue
        question_id = str(question_id)
        if question_id not in latest_by_id:
            ordered_ids.append(question_id)
        latest_by_id[question_id] = row
    _write_jsonl(path, [latest_by_id[question_id] for question_id in ordered_ids] + rows_without_id)


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path
    return base_dir / path


def _resolve_output_dir(template: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(template.replace("<timestamp>", stamp))


def _decode_response(response: requests.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return response.json()
    try:
        return response.json()
    except ValueError:
        return response.text


def _collect_eventstream_answer(lines) -> dict[str, Any]:
    answer_parts: list[str] = []
    last_data: dict[str, Any] = {}

    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line)
        line = line.strip()
        if not line or line.startswith(":") or line.lower().startswith("event:"):
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        if not line:
            continue
        if line == "[DONE]":
            break
        if line.startswith("[MESSAGE]"):
            _append_answer_part(answer_parts, line[len("[MESSAGE]") :])
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"Invalid event-stream JSON chunk: {line[:500]}") from exc

        if isinstance(payload, dict) and payload.get("code", 0) not in (0, None):
            raise BenchmarkError(f"RAGFlow stream code {payload.get('code')}: {payload.get('message') or _compact(payload)}")

        data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
        if data is True or data == "[DONE]":
            break
        if isinstance(data, dict):
            last_data = data
            answer = data.get("answer")
            if isinstance(answer, str) and answer:
                _append_answer_part(answer_parts, answer)
            if data.get("final") is True:
                break
        elif isinstance(data, str) and data:
            _append_answer_part(answer_parts, data)

    result = dict(last_data)
    result["answer"] = "".join(answer_parts) if answer_parts else str(result.get("answer") or "")
    result["answer"] = re.sub(r"<retrieving>.*</retrieving>", "", result["answer"])
    return result


def _append_answer_part(parts: list[str], text: str) -> None:
    if not text:
        return
    current = "".join(parts)
    if current and text.startswith(current):
        parts[:] = [text]
    else:
        parts.append(text)


def _should_retry(exc: Exception) -> bool:
    # NEVER retry a read timeout. The agentic pipeline is long-running, so a
    # timeout usually means the server is STILL working on this question; retrying
    # starts a second full pipeline for the same question (the server builds fresh
    # per-request state, so its one-run-per-turn guard cannot span requests) and
    # doubles the load that caused the timeout in the first place. Raise
    # ``timeout_seconds`` instead.
    if isinstance(exc, requests.Timeout):
        return False
    if isinstance(exc, requests.RequestException):
        return True
    text = str(exc)
    return "HTTP 429" in text or "HTTP 5" in text


def _parse_jsonish(text: str) -> dict[str, Any] | None:
    candidates = [text.strip()]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.append(fenced.group(1).strip())
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_verdict(parsed: dict[str, Any]) -> str | None:
    for key in ("verdict", "result", "judgement", "judgment"):
        value = parsed.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized == "correct":
            return "correct"
        if normalized == "incorrect":
            return "incorrect"
        if normalized.startswith("partial"):
            return "partial"
    return None


def _coerce_accuracy(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        score = float(value)
    elif isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "correct", "yes"}:
            return 1.0
        if lowered in {"false", "incorrect", "no"}:
            return 0.0
        try:
            score = float(lowered.rstrip("%"))
        except ValueError:
            return None
        if value.strip().endswith("%"):
            score = score / 100.0
    else:
        return None

    if 1.0 < score <= 100.0:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def _extract_score(parsed: dict[str, Any]) -> float | None:
    for key in ("accuracy", "normalized_score", "score", "correct", "is_correct"):
        if key not in parsed:
            continue
        value = parsed[key]
        if key == "score":
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = None
            if numeric in (0.0, 2.0, 4.0):
                return numeric / 4.0
        accuracy = _coerce_accuracy(value)
        if accuracy is not None:
            return accuracy
    return None


def _extract_level(parsed: dict[str, Any]) -> int | None:
    for key in ("level", "accuracy_level", "accuracyLevel"):
        if key not in parsed:
            continue
        try:
            level = int(parsed[key])
        except (TypeError, ValueError):
            continue
        return max(0, min(5, level))
    score = parsed.get("score")
    try:
        numeric = float(score)
    except (TypeError, ValueError):
        numeric = None
    if numeric in (0.0, 2.0, 4.0):
        return int(numeric)
    verdict = _extract_verdict(parsed)
    if verdict == "correct":
        return 4
    if verdict == "partial":
        return 2
    if verdict == "incorrect":
        return 0
    return None


def _row_has_error(row: dict[str, Any]) -> bool:
    return bool(row.get("ragflow_error")) or bool(row.get("judge_error")) or _as_float(row.get("accuracy")) is None


def _answer_row_has_error(row: dict[str, Any]) -> bool:
    if row.get("ragflow_error"):
        return True
    answer = row.get("ragflow_answer")
    return not isinstance(answer, str) or not answer.strip() or answer.lstrip().startswith("**ERROR**")


def _error_label(row: dict[str, Any]) -> str:
    if row.get("ragflow_error"):
        return "ragflow_error"
    if row.get("judge_error"):
        return "judge_error"
    return "missing_accuracy"


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _average(values: list[float | None]) -> float | None:
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    return round(sum(cleaned) / len(cleaned), 6)


def _natural_key(value: str) -> tuple[int, str]:
    try:
        return (0, f"{int(value):012d}")
    except ValueError:
        return (1, value)


def _compact(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    return text[:1000]


if __name__ == "__main__":
    sys.exit(main())
