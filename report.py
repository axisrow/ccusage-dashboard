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
MAX_SERIES = len(PALETTE)

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


def build_dimension(rows: list[Row], key, hours: list[str], cost_of) -> dict:
    """
    Свести записи в {серии, матрица час x серия} сразу в двух единицах.

    Порядок серий определяется по СТОИМОСТИ, а не по токенам: модель из подписки
    может дать миллиарды токенов при нулевой цене, и сортировка по объёму
    вытолкнула бы наверх то, что ничего не стоит.
    """
    cost_totals: dict[str, float] = defaultdict(float)
    token_totals: dict[str, int] = defaultdict(int)
    for r in rows:
        name = key(r)
        cost_totals[name] += cost_of(r)
        token_totals[name] += r.total

    ordered = sorted(
        token_totals, key=lambda n: (-cost_totals[n], -token_totals[n])
    )
    top = ordered[:MAX_SERIES]
    has_other = len(ordered) > MAX_SERIES
    names = top + (["Прочее"] if has_other else [])
    index = {name: i for i, name in enumerate(top)}
    other_i = len(top) if has_other else None

    grid_cost = {h: [0.0] * len(names) for h in hours}
    grid_tok = {h: [0] * len(names) for h in hours}
    for r in rows:
        i = index.get(key(r), other_i)
        if i is None or r.hour not in grid_cost:
            continue
        grid_cost[r.hour][i] += cost_of(r)
        grid_tok[r.hour][i] += r.total

    # Итоги по сериям — из уже посчитанных cost_totals/token_totals, а не повторным
    # суммированием grid: «Прочее» получает остаток по разности с топ-N.
    series_cost = [cost_totals[n] for n in top]
    series_tok = [token_totals[n] for n in top]
    if has_other:
        series_cost.append(sum(cost_totals[n] for n in ordered[MAX_SERIES:]))
        series_tok.append(sum(token_totals[n] for n in ordered[MAX_SERIES:]))

    return {
        "names": names,
        "grid": {
            "cost": [[round(v, 6) for v in grid_cost[h]] for h in hours],
            "tokens": [grid_tok[h] for h in hours],
        },
        "totals": {
            "cost": [round(v, 6) for v in series_cost],
            "tokens": series_tok,
        },
        "otherCount": len(ordered) - MAX_SERIES if has_other else 0,
    }


def build_payload(rows: list[Row], rates: dict) -> dict:
    hours_present = sorted({r.hour for r in rows})
    hours = hour_range(hours_present[0], hours_present[-1])

    def cost_of(r: Row) -> float:
        rate = rates.get(r.model)
        if rate is None:
            return 0.0
        return rate.cost(r.input, r.output, r.cache_create, r.cache_read, r.cache_create_1h)

    # Модели без ставки не считаются бесплатными — их объём выносится отдельно,
    # чтобы «$0» нельзя было спутать с «цена неизвестна».
    unpriced_models: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.model not in rates:
            unpriced_models[r.model] += r.total

    cost_hour: dict[str, float] = defaultdict(float)
    tok_hour: dict[str, int] = defaultdict(int)
    for r in rows:
        cost_hour[r.hour] += cost_of(r)
        tok_hour[r.hour] += r.total

    by_hour = {
        "cost": [round(cost_hour.get(h, 0.0), 6) for h in hours],
        "tokens": [tok_hour.get(h, 0) for h in hours],
    }

    # «Активный час» определяется по расходу токенов: час, в котором работа шла,
    # но вся она пришлась на модель из подписки, всё равно активен.
    active_idx = [i for i, v in enumerate(by_hour["tokens"]) if v > 0]
    n_active = len(active_idx) or 1

    metrics = {}
    for unit in ("cost", "tokens"):
        series = by_hour[unit]
        grand = sum(series)
        peak = max(series) if series else 0
        metrics[unit] = {
            "grand": round(grand, 6) if unit == "cost" else grand,
            "avgActive": round(grand / n_active, 6) if unit == "cost" else grand // n_active,
            "avgCalendar": round(grand / len(hours), 6) if unit == "cost" else grand // len(hours),
            "peak": round(peak, 6) if unit == "cost" else peak,
            "peakHour": hours[series.index(peak)] if series else "",
        }

    return {
        "hours": hours,
        "byHour": by_hour,
        "metrics": metrics,
        "activeHours": len(active_idx),
        "calendarHours": len(hours),
        "dateFrom": min(r.date for r in rows),
        "dateTo": max(r.date for r in rows),
        "records": len(rows),
        "unpriced": {
            "tokens": sum(unpriced_models.values()),
            "models": sorted(unpriced_models, key=lambda m: -unpriced_models[m])[:5],
        },
        "subscription": sorted(
            {m for m, rt in rates.items() if rt.source == "subscription"}
        ),
        "hasCodex": any(r.tool == "codex" for r in rows),
        "dimensions": {
            dim: build_dimension(rows, key, hours, cost_of)
            for dim, (_, key) in DIMENSIONS.items()
        },
        "dimLabels": {dim: label for dim, (label, _) in DIMENSIONS.items()},
        "palette": PALETTE,
        "otherColor": OTHER_COLOR,
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
  .avgline { stroke: var(--ink-2); stroke-width: 2; stroke-dasharray: 5 4; }
  .avglabel { fill: var(--ink-2); font-size: 11px; font-weight: 500; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--grid); }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  td { font-variant-numeric: tabular-nums; }
  .name-cell { display: flex; align-items: center; gap: 8px; }
  tfoot td { font-weight: 600; border-bottom: none; }
  .tip {
    position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
    background: var(--ink); color: var(--surface); padding: 8px 11px;
    border-radius: 7px; font-size: 12px; line-height: 1.45; z-index: 9;
    box-shadow: 0 4px 14px rgba(0,0,0,.18); max-width: 280px;
  }
  .tip b { font-weight: 600; }
  .tip .row { display: flex; justify-content: space-between; gap: 14px; font-variant-numeric: tabular-nums; }
  .foot { color: var(--muted); font-size: 12px; margin-top: 18px; }
