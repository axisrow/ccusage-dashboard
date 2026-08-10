#!/usr/bin/env python3
"""
pricing.py — ставки моделей в долларах за токен.

Токены плохо отражают деньги: у claude-sonnet-5 чтение кэша стоит $0.20 за Mtok,
а вывод — $10.00, разница в 50 раз. Поэтому дашборд считает стоимость.

Ставки берутся из четырёх источников, в порядке доверия (всё кэшируется в rates.json):

1. LiteLLM (тот же прайсинг, что использует сам ccusage). Учитывается TTL кэша:
   запись с ttl=1h дороже 5m — у sonnet-5 $4.00 против $2.50. Без этого расчёт
   занижался примерно на 9%. Если имени в логах нет в LiteLLM под точным ключом,
   пробуем каноническое имя по таблице алиасов ALIASES (glm-5.2:cloud → glm-5.2).
2. Фоллбэк-список FALLBACK — цены для моделей, которых нет в LiteLLM под именами из
   PREFIXES (glm-5.2 под облачным провайдером, deepseek-v4-flash:0731). Детерминированная
   цена надёжнее подгонки, поэтому идёт перед ней.
3. Подгонка из ccusage методом наименьших квадратов — для моделей, которых нет в
   LiteLLM под этим именем (glm-5-turbo, glm-4.7 через прокси). У них простая
   тарификация без TTL, подгонка сходится с невязкой около 0%.
   Подгонка НЕ применяется к моделям Anthropic: там TTL-кэш и мало уравнений,
   на них МНК даёт абсурдные ставки (в тестах — $28539 за Mtok input).
4. cost = 0, если ccusage говорит, что модель за период не стоила ничего:
   это подписка (Max-план) или бесплатная модель. На проверенных данных так
   тарифицируются claude-opus-5, glm-5.2, deepseek — вместе это 460+ млн токенов,
   которые по API-ставкам дали бы фиктивные сотни долларов.

Модель, для которой ставку получить не удалось, НЕ считается бесплатной: её расход
попадает в отдельный счётчик «без прайсинга», который дашборд показывает явно.

Обновление кэша:
    ./pricing.py --refresh          # перечитать LiteLLM и переподогнать из ccusage
    ./pricing.py --show             # показать текущие ставки
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from itertools import combinations
from pathlib import Path

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE = Path(__file__).with_name("rates.json")

# Порядок компонентов везде одинаков: input, output, cache_create, cache_read
CCUSAGE_KEYS = ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens")
LITELLM_KEYS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_creation_input_token_cost",
    "cache_read_input_token_cost",
)
# Префиксы провайдеров: LiteLLM хранит часть моделей как "zai/glm-4.7".
# Порядок важен — берём первое совпадение, поэтому сначала точное имя.
PREFIXES = ("", "anthropic/", "openai/", "chatgpt/", "zai/", "deepseek/")

# Модели этих семейств подгонять из ccusage нельзя (см. док-строку модуля)
NO_FIT = ("claude-", "anthropic/")

# Стабильные алиасы/варианты моделей: имя в логах -> каноническое (тарифицируемое) имя.
# Один тариф размазан по нескольким строкам имени, а from_litellm ищет только точное
# совпадение. Явная таблица безопаснее эвристического среза ":suffix": не склеивает
# реально разные модели (gpt-5.3-codex vs gpt-5.3-codex-spark). Куда сведено имя —
# туда, где есть достоверная ставка:
#   - glm-5.2:cloud — вариант того же GLM-5.2 (в LiteLLM база только под
#     cloudflare/@cf/zai-org/glm-5.2, недостижима из PREFIXES) -> ставка из FALLBACK;
#   - deepseek-v4-flash:cloud — стабильный алиас к датированному снимку :0731,
#     который, в свою очередь, тот же deepseek-v4-flash. База есть в LiteLLM
#     (in 0.14 / out 0.28 $ за Mtok) -> сводим ВСЕ варианты к голому имени, чтобы
#     они взяли эту авторитетную ставку, а не вырожденную подгонку (out=0).
ALIASES = {
    "glm-5.2:cloud": "glm-5.2",
    "deepseek-v4-flash:cloud": "deepseek-v4-flash",
    "deepseek-v4-flash:0731-cloud": "deepseek-v4-flash",
    "deepseek-v4-flash:0731": "deepseek-v4-flash",
}


def canonical_name(name: str) -> str:
    """Свести имя модели из логов к каноническому имени, у которого есть ставка."""
    return ALIASES.get(name, name)


class Rates:
    """Ставки одной модели: 4 компонента + отдельная ставка записи кэша с TTL 1h."""

    __slots__ = ("input", "output", "cache_create", "cache_read", "cache_create_1h", "source")

    def __init__(self, values, cache_create_1h=None, source="litellm"):
        self.input, self.output, self.cache_create, self.cache_read = values
        self.cache_create_1h = cache_create_1h if cache_create_1h else self.cache_create
        self.source = source

    def cost(self, inp: int, out: int, cc: int, cr: int, cc_1h: int = 0) -> float:
        """Стоимость записи. cc_1h — часть cc, записанная с TTL 1h (тарифицируется дороже)."""
        cc_5m = max(cc - cc_1h, 0)
        return (
            inp * self.input
            + out * self.output
            + cc_5m * self.cache_create
            + cc_1h * self.cache_create_1h
            + cr * self.cache_read
        )

    def as_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cache_create": self.cache_create,
            "cache_read": self.cache_read,
            "cache_create_1h": self.cache_create_1h,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Rates":
        r = cls(
            (d["input"], d["output"], d["cache_create"], d["cache_read"]),
            d.get("cache_create_1h"),
            d.get("source", "cache"),
        )
        return r


# --------------------------------------------------------------------------- #
# Источник 1: LiteLLM
# --------------------------------------------------------------------------- #


def fetch_litellm(timeout: int = 240) -> dict:
    try:
        with urllib.request.urlopen(LITELLM_URL, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"  LiteLLM недоступен ({exc}) — используются только подогнанные ставки.",
              file=sys.stderr)
        return {}


def from_litellm(model: str, prices: dict) -> Rates | None:
    for prefix in PREFIXES:
        entry = prices.get(prefix + model)
        if not entry or entry.get("input_cost_per_token") is None:
            continue
        values = [entry.get(k) or 0.0 for k in LITELLM_KEYS]
        return Rates(
            values,
            entry.get("cache_creation_input_token_cost_above_1hr"),
            "litellm" if prefix == "" else f"litellm:{prefix}{model}",
        )
    return None


def run_ccusage(tool: str, since: str, until: str, extra_args: tuple = ()) -> dict | None:
    """
    ccusage <tool> daily --json. Возвращает None, если инструмент недоступен.

    Если ccusage не установлен глобально, пробуем bunx — так его обычно и держат.
    """
    cmd = ["ccusage", tool, "daily", "--json", "--since", since, "--until", until, *extra_args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
    except FileNotFoundError:
        cmd[0:1] = ["bunx", "ccusage@latest"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=900)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
    except subprocess.TimeoutExpired:
        print(f"  ccusage {tool}: превышен таймаут", file=sys.stderr)
        return None

    if proc.returncode != 0:
        print(f"  ccusage {tool} завершился с кодом {proc.returncode}", file=sys.stderr)
        if proc.stderr.strip():
            print("  " + proc.stderr.strip().splitlines()[0], file=sys.stderr)
        return None

    # bunx печатает строки резолвинга зависимостей перед JSON
    brace = proc.stdout.find("{")
    if brace == -1:
        return None
    try:
        return json.loads(proc.stdout[brace:])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Источник 2: фоллбэк-список цен
# --------------------------------------------------------------------------- #


# Цены для моделей, которых нет в LiteLLM под именами из PREFIXES. Ключ —
# каноническое имя (из ALIASES). Значения — $/токен, 4 компонента:
# (input, output, cache_create, cache_read). Цифры взяты из LiteLLM
# (cloudflare/@cf/zai-org/glm-5.2: input 1.4e-6, output 4.4e-6, cache_read 2.6e-7).
FALLBACK = {
    "glm-5.2": Rates((1.4e-6, 4.4e-6, 0.0, 2.6e-7), source="fallback"),
}


# --------------------------------------------------------------------------- #
# Источник 3: подгонка из ccusage
# --------------------------------------------------------------------------- #


def _lstsq(rows, idx) -> list[float] | None:
    """МНК по подмножеству компонентов через нормальные уравнения и метод Гаусса."""
    n = len(idx)
    ata = [[0.0] * n for _ in range(n)]
    atb = [0.0] * n
    for feats, cost in rows:
        f = [feats[i] for i in idx]
        for i in range(n):
            atb[i] += f[i] * cost
            for j in range(n):
                ata[i][j] += f[i] * f[j]
    m = [ata[i][:] + [atb[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-9:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= factor * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_rates(rows, max_err: float = 1.0) -> tuple[list[float], float] | None:
    """
    Подобрать ставки по наблюдениям [(4 компонента, cost)].

    Перебираем подмножества компонентов от полного к меньшим: у части моделей
    какой-то компонент всегда нулевой, и полная система вырождается. Отрицательные
    ставки отбрасываем — они означают переподгонку, а не реальный тариф.
    """
    total = sum(c for _, c in rows)
    if total <= 0:
        return None
    present = [i for i in range(4) if any(f[i] for f, _ in rows)]
    best: tuple[list[float], float] | None = None

    for size in range(len(present), 0, -1):
        for idx in combinations(present, size):
            x = _lstsq(rows, idx)
            if x is None or any(v < -1e-12 for v in x):
                continue
            full = [0.0] * 4
            for pos, i in enumerate(idx):
                full[i] = max(x[pos], 0.0)
            err = (
                sum(abs(sum(f[i] * full[i] for i in range(4)) - c) for f, c in rows)
                / total
                * 100
            )
            if best is None or err < best[1]:
                best = (full, err)
        if best and best[1] < 0.5:
            break

    if best is None or best[1] > max_err:
        return None
    return best


def ccusage_breakdowns(since: str, until: str) -> dict[str, list]:
    """Собрать по каждой модели наблюдения (компоненты токенов, cost) из ccusage."""
    agg: dict[str, list] = {}
    for tool in ("claude", "codex"):
        # см. пояснение про fast-множитель в run_ccusage
        extra_args = ("--speed", "standard") if tool == "codex" else ()
        data = run_ccusage(tool, since, until, extra_args)
        if data is None:
            continue
        for day in data.get("daily", []):
            # У codex modelBreakdowns == null: ccusage не раскладывает его стоимость
            # по моделям, поэтому наблюдений для подгонки оттуда не получить —
            # такие модели тарифицируются напрямую по LiteLLM.
            for mb in day.get("modelBreakdowns") or []:
                feats = tuple(mb.get(k, 0) or 0 for k in CCUSAGE_KEYS)
                if sum(feats):
                    agg.setdefault(mb["modelName"], []).append((feats, mb.get("cost", 0.0) or 0.0))
    return agg


def models_seen(rows) -> set[str]:
    """Имена моделей из собственных записей — включая те, которых нет в разбивках ccusage."""
    return {r.model for r in rows}


# --------------------------------------------------------------------------- #
# Сборка таблицы ставок
# --------------------------------------------------------------------------- #


def build(
    since: str, until: str, verbose: bool = True, extra_models: set[str] | None = None
) -> dict[str, Rates]:
    if verbose:
        print("Загружаю прайсинг LiteLLM…")
    prices = fetch_litellm()

    if verbose:
        print("Собираю разбивки ccusage (это медленно — он читает все логи)…")
    observed = ccusage_breakdowns(since, until)

    # Модели, которых нет в разбивках ccusage (у codex modelBreakdowns == null),
    # всё равно нужно оценить — для них есть только LiteLLM.
    for model in extra_models or ():
        observed.setdefault(model, [])

    table: dict[str, Rates] = {}
    for model, rows in observed.items():
        total_cost = sum(c for _, c in rows)
        # Пустой список наблюдений (модель известна только нам) — это НЕ признак
        # бесплатности: судить о подписке можно лишь когда ccusage реально
        # отчитался о нулевой стоимости при ненулевых токенах.
        zero_cost = bool(rows) and total_cost == 0

        canonical = canonical_name(model)
        # 1. LiteLLM — основной источник, он же приоритетнее нулей от ccusage.
        # Нулевой cost НЕ означает бесплатность: ccusage отдаёт 0, пока не знает
        # цену новой модели. На проверке claude-opus-5 показывал $0, а после
        # обновления прайсинга — $790 за три дня. Поэтому если цена в LiteLLM
        # есть, она и используется. Ищем по каноническому имени: для голых моделей
        # оно равно имени в логах, для вариантов/алиасов (glm-5.2:cloud,
        # deepseek-v4-flash:cloud) сводится к базе через ALIASES.
        rates = from_litellm(canonical, prices)
        if rates is not None:
            table[model] = rates
            continue

        # 2. Фоллбэк-список FALLBACK — модели, которых нет в LiteLLM под PREFIXES
        # (glm-5.2 под облачным провайдером). Детерминированная цена перед подгонкой.
        if canonical in FALLBACK:
            table[model] = FALLBACK[canonical]
            continue

        # 3. Цены в LiteLLM нет, и ccusage стабильно (не менее трёх дней) отдаёт
        # ноль — вот это уже похоже на подписку или бесплатную модель. Одного
        # нулевого дня недостаточно: так выглядит и просто отсутствие прайсинга.
        if zero_cost and len(rows) >= 3:
            table[model] = Rates([0.0] * 4, 0.0, "subscription")
            continue
        if zero_cost:
            continue  # мало данных, чтобы решить — честнее оставить без прайсинга
        if not rows:
            continue  # нет ни цены, ни наблюдений — честно оставляем без прайсинга

        # 4. подгонка — только для не-Anthropic моделей
        if any(model.startswith(p) for p in NO_FIT):
            continue
        fitted = fit_rates(rows)
        if fitted is not None:
            values, err = fitted
            table[model] = Rates(values, None, f"fitted:{err:.2f}%")

    return table


def save(table: dict[str, Rates]) -> None:
    CACHE.write_text(
        json.dumps({m: r.as_dict() for m, r in table.items()}, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )


def load() -> dict[str, Rates]:
    if not CACHE.exists():
        return {}
    try:
        raw = json.loads(CACHE.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return {m: Rates.from_dict(d) for m, d in raw.items()}


def describe(table: dict[str, Rates]) -> None:
    if not table:
        print("Ставок нет. Запустите: ./pricing.py --refresh")
        return
    print(f"{'модель':<30} {'in':>8} {'out':>8} {'cc':>8} {'cc 1h':>8} {'cr':>8}  источник")
    print("-" * 92)
    for model, r in sorted(table.items()):
        print(
            f"{model:<30} {r.input * 1e6:>8.2f} {r.output * 1e6:>8.2f} "
            f"{r.cache_create * 1e6:>8.2f} {r.cache_create_1h * 1e6:>8.2f} "
            f"{r.cache_read * 1e6:>8.2f}  {r.source}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Ставки моделей для расчёта стоимости")
    ap.add_argument("--refresh", action="store_true", help="перечитать LiteLLM и ccusage")
    ap.add_argument("--show", action="store_true", help="показать текущие ставки")
    ap.add_argument("--since", default="20260701", help="период для подгонки из ccusage")
    ap.add_argument("--until", default="20260803")
    args = ap.parse_args()

    if args.refresh:
        from parse import collect

        # Модели собираем по ВСЕЙ истории, а не только за период подгонки: иначе
        # редкая модель, не попавшая в этот период, останется без ставки, хотя
        # цена для неё в LiteLLM есть.
        seen = models_seen(collect())
        table = build(args.since, args.until, extra_models=seen)
        save(table)
        print(f"\nСохранено {len(table)} моделей в {CACHE.name}\n")
        describe(table)
    else:
        describe(load())


if __name__ == "__main__":
    main()
