"""
main.py
Стратегия: Dependency Injection, Application Bootstrap
Полный запуск симуляции: импорт, инициализация, цикл симуляции.
"""

import sys
import logging
import pygame
import time
import json
from pathlib import Path
from typing import Dict

# Импорты наших модулей
from import_module import MapImporter
from inventory_manager import InventoryManager
from dispatcher import Dispatcher
from visualizer import PygameVisualizer
from agent_module import AgentManager, AStarRouter
from router_interface import RouterContext, RoutingAlgorithmRegistry

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main():
    logging.info("🚀 Запуск симуляции роботизированного склада")

    # === 1. Конфигурация ===
    config = {
        "excel_map": r"A:\projects in programming\python\AgentStorage\AgentStorageDiplom\maps\map1.xlsx",
        "save_fixed_map": "data/map_clean.json",
        # Сиды для воспроизводимости экспериментов
        "shelf_seed": 42,       # Сид распределения товаров по полкам
        "order_seed": 123,      # Сид генерации заказов и их появления
        "num_agents": 20,       # Количество агентов
        "high_priority_threshold": 8, # Приоритет >= этого значения прерывает очередь

        # ── Генерация заказов ─────────────────────────────────────────────────
        # num_orders     — сколько заказов сгенерировать за всю симуляцию
        # max_spawn_tick — до какого тика включительно появляются заказы
        "num_orders":     60,    # 60 заказов равномерно до тика 5000
        "max_spawn_tick": 5000,  # последний возможный тик появления заказа

        # ── Алгоритм маршрутизации ────────────────────────────────────────────
        # Выберите один из трёх вариантов:
        #
        #   "prioritized_astar"  — жадный последовательный A*.
        #                          Рекомендуется для 10+ агентов.
        #                          Быстрый, субоптимальный. Дедлоки решаются
        #                          через DeadlockResolver (L1+L2+L3).
        #
        #   "cbs"                — Conflict-Based Search.
        #                          Оптимален для небольших групп (≤8 агентов).
        #                          При 30 агентах упирается в лимит итераций
        #                          и возвращает лучший найденный узел дерева.
        #
        #   "lns"                — Large Neighbourhood Search поверх CBS.
        #                          Компромисс качества и скорости.
        #                          Хорошо работает при 8-20 агентах.
        #
        "router_algorithm": "lns",
        "tick_delay_ms":0
    }

    # === 2. Импорт карты ===
    logging.info("📦 Импорт карты из Excel...")
    importer = MapImporter(
        excel_path=config["excel_map"],
        auto_fix=True,
        output_json=config["save_fixed_map"]
    )
    import_res = importer.import_map()

    if not import_res.success:
        logging.error("❌ Ошибка карты:")
        for err in import_res.errors:
            logging.error(f"   - {err}")
        sys.exit(1)

    logging.info(f"✅ Карта готова: {import_res.map_data['width']}x{import_res.map_data['height']}")

    # === 3. Анализ карты (поиск стеллажей и фасовщиков) ===
    logging.info("🔍 Сканирование карты на наличие объектов...")
    shelf_positions = []
    packer_positions = {}  # {packer_id: (row, col)}
    packer_ids = []

    cells = import_res.map_data['cells']
    for row in cells:
        for cell in row:
            if cell['type'] == 'S':
                shelf_positions.append((cell['row'], cell['col']))
            elif cell['type'] == 'P':
                # Парсим ID фасовщика из текста ячейки (формат: P|...|...|{"packer_id": 1})
                parts = cell['text'].split('|')
                try:
                    logic = json.loads(parts[3]) if len(parts) > 3 else {}
                    pid = logic.get('packer_id')
                    if pid is not None:
                        packer_ids.append(pid)
                        packer_positions[pid] = (cell['row'], cell['col'])
                except json.JSONDecodeError:
                    logging.warning(f"⚠️ Не удалось прочитать логику фасовщика в {cell['row']},{cell['col']}")

    logging.info(f"   📦 Найдено стеллажей: {len(shelf_positions)}")
    logging.info(f"   📦 Найдено фасовщиков: {len(packer_ids)} (IDs: {packer_ids})")

    # === 3б. Подсчёт Z-слотов для каждого фасовщика ===
    # Нужно до создания Dispatcher, чтобы размер подзаказа = реальному числу слотов на карте.
    packer_slots: Dict[int, int] = {}
    for row in cells:
        for cell in row:
            if cell['type'] == 'Z':
                parts = cell['text'].split('|')
                try:
                    logic = json.loads(parts[3]) if len(parts) > 3 else {}
                    pid   = logic.get('packer_id')
                    zone  = logic.get('zone', 'order')
                    if pid is not None and zone == 'order':
                        packer_slots[pid] = packer_slots.get(pid, 0) + 1
                except Exception:
                    pass
    logging.info(f"   📦 Z-слотов по фасовщикам: {packer_slots}")

    if not shelf_positions:
        logging.error("❌ На карте не найдено ни одного стеллажа (тип S)")
        sys.exit(1)

    # === 4. Инициализация подсистем ===
    logging.info("🔧 Инициализация подсистем...")

    # 4.1 Визуализатор
    metrics = None  # Заглушка, если metrics_collector.py еще не готов
    visual = PygameVisualizer(
        grid_data=import_res.map_data,
        metrics=metrics,
        cell_size=30
    )

    # 4.2 Менеджер запасов (распределяет товары по стеллажам)
    inventory = InventoryManager(
        shelf_seed=config["shelf_seed"],
        shelf_positions=shelf_positions
    )
    logging.info(f"   📊 Товаров на складе: {inventory.get_total_products()}")

    # 4.3 Диспетчер (генерирует заказы)
    dispatcher = Dispatcher(
        order_seed=config["order_seed"],
        inventory_manager=inventory,
        packer_ids=packer_ids if packer_ids else [1],
        packer_slots=packer_slots if packer_slots else None,
        high_priority_threshold=config["high_priority_threshold"],
        num_orders=config["num_orders"],
        max_spawn_tick=config["max_spawn_tick"],
    )
    logging.info(f"   📋 Заказов: {config['num_orders']} до тика {config['max_spawn_tick']}")

    # 4.4 Маршрутизатор — выбирается из конфига ("router_algorithm")
    _router_name = config["router_algorithm"]
    logging.info(f"🔀 Алгоритм маршрутизации: '{_router_name}'")
    _algo  = RoutingAlgorithmRegistry.create(_router_name, import_res.map_data)
    router = RouterContext(_algo)
    router.set_map_data(import_res.map_data)

    # 4.5 Менеджер агентов
    packer_delivery_zones = {}  # {packer_id: (row, col)}

    cells = import_res.map_data['cells']
    for row in cells:
        for cell in row:
            if cell['type'] == 'Z':
                parts = cell['text'].split('|')
                try:
                    logic = json.loads(parts[3]) if len(parts) > 3 else {}
                    pid = logic.get('packer_id')
                    slot = logic.get('slot')
                    # Собираем только зоны выдачи (slot 1)
                    if pid is not None and slot == 1:
                        packer_delivery_zones[pid] = (cell['row'], cell['col'])
                except:
                    pass

    logging.info(f"📦 Зон выдачи (slot 1): {len(packer_delivery_zones)}")

    # Инициализация агентов с зонами выдачи
    agent_manager = AgentManager(
        env=None,
        router=router,
        map_data=import_res.map_data,
        dispatcher=dispatcher,
        inventory=inventory,
        packer_positions=packer_positions,
        packer_delivery_zones=packer_delivery_zones,
        num_agents=config["num_agents"]
    )

    # === 5. Главный цикл симуляции ===
    logging.info("🎮 Запуск цикла симуляции (SPACE - пауза, ESC - выход)")
    tick = 0
    max_ticks = 6000  # Ограничитель для демо (заказы заканчиваются к 5000)

    try:
        while visual.running:
            # 5.1 Шаг диспетчера (спавн новых заказов, пересчет приоритетов)
            dispatcher.step(tick)

            # 5.2 Шаг агентов (назначение задач, движение, A*)
            state = agent_manager.run_tick()
            state['tick'] = tick

            # 5.3 Добавляем active_info и inventory для визуализатора
            state['inventory'] = {
                pos: inventory.get_shelf_products(pos)
                for pos in inventory.shelves
            }

            orders_by_packer = {}
            active_info      = {}
            for order in dispatcher.active_queue:
                pid     = str(order.packer_id)
                pid_int = order.packer_id
                if pid not in orders_by_packer:
                    orders_by_packer[pid] = {'orders': []}
                cur_sid = dispatcher.active_suborder_id.get(pid_int)
                for sub in order.suborders:
                    from dispatcher import SubOrderStatus
                    if sub.status == SubOrderStatus.COMPLETED: continue
                    items_count: dict = {}
                    for item in sub.items:
                        items_count[item.product_id] = items_count.get(item.product_id,0)+1
                    orders_by_packer[pid]['orders'].append({
                        'order_id':    order.order_id,
                        'suborder_id': sub.suborder_id,
                        'active':      (sub.suborder_id == cur_sid),
                        'items':       items_count,
                        'num_shelves': sub.num_shelves,
                        'num_items':   sub.num_items,
                        'status':      sub.status.value,
                        'num_pending': len(sub.pending_shelves),
                        'num_active':  sub.active_agents,
                    })

            # active_info: текущий заказ, подзаказ, слоты, очередь для каждого фасовщика
            for pid_int in agent_manager.slot_manager.slots:
                pid = str(pid_int)
                sm      = agent_manager.slot_manager
                owners  = sm.slot_owners.get(pid_int, {})
                total   = sm.total_slots(pid_int)
                free    = sm.free_slots_count(pid_int)
                slot_queue = sorted(
                    [(sn, aid) for sn, aid in owners.items() if aid is not None],
                    key=lambda x: x[0])
                active_info[pid] = {
                    'order_id':    dispatcher.active_order_id.get(pid_int),
                    'suborder_id': dispatcher.active_suborder_id.get(pid_int),
                    'slots_total': total,
                    'slots_free':  free,
                    'slot_queue':  slot_queue,
                }
            state['dispatcher'] = {
                'orders_by_packer': orders_by_packer,
                'active_info':      active_info,
            }

            # 5.4 Обновление экрана
            visual.on_state_change(state)
            visual.update()

            # if tick % 50 == 0:
            agent_manager.print_order_summary()

            tick += 1
            if config.get("tick_delay_ms", 90) > 0:
                time.sleep(config.get("tick_delay_ms", 90) / 1000.0)

            if tick >= max_ticks:
                logging.info(f"🏁 Демо-лимит ({max_ticks} тактов) достигнут.")
                break

    finally:
        pygame.quit()
        logging.info("✅ Симуляция завершена")


if __name__ == "__main__":
    main()