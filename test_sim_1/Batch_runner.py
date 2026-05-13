#!/usr/bin/env python3
"""
batch_runner.py
Пакетное тестирование алгоритмов симуляции.

Запуск:
  python batch_runner.py                   # путь спросит интерактивно
  python batch_runner.py --dry-run         # показать план
  python batch_runner.py --test            # один запуск для проверки
  python batch_runner.py --algo cbs --agents 5
"""
from __future__ import annotations
import sys, os, json, time, traceback, argparse
from pathlib import Path
from typing import List

ALGORITHMS   = ["prioritized_astar", "cbs", "lns1_cbs", "lns2_simple"]
AGENT_COUNTS = [5, 10, 15, 20, 25, 30]
SEEDS        = [0, 111, 222, 333]
SHELF_SEED   = 42
NUM_ORDERS   = 25
MAX_SPAWN_T  = 4_000
MAX_TICKS    = 20_000


def _resolve_maps_folder(cli_parts: list) -> str:
    if cli_parts:
        p = ' '.join(cli_parts).strip().strip('"').strip("'")
        if p:
            return p
    cfg = Path(__file__).parent / 'maps_path.txt'
    if cfg.exists():
        p = cfg.read_text(encoding='utf-8').strip().strip('"').strip("'")
        if p:
            print(f"  Путь из maps_path.txt: {p}")
            return p
    print("\n  Укажите папку с картами (или создайте maps_path.txt):")
    p = input("  > ").strip().strip('"').strip("'")
    if not p:
        print("❌ Путь не указан."); sys.exit(1)
    return p


def scan_maps(folder: str) -> List[Path]:
    """Только .xlsx файлы в корне папки, без подпапок."""
    p = Path(folder)
    if not p.is_dir():
        raise FileNotFoundError(f"Папка не найдена: {folder}")
    return sorted(f for f in p.iterdir()
                  if f.is_file() and f.suffix.lower() == '.xlsx')


def run_one(excel_map: str, algorithm: str, num_agents: int,
            order_seed: int, output_dir: Path, map_name: str = '') -> str:
    """Один запуск симуляции без GUI. Возвращает путь к xlsx с метриками."""
    import import_module as im_mod
    from inventory_manager import InventoryManager
    from dispatcher        import Dispatcher, SubOrderStatus
    from agent_module      import AgentManager
    from router_interface  import RouterContext, RoutingAlgorithmRegistry
    from metrics_collector import MetricsCollector

    # ── Импорт карты ─────────────────────────────────────────────────────────
    importer = im_mod.MapImporter(
        excel_path=excel_map,
        auto_fix=True,
        output_json=None)       # не сохраняем json в batch-режиме
    res = importer.import_map()
    if not res.success:
        raise RuntimeError(f"Ошибка карты: {res.errors}")

    cells = res.map_data['cells']

    # ── Объекты карты ─────────────────────────────────────────────────────────
    shelf_positions, packer_positions, packer_ids = [], {}, []
    packer_slots, packer_delivery_zones = {}, {}
    for row in cells:
        for c in row:
            t = c['type']
            if t == 'S':
                shelf_positions.append((c['row'], c['col']))
            elif t == 'P':
                parts = c['text'].split('|')
                try:
                    lg  = json.loads(parts[3]) if len(parts) > 3 else {}
                    pid = lg.get('packer_id')
                    if pid is not None:
                        packer_ids.append(pid)
                        packer_positions[pid] = (c['row'], c['col'])
                except Exception: pass
            elif t == 'Z':
                parts = c['text'].split('|')
                try:
                    lg   = json.loads(parts[3]) if len(parts) > 3 else {}
                    pid  = lg.get('packer_id')
                    zone = lg.get('zone', 'order')
                    slot = lg.get('slot')
                    if pid is not None:
                        if zone == 'order':
                            packer_slots[pid] = packer_slots.get(pid, 0) + 1
                        if slot == 1:
                            packer_delivery_zones[pid] = (c['row'], c['col'])
                except Exception: pass

    config = dict(
        map_name    = map_name or Path(excel_map).stem,
        num_agents  = num_agents,
        order_seed  = order_seed,
        shelf_seed  = SHELF_SEED,
        num_orders  = NUM_ORDERS,
        max_spawn_tick = MAX_SPAWN_T,
        router_algorithm = algorithm)

    # ── Подсистемы ────────────────────────────────────────────────────────────
    inventory  = InventoryManager(SHELF_SEED, shelf_positions)
    dispatcher = Dispatcher(
        order_seed=order_seed, inventory_manager=inventory,
        packer_ids=packer_ids or [1],
        packer_slots=packer_slots or None,
        high_priority_threshold=8,
        num_orders=NUM_ORDERS, max_spawn_tick=MAX_SPAWN_T)

    algo_obj = RoutingAlgorithmRegistry.create(algorithm, res.map_data)
    router   = RouterContext(algo_obj)
    router.set_map_data(res.map_data)

    metrics = MetricsCollector(algorithm_name=algorithm, sim_params=config)
    metrics.on_sim_start(0)
    algo_obj._metrics = metrics

    agent_manager = AgentManager(
        env=None, router=router, map_data=res.map_data,
        dispatcher=dispatcher, inventory=inventory,
        packer_positions=packer_positions,
        packer_delivery_zones=packer_delivery_zones,
        num_agents=num_agents, metrics=metrics)

    # ── Главный цикл (без GUI) ────────────────────────────────────────────────
    tick = 0
    while tick < MAX_TICKS:
        dispatcher.step(tick)
        agent_manager.run_tick()

        all_orders_spawned = (tick >= MAX_SPAWN_T or
                              len(dispatcher.orders) >= NUM_ORDERS)
        if all_orders_spawned and len(dispatcher.orders) > 0:
            # Защита от пустых suborders — хотя бы один подзаказ должен существовать
            all_subs = [sub for order in dispatcher.orders.values()
                        for sub in order.suborders]
            if all_subs and all(sub.status == SubOrderStatus.COMPLETED
                                for sub in all_subs):
                break
        tick += 1

    metrics.on_sim_end(tick)
    seed_label = f"seed{order_seed:03d}"
    fpath = metrics.save_to_excel_simple(str(output_dir), filename=seed_label)
    if not fpath:
        raise RuntimeError("save_to_excel_simple вернул пустую строку — "
                           "возможно, openpyxl не установлен")
    return fpath


