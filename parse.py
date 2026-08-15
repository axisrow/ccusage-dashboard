#!/usr/bin/env python3
"""
parse.py — быстрый сбор расхода токенов из локальных логов Claude Code, Codex и ZCode.

Зачем не ccusage: он сканирует все логи целиком (~6 ГБ) независимо от --since —
49 с за два дня, больше двух минут за полный период. Этот парсер читает всю
историю Claude за ~1.3 с и Codex за ~1.7 с, поэтому кэш не нужен: пересчёт
с нуля дешевле чтения кэша.

Ловушки, из-за которых цифры разъезжаются (все проверены на реальных данных,
подробности в README):
  1. Часы и дни считаются в ЛОКАЛЬНОЙ таймзоне. По UTC расхождение с ccusage
     было -17.5%, по локальной — +0.1%.
  2. Claude дедуплицируется по requestId: 64% id встречаются 2+ раз (стриминг),
     без дедупа завышение в 1.58 раза.
  3. Подагенты Claude лежат в отдельных файлах <session>/subagents/agent-*.jsonl —
     это 1932 из 2856 файлов, поэтому glob обязан быть рекурсивным.
  4. У Codex total_token_usage кумулятивен, суммировать нужно last_token_usage,
     но и он дублируется — дедуп по кумулятивному ключу.
  5. Модель Codex лежит в turn_context, а не в session_meta.
  6. payload.source у Codex бывает объектом (подагенты guardian/review).
  7. ZCode хранит расход не в jsonl, а в SQLite ~/.zcode/cli/db/db.sqlite, и его
     input_tokens ВКЛЮЧАЕТ обе части кэша — свежий input добывается вычитанием.
     started_at там в миллисекундах, а reasoning_tokens уже входит в output.

Только stdlib, Python 3.9+.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Iterable, NamedTuple

HOME = Path.home()
CLAUDE_GLOB = str(HOME / ".claude/projects/**/*.jsonl")
CODEX_GLOB = str(HOME / ".codex/sessions/**/rollout-*.jsonl")
# ZCode — единственный источник-БД, а не набор файлов (ловушка 7). ZCODE_HOME
# задаёт home-каталоги через запятую (так же, как в ccusage), путь к самой базе
# внутри каждого из них фиксирован.
ZCODE_DB_RELATIVE = "cli/db/db.sqlite"

TOOLS = ("claude", "codex", "zcode")

WORKERS = min(8, (os.cpu_count() or 4))


def zcode_databases() -> list[Path]:
    """Существующие базы ZCode. Пусто, если ZCode не установлен."""
    raw = os.environ.get("ZCODE_HOME", "")
    # dict.fromkeys — дедуп с сохранением порядка: один и тот же каталог, дважды
    # названный в ZCODE_HOME, иначе удвоил бы весь расход ZCode.
    named = dict.fromkeys(p.strip() for p in raw.split(",") if p.strip())
    homes = [Path(p).expanduser() for p in named] or [HOME / ".zcode"]
    return [db for home in homes if (db := home / ZCODE_DB_RELATIVE).is_file()]


class Row(NamedTuple):
    """Одна запись расхода, уже приведённая к локальному времени."""

    hour: str  # "YYYY-MM-DDTHH" в локальной TZ
    date: str  # "YYYY-MM-DD" в локальной TZ
    tool: str  # claude | codex | zcode
    model: str
    agent: str  # main / имя подагента
    project: str  # абсолютный cwd либо "unknown"
    session: str  # каталог сессии (идентичность уникальна в рамках инструмента)
    input: int
    output: int
    cache_create: int
    cache_read: int
    cache_create_1h: int = 0  # часть cache_create, записанная с TTL 1h — она дороже

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_create + self.cache_read


def _parts_of(dt: datetime) -> tuple[str, str]:
    """Локальное время -> (час, дата) в формате Row. Единственное место с этими
    форматами: разъедься они между инструментами — записи перестанут склеиваться
    в один часовой бакет, и это не упадёт, а тихо исказит цифры."""
    return dt.strftime("%Y-%m-%dT%H"), dt.strftime("%Y-%m-%d")


def _local_parts(ts: str) -> tuple[str, str] | None:
    """ISO-строка с Z -> (час, дата) в локальной таймзоне. Ловушка 1."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    return _parts_of(dt)


