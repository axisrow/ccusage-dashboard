#!/usr/bin/env python3
"""
verify.py — сверка своего парсера с ccusage.

Дашборд считает токены сам, потому что ccusage сканирует все логи целиком и на
этом объёме занимает минуты. Цена такого решения — риск незаметно разойтись с
эталоном, поэтому расхождение проверяется отдельной командой.

Запускается вручную и работает медленно (столько же, сколько ccusage), в
генерацию дашборда не входит. Эталон при разработке: +0.1% по Claude за 2 дня.

Заодно печатает стоимость в долларах: прайсинг здесь не хардкодится (в
ccusage-scripts/archive он уже устарел), $ берётся из ccusage как есть.

Использование:
    ./verify.py --since 20260802 --until 20260803
"""

from __future__ import annotations

import argparse

from parse import TOOLS, collect, normalize_date
from pricing import load as load_rates, run_ccusage

COMPONENTS = (
    ("input", "inputTokens"),
    ("output", "outputTokens"),
    ("cache_create", "cacheCreationTokens"),
    ("cache_read", "cacheReadTokens"),
)

# Для codex запрашивается --speed standard. По умолчанию ccusage работает в
# режиме auto и удваивает цену turn-ов, которые счёл «быстрыми», но признака
# speed в rollout-логах нет — воспроизвести это решение локально нечем.
# Сравнение со standard делает сверку честной: на 03.08 расхождение 0.1%
# ($20.32 против $20.35), тогда как против auto было бы -9%.
CODEX_ARGS = ("--speed", "standard")

# ZCode не хранит стоимость в своей базе, поэтому ccusage считает её из прайса.
# --offline берёт встроенную цену GLM-5.2 вместо похода в LiteLLM: сверка не
# должна зависеть от сети и от того, обновился ли там прайс.
ZCODE_ARGS = ("--offline",)

TOOL_ARGS = {"codex": CODEX_ARGS, "zcode": ZCODE_ARGS}


def mine(tool: str, since: str, until: str) -> dict:
    rows = collect(since=since, until=until, tools=(tool,))
    rates = load_rates()
    resolved = {m: rates.get(m) for m in {r.model for r in rows}}
    out = {name: 0 for name, _ in COMPONENTS}
    out["_cost"] = 0.0
    out["_unpriced"] = 0
    for r in rows:
        out["input"] += r.input
        out["output"] += r.output
        out["cache_create"] += r.cache_create
        out["cache_read"] += r.cache_read
        rate = resolved[r.model]
        if rate is None:
            out["_unpriced"] += r.total
        else:
            out["_cost"] += rate.cost(
                r.input, r.output, r.cache_create, r.cache_read, r.cache_create_1h
            )
    out["_records"] = len(rows)
    return out


def theirs(data: dict) -> dict[str, int]:
    totals = data.get("totals") or {}
    return {name: int(totals.get(key, 0) or 0) for name, key in COMPONENTS}


def compare(tool: str, since: str, until: str, threshold: float) -> bool:
    print(f"\n{'=' * 66}\n{tool.upper()}  {since} .. {until}\n{'=' * 66}")

    my = mine(tool, since, until)
    data = run_ccusage(tool, since, until, TOOL_ARGS.get(tool, ()))
    if data is None:
        print("  ccusage недоступен — сверка пропущена.")
        print(f"  мой парсер: {sum(v for k, v in my.items() if not k.startswith('_')):,} токенов")
        return True

    cc = theirs(data)
    print(f"{'компонент':<16} {'мой парсер':>17} {'ccusage':>17} {'расхождение':>13}")
    print("-" * 66)

    for name, _ in COMPONENTS:
        a, b = my[name], cc[name]
        delta = (a - b) / b * 100 if b else (0.0 if a == 0 else 100.0)
        print(f"{name:<16} {a:>17,} {b:>17,} {delta:>12.2f}%")

    a = sum(my[n] for n, _ in COMPONENTS)
    b = sum(cc[n] for n, _ in COMPONENTS)
    delta = (a - b) / b * 100 if b else (0.0 if a == 0 else 100.0)
    print("-" * 66)
    print(f"{'ИТОГО':<16} {a:>17,} {b:>17,} {delta:>12.2f}%")

    ok = abs(delta) <= threshold
    if not ok:
        print(f"\n  РАСХОЖДЕНИЕ выше порога {threshold}%.")
        print("  Частая причина — граница суток: ccusage группирует по локальной дате.")
    else:
        print(f"\n  В пределах порога {threshold}%.")

    # У claude поле называется totalCost, у codex — costUSD
    totals = data.get("totals") or {}
    cost = totals.get("totalCost") or totals.get("costUSD") or 0.0
    if cost:
        my_cost = my["_cost"]
        cd = (my_cost - cost) / cost * 100 if cost else 0.0
        print(f"\n{'стоимость':<16} {my_cost:>16.2f}$ {cost:>16.2f}$ {cd:>12.2f}%")
        if my["_unpriced"]:
            print(f"  без прайсинга: {my['_unpriced']:,} токенов не учтены в моей сумме")
        if abs(cd) > threshold:
            if my["_unpriced"] and my_cost < cost:
                # Недобор объясняется тем, что часть токенов посчитать нечем —
                # это заявленный пробел, а не ошибка расчёта.
                implied = (cost - my_cost) / my["_unpriced"] * 1e6
                print(
                    f"  Недобор ${cost - my_cost:.2f} приходится на непрайсованные токены "
                    f"(эквивалент ${implied:.2f}/Mtok)."
                )
            else:
                ok = False
                print("  Стоимость расходится больше порога — обновите ставки: ./pricing.py --refresh")

    print(f"  Записей у меня: {my['_records']:,}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description="Сверка парсера с ccusage")
    ap.add_argument("--since", required=True, help="дата начала, YYYY-MM-DD или YYYYMMDD")
    ap.add_argument("--until", required=True, help="дата конца, включительно")
    ap.add_argument("--tool", choices=list(TOOLS), action="append")
    ap.add_argument("--threshold", type=float, default=1.0, help="порог расхождения в %% (по умолчанию 1)")
    args = ap.parse_args()

    since = normalize_date(args.since)
    until = normalize_date(args.until)
    print("ccusage сканирует все логи целиком — это займёт минуту-другую.")

    all_ok = True
    for tool in args.tool or TOOLS:
        if not compare(tool, since, until, args.threshold):
            all_ok = False

    print()
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
