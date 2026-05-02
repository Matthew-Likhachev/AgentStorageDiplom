"""
dispatcher.py
Диспетчерская подсистема заказов

Ключевые правила:
  - 1 заказ → 1 фасовщик
  - Подзаказы выполняются ПОСЛЕДОВАТЕЛЬНО (следующий стартует как только
    у фасовщика есть свободные слоты, не дожидаясь полного завершения предыдущего)
  - Размер подзаказа = кол-во Z-слотов фасовщика (packer_slots[packer_id])
  - У каждого фасовщика одновременно ведётся 1 активный заказ
  - Срочные заказы (urgent=True) прерывают очередь
  - get_assignable_suborders(packer_id, free_slots) возвращает подзаказ
    ТОЛЬКО для свободных слотов (агентов не больше чем слотов)
"""

import random
import logging
from typing import Dict, List, Set, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum


class OrderStatus(Enum):
    PENDING   = "pending"
    ACTIVE    = "active"
    COMPLETED = "completed"


class SubOrderStatus(Enum):
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"   # агенты в пути / у фасовщика
    COMPLETING  = "completing"    # все агенты сданы, возвращают стеллажи
    COMPLETED   = "completed"


@dataclass
class OrderItem:
    product_id: int
    shelf_pos:  Tuple[int, int]
    quantity:   int = 1


@dataclass
class SubOrder:
    suborder_id:    int
    order_id:       int
    packer_id:      int
    items:          List[OrderItem] = field(default_factory=list)
    status:         SubOrderStatus  = SubOrderStatus.PENDING
    priority:       int             = 1
    # Сколько агентов сейчас работают над подзаказом (ещё не вернули стеллаж)
    active_agents:  int             = 0
    # Множество стеллажей, которые ещё не взяты агентами
    pending_shelves: Set[Tuple[int, int]] = field(default_factory=set)

    def __post_init__(self):
        self.pending_shelves = {item.shelf_pos for item in self.items}

    @property
    def required_shelves(self) -> Set[Tuple[int, int]]:
        return {item.shelf_pos for item in self.items}


@dataclass
class Order:
    order_id:   int
    spawn_tick: int
    packer_id:  int
    items:      List[OrderItem] = field(default_factory=list)
    suborders:  List[SubOrder]  = field(default_factory=list)
    status:     OrderStatus     = OrderStatus.PENDING
    priority:   int             = 1
    urgent:     bool            = False