def _session_id(path: str, marker: str, offset: int) -> str:
    """Идентификатор сессии из пути лога — компонент, отстоящий на `offset` от маркера.

    Claude:  основные файлы ~/.claude/projects/<проект>/<sessionId>.jsonl (плоские),
             сабагенты  <проект>/<sessionId>/subagents/agent-*.jsonl -> "projects", offset 2
    Основной Claude-файл приносит сессию с суффиксом «.jsonl», а сабагент — без,
    поэтому срезаем расширение, чтобы оба отображались на одну сессию.
    Codex не использует этот хелпер: его сессия — сам rollout-файл (см. parse_codex)."""
    parts = path.split(os.sep)
    # Якорь — каталог ".claude": "projects" идёт сразу после него, а не первый
    # "projects" в пути. Иначе домашний каталог, сам названный projects
    # (например /Users/projects/axisrow/...), сдвигал бы индекс и схлопывал все
    # Claude-сессии в одно значение ".claude".
    if ".claude" in parts:
        i = parts.index(".claude") + 1
        if i < len(parts) and parts[i] == marker:
            j = i + offset
            if j < len(parts):
                return os.path.splitext(parts[j])[0]
    return "unknown"


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #


def parse_claude(path: str) -> list[Row]:
    """
    Воркер для одного .jsonl Claude Code.

    Дедуп по requestId делается ПОФАЙЛОВО и это корректно: requestId никогда не
    пересекается между файлами (проверено на 11623 записях). Дубли всегда несут
    одинаковые значения, поэтому берём максимум — ранние стрим-записи бывают
    частичными.
    """
    best: dict[str, tuple[int, Row]] = {}
    # сессия одна на весь файл — вычислить до цикла, а не на каждую строку
    session = _session_id(path, "projects", 2)
    try:
        fh = open(path, errors="ignore")
    except OSError:
        return []

    with fh:
        for line in fh:
            # дешёвый префильтр до json.loads — ради него весь парсинг и укладывается в секунды
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue

            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue

            parts = _local_parts(rec.get("timestamp") or "")
            if parts is None:
                continue
            hour, date = parts

            inp = usage.get("input_tokens", 0) or 0
            out = usage.get("output_tokens", 0) or 0
            cc = usage.get("cache_creation_input_tokens", 0) or 0
            cr = usage.get("cache_read_input_tokens", 0) or 0
            total = inp + out + cc + cr
            if total == 0:
                continue

            # запись кэша с TTL 1h тарифицируется дороже 5m (у sonnet-5 $4.00 против
            # $2.50 за Mtok) — без этого расчёт стоимости занижается примерно на 9%
            detail = usage.get("cache_creation")
            cc_1h = (detail or {}).get("ephemeral_1h_input_tokens", 0) or 0

            # ловушка 3: attributionAgent есть только в subagents-файлах
            agent = rec.get("attributionAgent")
            if not agent:
                agent = "sidechain" if rec.get("isSidechain") else "main"

            row = Row(
                hour=hour,
                date=date,
                tool="claude",
                model=msg.get("model") or "unknown",
                agent=agent,
                project=rec.get("cwd") or "unknown",
                session=session,
                input=inp,
                output=out,
                cache_create=cc,
                cache_read=cr,
                cache_create_1h=cc_1h,
            )

            key = rec.get("requestId") or msg.get("id") or rec.get("uuid") or ""
            prev = best.get(key)
            if prev is None or total > prev[0]:
                best[key] = (total, row)

    return [row for _, row in best.values()]


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #


def _codex_agent(source: object, originator: str | None) -> str:
    """
    payload.source бывает строкой ("cli"/"vscode"/"exec") либо объектом для
    подагентов. Логика повторяет ccusage-scripts/codex_breakdown.py. Ловушка 6.
    """
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, str):
            return f"subagent:{sub}"
        if isinstance(sub, dict):
            if "other" in sub:
                return f"subagent:{sub['other']}"
            spawn = sub.get("thread_spawn")
            if isinstance(spawn, dict):
                p = spawn.get("agent_path") or spawn.get("agent_nickname")
                return f"subagent:{os.path.basename(p) if p else '?'}"
        return "subagent:?"
    if isinstance(source, str) and source:
        return source
    return originator or "unknown"