</style>
</head>
<body>
<h1>Расход по часам</h1>
<div class="sub" id="subtitle"></div>

<div class="card">
  <div class="tiles" id="tiles"></div>
  <div class="tile-note" id="caveat" style="margin-top:14px"></div>
</div>

<div class="card">
  <div class="controls" id="controls"></div>
  <ul class="legend" id="legend"></ul>
  <div class="chart-wrap"><svg id="chart"></svg></div>
</div>

<div class="card">
  <table id="table"></table>
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

let dim = 'tool';
let unit = 'cost';                       // cost | tokens

const isCost = () => unit === 'cost';
// Компактная форма для осей и плиток, точная — для таблицы и тултипа
const compact = v => isCost() ? money(v) : compactTok(v);
const exact = v => isCost()
  ? '$' + v.toFixed(2).replace('.', ',')
  : fmtInt(Math.round(v));

function tiles() {
  const m = DATA.metrics[unit];
  const what = isCost() ? 'Расход' : 'Токены';
  const items = [
    [what + ' в среднем за активный час', compact(m.avgActive),
     DATA.activeHours + ' активных часов из ' + DATA.calendarHours],
    ['В среднем за календарный час', compact(m.avgCalendar), 'с учётом простоев'],
    ['Пик за час', compact(m.peak), m.peakHour ? hourLabel(m.peakHour) : ''],
    [isCost() ? 'Всего' : 'Всего токенов', compact(m.grand),
     fmtInt(DATA.records) + ' записей'],
  ];
  document.getElementById('tiles').innerHTML = items.map(([l, v, n]) =>
    `<div><div class="tile-label">${l}</div><div class="tile-value">${v}</div>` +
    `<div class="tile-note">${n}</div></div>`).join('');

  // Что именно не попало в сумму денег — «$0» не должно читаться как «бесплатно»
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
    '<span style="flex:1"></span>' +
    '<span style="color:var(--muted);font-size:12px">Единицы:</span>' +
    `<button data-unit="cost" aria-pressed="${unit === 'cost'}">$</button>` +
    `<button data-unit="tokens" aria-pressed="${unit === 'tokens'}">токены</button>`;
  document.querySelectorAll('#controls button[data-dim]').forEach(b =>
    b.onclick = () => { dim = b.dataset.dim; render(); });
  document.querySelectorAll('#controls button[data-unit]').forEach(b =>
    b.onclick = () => { unit = b.dataset.unit; render(); });
}

function legend() {
  const d = DATA.dimensions[dim], tot = d.totals[unit];
  // При единственной серии легенда не нужна — её называет заголовок и таблица.
  const shown = d.names.filter((n, i) => tot[i] > 0);
  document.getElementById('legend').innerHTML = shown.length < 2 ? '' :
    d.names.map((n, i) => tot[i] > 0
      ? `<li><span class="swatch" style="background:${colorAt(i)}"></span>${n}</li>` : '').join('');
}