def _check_imports(verbose: bool = False):
    """Проверяет что все нужные модули импортируются без ошибок."""
    problems = []
    for mod in ['import_module', 'inventory_manager', 'dispatcher',
                'agent_module', 'router_interface', 'metrics_collector',
                'deadlock_module']:
        try:
            __import__(mod)
            if verbose: print(f"  ✓  {mod}")
        except Exception as e:
            problems.append(f"{mod}: {e}")
            print(f"  ❌ {mod}: {e}")
    if problems:
        print(f"\n❌ Ошибки импорта ({len(problems)}). "
              "Запустите из папки проекта.")
        sys.exit(1)
    # Проверяем openpyxl
    try:
        import openpyxl
        if verbose: print("  ✓  openpyxl")
    except ImportError:
        print("  ❌ openpyxl не установлен: pip install openpyxl")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description='Пакетное тестирование алгоритмов',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
БЫСТРЫЙ СТАРТ:
  python batch_runner.py                         # путь спросит
  python batch_runner.py --test                  # тест 1 запуска
  python batch_runner.py --dry-run               # план без запуска
  python batch_runner.py --algo cbs --agents 5   # ограниченный прогон

ПУТЬ С ПРОБЕЛАМИ — создайте maps_path.txt рядом со скриптом:
  A:\\projects in programming\\python\\maps\\