def parse_codex(path: str) -> list[Row]:
    """
    Воркер для одного rollout-*.jsonl.

    total_token_usage кумулятивен, поэтому складываем дельты last_token_usage.
    Но и они дублируются — последняя запись повторяется, — поэтому дедуп идёт по
    кумулятивному ключу: он монотонно растёт и уникален для каждого turn.
    Ловушка 4.
    """
    rows: list[Row] = []
    seen: set[tuple] = set()
    # сессия одна на весь файл — вычислить до цикла, а не на каждую строку.
    # Codex-сессия — это сам rollout-файл: layout ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl,
    # поэтому идентичность берём из имени файла (в нём UUID), а не из каталога (там год).
    session = os.path.splitext(os.path.basename(path))[0]
    cwd = "unknown"
    source: object = None
    originator: str | None = None
    model: str | None = None

    try:
        fh = open(path, errors="ignore")
    except OSError:
        return []

    with fh:
        for line in fh:
            # session_meta — всегда первая строка; base_instructions в ней огромный,
            # но json.loads одной строки дешевле, чем городить ручной разбор
            if '"session_meta"' in line and cwd == "unknown":
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "session_meta":
                    p = rec.get("payload") or {}
                    cwd = p.get("cwd") or "unknown"
                    source = p.get("source")
                    originator = p.get("originator")
                continue

            # ловушка 5: модель только здесь, в session_meta её нет
            if model is None and '"turn_context"' in line:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "turn_context":
                    model = (rec.get("payload") or {}).get("model")
                continue

            if '"token_count"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue

            payload = rec.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            total_usage = info.get("total_token_usage") or {}
            if not last:
                continue

            key = (
                total_usage.get("input_tokens"),
                total_usage.get("output_tokens"),
                total_usage.get("total_tokens"),
            )
            if key in seen:
                continue
            seen.add(key)

            parts = _local_parts(rec.get("timestamp") or "")
            if parts is None:
                continue
            hour, date = parts

            inp = last.get("input_tokens", 0) or 0
            cached = last.get("cached_input_tokens", 0) or 0
            out = last.get("output_tokens", 0) or 0
            if inp + out == 0:
                continue

            # input_tokens у Codex ВКЛЮЧАЕТ cached_input_tokens — раскладываем,
            # чтобы не посчитать кэш дважды при суммировании компонентов
            rows.append(
                Row(
                    hour=hour,
                    date=date,
                    tool="codex",
                    model=model or "unknown",
                    agent=_codex_agent(source, originator),
                    project=cwd,
                    session=session,
                    input=max(inp - cached, 0),
                    output=out,
                    cache_create=last.get("cache_write_input_tokens", 0) or 0,
                    cache_read=cached,
                )
            )

    return rows


# --------------------------------------------------------------------------- #
# ZCode
# --------------------------------------------------------------------------- #

# status: строки running/error/cancelled — это незавершённые или неоплаченные
# запросы, ccusage их тоже отбрасывает.
ZCODE_SQL = """
SELECT mu.model_id, mu.started_at, mu.agent, mu.session_id,
       mu.input_tokens, mu.output_tokens,
       mu.cache_creation_input_tokens, mu.cache_read_input_tokens,
       s.directory
FROM model_usage AS mu
LEFT JOIN session AS s ON s.id = mu.session_id
WHERE mu.status = 'completed'
"""


def parse_zcode(db_path: str) -> list[Row]:
    """
    Воркер для одной базы ZCode (ловушка 7).

    Дедуп не нужен, в отличие от Claude и Codex: model_usage.id — первичный ключ,
    одна строка = один запрос к модели, счётчики инкрементальны. Ретраи легли бы
    отдельными строками с attempt_index > 0, но ccusage их тоже не схлопывает.
    """
    try:
        # Строго read-only: база живёт в WAL-режиме под работающим ZCode,
        # дашборд не должен её трогать на запись.
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"  zcode {db_path}: база недоступна ({exc})", file=sys.stderr)
        return []

    rows: list[Row] = []
    try:
        for model, started, agent, session, inp, out, cc, cr, directory in con.execute(ZCODE_SQL):
            if not started:
                continue
            inp, out = inp or 0, out or 0
            cc, cr = cc or 0, cr or 0
            # input_tokens — надмножество: включает и cache-read, и cache-write
            fresh = max(inp - cr - cc, 0)
            if fresh + out + cc + cr == 0:
                continue

            # started_at в миллисекундах, дальше — та же локальная TZ (ловушка 1)
            hour, date = _parts_of(datetime.fromtimestamp(started / 1000).astimezone())
            rows.append(
                Row(
                    hour=hour,
                    date=date,
                    tool="zcode",
                    model=(model or "unknown").strip(),
                    agent=agent or "main",
                    project=directory or "unknown",
                    session=session or "unknown",
                    input=fresh,
                    output=out,
                    cache_create=cc,
                    cache_read=cr,
                )
            )
    except sqlite3.Error as exc:
        # битую базу отдаём тем, что успели прочитать, а не роняем весь сбор —
        # но молчать об этом нельзя, иначе усечённый результат неотличим от
        # честного отсутствия расхода ZCode.
        print(f"  zcode {db_path}: чтение прервано ({exc}), данные неполные", file=sys.stderr)
    finally:
        con.close()

    return rows