class Dispatcher:
    def __init__(self, order_seed: int, inventory_manager,
                 packer_ids: List[int],
                 packer_slots: Optional[Dict[int, int]] = None,
                 high_priority_threshold: int = 8):

        self.order_seed              = order_seed
        self.inventory               = inventory_manager
        self.packer_ids              = packer_ids
        self.packer_slots            = packer_slots or {p: 10 for p in packer_ids}
        self.high_priority_threshold = high_priority_threshold
        self.rng                     = random.Random(order_seed)

        self.orders:         Dict[int, Order]  = {}
        self.active_queue:   List[Order]       = []
        self.pending_spawns: List[Order]       = []

        # Текущий активный заказ и подзаказ на каждого фасовщика
        self.active_order_id:    Dict[int, Optional[int]] = {p: None for p in packer_ids}
        self.active_suborder_id: Dict[int, Optional[int]] = {p: None for p in packer_ids}

        self.next_order_id    = 0
        self.next_suborder_id = 0

        self._pre_generate_all_orders()

    # ── генерация ─────────────────────────────────────────────────────────────
    def _pre_generate_all_orders(self):
        spawn_ticks = sorted([self.rng.randint(0, 5000) for _ in range(100)])
        for tick in spawn_ticks:
            self.next_order_id += 1
            pid       = self.rng.choice(self.packer_ids)
            num_items = self.rng.randint(1, 30)
            # Только 3 вида товаров (0, 1, 2)
            products  = list(range(3)) * (num_items // 3 + 1)
            self.rng.shuffle(products)

            items = []
            for prod_id in products[:num_items]:
                shelves = self.inventory.get_shelves_with_product(prod_id)
                if shelves:
                    items.append(OrderItem(
                        product_id=prod_id,
                        shelf_pos=self.rng.choice(shelves)
                    ))

            order = Order(order_id=self.next_order_id, spawn_tick=tick,
                          packer_id=pid, items=items)
            self.orders[order.order_id] = order
            self.pending_spawns.append(order)

    # ── разбивка ──────────────────────────────────────────────────────────────
    def _split_to_suborders(self, order: Order) -> List[SubOrder]:
        """
        Разбивает заказ на подзаказы.
        Макс стеллажей в подзаказе = packer_slots[packer_id].
        Один стеллаж — только один раз в подзаказе.
        """
        max_shelves = self.packer_slots.get(order.packer_id, 10)
        suborders: List[SubOrder] = []
        current_sub:     Optional[SubOrder]        = None
        current_shelves: Set[Tuple[int, int]]      = set()

        for item in order.items:
            need_new = (current_sub is None
                        or len(current_shelves) >= max_shelves
                        or item.shelf_pos in current_shelves)
            if need_new:
                if current_sub is not None:
                    suborders.append(current_sub)
                self.next_suborder_id += 1
                current_sub     = SubOrder(suborder_id=self.next_suborder_id,
                                           order_id=order.order_id,
                                           packer_id=order.packer_id)
                current_shelves = set()
            current_sub.items.append(item)
            current_shelves.add(item.shelf_pos)

        if current_sub:
            suborders.append(current_sub)

        # Инициализируем pending_shelves
        for sub in suborders:
            sub.pending_shelves = set(sub.required_shelves)

        logging.info(
            f"  📦 Заказ #{order.order_id} → {len(suborders)} подзаказов "
            f"(фасовщик {order.packer_id}, слотов={max_shelves})"
        )
        return suborders

    # ── основной тик ──────────────────────────────────────────────────────────
    def step(self, current_tick: int):
        # 1. Активируем заказы по расписанию
        while self.pending_spawns and self.pending_spawns[0].spawn_tick <= current_tick:
            order = self.pending_spawns.pop(0)
            order.status    = OrderStatus.ACTIVE
            order.suborders = self._split_to_suborders(order)
            self.active_queue.append(order)

        # 2. Пересчёт приоритетов
        self._update_priorities()

        # 3. Активируем заказ / подзаказ для каждого фасовщика
        for pid in self.packer_ids:
            self._try_activate_next_order(pid)
            self._try_activate_next_suborder(pid)

    def _try_activate_next_order(self, packer_id: int):
        cur = self.active_order_id.get(packer_id)
        if cur is not None:
            order = self.orders.get(cur)
            if order and order.status != OrderStatus.COMPLETED:
                return
        for order in self.active_queue:
            if order.packer_id == packer_id:
                self.active_order_id[packer_id] = order.order_id
                logging.info(f"  Фасовщик {packer_id}: активирован заказ #{order.order_id}")
                return
        self.active_order_id[packer_id] = None

    def _try_activate_next_suborder(self, packer_id: int):
        """
        Активирует следующий PENDING подзаказ если текущий достаточно продвинулся.

        Правило продвижения:
          • IN_PROGRESS + pending_shelves НЕ пуст → ждём (агенты ещё не взяли все стеллажи)
          • IN_PROGRESS + pending_shelves ПУСТ    → можно активировать следующий.
            Все стеллажи разобраны агентами, свободные слоты (выше текущих агентов)
            могут занять агенты следующего подзаказа.
          • COMPLETING / COMPLETED               → обязательно активируем следующий.
        """
        oid = self.active_order_id.get(packer_id)
        if oid is None:
            self.active_suborder_id[packer_id] = None
            return

        order = self.orders.get(oid)
        if order is None:
            return

        cur_sid = self.active_suborder_id.get(packer_id)
        if cur_sid is not None:
            cur_sub = next((s for s in order.suborders if s.suborder_id == cur_sid), None)
            if cur_sub and cur_sub.status == SubOrderStatus.IN_PROGRESS:
                if cur_sub.pending_shelves:
                    # Ещё есть неразобранные стеллажи — следующий не стартует
                    return
                # pending_shelves пуст: все стеллажи взяты, свободные слоты доступны
                # следующему подзаказу — активируем его.

        # Ищем следующий PENDING
        for sub in order.suborders:
            if sub.status == SubOrderStatus.PENDING:
                self.active_suborder_id[packer_id] = sub.suborder_id
                logging.info(
                    f"  Фасовщик {packer_id}: активирован подзаказ #{sub.suborder_id} "
                    f"({len(sub.required_shelves)} стеллажей)"
                )
                return

        # Нет PENDING подзаказов
        self.active_suborder_id[packer_id] = None

    # ── приоритеты ────────────────────────────────────────────────────────────
    def _update_priorities(self):
        for i, order in enumerate(self.active_queue):
            new_priority = max(1, 10 - i)
            if order.priority != new_priority:
                order.priority = new_priority
                for sub in order.suborders:
                    sub.priority = new_priority

    # ── API для AgentManager ──────────────────────────────────────────────────
    def get_assignable_suborders(self, packer_id: int, free_slots: int) -> Optional['SubOrder']:
        """
        Возвращает активный подзаказ фасовщика ЕСЛИ есть свободные слоты.
        free_slots — сколько Z-слотов свободно прямо сейчас.
        Возвращает None если слотов нет или подзаказа нет.
        """
        if free_slots <= 0:
            return None
        sid = self.active_suborder_id.get(packer_id)
        if sid is None:
            return None
        oid = self.active_order_id.get(packer_id)
        if oid is None:
            return None
        order = self.orders.get(oid)
        if order is None:
            return None
        sub = next((s for s in order.suborders if s.suborder_id == sid), None)
        if sub is None:
            return None
        # Подзаказ доступен если PENDING или IN_PROGRESS с оставшимися стеллажами
        if sub.status in (SubOrderStatus.PENDING, SubOrderStatus.IN_PROGRESS):
            if sub.pending_shelves:  # есть стеллажи которые ещё никто не взял
                return sub
        return None

    def get_next_pending_suborder(self, packer_id: int):
        """
        Возвращает следующий PENDING подзаказ — тот что идёт ПОСЛЕ активного.
        Используется чтобы первый агент следующего подзаказа мог занять
        последний свободный слот очереди, не дожидаясь завершения текущего.
        """
        oid = self.active_order_id.get(packer_id)
        if oid is None: return None
        order = self.orders.get(oid)
        if order is None: return None
        cur_sid    = self.active_suborder_id.get(packer_id)
        passed_cur = (cur_sid is None)
        for sub in order.suborders:
            if sub.status == SubOrderStatus.PENDING:
                if passed_cur:
                    # Убеждаемся что у него есть стеллажи
                    if sub.pending_shelves:
                        return sub
                if sub.suborder_id == cur_sid:
                    passed_cur = True
        return None

    def take_shelf_from_suborder(self, suborder_id: int, shelf_pos: Tuple[int, int],
                                  agent_id: int) -> bool:
        """
        Агент берёт конкретный стеллаж из подзаказа.
        Убирает из pending_shelves, инкрементирует active_agents.
        """
        for order in self.active_queue:
            for sub in order.suborders:
                if sub.suborder_id != suborder_id:
                    continue
                if shelf_pos not in sub.pending_shelves:
                    return False
                sub.pending_shelves.discard(shelf_pos)
                sub.active_agents += 1
                if sub.status == SubOrderStatus.PENDING:
                    sub.status = SubOrderStatus.IN_PROGRESS

                # Если последний стеллаж только что разобран — активируем следующий подзаказ.
                # Агенты следующего подзаказа займут свободные слоты выше текущих,
                # не мешая доставке текущего подзаказа.
                if not sub.pending_shelves:
                    self._try_activate_next_suborder(order.packer_id)

                return True
        return False

    def complete_agent_in_suborder(self, suborder_id: int) -> bool:
        """
        Вызывается когда агент вернул стеллаж на место.
        Уменьшает active_agents. Когда 0 и pending_shelves пуст → COMPLETED.
        """
        for order in self.active_queue:
            for sub in order.suborders:
                if sub.suborder_id != suborder_id:
                    continue

                sub.active_agents = max(0, sub.active_agents - 1)

                # Завершён когда: нет стеллажей в ожидании И все агенты вернули
                if sub.active_agents == 0 and not sub.pending_shelves:
                    if sub.status == SubOrderStatus.COMPLETED:
                        return True  # уже завершён

                    sub.status = SubOrderStatus.COMPLETED
                    logging.info(f"  ✅ Подзаказ #{suborder_id} завершён (заказ #{order.order_id})")

                    pid = order.packer_id
                    if self.active_suborder_id.get(pid) == suborder_id:
                        self.active_suborder_id[pid] = None

                    # Сразу пробуем активировать следующий
                    self._try_activate_next_suborder(pid)

                    if all(s.status == SubOrderStatus.COMPLETED for s in order.suborders):
                        order.status = OrderStatus.COMPLETED
                        self.active_queue.remove(order)
                        if self.active_order_id.get(pid) == order.order_id:
                            self.active_order_id[pid] = None
                        logging.info(f"  🏁 Заказ #{order.order_id} выполнен полностью")
                        self._update_priorities()
                        self._try_activate_next_order(pid)
                        self._try_activate_next_suborder(pid)
                else:
                    # Не все агенты вернулись — переходим в COMPLETING
                    if sub.status == SubOrderStatus.IN_PROGRESS and not sub.pending_shelves:
                        sub.status = SubOrderStatus.COMPLETING
                        pid = order.packer_id
                        # Можно уже брать следующий подзаказ!
                        if self.active_suborder_id.get(pid) == suborder_id:
                            self.active_suborder_id[pid] = None
                        self._try_activate_next_suborder(pid)
                        logging.info(
                            f"  🔄 Подзаказ #{suborder_id} COMPLETING "
                            f"(осталось {sub.active_agents} агентов в пути)"
                        )
                return True
        return False

    # Обратная совместимость со старым API
    def complete_suborder(self, suborder_id: int) -> bool:
        return self.complete_agent_in_suborder(suborder_id)

    def assign_suborder(self, suborder_id: int, agent_id: int) -> bool:
        """Обратная совместимость — теперь используем take_shelf_from_suborder."""
        for order in self.active_queue:
            for sub in order.suborders:
                if sub.suborder_id == suborder_id:
                    return True
        return False

    def get_statistics(self) -> Dict:
        return {
            "total_orders":          len(self.orders),
            "active_orders":         len(self.active_queue),
            "pending_spawns":        len(self.pending_spawns),
            "active_per_packer":     dict(self.active_order_id),
            "active_sub_per_packer": dict(self.active_suborder_id),
            "completed_suborders":   sum(
                sum(1 for s in o.suborders if s.status == SubOrderStatus.COMPLETED)
                for o in self.orders.values()
            ),
        }