""")
    parser.add_argument('path_parts', nargs='*', metavar='PATH',
                        help='Папка с картами (части склеиваются через пробел)')
    parser.add_argument('--output',   default='results')
    parser.add_argument('--dry-run',  action='store_true')
    parser.add_argument('--test',     action='store_true',
                        help='Один тестовый запуск: первая карта, prioritized_astar, '
                             '5 агентов, seed 0. Показывает полный traceback при ошибке.')
    parser.add_argument('--algo',   nargs='+', choices=ALGORITHMS)
    parser.add_argument('--agents', nargs='+', type=int)
    parser.add_argument('--seeds',  nargs='+', type=int)
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()

    # ── Проверка импортов ─────────────────────────────────────────────────────
    print("\n  Проверка модулей...")
    _check_imports(verbose=args.test)

    maps_path = _resolve_maps_folder(args.path_parts)
    try:
        maps = scan_maps(maps_path)
    except FileNotFoundError as e:
        print(f"❌ {e}"); sys.exit(1)

    if not maps:
        print(f"❌ xlsx-файлы не найдены в: {maps_path}"); sys.exit(1)

    algos  = args.algo   or ALGORITHMS
    agents = args.agents or AGENT_COUNTS
    seeds  = args.seeds  or SEEDS
    skip   = not args.overwrite
    output = Path(args.output)

    total = len(maps) * len(algos) * len(agents) * len(seeds)

    print(f"\n{'═'*62}")
    print(f"  ПАКЕТНЫЙ ЗАПУСК СИМУЛЯЦИЙ")
    print(f"{'─'*62}")
    print(f"  Папка карт:     {maps_path}")
    print(f"  Карт:           {len(maps)}  {[m.name for m in maps]}")
    print(f"  Алгоритмов:     {len(algos)}  {algos}")
    print(f"  Наборы агентов: {len(agents)}  {agents}")
    print(f"  Сидов:          {len(seeds)}")
    print(f"  Итого запусков: {total}")
    print(f"  Результаты:     {output.absolute()}")
    print(f"{'═'*62}\n")

    # ── Режим --test: один запуск с полным выводом ────────────────────────────
    if args.test:
        map_path = maps[0]
        algo     = algos[0]
        n_ag     = agents[0]
        seed     = seeds[0]
        out_dir  = output / map_path.stem / algo / f"{n_ag}ag"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"  ТЕСТ: {map_path.name} | {algo} | {n_ag}ag | seed{seed:03d}\n")
        try:
            t0   = time.time()
            fp   = run_one(str(map_path), algo, n_ag, seed, out_dir, map_path.stem)
            dt   = time.time() - t0
            print(f"\n  ✅ Успешно за {dt:.1f}с")
            print(f"  Файл: {fp}")
        except Exception:
            print("\n  ❌ ОШИБКА:")
            traceback.print_exc()
        return

    # ── Режим --dry-run ───────────────────────────────────────────────────────
    if args.dry_run:
        for m in maps:
            for a in algos:
                for n in agents:
                    for s in seeds:
                        out_d  = output / m.stem / a / f"{n}ag"
                        exists = (out_d / f"seed{s:03d}.xlsx").exists()
                        mark   = "✓" if exists else "·"
                        print(f"  [{mark}] {m.stem:20s} | {a:20s} | "
                              f"{n:2d}ag | seed{s:03d}")
        return

    # ── Основной прогон ───────────────────────────────────────────────────────
    done = 0; errors = 0; skipped = 0
    t_start = time.time()

    for map_path in maps:
        map_name = map_path.stem
        print(f"\n📦 Карта: {map_name}")

        for algo in algos:
            print(f"  🔀 {algo}")

            for n_ag in agents:
                out_dir = output / map_name / algo / f"{n_ag}ag"
                out_dir.mkdir(parents=True, exist_ok=True)
                row_results = []

                for seed in seeds:
                    seed_file = out_dir / f"seed{seed:03d}.xlsx"
                    if skip and seed_file.exists():
                        skipped += 1; done += 1
                        row_results.append(f"⏭{seed:03d}")
                        continue

                    try:
                        t0  = time.time()
                        run_one(str(map_path), algo, n_ag, seed,
                                out_dir, map_name)
                        dt  = time.time() - t0
                        done += 1
                        elapsed = time.time() - t_start
                        rate    = done / elapsed if elapsed > 0 else 1e-9
                        eta_s   = (total - done) / rate
                        row_results.append(f"✓{seed:03d}({dt:.0f}с)")
                    except Exception as exc:
                        errors += 1; done += 1
                        row_results.append(f"✗{seed:03d}")
                        # Полный traceback сразу в консоль
                        print(f"\n    ❌ ОШИБКА: {map_name}/{algo}/{n_ag}ag/"
                              f"seed{seed:03d}")
                        traceback.print_exc()
                        print()

                # Итоговая строка для этого набора агентов
                eta_s = ((total - done) / (done / (time.time() - t_start + 1e-9)))
                print(f"    {n_ag:2d}ag  "
                      + "  ".join(row_results[-6:])
                      + f"  [{done}/{total}]  ETA {eta_s/60:.1f}мин")

    elapsed = time.time() - t_start
    print(f"\n{'═'*62}")
    print(f"  Успешно:   {done - errors - skipped}")
    print(f"  Пропущено: {skipped}  |  Ошибок: {errors}")
    print(f"  Время:     {elapsed/60:.1f} мин")
    print(f"{'═'*62}")
    if done > skipped:
        print(f"\n  Следующий шаг:")
        print(f"  python compute_averages.py {output}\n")


if __name__ == '__main__':
    main()