# --------------------------------------------------------------------------- #
# Сбор
# --------------------------------------------------------------------------- #


def collect(
    since: str | None = None,
    until: str | None = None,
    tools: Iterable[str] = TOOLS,
) -> list[Row]:
    """Собрать все записи за период. since/until — 'YYYY-MM-DD', границы включительно."""
    tools = set(tools)
    jobs: list[tuple] = []
    if "claude" in tools:
        jobs.append((parse_claude, glob.glob(CLAUDE_GLOB, recursive=True)))
    if "codex" in tools:
        jobs.append((parse_codex, glob.glob(CODEX_GLOB, recursive=True)))

    rows: list[Row] = []
    # ZCode мимо пула: это одна база на инструмент, распараллеливать нечего,
    # а sqlite-соединение через ProcessPoolExecutor не переживёт пикла.
    if "zcode" in tools:
        for db in zcode_databases():
            rows.extend(parse_zcode(str(db)))
    before_pool = len(rows)  # граница, за которой начинается вклад пула

    try:
        with ProcessPoolExecutor(max_workers=WORKERS) as pool:
            for fn, files in jobs:
                if not files:
                    continue
                for chunk in pool.map(fn, files, chunksize=20):
                    rows.extend(chunk)
    except RuntimeError:
        # collect() вызвали при импорте модуля (вне if __name__ == "__main__"):
        # на spawn-платформах пул стартовать нельзя. Считаем последовательно —
        # медленнее в разы, но результат идентичен. Пул мог успеть отдать часть
        # чанков — отбрасываем ровно их, по границе, а не по признаку инструмента:
        # иначе следующий не-пуловый источник тихо потеряется на этом пути.
        del rows[before_pool:]
        for fn, files in jobs:
            for path in files:
                rows.extend(fn(path))

    if since:
        rows = [r for r in rows if r.date >= since]
    if until:
        rows = [r for r in rows if r.date <= until]
    return rows


def normalize_date(value: str | None) -> str | None:
    """Принять и 20260801, и 2026-08-01."""
    if not value:
        return None
    v = value.strip()
    if len(v) == 8 and v.isdigit():
        return f"{v[:4]}-{v[4:6]}-{v[6:]}"
    return v


def _stats(rows: list[Row], elapsed: float) -> None:
    if not rows:
        print("Записей не найдено.")
        return

    print(f"записей: {len(rows):,}   время: {elapsed:.1f} с")
    print(f"токенов: {sum(r.total for r in rows):,}")
    print(f"период:  {min(r.date for r in rows)} .. {max(r.date for r in rows)}")

    for title, key in (
        ("инструмент", lambda r: r.tool),
        ("модель", lambda r: r.model),
        ("агент", lambda r: r.agent),
        ("проект", lambda r: os.path.basename(r.project.rstrip("/")) or r.project),
    ):
        counter: Counter[str] = Counter()
        for r in rows:
            counter[key(r)] += r.total
        print(f"\nпо {title}:")
        for name, value in counter.most_common(8):
            print(f"  {name[:44]:<44} {value:>18,}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Сбор расхода токенов из логов Claude Code, Codex и ZCode")
    ap.add_argument("--since", help="дата начала, YYYY-MM-DD или YYYYMMDD")
    ap.add_argument("--until", help="дата конца, включительно")
    ap.add_argument("--tool", choices=list(TOOLS), action="append", help="ограничить инструментом")
    ap.add_argument("--stats", action="store_true", help="показать сводку вместо JSON")
    ap.add_argument("--json", action="store_true", help="выгрузить записи в JSON")
    args = ap.parse_args()

    started = time.time()
    rows = collect(
        since=normalize_date(args.since),
        until=normalize_date(args.until),
        tools=args.tool or TOOLS,
    )
    elapsed = time.time() - started

    if args.json:
        print(json.dumps([r._asdict() for r in rows], ensure_ascii=False))
    else:
        _stats(rows, elapsed)


if __name__ == "__main__":
    main()
