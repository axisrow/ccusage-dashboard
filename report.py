#!/usr/bin/env python3
"""
report.py — почасовой дашборд расхода токенов, один self-contained HTML.

Читает записи через parse.py, раскладывает их по часам локального времени и
рендерит: столбики по часам с накоплением по выбранному измерению, линию среднего
и сводную таблицу.

Среднее считается по АКТИВНЫМ часам (где расход ненулевой), а не делением на все
календарные часы периода — иначе ночные простои размажут метрику в ноль. В шапке
показываются оба числа, чтобы разница была видна.

Цвета — валидированная категориальная палитра (проверена скриптом
validate_palette.js в обеих темах: adjacent CVD ΔE 9.1 light / 8.4 dark).
Слотов восемь, девятая серия не изобретается, а сворачивается в «Прочее».

Только stdlib, Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from parse import Row, collect, normalize_date
from pricing import load as load_rates
from pricing import split_cache_create

# Порядок компонентов расхода — единственный источник истины для build_raw
# (Python) и COMP_KEYS/COMP_COST_KEYS в TEMPLATE (JS); менять только вместе.
COMPONENT_KEYS = ("in", "out", "cc5m", "cc1h", "cr")


def cost_key(component: str) -> str:
    """Имя денежного ключа в payload["raw"] для компонента ("in" -> "costIn")."""
    return f"cost{component[0].upper()}{component[1:]}"

# Валидированная палитра: слот -> (light, dark). Порядок фиксирован и не тасуется —
# именно он обеспечивает CVD-разделение соседних серий.
PALETTE = [
    ("#2a78d6", "#3987e5"),  # blue
    ("#eb6834", "#d95926"),  # orange
    ("#1baf7a", "#199e70"),  # aqua
    ("#eda100", "#c98500"),  # yellow
    ("#e87ba4", "#d55181"),  # magenta
    ("#008300", "#008300"),  # green
    ("#4a3aa7", "#9085e9"),  # violet
    ("#e34948", "#e66767"),  # red
]
OTHER_COLOR = ("#898781", "#898781")  # muted — «Прочее» намеренно неяркое

def project_name(path: str) -> str:
    """
    Имя проекта из cwd, со схлопыванием git-worktree к родительскому проекту.

    Воркеры оркестратора живут в ~/.ao/data/worktrees/<проект>/<проект>-NN — без
    схлопывания один проект рассыпается на десятки серий и всё уезжает в «Прочее».
    Родитель берётся из самого пути, а не отрезанием суффикса «-NN»: иначе
    у обычного проекта вроде nokia-3310 отрезало бы часть имени.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return "unknown"
    if "worktrees" in parts:
        i = parts.index("worktrees")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1]


DIMENSIONS = {
    "tool": ("Инструмент", lambda r: r.tool),
    "model": ("Модель", lambda r: r.model),
    "agent": ("Агент", lambda r: r.agent),
    "project": ("Проект", lambda r: project_name(r.project)),
}


def hour_range(first: str, last: str) -> list[str]:
    """Непрерывная почасовая сетка, чтобы простои были видны как пустоты, а не схлопывались."""
    start = datetime.strptime(first, "%Y-%m-%dT%H")
    end = datetime.strptime(last, "%Y-%m-%dT%H")
    out = []
    cur = start
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%dT%H"))
        cur += timedelta(hours=1)
    return out


def build_raw(rows: list[Row], hours: list[str], components_of) -> dict:
    """
    Разреженная агрегация по (час, инструмент, модель, агент, проект) —
    сырьё для клиентской фасетной фильтрации по чекбоксам.

    Схлопывание по этим пяти ключам (не по строкам исходных Row) даёт на
    порядки меньше записей — дальше группировку по любому одному измерению
    (с учётом фильтров по остальным) делает JS, а не Python: иначе на клиенте
    нет данных для пересчёта при комбинированном фильтре вида
    «инструмент=codex И модель=X».

    Каждая ячейка несёт разбивку по 5 компонентам (in, out, cc5m, cc1h, cr) —
    в токенах и в деньгах — чтобы клиентская фасетная фильтрация пересчитывала
    разбивку при фильтрах. Итоговые cost/tokens ячейки — сумма её компонентов,
    выводится на JS-стороне (см. cellCost/cellTok в TEMPLATE), а не хранится
    здесь отдельным полем.
    """
    dim_values = {dim: sorted({key(r) for r in rows}) for dim, (_, key) in DIMENSIONS.items()}
    dim_idx = {dim: {v: i for i, v in enumerate(vs)} for dim, vs in dim_values.items()}
    tools, models, agents, projects = (
        dim_values["tool"], dim_values["model"], dim_values["agent"], dim_values["project"]
    )
    # Сессии не входят в DIMENSIONS (это не измерение-фильтр), но нужны в ключе
    # агрегации и как параллельный массив sessionIdx: иначе на клиенте нельзя
    # считать уникальные сессии в час. Ключ — пара (tool, session): имя каталога
    # сессии уникально лишь в рамках инструмента, а кортеж не требует разделителя.
    session_idx = {s: i for i, s in enumerate(sorted({(r.tool, r.session) for r in rows}))}
    hour_idx = {h: i for i, h in enumerate(hours)}

    # cell = [(5 токен-компонентов), (5 денежных компонентов)] — единый источник
    # порядка компонентов с Rates.cost_components/components_of (in, out, cc5m,
    # cc1h, cr), без отдельного магического смещения по индексам.
    cells: dict[tuple, list] = {}
    for r in rows:
        key = (
            hour_idx[r.hour],
            session_idx[(r.tool, r.session)],
            *(dim_idx[dim][key_fn(r)] for dim, (_, key_fn) in DIMENSIONS.items()),
        )
        tok_comp, cost_comp = components_of(r)
        cell = cells.get(key)
        if cell is None:
            cells[key] = [list(tok_comp), list(cost_comp)]
        else:
            tc, cc = cell
            for i in range(5):
                tc[i] += tok_comp[i]
                cc[i] += cost_comp[i]

    # Параллельные плоские массивы вместо списка кортежей на запись — компактнее
    # в JSON и тривиально разбираются в JS.
    h_a, s_a, t_a, m_a, a_a, p_a = [], [], [], [], [], []
    tok_arrs = {k: [] for k in COMPONENT_KEYS}
    cost_arrs = {k: [] for k in COMPONENT_KEYS}
    for (h, s, t, m, a, p), (tok_comp, cost_comp) in cells.items():
        h_a.append(h)
        s_a.append(s)
        t_a.append(t)
        m_a.append(m)
        a_a.append(a)
        p_a.append(p)
        for k, tok_v, cost_v in zip(COMPONENT_KEYS, tok_comp, cost_comp):
            tok_arrs[k].append(tok_v)
            cost_arrs[k].append(round(cost_v, 6))

    return {
        "tools": tools,
        "models": models,
        "agents": agents,
        "projects": projects,
        "hourIdx": h_a,
        "sessionIdx": s_a,
        "toolIdx": t_a,
        "modelIdx": m_a,
        "agentIdx": a_a,
        "projectIdx": p_a,
        **tok_arrs,
        **{cost_key(k): v for k, v in cost_arrs.items()},
    }


def build_payload(rows: list[Row], rates: dict) -> dict:
    hours_present = sorted({r.hour for r in rows})
    hours = hour_range(hours_present[0], hours_present[-1])

    def components_of(r: Row) -> tuple[tuple[int, ...], tuple[float, ...]]:
        """Токен- и денежные компоненты записи (in, out, cc5m, cc1h, cr) —
        split_cache_create делит cache_create на 5m/1h один раз, дальше токены
        идут как есть, а деньги — через Rates.cost_components (там та же
        split_cache_create). Без ставки — токены настоящие, деньги нули."""
        cc5m, cc1h = split_cache_create(r.cache_create, r.cache_create_1h)
        tok_comp = (r.input, r.output, cc5m, cc1h, r.cache_read)
        rate = rates.get(r.model)
        cost_comp = (
            rate.cost_components(r.input, r.output, r.cache_create, r.cache_read, r.cache_create_1h)
            if rate is not None else (0.0, 0.0, 0.0, 0.0, 0.0)
        )
        return tok_comp, cost_comp

    # Модели без ставки не считаются бесплатными — их объём выносится отдельно,
    # чтобы «$0» нельзя было спутать с «цена неизвестна». Это caveat про весь
    # период целиком, поэтому НЕ пересчитывается на клиенте при фильтрах.
    unpriced_models: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.model not in rates:
            unpriced_models[r.model] += r.total

    return {
        "hours": hours,
        "dateFrom": min(r.date for r in rows),
        "dateTo": max(r.date for r in rows),
        "rawRecords": len(rows),
        "unpriced": {
            "tokens": sum(unpriced_models.values()),
            "models": sorted(unpriced_models, key=lambda m: -unpriced_models[m])[:5],
        },
        "subscription": sorted(
            {m for m, rt in rates.items() if rt.source == "subscription"}
        ),
        "hasCodex": any(r.tool == "codex" for r in rows),
        "dimLabels": {dim: label for dim, (label, _) in DIMENSIONS.items()},
        "palette": PALETTE,
        "otherColor": OTHER_COLOR,
        # Ставки по компонентам для показа в таблице (Rates.as_dict — те же поля).
        "rates": {m: rt.as_dict() for m, rt in rates.items()},
        "componentPalette": PALETTE[:5],
        "raw": build_raw(rows, hours, components_of),
    }


TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Расход токенов по часам</title>
<style>
  :root {
    color-scheme: light;
    --surface: #fcfcfb;
    --plane: #f9f9f7;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --muted: #898781;
    --grid: #e1e0d9;
    --axis: #c3c2b7;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface: #1a1a19;
      --plane: #0d0d0d;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --muted: #898781;
      --grid: #2c2c2a;
      --axis: #383835;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 28px 48px;
    background: var(--plane); color: var(--ink);
    font: 14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
  }
  h1 { font-size: 19px; font-weight: 600; margin: 0 0 4px; letter-spacing: -.01em; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
  .card {
    background: var(--surface); border: 1px solid var(--grid);
    border-radius: 10px; padding: 20px; margin-bottom: 20px;
  }
  .tiles { display: flex; flex-wrap: wrap; gap: 28px; margin-bottom: 4px; }
  .tile-label { color: var(--muted); font-size: 12px; margin-bottom: 3px; }
  .tile-value { font-size: 26px; font-weight: 600; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
  .tile-note { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 18px; }
  button {
    font: inherit; font-size: 13px; padding: 6px 13px; cursor: pointer;
    background: var(--surface); color: var(--ink-2);
    border: 1px solid var(--axis); border-radius: 999px;
  }
  button[aria-pressed="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0 0 16px; padding: 0; list-style: none; }
  .legend li { display: flex; align-items: center; gap: 7px; font-size: 13px; color: var(--ink-2); }
  .swatch { width: 11px; height: 11px; border-radius: 3px; flex: none; }
  .chart-wrap { overflow-x: auto; }
  svg { display: block; }
  .tick { fill: var(--muted); font-size: 11px; }
  .gridline { stroke: var(--grid); stroke-width: 1; }
  .dayline { stroke: var(--axis); stroke-width: 1; stroke-dasharray: 3 3; }
  .avgline { stroke: var(--ink-2); stroke-width: 2; stroke-dasharray: 5 4; }
  .avglabel { fill: var(--ink-2); font-size: 11px; font-weight: 500; }
  .pie-title { font-size: 17px; font-weight: 600; margin: 0 0 4px; letter-spacing: -.01em; }
  .pie-sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
  .pie-wrap { display: flex; gap: 48px; align-items: center; justify-content: center; flex-wrap: wrap; }
  .pie-svg-wrap { position: relative; flex: none; width: 280px; height: 280px; }
  .pie-slice { stroke: var(--surface); stroke-width: 2; }
  .pie-total { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); text-align: center; pointer-events: none; }
  .pie-total .l { font-size: 12px; color: var(--muted); margin-bottom: 2px; }
  .pie-total .v { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
  .pie-slice-label { font-size: 13px; fill: var(--ink-2); text-anchor: middle; pointer-events: none; }
  .pie-legend { flex: none; display: flex; flex-direction: column; gap: 13px; list-style: none; margin: 0; padding: 0; }
  .pie-legend li { display: flex; align-items: center; gap: 10px; font-size: 14px; color: var(--ink); }
  .pie-legend .name { min-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pie-legend .pct { font-variant-numeric: tabular-nums; color: var(--ink-2); font-size: 14px; margin-left: auto; padding-left: 24px; }
  .table-wrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--grid); }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  td { font-variant-numeric: tabular-nums; }
  .name-cell { display: flex; align-items: center; gap: 8px; }
  tfoot td { font-weight: 600; border-bottom: none; }
  /* Ячейка компонента: стоимость сверху, токены снизу мелко и приглушённо */
  .comp-cost { font-variant-numeric: tabular-nums; }
  .comp-tok { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
  .rate-cell { white-space: nowrap; }
  .tip {
    position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
    background: var(--ink); color: var(--surface); padding: 8px 11px;
    border-radius: 7px; font-size: 12px; line-height: 1.45; z-index: 9;
    box-shadow: 0 4px 14px rgba(0,0,0,.18); max-width: 280px;
  }
  .tip b { font-weight: 600; }
  .tip .row { display: flex; justify-content: space-between; gap: 14px; font-variant-numeric: tabular-nums; }
  .foot { color: var(--muted); font-size: 12px; margin-top: 18px; }
  .filters-head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
  .filters-title { font-weight: 600; font-size: 14px; }
  .filters-count { color: var(--muted); font-size: 12px; flex: 1; }
  #filtersReset { margin-left: auto; }
  .filter-groups { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
  .filter-group { border: 1px solid var(--grid); border-radius: 8px; padding: 10px; display: flex; flex-direction: column; min-width: 0; }
  .filter-group-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .filter-group-head > span:first-child { font-weight: 500; font-size: 13px; flex: 1; }
  .filter-group-actions button {
    font-size: 11px; padding: 3px 8px; border-radius: 999px;
    background: var(--surface); color: var(--ink-2); border: 1px solid var(--axis);
  }
  .filter-search {
    font: inherit; font-size: 12px; padding: 5px 9px; margin-bottom: 8px;
    background: var(--plane); color: var(--ink); border: 1px solid var(--grid); border-radius: 6px;
  }
  .filter-options { max-height: 220px; overflow-y: auto; display: flex; flex-direction: column; gap: 1px; }
  .filter-opt {
    display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--ink-2);
    padding: 3px 4px; border-radius: 5px; cursor: pointer;
  }
  .filter-opt:hover { background: var(--plane); }
  .filter-opt input { flex: none; }
  .filter-opt span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
</head>
<body>
<h1>Расход по часам</h1>
<div class="sub" id="subtitle"></div>

<div class="card">
  <div class="tiles" id="tiles"></div>
  <div class="tile-note" id="caveat" style="margin-top:14px"></div>
</div>

<div class="card" id="filtersCard">
  <div class="filters-head">
    <span class="filters-title">Фильтры</span>
    <span class="filters-count" id="filterCount"></span>
    <button id="filtersReset">Сбросить все</button>
  </div>
  <div class="filter-groups" id="filterGroups"></div>
</div>

<div class="card">
  <div class="controls" id="controls"></div>
  <ul class="legend" id="legend"></ul>
  <div class="chart-wrap"><svg id="chart"></svg></div>
</div>

<div class="card">
  <h2 class="pie-title" id="pieTitle"></h2>
  <div class="pie-sub" id="pieSub"></div>
  <div class="pie-wrap">
    <div class="pie-svg-wrap">
      <svg id="pie" viewBox="0 0 200 200"></svg>
      <div class="pie-total"><div class="l">Итого</div><div class="v" id="pieTotal"></div></div>
    </div>
    <ul class="pie-legend" id="pieLegend"></ul>
  </div>
</div>

<div class="card">
  <div class="table-wrap"><table id="table"></table></div>
</div>

<div class="foot" id="foot"></div>
<div class="tip" id="tip"></div>

<script>
const DATA = __DATA__;

const dark = () => matchMedia('(prefers-color-scheme: dark)').matches;
const colorAt = i => {
  const p = i < DATA.palette.length ? DATA.palette[i] : DATA.otherColor;
  return dark() ? p[1] : p[0];
};
const surface = () => getComputedStyle(document.body).getPropertyValue('--surface').trim();

const fmtInt = n => n.toLocaleString('ru-RU');
const compactTok = n => {
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 0 : 1).replace('.', ',') + ' млрд';
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace('.', ',') + ' млн';
  if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1).replace('.', ',') + ' тыс';
  return String(Math.round(n));
};
const money = n => {
  if (n === 0) return '$0';
  if (Math.abs(n) >= 1000) return '$' + Math.round(n).toLocaleString('ru-RU');
  if (Math.abs(n) >= 10) return '$' + n.toFixed(0);
  if (Math.abs(n) >= 1) return '$' + n.toFixed(2).replace('.', ',');
  return '$' + n.toFixed(2).replace('.', ',');
};
const hourLabel = h => h.slice(8, 10) + '.' + h.slice(5, 7) + ' ' + h.slice(11) + ':00';
const dayLabel = h => h.slice(8, 10) + '.' + h.slice(5, 7);
// Полночь на наивной почасовой сетке DATA.hours (ровно 24 записи на сутки, без
// сдвига на DST — см. комментарий у bucketOffset). Единый источник определения
// границы суток и для bucketOffset, и для пунктиров dayline.
const isMidnight = h => h.slice(11) === '00';
const escapeHtml = s => String(s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const average = xs => xs.length ? xs.reduce((a, v) => a + v, 0) / xs.length : 0;

// Гранулярность оси X графика — по длине периода, чтобы столбики не
// схлопывались в нечитаемую полосу и не требовали горизонтального скролла.
// Внутри диапазона DAY число бакетов держится в пределах ~10-90; на переходе
// DAY→WEEK оно скачком падает (90 дней → ~13 недель) — это ожидаемо, не баг.
const HOUR = 1, DAY = 24, WEEK = 24 * 7;
// Подпись единицы измерения для линии среднего — рядом с определением
// размеров бакетов, чтобы новый размер (например MONTH) добавлялся в одном месте.
const BUCKET_UNIT_LABEL = { [HOUR]: 'активный час', [DAY]: 'активный день', [WEEK]: 'активную неделю' };
function pickBucketSize(hoursLen) {
  if (hoursLen > DAY * 90) return WEEK;   // дольше ~3 месяцев -> недели
  if (hoursLen > DAY * 10) return DAY;    // дольше ~10 дней -> дни
  return HOUR;
}
// Грануляция для текущего рендера: ручной выбор (bucketSizeMode) или авто по
// длине периода (pickBucketSize). Ключ кнопки ('hour'/'day'/'week') переводится
// в размер бакета здесь — единственное место конвертации из строки в число.
const BUCKET_SIZE = { hour: HOUR, day: DAY, week: WEEK };
function resolveBucketSize() {
  return bucketSizeMode === 'auto' ? pickBucketSize(DATA.hours.length) : BUCKET_SIZE[bucketSizeMode];
}

// Смещение первого бакета, чтобы границы DAY/WEEK совпадали с календарными
// (полночь / понедельник), а не с часом первой записи в данных. Без этого
// бакет "05.07" мог реально содержать часы 05.07 14:00 — 06.07 13:00, и расход
// 06.07 размазывался бы по двум бакетам под чужими подписями.
// allHours — НАИВНАЯ почасовая сетка (Python hour_range: += timedelta(hours=1) без
// tzinfo) — ровно 24 записи на календарный день независимо от перехода на летнее/
// зимнее время. Считать offset через мс-арифметику Date (которая DST-зависима в
// таймзоне браузера) значило бы разъехаться с этой сеткой на переходный час —
// поэтому ищем границу СКАНИРОВАНИЕМ ИНДЕКСОВ той же сетки, а не через Date.getTime().
function bucketOffset(allHours, bucketSize) {
  if (bucketSize === HOUR || !allHours.length) return 0;
  let i = 0;
  while (i < allHours.length && !isMidnight(allHours[i])) i++;   // первая полночь в сетке
  if (bucketSize === DAY) return i;
  while (i < allHours.length && new Date(allHours[i].slice(0, 10) + 'T00:00:00').getDay() !== 1) i += DAY;
  return i;
}

// Границы бакета bi (0-based) с учётом календарного сдвига offset (см. bucketOffset).
// Одна формула вместо двух: virtual — позиция конца бакета bi на прямой, где offset
// уже сдвинут на -bucketSize (так бакет 0 — это [0, offset), «неполный хвост» перед
// первой календарной границей, единообразно с полными бакетами далее). При offset=0
// (данные уже начинаются на границе) формула вырождается в bi*bucketSize без особого случая.
// Единственный источник этой формулы для bucketize/bucketizeSeries/bucketLabel.
function bucketBounds(bi, bucketSize, offset, len) {
  const virtualStart = offset > 0 ? offset - bucketSize : 0;
  const start = Math.max(0, virtualStart + bi * bucketSize);
  const end = Math.min(virtualStart + (bi + 1) * bucketSize, len);
  return [start, end];
}

function bucketCount(bucketSize, offset, len) {
  const virtualStart = offset > 0 ? offset - bucketSize : 0;
  return Math.ceil((len - virtualStart) / bucketSize);
}

// Подпись бакета: час — как раньше (дата+время), день/неделя — просто дата
// начала бакета (неделя выводится как диапазон, чтобы было видно охват).
// allHours — исходный ПОЧАСОВОЙ массив (DATA.hours), не забакеченный, иначе
// индекс конца недели считается неверно. Границы — из bucketBounds, тот же
// источник, что и у bucketize/bucketizeSeries.
function bucketLabel(bucketSize, allHours, bi, offset) {
  const [start, end] = bucketBounds(bi, bucketSize, offset, allHours.length);
  if (bucketSize === HOUR) return hourLabel(allHours[start]);
  if (bucketSize === DAY) return dayLabel(allHours[start]);
  return dayLabel(allHours[start]) + '–' + dayLabel(allHours[end - 1]);
}

// Схлопывает один почасовой ряд в бакеты суммированием — используется для
// «альтернативной» серии тултипа, где нужна только сумма, без разбивки grid.
function bucketizeSeries(hours, series, bucketSize, offset) {
  if (bucketSize === HOUR) return series;
  const n = bucketCount(bucketSize, offset, hours.length);
  const out = new Array(n).fill(0);
  for (let bi = 0; bi < n; bi++) {
    const [start, end] = bucketBounds(bi, bucketSize, offset, hours.length);
    for (let hi = start; hi < end; hi++) out[bi] += series[hi];
  }
  return out;
}

// Схлопывает почасовые hours/grid/series в бакеты по bucketSize последовательных
// часов, выровненных по календарю через offset (см. bucketOffset и bucketBounds).
// Первый бакет может быть короче bucketSize (обрезан началом данных), последний —
// неполным по факту наличия данных. Границы бакета — из bucketBounds (один источник
// формулы для bucketize/bucketizeSeries/bucketLabel), outSeries — вызовом
// bucketizeSeries, не пересчитывается здесь заново.
function bucketize(hours, grid, series, bucketSize, offset) {
  if (bucketSize === HOUR) return { hours, grid, series };
  const n = bucketCount(bucketSize, offset, hours.length);
  const S = grid.length ? grid[0].length : 0;
  const outHours = new Array(n);
  const outGrid = Array.from({ length: n }, () => new Array(S).fill(0));
  for (let bi = 0; bi < n; bi++) {
    const [start, end] = bucketBounds(bi, bucketSize, offset, hours.length);
    outHours[bi] = hours[start];
    for (let hi = start; hi < end; hi++) {
      for (let si = 0; si < S; si++) outGrid[bi][si] += grid[hi][si];
    }
  }
  const outSeries = bucketizeSeries(hours, series, bucketSize, offset);
  return { hours: outHours, grid: outGrid, series: outSeries };
}

let dim = 'tool';                        // tool | model | agent | project | components
let unit = 'cost';                       // cost | tokens
let bucketSizeMode = 'auto';             // 'auto' | 'hour' | 'day' | 'week' — ключ кнопки грануляции, как dim/unit
let avgMode = 'hour';                    // 'hour' | 'session' — знаменатель линии среднего на графике
const FILTER_DIMS = ['tool', 'model', 'agent', 'project'];
const filters = { tool: new Set(), model: new Set(), agent: new Set(), project: new Set(), components: new Set() };

// «Компоненты» — отдельное измерение (серии = 5 компонентов расхода), а не
// переключатель стека: выбор «Компоненты» заменяет Инструмент/Модель/Агент/Проект.
const COMP_DIM_LABEL = 'Компоненты';
// Компоненты расхода: ключи в DATA.raw, подписи и цвета. cache_create_1h (TTL 1h)
// тарифицируется дороже 5m, поэтому хранится отдельно, но в таблице «Кэш-запись»
// объединяет cc5m+cc1h (см. comp4 в table()).
const COMP_KEYS = ['in', 'out', 'cc5m', 'cc1h', 'cr'];
const COMP_COST_KEYS = ['costIn', 'costOut', 'costCc5m', 'costCc1h', 'costCr'];
const COMP_LABELS = ['Вход', 'Выход', 'Кэш-запись 5м', 'Кэш-запись 1ч', 'Кэш-чтение'];

// DATA.raw не везёт готовую сумму (cost/tokens) по ячейке — она выводима из 5
// компонентов, и хранить её ещё раз в JSON было бы избыточно. Сумма считается
// один раз здесь (не в build_raw и не при каждом render()), чтобы частый случай
// «фильтр компонентов не задан» в cellCost/cellTok не пересчитывал reduce по 5
// массивам на каждую ячейку при каждом клике по фильтру.
DATA.raw.cost = DATA.raw.costIn.map((_, i) =>
  COMP_COST_KEYS.reduce((s, k) => s + DATA.raw[k][i], 0));
DATA.raw.tokens = DATA.raw.in.map((_, i) =>
  COMP_KEYS.reduce((s, k) => s + DATA.raw[k][i], 0));
const compColorAt = i => {
  const p = i < DATA.componentPalette.length ? DATA.componentPalette[i] : DATA.otherColor;
  return dark() ? p[1] : p[0];
};
// Цвет серии: для измерения «Компоненты» — палитра компонентов, иначе — серийная.
const seriesColorAt = i => dim === 'components' ? compColorAt(i) : colorAt(i);
const dimLabel = d => DATA.dimLabels[d] || COMP_DIM_LABEL;

const isCost = () => unit === 'cost';
// Компактная форма для осей и плиток, точная — для таблицы и тултипа
const compact = v => isCost() ? money(v) : compactTok(v);
// Точная денежная форма независимо от текущего unit — используется и exact()
// (когда unit === 'cost'), и местами, которым нужны именно доллары (таблица
// компонентов/ставок, тултип), поэтому вынесена отдельно, а не продублирована.
const money2 = v => '$' + v.toFixed(2).replace('.', ',');
const exact = v => isCost() ? money2(v) : fmtInt(Math.round(v));

// Индексы агрегированных ячеек DATA.raw, прошедшие фильтр: AND между
// измерениями (tool И model И agent И project), OR внутри одного измерения
// (любой отмеченный чекбокс подходит); пустой набор чекбоксов = не ограничивает.
function filteredIndexes() {
  const r = DATA.raw, n = r.hourIdx.length, out = [];
  const active = FILTER_DIMS.filter(k => filters[k].size > 0);
  // Фильтр по компонентам НЕ отбирает ячейки — у каждой ячейки всегда все 5
  // компонентов сразу, поэтому отбор по «ненулевой компонент» затаскивал бы в
  // сумму и остальные 4 компонента той же ячейки. Компоненты — это маска
  // слагаемых при суммировании (см. activeComps/cellCost/cellTok), а не фильтр строк.
  outer:
  for (let i = 0; i < n; i++) {
    for (const k of active) {
      if (!filters[k].has(r[k + 's'][r[k + 'Idx'][i]])) continue outer;
    }
    out.push(i);
  }
  return out;
}

// Индексы компонентов (0..4), участвующих в сумме. Пустой чекбокс-набор = все 5
// (как «фильтр не задан»). Используется вместе с cellCost/cellTok, чтобы везде,
// где раньше суммировался r.cost[i]/r.tokens[i] целиком, суммировалась только
// выбранная часть компонентов.
const ALL_COMPS = [...COMP_KEYS.keys()];
const activeComps = () => filters.components.size
  ? ALL_COMPS.filter(i => filters.components.has(COMP_KEYS[i]))
  : ALL_COMPS;
// Частый случай — фильтр компонентов не задан (comps === ALL_COMPS) — читает
// готовую сумму DATA.raw.cost/tokens (посчитана один раз при загрузке, см. выше)
// вместо reduce по 5 массивам на каждой ячейке при каждом render().
const cellCost = (i, comps) => comps === ALL_COMPS
  ? DATA.raw.cost[i]
  : comps.reduce((s, k) => s + DATA.raw[COMP_COST_KEYS[k]][i], 0);
const cellTok = (i, comps) => comps === ALL_COMPS
  ? DATA.raw.tokens[i]
  : comps.reduce((s, k) => s + DATA.raw[COMP_KEYS[k]][i], 0);

// Группировка отфильтрованных ячеек по ОДНОМУ измерению (dim) — топ-8 серий
// по стоимости + «Прочее», та же форма, что раньше строил Python build_dimension.
// Для dim === 'components' серии — это 5 компонентов расхода (без «Прочее»).
function aggregateByDim(idxs, groupDim) {
  const r = DATA.raw;
  const H = DATA.hours.length;
  const comps = activeComps();

  if (groupDim === 'components') {
    const gridCost = Array.from({ length: H }, () => new Array(5).fill(0));
    const gridTok = Array.from({ length: H }, () => new Array(5).fill(0));
    const totals = { cost: new Array(5).fill(0), tokens: new Array(5).fill(0) };
    for (const i of idxs) {
      const hi = r.hourIdx[i];
      for (const k of comps) {
        gridCost[hi][k] += r[COMP_COST_KEYS[k]][i];
        gridTok[hi][k] += r[COMP_KEYS[k]][i];
        totals.cost[k] += r[COMP_COST_KEYS[k]][i];
        totals.tokens[k] += r[COMP_KEYS[k]][i];
      }
    }
    return {
      names: COMP_LABELS,
      grid: { cost: gridCost, tokens: gridTok },
      totals,
      compTok: null, compCost: null,
      otherCount: 0,
    };
  }

  const names = r[groupDim + 's'], idxArr = r[groupDim + 'Idx'];
  // cellCost/cellTok посчитаны по каждому idx один раз и переиспользуются в обоих
  // проходах ниже (ранжирование top-N и заполнение сеток) — иначе reduce по
  // компонентам считался бы дважды на строку.
  const costs = idxs.map(i => cellCost(i, comps));
  const toks = idxs.map(i => cellTok(i, comps));
  const costTotals = new Map(), tokTotals = new Map();
  idxs.forEach((i, j) => {
    const name = names[idxArr[i]];
    costTotals.set(name, (costTotals.get(name) || 0) + costs[j]);
    tokTotals.set(name, (tokTotals.get(name) || 0) + toks[j]);
  });
  const ordered = [...tokTotals.keys()].sort((a, b) =>
    (costTotals.get(b) - costTotals.get(a)) || (tokTotals.get(b) - tokTotals.get(a)));
  const MAX_SERIES = DATA.palette.length;
  const top = ordered.slice(0, MAX_SERIES);
  const hasOther = ordered.length > MAX_SERIES;
  const outNames = hasOther ? [...top, 'Прочее'] : top;
  const index = new Map(top.map((n, i) => [n, i]));
  const otherI = hasOther ? top.length : null;

  const S = outNames.length;
  const gridCost = Array.from({ length: H }, () => new Array(S).fill(0));
  const gridTok = Array.from({ length: H }, () => new Array(S).fill(0));
  // Разбивка по 5 компонентам по сериям — для столбцов компонентов в таблице.
  // Невыбранные компоненты остаются нулями, чтобы колонки таблицы (см. table())
  // не расходились с итоговой стоимостью/токенами серии при активном фильтре.
  const compTok = Array.from({ length: S }, () => new Array(5).fill(0));
  const compCost = Array.from({ length: S }, () => new Array(5).fill(0));
  idxs.forEach((i, j) => {
    const name = names[idxArr[i]];
    const si = index.has(name) ? index.get(name) : otherI;
    if (si == null) return;
    const hi = r.hourIdx[i];
    const c = costs[j], t = toks[j];
    gridCost[hi][si] += c;
    gridTok[hi][si] += t;
    for (const k of comps) {
      compTok[si][k] += r[COMP_KEYS[k]][i];
      compCost[si][k] += r[COMP_COST_KEYS[k]][i];
    }
  });

  const seriesCost = top.map(n => costTotals.get(n));
  const seriesTok = top.map(n => tokTotals.get(n));
  if (hasOther) {
    seriesCost.push(ordered.slice(MAX_SERIES).reduce((s, n) => s + costTotals.get(n), 0));
    seriesTok.push(ordered.slice(MAX_SERIES).reduce((s, n) => s + tokTotals.get(n), 0));
  }

  return {
    names: outNames,
    grid: { cost: gridCost, tokens: gridTok },
    totals: { cost: seriesCost, tokens: seriesTok },
    compTok, compCost,
    otherCount: hasOther ? ordered.length - MAX_SERIES : 0,
  };
}

// Замена Python metrics/byHour/activeHours/calendarHours — считается из
// отфильтрованных ячеек, чтобы тайлы пересчитывались вместе с фильтром.
function computeMetrics(idxs) {
  const r = DATA.raw, H = DATA.hours.length;
  const comps = activeComps();
  const costHour = new Array(H).fill(0), tokHour = new Array(H).fill(0);
  // Уникальные сессии в каждом часу — та же «активность», что и tokHour (comps):
  // сессия «в часу», если у неё есть ячейка с ненулевой активностью под фильтром.
  // totalSessions собирается в том же цикле и по тому же правилу (t > 0), иначе
  // сессия с нулевой активностью по выбранным компонентам попала бы в знаменатель,
  // но не в числитель, занижая avgSession.
  const sessInHour = Array.from({ length: H }, () => new Set());
  const allSessions = new Set();
  for (const i of idxs) {
    const hi = r.hourIdx[i];
    const t = cellTok(i, comps);
    costHour[hi] += cellCost(i, comps);
    tokHour[hi] += t;
    if (t > 0) {
      allSessions.add(r.sessionIdx[i]);
      sessInHour[hi].add(r.sessionIdx[i]);
    }
  }
  const byHour = { cost: costHour, tokens: tokHour };
  const activeIdx = [];
  tokHour.forEach((v, i) => { if (v > 0) activeIdx.push(i); });
  const nActive = activeIdx.length || 1;
  // сессия-часы = Σ уникальных сессий по активным часам; totalSessions = число
  // уникальных сессий среди отфильтрованных ячеек за период.
  const totalSessionHours = activeIdx.reduce((s, h) => s + sessInHour[h].size, 0);
  const totalSessions = allSessions.size;
  const metrics = {};
  for (const u of ['cost', 'tokens']) {
    const series = byHour[u];
    const grand = series.reduce((a, b) => a + b, 0);
    const peak = series.length ? Math.max(...series) : 0;
    metrics[u] = {
      grand, avgActive: grand / nActive, avgCalendar: grand / H,
      // Нормализация на сессию: avgSessionPerActiveHour = расход на сессию за
      // активный час с учётом параллельности (Σ сессий по активным часам);
      // avgSession = средний расход одной сессии за весь период.
      avgSessionPerActiveHour: totalSessionHours ? grand / totalSessionHours : 0,
      avgSession: totalSessions ? grand / totalSessions : 0,
      peak, peakHour: series.length && peak > 0 ? DATA.hours[series.indexOf(peak)] : '',
    };
  }
  return { byHour, metrics, activeHours: activeIdx.length, calendarHours: H,
    totalSessions, totalSessionHours,
    sessionsPerHour: sessInHour.map(s => s.size), records: idxs.length };
}

function tiles(m) {
  const mu = m.metrics[unit];
  const what = isCost() ? 'Расход' : 'Токены';
  // Переключатель «Среднее: активный час / сессия» переключает основные тайлы:
  // в режиме «сессия» тайлы «активный час»/«календарный час» становятся
  // «на сессию в активном часу»/«на сессию за период».
  const session = avgMode === 'session';
  const items = [
    [session ? what + ' в среднем на сессию в активном часу' : what + ' в среднем за активный час',
     compact(session ? mu.avgSessionPerActiveHour : mu.avgActive),
     session ? m.totalSessions + ' сессий · ' + m.totalSessionHours + ' сессия-часов'
             : m.activeHours + ' активных часов из ' + m.calendarHours],
    [session ? 'В среднем на сессию за период' : 'В среднем за календарный час',
     compact(session ? mu.avgSession : mu.avgCalendar),
     session ? m.totalSessions + ' сессий за период' : 'с учётом простоев'],
    ['Пик за час', compact(mu.peak), mu.peakHour ? hourLabel(mu.peakHour) : ''],
    [isCost() ? 'Всего' : 'Всего токенов', compact(mu.grand),
     fmtInt(DATA.rawRecords) + ' записей за период'],
  ];
  document.getElementById('tiles').innerHTML = items.map(([l, v, n]) =>
    `<div><div class="tile-label">${l}</div><div class="tile-value">${v}</div>` +
    `<div class="tile-note">${n}</div></div>`).join('');

  // Что именно не попало в сумму денег — «$0» не должно читаться как «бесплатно».
  // Это caveat про весь период (не про текущий фильтр) — ставки/подписки не
  // зависят от того, что сейчас отмечено чекбоксами.
  const parts = [];
  if (DATA.subscription.length)
    parts.push('По подписке (стоимость $0): ' + DATA.subscription.join(', ') + '.');
  if (DATA.unpriced.tokens)
    parts.push('Без прайсинга: ' + compactTok(DATA.unpriced.tokens) +
      ' токенов (' + DATA.unpriced.models.join(', ') + ') — в сумму денег не входят.');
  if (DATA.hasCodex)
    parts.push('Codex посчитан по обычному тарифу: признака fast-режима в логах нет, ' +
      'в нём цена вдвое выше.');
  document.getElementById('caveat').textContent = parts.join(' ');
}

function controls() {
  document.getElementById('controls').innerHTML =
    '<span style="color:var(--muted);font-size:12px">Разбивка:</span>' +
    Object.entries(DATA.dimLabels).map(([k, label]) =>
      `<button data-dim="${k}" aria-pressed="${k === dim}">${label}</button>`).join('') +
    `<button data-dim="components" aria-pressed="${dim === 'components'}">${COMP_DIM_LABEL}</button>` +
    '<span style="color:var(--muted);font-size:12px">Грануляция:</span>' +
    `<button data-bucket="auto" aria-pressed="${bucketSizeMode === 'auto'}">Авто</button>` +
    `<button data-bucket="hour" aria-pressed="${bucketSizeMode === 'hour'}">Час</button>` +
    `<button data-bucket="day" aria-pressed="${bucketSizeMode === 'day'}">День</button>` +
    `<button data-bucket="week" aria-pressed="${bucketSizeMode === 'week'}">Неделя</button>` +
    '<span style="flex:1"></span>' +
    '<span style="color:var(--muted);font-size:12px">Единицы:</span>' +
    `<button data-unit="cost" aria-pressed="${unit === 'cost'}">$</button>` +
    `<button data-unit="tokens" aria-pressed="${unit === 'tokens'}">токены</button>` +
    '<span style="color:var(--muted);font-size:12px">Среднее:</span>' +
    `<button data-avg="hour" aria-pressed="${avgMode === 'hour'}">активный час</button>` +
    `<button data-avg="session" aria-pressed="${avgMode === 'session'}">сессия</button>`;
  document.querySelectorAll('#controls button[data-dim]').forEach(b =>
    b.onclick = () => { dim = b.dataset.dim; render(); });
  document.querySelectorAll('#controls button[data-unit]').forEach(b =>
    b.onclick = () => { unit = b.dataset.unit; render(); });
  // Группа кнопок-переключателей: читают dataset-ключ, пишут state-переменную,
  // обновляют aria-pressed по группе и вызывают колбэк. Грануляция влияет только
  // на график (chart поверх кэша), «Среднее» — на тайлы/таблицу/график (render).
  const bindToggle = (sel, key, set, after) => {
    document.querySelectorAll(sel).forEach(b =>
      b.onclick = () => {
        set(b.dataset[key]);
        document.querySelectorAll(sel).forEach(x =>
          x.setAttribute('aria-pressed', String(x.dataset[key] === b.dataset[key])));
        after();
      });
  };
  bindToggle('#controls button[data-bucket]', 'bucket', v => bucketSizeMode = v,
    () => { if (curD && curM) chart(curD, curM); });
  bindToggle('#controls button[data-avg]', 'avg', v => avgMode = v, render);
}

// Одна группа фильтра: заголовок, «Все/Сброс», поиск, чекбоксы. opts — значения
// чекбоксов, labels — подписи (для фасетных измерений labels == opts).
function renderFilterGroup(dimKey, label, opts, labels) {
  return `<div class="filter-group" data-dim="${dimKey}">` +
    `<div class="filter-group-head"><span>${escapeHtml(label)}</span>` +
    `<span class="filter-group-actions">` +
    `<button type="button" data-act="all">Все</button>` +
    `<button type="button" data-act="none">Сброс</button></span></div>` +
    `<input type="search" class="filter-search" data-dim="${dimKey}" placeholder="Поиск…">` +
    `<div class="filter-options" data-dim="${dimKey}">` +
    opts.map((v, i) => `<label class="filter-opt"><input type="checkbox" value="${escapeHtml(v)}">` +
      `<span title="${escapeHtml(labels[i])}">${escapeHtml(labels[i])}</span></label>`).join('') +
    `</div></div>`;
}

function renderFilterGroups() {
  // Группа «Компоненты» — не фасетная (у ячейки все 5 компонентов сразу), поэтому
  // опции задаём явно: чекбокс = компонент, ячейка проходит, если он в ней ненулевой.
  const compGroup = renderFilterGroup('components', COMP_DIM_LABEL, COMP_KEYS, COMP_LABELS);

  document.getElementById('filterGroups').innerHTML =
    FILTER_DIMS.map(k =>
      renderFilterGroup(k, DATA.dimLabels[k], DATA.raw[k + 's'], DATA.raw[k + 's'])
    ).join('') + compGroup;

  const groups = document.getElementById('filterGroups');
  groups.addEventListener('change', e => {
    if (e.target.type !== 'checkbox') return;
    const dimKey = e.target.closest('.filter-group').dataset.dim;
    if (e.target.checked) filters[dimKey].add(e.target.value);
    else filters[dimKey].delete(e.target.value);
    render();
  });
  groups.addEventListener('input', e => {
    if (!e.target.classList.contains('filter-search')) return;
    const q = e.target.value.trim().toLowerCase();
    e.target.closest('.filter-group').querySelectorAll('.filter-opt').forEach(el => {
      el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  });
  groups.addEventListener('click', e => {
    const act = e.target.dataset.act;
    if (!act) return;
    const group = e.target.closest('.filter-group'), dimKey = group.dataset.dim;
    group.querySelectorAll('.filter-opt').forEach(el => {
      if (el.style.display === 'none') return;   // поиск сузил список — не трогаем скрытые
      const cb = el.querySelector('input');
      cb.checked = act === 'all';
      if (act === 'all') filters[dimKey].add(cb.value); else filters[dimKey].delete(cb.value);
    });
    render();
  });

  document.getElementById('filtersReset').onclick = () => {
    FILTER_DIMS.forEach(k => filters[k].clear());
    filters.components.clear();
    groups.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
    render();
  };
}

function updateFilterCount(idxs) {
  const rowsActive = FILTER_DIMS.some(k => filters[k].size > 0);
  const compsActive = filters.components.size > 0;
  const parts = [];
  if (rowsActive)
    parts.push(`показано ${fmtInt(idxs.length)} из ${fmtInt(DATA.raw.cost.length)} агрегированных строк`);
  // Фильтр компонентов не отбирает строки — он сужает, какие слагаемые (Вход/Выход/…)
  // входят в сумму, поэтому подпись формулируется отдельно, чтобы не читаться как «строк меньше».
  if (compsActive)
    parts.push(`учтены компоненты: ${[...filters.components].map(k => COMP_LABELS[COMP_KEYS.indexOf(k)]).join(', ')}`);
  document.getElementById('filterCount').textContent = parts.join(' · ');
}

function legend(d) {
  const tot = d.totals[unit];
  // При единственной серии легенда не нужна — её называет заголовок и таблица.
  const shown = d.names.filter((n, i) => tot[i] > 0);
  document.getElementById('legend').innerHTML = shown.length < 2 ? '' :
    d.names.map((n, i) => tot[i] > 0
      ? `<li><span class="swatch" style="background:${seriesColorAt(i)}"></span>${escapeHtml(n)}</li>` : '').join('');
}

// Значение линии среднего: HOUR — готовый метрик из m.metrics[unit]; DAY/WEEK —
// среднее по ненулевым бакетам отношения суммы бакета к знаменателю. Для
// «активного часа» знаменатель 1 (среднее по бакетам), для «на сессию» — число
// сессия-часов в бакете (бакетизируется через bucketizeSeries). Оба режима —
// частные случаи одного «среднее по бакетам с знаменателем», поэтому одна функция.
function avgLineY(bucketSize, m, series, hours, offset, mode) {
  if (bucketSize === HOUR)
    return m.metrics[unit][mode === 'session' ? 'avgSessionPerActiveHour' : 'avgActive'];
  const sessPerBucket = mode === 'session'
    ? bucketizeSeries(hours, m.sessionsPerHour, bucketSize, offset) : null;
  const per = [];
  for (let bi = 0; bi < series.length; bi++) {
    const d = sessPerBucket ? sessPerBucket[bi] : 1;
    if (series[bi] > 0 && d > 0) per.push(series[bi] / d);
  }
  return average(per);
}

function chart(d, m) {
  const bucketSize = resolveBucketSize();
  const offset = bucketOffset(DATA.hours, bucketSize);
  const b = bucketize(DATA.hours, d.grid[unit], m.byHour[unit], bucketSize, offset);
  // hours ниже — забакеченный массив (длина = число бакетов), для итерации баров/тиков.
  // DATA.hours — исходный почасовой; их не путать, bucketLabel ниже намеренно берёт
  // именно DATA.hours (см. её комментарий).
  const { hours, grid, series } = b;
  // тултип берёт агрегированные по бакету данные отсюда, а не из DATA.hours напрямую;
  // altUnit нужен для второй строки тултипа («в токенах»/«в деньгах»). Для alt — только
  // сумма, полный grid не нужен, поэтому bucketizeSeries вместо bucketize (не тратим
  // O(hours*S) на agregацию неиспользуемой разбивки по сериям).
  const altUnit = isCost() ? 'tokens' : 'cost';
  const altSeries = bucketizeSeries(DATA.hours, m.byHour[altUnit], bucketSize, offset);
  // В режиме «сессия» столбики показывают расход на сессию в бакете, а не сумму
  // бакета: иначе переключатель менял бы только линию среднего, и график «не
  // перестраивался» бы с точки зрения пользователя. Нормируем series/grid/altSeries
  // на число сессия-часов в бакете. Линия среднего (avgLineY) считает по СЫРОМУ
  // series, поэтому нормировка делается после неё, а здесь храним оба варианта.
  const sessPerBucket = avgMode === 'session'
    ? bucketizeSeries(DATA.hours, m.sessionsPerHour, bucketSize, offset) : null;
  const dispSeries = sessPerBucket
    ? series.map((v, bi) => sessPerBucket[bi] > 0 ? v / sessPerBucket[bi] : 0) : series;
  const dispGrid = sessPerBucket
    ? grid.map((row, bi) => { const d = sessPerBucket[bi]; return d > 0 ? row.map(v => v / d) : row.map(() => 0); })
    : grid;
  const dispAlt = sessPerBucket
    ? altSeries.map((v, bi) => sessPerBucket[bi] > 0 ? v / sessPerBucket[bi] : 0) : altSeries;
  curBucket = { hours, grid: dispGrid, series: dispSeries, altSeries: dispAlt, names: d.names, bucketSize, offset };
  const L = 62, R = 44, T = 12, B = 46, H = 300;   // R с запасом под последнюю подпись оси

  // Ширина столбика — от реальной ширины контейнера, а не от фиксированных
  // ступеней: иначе при числе точек, для которого ступень ещё не сработала
  // (например ~150 часов при пороге >160), SVG всё равно мог быть шире
  // экрана и упирался в горизонтальный скролл. minBw — нижний предел
  // читаемости; если контейнер совсем не тянет, включается overflow-x
  // как safety net, а не основной способ просмотра.
  const wrapWidth = document.querySelector('.chart-wrap').clientWidth || 900;
  const avail = Math.max(wrapWidth - L - R, 100);
  const minBw = hours.length > 400 ? 2 : hours.length > 160 ? 3 : 4;
  // Независимая переменная — шаг на бакет (bw + gap), а не bw и gap по отдельности:
  // так gap выводится из шага одной формулой, без взаимозависимого подбора.
  // bw кламплен потолком 20 — при большом step (мало бакетов, широкий контейнер)
  // фактическая bw+gap может оказаться меньше step, и W (ниже, из факта bw/gap)
  // тогда меньше avail — это ожидаемо, не переполнение.
  const step = Math.max(minBw + 1, Math.floor(avail / hours.length));
  const gap = step - minBw > 6 ? 2 : 1;
  const bw = Math.max(minBw, Math.min(20, step - gap));
  const W = L + R + hours.length * (bw + gap);
  const max = Math.max(...dispSeries, isCost() ? 0.01 : 1);

  // округляем верх шкалы до «чистого» числа
  const pow = Math.pow(10, Math.floor(Math.log10(max)));
  const top = Math.ceil(max / (pow / 2)) * (pow / 2);
  const y = v => T + H - (v / top) * H;

  let s = '';
  for (let i = 0; i <= 4; i++) {
    const v = top * i / 4, yy = y(v);
    s += `<line class="gridline" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/>` +
         `<text class="tick" x="${L - 8}" y="${yy + 4}" text-anchor="end">${compact(v)}</text>`;
  }

  const sc = surface();
  hours.forEach((h, hi) => {
    const x = L + hi * (bw + gap);
    let acc = 0;
    dispGrid[hi].forEach((v, si) => {
      if (v <= 0) return;
      const y0 = y(acc + v), y1 = y(acc);
      const hgt = Math.max(y1 - y0, .6);
      s += `<rect x="${x}" y="${y0}" width="${bw}" height="${hgt}" fill="${seriesColorAt(si)}" ` +
           `rx="${bw >= 8 ? 2 : 1}" data-h="${hi}" data-s="${si}"/>`;
      // 2px разделитель цветом поверхности между сегментами стека
      if (acc > 0) s += `<rect x="${x}" y="${y1 - 1}" width="${bw}" height="2" fill="${sc}"/>`;
      acc += v;
    });
  });

  // Вертикальные пунктиры на границах календарных суток (полночь) — при почасовой
  // грануляции, чтобы дни читались визуально. Рисуются ПОСЛЕ столбиков (поверх них):
  // иначе непрозрачный столбик часа "00" (граница суток лежит на его левом крае)
  // закрашивал бы линию, и разделитель был бы виден только при пустом полуночном
  // бакете. hours при HOUR — исходный почасовой массив DATA.hours; новый день
  // начинается с часа "00". hi > 0: если данные начинаются ровно в полночь, линия
  // на левой рамке области не нужна.
  if (bucketSize === HOUR) {
    hours.forEach((h, hi) => {
      if (hi > 0 && isMidnight(h)) {
        const x = L + hi * (bw + gap);
        s += `<line class="dayline" x1="${x}" y1="${T}" x2="${x}" y2="${T + H}"/>`;
      }
    });
  }

  // подписи оси X — разрежённые, чтобы не наезжали друг на друга.
  // Последнюю пропускаем, если она не помещается целиком: обрезанный текст хуже отсутствующего.
  const tickStep = Math.max(1, Math.ceil(hours.length / Math.floor((W - L - R) / 78)));
  hours.forEach((h, hi) => {
    if (hi % tickStep) return;
    const x = L + hi * (bw + gap) + bw / 2;
    if (x + 36 > W) return;
    s += `<text class="tick" x="${x}" y="${T + H + 18}" text-anchor="middle">${bucketLabel(bucketSize, DATA.hours, hi, offset)}</text>`;
  });

  // Линия среднего идёт поверх столбиков, поэтому подпись ставим у правого края
  // и подкладываем плашку цветом поверхности — иначе текст читается по столбикам.
  // avgActive — среднее ЗА ЧАС, столбики при DAY/WEEK — суммы за бакет (24/168 часов):
  // на одной оси с ними почасовое среднее легло бы почти на дно графика, как будто
  // расход нулевой. Поэтому при бакетинге считаем среднее по самим бакетам (той же
  // размерности, что и столбики), а не пересчитываем avgActive обратно в часы —
  // средний размер активного бакета (последний может быть неполным) не совпадает
  // с bucketSize, так что домножение на bucketSize было бы неточным.
  // hours — забакеченный массив (длина = число бакетов), а знаменатель сессии
  // (sessionsPerHour) — сырой почасовой (длина H), поэтому в avgLineY идёт
  // DATA.hours, как и в altSeries выше; иначе bucketizeSeries считал бы границы
  // по длине бакетов и индексировал бы сырой массив неверно.
  const avgY = avgLineY(bucketSize, m, series, DATA.hours, offset, avgMode);
  const avgLabel = avgMode === 'session'
    ? `среднее на сессию за ${BUCKET_UNIT_LABEL[bucketSize]}`
    : `среднее за ${BUCKET_UNIT_LABEL[bucketSize]}`;
  const ya = y(avgY);
  const avgText = `${avgLabel} · ${compact(avgY)}`;
  const tw = avgText.length * 5.9 + 10;
  const tx = Math.max(W - R - tw, L + 2);
  s += `<line class="avgline" x1="${L}" x2="${W - R}" y1="${ya}" y2="${ya}"/>` +
       `<rect x="${tx}" y="${ya - 19}" width="${tw}" height="15" fill="${sc}" rx="3"/>` +
       `<text class="avglabel" x="${tx + 5}" y="${ya - 7}">${avgText}</text>`;
  s += `<line x1="${L}" x2="${W - R}" y1="${T + H}" y2="${T + H}" stroke="var(--axis)" stroke-width="1"/>`;

  const svg = document.getElementById('chart');
  svg.setAttribute('width', W);
  svg.setAttribute('height', T + H + B);
  svg.innerHTML = s;
}

function pie(d) {
  const tot = d.totals[unit];
  const total = tot.reduce((a, b) => a + b, 0);
  const rows = d.names.map((n, i) => [n, tot[i], i]).filter(r => r[1] > 0);
  const cx = 100, cy = 100, rOuter = 92, rInner = 54, gap = 1.6;

  document.getElementById('pieTitle').textContent = 'Расходы по измерению «' + dimLabel(dim) + '»';
  document.getElementById('pieSub').textContent =
    `${DATA.dateFrom} – ${DATA.dateTo} · всего ${exact(total)}`;
  document.getElementById('pieTotal').textContent = compact(total);

  const polar = (r, deg) => {
    const rad = deg * Math.PI / 180;
    return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
  };
  const donutPath = (rO, rI, a0, a1) => {
    const [x0, y0] = polar(rO, a1), [x1, y1] = polar(rO, a0);
    const [x2, y2] = polar(rI, a0), [x3, y3] = polar(rI, a1);
    const large = a1 - a0 <= 180 ? 0 : 1;
    return `M${x0},${y0} A${rO},${rO} 0 ${large} 0 ${x1},${y1} L${x2},${y2} A${rI},${rI} 0 ${large} 1 ${x3},${y3} Z`;
  };

  let angle = -90, s = '';
  rows.forEach(([n, v, i]) => {
    const sweep = total ? v / total * 360 : 0;
    const a0 = angle + gap / 2, a1 = angle + sweep - gap / 2;
    if (a1 > a0) {
      s += `<path class="pie-slice" d="${donutPath(rOuter, rInner, a0, a1)}" fill="${seriesColorAt(i)}"/>`;
      if (sweep >= 14) {
        const [lx, ly] = polar((rOuter + rInner) / 2, (a0 + a1) / 2);
        const pct = (v / total * 100).toFixed(1).replace('.', ',');
        s += `<text class="pie-slice-label" x="${lx}" y="${ly + 4}">${pct}%</text>`;
      }
    }
    angle += sweep;
  });
  document.getElementById('pie').innerHTML = s;

  document.getElementById('pieLegend').innerHTML = rows
    .sort((a, b) => b[1] - a[1])
    .map(([n, v, i]) =>
      `<li><span class="swatch" style="background:${seriesColorAt(i)}"></span>` +
      `<span class="name">${escapeHtml(n)}</span>` +
      `<span class="pct">${total ? (v / total * 100).toFixed(1).replace('.', ',') : '0,0'}%</span></li>`)
    .join('');
}

function table(d, m) {
  const main = d.totals[unit], other = d.totals[isCost() ? 'tokens' : 'cost'];
  // Сортируем по выбранной единице, но показываем обе — так видно, что миллиарды
  // токенов из подписки стоят $0, а скромный объём на opus стоит дорого.
  const rows = d.names.map((n, i) => [n, main[i], other[i], i])
    .filter(r => r[1] > 0 || r[2] > 0).sort((a, b) => b[1] - a[1] || b[2] - a[2]);
  const total = main.reduce((a, b) => a + b, 0);
  // Знаменатель столбца «За активный час»/«За сессию» — глобальный, как и раньше
  // (активные часы), но в режиме «сессия» — сессия-часы (Σ уникальных сессий по
  // активным часам), тот же знаменатель, что у линии среднего на графике и у
  // тайла «на сессию в активном часу» (avgSessionPerActiveHour).
  const denom = avgMode === 'session' ? Math.max(m.totalSessionHours, 1) : Math.max(m.activeHours, 1);
  const rateLabel = avgMode === 'session' ? 'За сессию' : 'За активный час';
  const otherLabel = isCost() ? 'Токенов' : 'Стоимость';
  const fmtOther = v => isCost() ? compactTok(v) : money2(v);
  // Ставка ($/Mtok) есть только у моделей — у инструмента/агента/проекта это смесь.
  const showRates = dim === 'model';

  // Столбцы компонентов (Вход/Выход/Кэш-запись/Кэш-чтение) показываем только когда
  // строки НЕ сами компоненты — иначе избыточно. В ячейке стоимость сверху, токены
  // снизу — независимо от переключателя unit.
  const showCompCols = dim !== 'components';
  // Объединяет 5 сырых компонентов [in, out, cc5m, cc1h, cr] в 4 столбца таблицы
  // (Вход/Выход/Кэш-запись/Кэш-чтение), склеивая cc5m+cc1h в один «Кэш-запись» —
  // общий шаг для одной серии (comp4) и для итоговой строки (tot4).
  const merge4 = ([in_, out_, cc5m, cc1h, cr]) => [in_, out_, [cc5m[0] + cc1h[0], cc5m[1] + cc1h[1]], cr];
  const comp4 = si => merge4([0, 1, 2, 3, 4].map(k => [d.compCost[si][k], d.compTok[si][k]]));
  const compCell = ([c, t]) =>
    `<td><div class="comp-cost">${money2(c)}</div><div class="comp-tok">${compactTok(t)}</div></td>`;

  const rateCell = n => {
    const rt = DATA.rates[n];
    if (!rt) return '<td class="rate-cell"><span class="comp-tok">—</span></td>';
    const r = v => '$' + (v * 1e6).toFixed(2).replace('.', ',');
    const cc = rt.cache_create_1h !== rt.cache_create
      ? r(rt.cache_create) + ' / ' + r(rt.cache_create_1h)
      : r(rt.cache_create);
    return `<td class="rate-cell" title="Вход / Выход / Кэш-запись / Кэш-чтение, $/Mtok">` +
      `<div class="comp-cost">${r(rt.input)}</div>` +
      `<div class="comp-tok">${r(rt.output)}</div>` +
      `<div class="comp-tok">${cc}</div>` +
      `<div class="comp-tok">${r(rt.cache_read)}</div></td>`;
  };

  const compHeaders = showCompCols
    ? ['Вход', 'Выход', 'Кэш-запись', 'Кэш-чтение'].map(h => `<th>${h}</th>`).join('')
    : '';
  const sumCol = k => [
    d.compCost.reduce((s, row) => s + row[k], 0),
    d.compTok.reduce((s, row) => s + row[k], 0),
  ];
  const tot4 = showCompCols ? merge4([0, 1, 2, 3, 4].map(sumCol)) : [];

  document.getElementById('table').innerHTML =
    `<thead><tr><th>${dimLabel(dim)}</th>` +
    `<th>${isCost() ? 'Стоимость' : 'Всего токенов'}</th><th>Доля</th>` +
    `<th>${rateLabel}</th><th>${otherLabel}</th>` +
    compHeaders + (showRates ? '<th>Ставка $/Mtok</th>' : '') +
    `</tr></thead><tbody>` +
    rows.map(([n, v, o, i]) =>
      `<tr><td><span class="name-cell"><span class="swatch" style="background:${seriesColorAt(i)}"></span>${escapeHtml(n)}</span></td>` +
      `<td>${exact(v)}</td>` +
      `<td>${total ? (v / total * 100).toFixed(1).replace('.', ',') : '0,0'}%</td>` +
      `<td>${exact(v / denom)}</td><td>${fmtOther(o)}</td>` +
      (showCompCols ? comp4(i).map(compCell).join('') : '') +
      (showRates ? rateCell(n) : '') +
      `</tr>`).join('') +
    `</tbody><tfoot><tr><td>Итого</td><td>${exact(total)}</td><td>100,0%</td>` +
    `<td>${exact(total / denom)}</td>` +
    `<td>${fmtOther(other.reduce((a, b) => a + b, 0))}</td>` +
    (showCompCols ? tot4.map(compCell).join('') : '') +
    (showRates ? '<td></td>' : '') +
    `</tr></tfoot>`;
}

let curD = null, curM = null, curBucket = null;
const tip = document.getElementById('tip');
document.getElementById('chart').addEventListener('mousemove', e => {
  const t = e.target.closest('rect[data-h]');
  if (!t || !curBucket) { tip.style.opacity = 0; return; }
  const hi = +t.dataset.h, b = curBucket;
  const cells = b.grid[hi].map((v, i) => [b.names[i], v, i])
    .filter(r => r[1] > 0).sort((a, b) => b[1] - a[1]);
  const alt = b.altSeries[hi];
  tip.innerHTML = `<b>${bucketLabel(b.bucketSize, DATA.hours, hi, b.offset)}</b>` +
    `<div class="row"><span>всего</span><span>${exact(b.series[hi])}</span></div>` +
    `<div class="row" style="opacity:.65"><span>${isCost() ? 'токенов' : 'стоимость'}</span>` +
    `<span>${isCost() ? compactTok(alt) : money2(alt)}</span></div>` +
    cells.map(([n, v, i]) =>
      `<div class="row"><span><span class="swatch" style="display:inline-block;background:${seriesColorAt(i)}"></span> ${escapeHtml(n)}</span>` +
      `<span>${exact(v)}</span></div>`).join('');
  tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(e.clientX + 14, innerWidth - r.width - 10) + 'px';
  tip.style.top = Math.max(e.clientY - r.height - 12, 8) + 'px';
});
document.getElementById('chart').addEventListener('mouseleave', () => tip.style.opacity = 0);

function render() {
  document.querySelectorAll('#controls button[data-dim]').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.dim === dim)));
  document.querySelectorAll('#controls button[data-unit]').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.unit === unit)));

  const idxs = filteredIndexes();
  const d = aggregateByDim(idxs, dim);
  const m = computeMetrics(idxs);
  curD = d; curM = m;

  tiles(m); legend(d); chart(d, m); pie(d); table(d, m);
  updateFilterCount(idxs);
  document.getElementById('foot').textContent = d.otherCount
    ? `«Прочее» объединяет ещё ${d.otherCount} значений измерения «${dimLabel(dim)}».` : '';
}

