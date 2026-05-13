#!/usr/bin/env python3
"""
compute_averages.py
Усредняет метрики по 10 запускам с разными сидами.

Для каждой папки results/{map}/{algo}/{N}ag/:
  • читает все seed*.xlsx (1 строка данных каждый)
  • вычисляет среднее числовых метрик по сидам
  • сохраняет avg_summary.xlsx в той же папке

Формат avg_summary.xlsx:
  Строка 1: AVERAGE — средние значения (n_seed_runs = кол-во прочитанных файлов)
  Строки 2+: все seed-строки для справки

Запуск:
    python compute_averages.py results/
    python compute_averages.py results/ --map map1
    python compute_averages.py results/ --map map1 --algo cbs
    python compute_averages.py results/ --force     # перезаписать существующие
"""
from __future__ import annotations
import sys, argparse
from pathlib import Path
from typing import List


def average_folder(agents_dir: Path, force: bool = False,
                   verbose: bool = True) -> bool:
    """
    Усредняет seed*.xlsx в указанной папке {map}/{algo}/{N}ag/.
    Возвращает True если файл создан/обновлён.
    """
    try:
        import pandas as pd
    except ImportError:
        print("❌ pandas не установлен: pip install pandas openpyxl")
        sys.exit(1)

    seed_files = sorted(agents_dir.glob("seed*.xlsx"))
    if not seed_files:
        return False

    out_path = agents_dir / "avg_summary.xlsx"
    if out_path.exists() and not force:
        if verbose:
            print(f"  ⏭  {_rel(agents_dir, 3)} — уже существует ({len(seed_files)} сидов)")
        return False

    # Читаем все seed-файлы
    dfs: List = []
    for f in seed_files:
        try:
            df = pd.read_excel(f, engine='openpyxl')
            df['seed_file'] = f.name
            dfs.append(df)
        except Exception as e:
            if verbose:
                print(f"    ⚠️  Не удалось прочитать {f.name}: {e}")

    if not dfs:
        return False

    combined = pd.concat(dfs, ignore_index=True)

    # Определяем столбцы
    str_cols = combined.select_dtypes(exclude='number').columns.tolist()
    num_cols = combined.select_dtypes(include='number').columns.tolist()

    # Столбцы которые не имеет смысла усреднять — берём из первой строки
    _no_avg = {'order_seed', 'seed'}

    avg_row = {}
    for col in str_cols:
        avg_row[col] = ('AVERAGE' if col == 'seed_file'
                        else combined[col].iloc[0])
    for col in num_cols:
        if col in _no_avg:
            avg_row[col] = -1   # маркер "н/п"
        else:
            avg_row[col] = round(float(combined[col].mean()), 4)

    avg_row['n_seed_runs'] = len(dfs)
    if 'n_seed_runs' not in combined.columns:
        combined['n_seed_runs'] = 1

    # Итоговый DataFrame: средняя строка + все seed-строки
    avg_df   = pd.DataFrame([avg_row])
    full_df  = pd.concat([avg_df, combined], ignore_index=True)

    # Стилизованный Excel
    _save_styled(full_df, out_path, n_seeds=len(dfs))

    if verbose:
        summary = avg_row
        print(f"  ✓  {_rel(agents_dir, 3)}"
              f"  ({len(dfs)} сидов)"
              f"  avg_ticks={summary.get('total_ticks','?')}"
              f"  deadlocks={summary.get('deadlock_count','?')}")
    return True


def _rel(path: Path, levels: int) -> str:
    """Возвращает последние `levels` компонентов пути для краткого отображения."""
    parts = path.parts
    return "/".join(parts[-levels:]) if len(parts) >= levels else str(path)


def _save_styled(df, out_path: Path, n_seeds: int):
    """Сохраняет DataFrame в Excel с минимальным оформлением."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        # Fallback: просто pandas
        df.to_excel(str(out_path), index=False)
        return

    wb  = Workbook()
    ws  = wb.active
    ws.title = "avg_summary"

    BLUE  = 'FF1F4E79'; WHITE = 'FFFFFFFF'
    GREEN = 'FFE2EFDA'; ALT   = 'FFF2F7FB'

    hf  = Font(name='Arial', bold=True, size=10, color=WHITE)
    hfill = PatternFill('solid', start_color=BLUE, fgColor=BLUE)
    ca  = Alignment(horizontal='center')
    la  = Alignment(horizontal='left')

    cols = list(df.columns)
    # Заголовок
    for ci, col in enumerate(cols, 1):
        c = ws.cell(1, ci, col)
        c.font = hf; c.fill = hfill; c.alignment = ca
        ws.column_dimensions[get_column_letter(ci)].width = max(14, len(col) + 2)

    # Данные
    for ri, (_, row) in enumerate(df.iterrows(), 2):
        is_avg = str(row.get('seed_file', '')).startswith('AVERAGE')
        bg     = GREEN if is_avg else (ALT if ri % 2 == 0 else WHITE)
        fill   = PatternFill('solid', start_color=bg, fgColor=bg)
        for ci, col in enumerate(cols, 1):
            val = row[col]
            # NaN → пустая строка
            import math
            if isinstance(val, float) and math.isnan(val):
                val = ''
            c = ws.cell(ri, ci, val)
            c.fill = fill
            c.font = Font(name='Arial', size=10,
                          bold=is_avg, color=BLUE if is_avg else '000000')
            c.alignment = ca

    wb.save(str(out_path))


def main():
    parser = argparse.ArgumentParser(
        description='Усредняет метрики по сидам для каждой группы {map}/{algo}/{N}ag/')
    parser.add_argument('results_dir',
                        help='Папка с результатами (напр. results/)')
    parser.add_argument('--map',   metavar='NAME',
                        help='Фильтр по имени карты')
    parser.add_argument('--algo',  metavar='ALGO',
                        help='Фильтр по алгоритму')
    parser.add_argument('--agents', metavar='N', type=int,
                        help='Фильтр по числу агентов (напр. 20)')
    parser.add_argument('--force', action='store_true',
                        help='Перезаписать существующие avg_summary.xlsx')
    parser.add_argument('--quiet', action='store_true',
                        help='Минимальный вывод')
    args = parser.parse_args()

    results = Path(args.results_dir)
    if not results.is_dir():
        print(f"❌ Папка не найдена: {results}"); sys.exit(1)

    created = 0; skipped = 0; total_maps = 0

    for map_dir in sorted(results.iterdir()):
        if not map_dir.is_dir(): continue
        if args.map and map_dir.name != args.map: continue
        total_maps += 1

        if not args.quiet:
            print(f"\n📦 Карта: {map_dir.name}")

        for algo_dir in sorted(map_dir.iterdir()):
            if not algo_dir.is_dir(): continue
            if args.algo and algo_dir.name != args.algo: continue

            if not args.quiet:
                print(f"  🔀 {algo_dir.name}")

            for agents_dir in sorted(algo_dir.iterdir()):
                if not agents_dir.is_dir(): continue
                if not agents_dir.name.endswith('ag'): continue
                if args.agents:
                    try:
                        n = int(agents_dir.name.replace('ag', ''))
                        if n != args.agents: continue
                    except ValueError: continue

                ok = average_folder(agents_dir, force=args.force,
                                    verbose=not args.quiet)
                if ok:   created += 1
                else:    skipped += 1

    print(f"\n{'═'*50}")
    print(f"  Карт обработано:    {total_maps}")
    print(f"  avg_summary создано: {created}")
    print(f"  Пропущено:          {skipped}")
    print(f"{'═'*50}")


if __name__ == '__main__':
    main()