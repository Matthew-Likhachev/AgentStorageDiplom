"""
main.py
Стратегия: Dependency Injection, Application Bootstrap
"""
import sys, logging, pygame, time, json
from pathlib import Path
from typing import Dict

from import_module  import MapImporter
from inventory_manager import InventoryManager
from dispatcher     import Dispatcher
from visualizer     import PygameVisualizer
from agent_module   import AgentManager, AStarRouter
from router_interface import RouterContext, RoutingAlgorithmRegistry

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main():
    logging.info("🚀 Запуск симуляции роботизированного склада")

    # ═══════════════════════════════════════════════════════════════════════
    # КОНФИГУРАЦИЯ
    # ═══════════════════════════════════════════════════════════════════════
    config = {
        "excel_map": r"A:\projects in programming\python\AgentStorage\AgentStorageDiplom\maps\map1.xlsx",
        "save_fixed_map": "data/map_clean.json",

        "shelf_seed":  42,    # воспроизводимость распределения товаров
        "order_seed":  124,   # воспроизводимость заказов
        "num_agents":  20,
        "high_priority_threshold": 8,
        "num_orders":     60,
        "max_spawn_tick": 500,

        # ── Выбор алгоритма маршрутизации ─────────────────────────────────
        #
        # ┌─────────────────┬────────────────────────────────────────────────┐
        # │ Ключ            │ Описание                                       │
        # ├─────────────────┼────────────────────────────────────────────────┤
        # │ prioritized_astar│ A* — жадный последовательный A*.               │
        # │                 │ Каждый агент планируется независимо по score.  │
        # │                 │ Быстрый, субоптимальный. WFG активен.          │
        # │                 │ Рекомендуется как BASE-LINE для сравнения.     │
        # │                 │ Хорошо для 10+ агентов.                        │
        # ├─────────────────┼────────────────────────────────────────────────┤
        # │ cbs             │ CBS — Conflict-Based Search.                   │
        # │                 │ Оптимален по sum-of-costs для малых групп.     │
        # │                 │ Централизованный, WFG не нужен.               │
        # │                 │ Экспоненциальный в худшем случае.              │
        # │                 │ Рекомендуется для ≤10 агентов.                 │
        # ├─────────────────┼────────────────────────────────────────────────┤
        # │ lns1_cbs        │ LNS-1 (верхний) + CBS (нижний).               │
        # │ (алиасы:        │ Destroy-and-repair: LNS разрушает подмн-во    │
        # │  lns1, lns)     │ путей и вызывает CBS для их перестройки.       │
        # │                 │ CBS гарантирует локальную бесконфликтность.    │
        # │                 │ Централизованный, WFG не нужен.               │
        # │                 │ Рекомендуется для 8–20 агентов.               │
        # ├─────────────────┼────────────────────────────────────────────────┤
        # │ lns2_simple     │ LNS-2 (верхний) + A* (нижний). БЕЗ CBS.       │
        # │ (алиасы:        │ Destroy-and-repair только через A*:            │
        # │  lns2)          │ быстрее LNS-1, но конфликты внутри            │
        # │                 │ neighbourhood не гарантированно устранены      │
        # │                 │ (следующие LNS-итерации их уменьшают).         │
        # │                 │ Централизованный, WFG не нужен.               │
        # │                 │ Рекомендуется для 20+ агентов.                │
        # └─────────────────┴────────────────────────────────────────────────┘
        #
        "router_algorithm": "lns1_cbs",   # ← выберите один из ключей выше

        "tick_delay_ms": 0,
    }

    # Импорт карты
    logging.info("📦 Импорт карты из Excel...")
    importer = MapImporter(
        excel_path=config["excel_map"], auto_fix=True,
        output_json=config["save_fixed_map"])
    import_res = importer.import_map()
    if not import_res.success:
        for err in import_res.errors: logging.error(f"   - {err}")
        sys.exit(1)
    logging.info(f"✅ Карта: {import_res.map_data['width']}x{import_res.map_data['height']}")

    cells = import_res.map_data['cells']

    # Стеллажи и фасовщики
    shelf_positions = []
    packer_positions = {}
    packer_ids = []
    for row in cells:
        for cell in row:
            if cell['type'] == 'S':
                shelf_positions.append((cell['row'], cell['col']))
            elif cell['type'] == 'P':
                parts = cell['text'].split('|')
                try:
                    logic = json.loads(parts[3]) if len(parts) > 3 else {}
                    pid   = logic.get('packer_id')
                    if pid is not None:
                        packer_ids.append(pid)
                        packer_positions[pid] = (cell['row'], cell['col'])
                except json.JSONDecodeError:
                    pass

    # Z-слоты
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
    logging.info(f"   📦 Стеллажей: {len(shelf_positions)}  "
                 f"Фасовщиков: {len(packer_ids)}  "
                 f"Z-слоты: {packer_slots}")

    if not shelf_positions:
        logging.error("❌ Стеллажи (тип S) не найдены"); sys.exit(1)

    # Подсистемы
    visual = PygameVisualizer(grid_data=import_res.map_data, metrics=None, cell_size=30)
    inventory = InventoryManager(shelf_seed=config["shelf_seed"],
                                 shelf_positions=shelf_positions)
    dispatcher = Dispatcher(
        order_seed=config["order_seed"],
        inventory_manager=inventory,
        packer_ids=packer_ids or [1],
        packer_slots=packer_slots or None,
        high_priority_threshold=config["high_priority_threshold"],
        num_orders=config["num_orders"],
        max_spawn_tick=config["max_spawn_tick"])

    _name  = config["router_algorithm"]
    logging.info(f"🔀 Алгоритм: '{_name}'")
    _algo  = RoutingAlgorithmRegistry.create(_name, import_res.map_data)
    router = RouterContext(_algo)
    router.set_map_data(import_res.map_data)

    # Зоны выдачи (slot == 1)
    packer_delivery_zones = {}
    for row in cells:
        for cell in row:
            if cell['type'] == 'Z':
                parts = cell['text'].split('|')
                try:
                    logic = json.loads(parts[3]) if len(parts) > 3 else {}
                    pid   = logic.get('packer_id')
                    slot  = logic.get('slot')
                    if pid is not None and slot == 1:
                        packer_delivery_zones[pid] = (cell['row'], cell['col'])
                except Exception:
                    pass

    agent_manager = AgentManager(
        env=None, router=router,
        map_data=import_res.map_data,
        dispatcher=dispatcher,
        inventory=inventory,
        packer_positions=packer_positions,
        packer_delivery_zones=packer_delivery_zones,
        num_agents=config["num_agents"])

    # Главный цикл
    logging.info("🎮 Запуск (SPACE — пауза, ESC — выход)")
    tick = 0; max_ticks = 6_000

    try:
        while visual.running:
            dispatcher.step(tick)
            state         = agent_manager.run_tick()
            state['tick'] = tick
            state['inventory'] = {pos: inventory.get_shelf_products(pos)
                                  for pos in inventory.shelves}

            orders_by_packer = {}; active_info = {}
            for order in dispatcher.active_queue:
                pid = str(order.packer_id)
                if pid not in orders_by_packer:
                    orders_by_packer[pid] = {'orders': []}
                cur_sid = dispatcher.active_suborder_id.get(order.packer_id)
                for sub in order.suborders:
                    from dispatcher import SubOrderStatus
                    if sub.status == SubOrderStatus.COMPLETED: continue
                    ic = {}
                    for item in sub.items:
                        ic[item.product_id] = ic.get(item.product_id, 0) + 1
                    orders_by_packer[pid]['orders'].append({
                        'order_id': order.order_id, 'suborder_id': sub.suborder_id,
                        'active': (sub.suborder_id == cur_sid), 'items': ic,
                        'num_shelves': sub.num_shelves, 'num_items': sub.num_items,
                        'status': sub.status.value,
                        'num_pending': len(sub.pending_shelves),
                        'num_active': sub.active_agents})

            for pid_int in agent_manager.slot_manager.slots:
                pid = str(pid_int); sm = agent_manager.slot_manager
                owners = sm.slot_owners.get(pid_int, {})
                active_info[pid] = {
                    'order_id':    dispatcher.active_order_id.get(pid_int),
                    'suborder_id': dispatcher.active_suborder_id.get(pid_int),
                    'slots_total': sm.total_slots(pid_int),
                    'slots_free':  sm.free_slots_count(pid_int),
                    'slot_queue':  sorted([(sn, aid) for sn, aid in owners.items()
                                           if aid is not None], key=lambda x: x[0])}
            state['dispatcher'] = {'orders_by_packer': orders_by_packer,
                                   'active_info': active_info}

            visual.on_state_change(state)
            visual.update()
            agent_manager.print_order_summary()

            tick += 1
            delay = config.get("tick_delay_ms", 0)
            if delay > 0:
                time.sleep(delay / 1000.0)
            if tick >= max_ticks:
                logging.info(f"🏁 Лимит {max_ticks} тактов достигнут."); break
    finally:
        pygame.quit()
        logging.info("✅ Симуляция завершена")


if __name__ == "__main__":
    main()