function chart() {
  const d = DATA.dimensions[dim], grid = d.grid[unit];
  const hours = DATA.hours;
  const series = DATA.byHour[unit];
  const bw = hours.length > 400 ? 3 : hours.length > 160 ? 6 : hours.length > 60 ? 11 : 20;
  const gap = bw > 6 ? 2 : 1;               // 2px surface gap; на узких столбиках 1px
  const L = 62, R = 44, T = 12, B = 46, H = 300;   // R с запасом под последнюю подпись оси
  const W = L + R + hours.length * (bw + gap);
  const max = Math.max(...series, isCost() ? 0.01 : 1);

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
    grid[hi].forEach((v, si) => {
      if (v <= 0) return;
      const y0 = y(acc + v), y1 = y(acc);
      const hgt = Math.max(y1 - y0, .6);
      s += `<rect x="${x}" y="${y0}" width="${bw}" height="${hgt}" fill="${colorAt(si)}" ` +
           `rx="${bw >= 8 ? 2 : 1}" data-h="${hi}" data-s="${si}"/>`;
      // 2px разделитель цветом поверхности между сегментами стека
      if (acc > 0) s += `<rect x="${x}" y="${y1 - 1}" width="${bw}" height="2" fill="${sc}"/>`;
      acc += v;
    });
  });

  // подписи оси X — разрежённые, чтобы не наезжали друг на друга.
  // Последнюю пропускаем, если она не помещается целиком: обрезанный текст хуже отсутствующего.
  const step = Math.max(1, Math.ceil(hours.length / Math.floor((W - L - R) / 78)));
  hours.forEach((h, hi) => {
    if (hi % step) return;
    const x = L + hi * (bw + gap) + bw / 2;
    if (x + 36 > W) return;
    s += `<text class="tick" x="${x}" y="${T + H + 18}" text-anchor="middle">${hourLabel(h)}</text>`;
  });

  // Линия среднего идёт поверх столбиков, поэтому подпись ставим у правого края
  // и подкладываем плашку цветом поверхности — иначе текст читается по столбикам.
  const ya = y(DATA.metrics[unit].avgActive);
  const avgText = `среднее за активный час · ${compact(DATA.metrics[unit].avgActive)}`;
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

function table() {
  const d = DATA.dimensions[dim];
  const main = d.totals[unit], other = d.totals[isCost() ? 'tokens' : 'cost'];
  // Сортируем по выбранной единице, но показываем обе — так видно, что миллиарды
  // токенов из подписки стоят $0, а скромный объём на opus стоит дорого.
  const rows = d.names.map((n, i) => [n, main[i], other[i], i])
    .filter(r => r[1] > 0 || r[2] > 0).sort((a, b) => b[1] - a[1] || b[2] - a[2]);
  const total = main.reduce((a, b) => a + b, 0);
  const ah = Math.max(DATA.activeHours, 1);
  const otherLabel = isCost() ? 'Токенов' : 'Стоимость';
  const fmtOther = v => isCost() ? compactTok(v) : '$' + v.toFixed(2).replace('.', ',');

  document.getElementById('table').innerHTML =
    `<thead><tr><th>${DATA.dimLabels[dim]}</th>` +
    `<th>${isCost() ? 'Стоимость' : 'Всего токенов'}</th><th>Доля</th>` +
    `<th>За активный час</th><th>${otherLabel}</th></tr></thead><tbody>` +
    rows.map(([n, v, o, i]) =>
      `<tr><td><span class="name-cell"><span class="swatch" style="background:${colorAt(i)}"></span>${n}</span></td>` +
      `<td>${exact(v)}</td>` +
      `<td>${total ? (v / total * 100).toFixed(1).replace('.', ',') : '0,0'}%</td>` +
      `<td>${exact(v / ah)}</td><td>${fmtOther(o)}</td></tr>`).join('') +
    `</tbody><tfoot><tr><td>Итого</td><td>${exact(total)}</td><td>100,0%</td>` +
    `<td>${exact(total / ah)}</td>` +
    `<td>${fmtOther(other.reduce((a, b) => a + b, 0))}</td></tr></tfoot>`;
}

const tip = document.getElementById('tip');
document.getElementById('chart').addEventListener('mousemove', e => {
  const t = e.target.closest('rect[data-h]');
  if (!t) { tip.style.opacity = 0; return; }
  const hi = +t.dataset.h, d = DATA.dimensions[dim];
  const cells = d.grid[unit][hi].map((v, i) => [d.names[i], v, i])
    .filter(r => r[1] > 0).sort((a, b) => b[1] - a[1]);
  const alt = DATA.byHour[isCost() ? 'tokens' : 'cost'][hi];
  tip.innerHTML = `<b>${hourLabel(DATA.hours[hi])}</b>` +
    `<div class="row"><span>всего</span><span>${exact(DATA.byHour[unit][hi])}</span></div>` +
    `<div class="row" style="opacity:.65"><span>${isCost() ? 'токенов' : 'стоимость'}</span>` +
    `<span>${isCost() ? compactTok(alt) : '$' + alt.toFixed(2).replace('.', ',')}</span></div>` +
    cells.map(([n, v, i]) =>
      `<div class="row"><span><span class="swatch" style="display:inline-block;background:${colorAt(i)}"></span> ${n}</span>` +
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
  tiles(); legend(); chart(); table();
  const d = DATA.dimensions[dim];
  document.getElementById('foot').textContent = d.otherCount
    ? `«Прочее» объединяет ещё ${d.otherCount} значений измерения «${DATA.dimLabels[dim]}».` : '';
}

document.getElementById('subtitle').textContent =
  `${DATA.dateFrom} — ${DATA.dateTo} · локальное время · источники: Claude Code, Codex`;
controls(); render();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render);
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

    print(f"{out}")
    print(
        f"записей: {len(rows):,} · активных часов: {payload['activeHours']} · "
        f"стоимость: ${payload['metrics']['cost']['grand']:,.2f}"
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