document.getElementById('subtitle').textContent =
  `${DATA.dateFrom} — ${DATA.dateTo} · локальное время · источники: Claude Code, Codex`;
controls(); renderFilterGroups(); render();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render);

// Ширина столбиков графика зависит от ширины контейнера — при ресайзе окна
// пересчитываем геометрию, иначе после первого рендера она «застынет».
let resizeTimer;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (curD && curM) chart(curD, curM); }, 120);
});
</script>
</body>
</html>
"""


def render_html(payload: dict) -> str:
    return TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description="Почасовой дашборд расхода токенов")
    ap.add_argument("--since", help="дата начала, YYYY-MM-DD или YYYYMMDD")
    ap.add_argument("--until", help="дата конца, включительно")
    ap.add_argument("--tool", choices=["claude", "codex"], action="append")
    ap.add_argument("--out", default="dashboard.html", help="куда сохранить HTML")
    ap.add_argument("--open", action="store_true", help="открыть в браузере")
    args = ap.parse_args()

    rows = collect(
        since=normalize_date(args.since),
        until=normalize_date(args.until),
        tools=args.tool or ("claude", "codex"),
    )
    if not rows:
        print("За указанный период записей не найдено — дашборд не создан.")
        print("Проверьте --since/--until или наличие логов в ~/.claude/projects и ~/.codex/sessions.")
        raise SystemExit(1)

    rates = load_rates()
    if not rates:
        print("Ставки не найдены — стоимость будет нулевой.")
        print("Соберите их один раз: ./pricing.py --refresh")

    out = Path(args.out).expanduser().resolve()
    payload = build_payload(rows, rates)
    out.write_text(render_html(payload), encoding="utf-8")

    # raw не хранит готовую сумму cost/tokens по ячейке (выводима из 5
    # компонентов, JS считает её один раз на клиенте) — здесь для сводки в
    # терминал складываем компоненты тем же способом.
    raw = payload["raw"]
    tok_cols = [raw[k] for k in COMPONENT_KEYS]
    cost_cols = [raw[cost_key(k)] for k in COMPONENT_KEYS]
    tokens_per_cell = [sum(col[i] for col in tok_cols) for i in range(len(raw["hourIdx"]))]
    hours_with_data = {raw["hourIdx"][i] for i, tok in enumerate(tokens_per_cell) if tok > 0}
    total_cost = sum(sum(col) for col in cost_cols)

    print(f"{out}")
    print(
        f"записей: {len(rows):,} · активных часов: {len(hours_with_data)} · "
        f"стоимость: ${total_cost:,.2f}"
    )
    if payload["unpriced"]["tokens"]:
        print(
            f"без прайсинга: {payload['unpriced']['tokens']:,} токенов "
            f"({', '.join(payload['unpriced']['models'])})"
        )
    if args.open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
