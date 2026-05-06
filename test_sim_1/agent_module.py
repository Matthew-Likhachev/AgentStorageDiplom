"""
agent_module.py
Centralized Orchestration + Intelligent Priority Score

Изменения:
  - AStarRouter удалён отсюда — теперь в router_interface.py (BaseGridRouter).
  - AgentManager использует любой IRouter через инъекцию зависимостей.
  - DeadlockResolver: IDLE/UNLOADING/WAITING_SLOT не считаются застрявшими.
  - Swap-конфликты решаются на уровне A* (_is_swap) и DeadlockResolver.
"""

import logging
from typing import Dict, List, Optional, Tuple, Any

try:
    from router_interface import IRouter, BaseGridRouter
except ImportError:
    class IRouter:
        def solve(self, tasks, reservations): raise NotImplementedError
    BaseGridRouter = IRouter

try:
    from deadlock_module import DeadlockResolver
except ImportError:
    class DeadlockResolver:
        def __init__(self, **kw): pass
        def update_last_move(self, agents, tick): pass
        def check_and_resolve(self, agents, res, tick): return {aid: "OK" for aid in agents}

# ── Статусы ──────────────────────────────────────────────────────────────────
STATUS_IDLE            = "IDLE"
STATUS_GO_TO_SHELF     = "GO_TO_SHELF"
STATUS_CARRY_TO_PACKER = "CARRY_TO_PACKER"
STATUS_WAITING_SLOT    = "WAITING_SLOT"
STATUS_UNLOADING       = "UNLOADING"
STATUS_RETURN_SHELF    = "RETURN_SHELF"

SCORE_WEIGHTS = {'w1': 3.0, 'w2': 2.0, 'w3': 5.0, 'w4': 1.0, 'w5': 2.0}


# ── Agent ────────────────────────────────────────────────────────────────────
class Agent:
    def __init__(self, agent_id: int, start_pos: Tuple[int, int]):
        self.id                  = agent_id
        self.pos                 = start_pos
        self.start_pos           = start_pos
        self.status              = STATUS_IDLE
        self.goal: Optional[Tuple[int, int]] = None
        self.path: List[Tuple[int, int]]     = []
        self.current_path_index  = 0
        self.waiting_time        = 0
        self.urgency             = 1
        self.score               = 0.0
        self.shortest_path_len   = 0
        self.current_suborder    = None
        self.has_cargo           = False
        self.unload_timer        = 0
        self.original_shelf_pos: Optional[Tuple[int, int]] = None
        self.assigned_slot_num:  Optional[int]             = None
        # Реальный диаметр карты (width + height). Устанавливается AgentManager
        # при создании агента. До установки используется безопасное значение 200.
        self.map_max_dist: int = 200

    def compute_score(self, actual_path_len: int = 0) -> float:
        w           = SCORE_WEIGHTS
        slot        = self.assigned_slot_num or 10
        urgency     = max(1, min(10, 11 - slot))
        wt          = min(self.waiting_time, 1000)
        prio        = self.current_suborder.priority if self.current_suborder else 1
        criticality = max(1, min(4, 1 + (prio - 1) * 3 // 9))
        dist        = (abs(self.pos[0] - self.goal[0]) + abs(self.pos[1] - self.goal[1])
                       if self.goal else self.map_max_dist)

        # ИСПРАВЛЕНИЕ: нормируем proximity на реальный диаметр карты
        # (width + height), а не на площадь сетки (10000).
        # Старое: proximity = max(1, 10000 - dist) / 10000  → макс ~0.0001 (мёртво)
        # Новое: 1.0 - dist/max_dist → диапазон [0.0, 1.0], реально влияет на score
        max_dist      = max(1, self.map_max_dist)
        proximity_norm = max(0.0, min(1.0, 1.0 - dist / max_dist))

        shortest    = self.shortest_path_len or dist
        if actual_path_len > 0 and shortest > 0:
            detour = max(0.0, min(1.0,
                (actual_path_len - shortest) / max(1, max_dist - shortest)))
        else:
            detour = 0.0

        # ИСПРАВЛЕНИЕ: detour со знаком ПЛЮС.
        # Прежний знак минус создавал отрицательную петлю: агент уже заплатил
        # за обход → score падал → приоритет снижался → его снова заставляли
        # уступать → новый обход → score ещё ниже. Знак плюс: агент, вынужденный
        # делать крюк, накапливает «долг» системы и получает приоритет вернуть его.
        self.score = (w['w1'] * (urgency / 10.0)
                      + w['w2'] * (wt / 1000.0)
                      + w['w3'] * (criticality / 4.0)
                      + w['w4'] * proximity_norm
                      + w['w5'] * detour)
        return self.score

    def move(self) -> Tuple[int, int]:
        if not self.path or self.current_path_index >= len(self.path):
            return self.pos
        self.pos = self.path[self.current_path_index]
        self.current_path_index += 1
        if self.current_path_index >= len(self.path):
            self.path = []; self.current_path_index = 0
        return self.pos


# ── PackerSlotManager ────────────────────────────────────────────────────────
class PackerSlotManager:
    """
    Управляет Z-слотами очереди фасовщика.

    АРХИТЕКТУРА КОНВОЯ:
      • Каждый слот — эксклюзивная физическая ячейка. Зарезервирована для
        конкретного агента в reservations: никто другой не может в неё войти.
      • Агенты двигаются вереницей: слот N → N-1 только когда позиция N-1
        физически пуста (предыдущий агент ушёл) И агент N физически стоит
        в своём слоте (WAITING_SLOT).
      • Последний слот (максимальный номер): когда освобождается, в него
        может войти первый агент СЛЕДУЮЩЕГО подзаказа (его номер в подзаказе=1,
        но в очереди он на последней позиции). Это обеспечивает непрерывный
        поток к фасовщику.

    slot_owners[pid][slot_num] = agent_id | None
    slot_suborder[pid][slot_num] = suborder_id | None
    slots[pid][slot_num] = (row, col)  — физическая позиция
    """
    def __init__(self, map_data: Dict):
        import json as _json
        self.slots:         Dict[int, Dict[int, Tuple[int, int]]] = {}
        self.slot_owners:   Dict[int, Dict[int, Optional[int]]]   = {}
        self.slot_suborder: Dict[int, Dict[int, Optional[int]]]   = {}

        for row in map_data.get('cells', []):
            for c in row:
                if c.get('type') != 'Z': continue
                parts = c.get('text', '').split('|')
                try:
                    logic    = _json.loads(parts[3]) if len(parts) > 3 else {}
                    pid      = logic.get('packer_id')
                    slot_num = logic.get('slot')
                    zone     = logic.get('zone', 'order')
                    if pid is None or slot_num is None or zone != 'order': continue
                    self.slots.setdefault(pid, {})[slot_num]          = (c['row'], c['col'])
                    self.slot_owners.setdefault(pid, {})[slot_num]    = None
                    self.slot_suborder.setdefault(pid, {})[slot_num]  = None
                except Exception:
                    pass

        for pid, s in self.slots.items():
            logging.info(f"  Фасовщик {pid}: {len(s)} Z-слотов {sorted(s.keys())}")

    def total_slots(self, packer_id: int) -> int:
        return len(self.slots.get(packer_id, {}))

    def free_slots_count(self, packer_id: int) -> int:
        return sum(1 for v in self.slot_owners.get(packer_id, {}).values() if v is None)

    def last_slot_num(self, packer_id: int) -> Optional[int]:
        s = self.slots.get(packer_id, {})
        return max(s.keys()) if s else None

    def last_slot_free(self, packer_id: int) -> bool:
        last = self.last_slot_num(packer_id)
        if last is None: return False
        return self.slot_owners.get(packer_id, {}).get(last) is None

    def acquire_last_slot(self, packer_id: int, agent_id: int,
                          suborder_id: Optional[int] = None
                          ) -> Optional[Tuple[int, Tuple[int, int]]]:
        """Занимает последний (максимальный) слот — для агентов следующего подзаказа."""
        last = self.last_slot_num(packer_id)
        if last is None: return None
        owners = self.slot_owners.get(packer_id, {})
        if owners.get(last) is not None: return None
        owners[last] = agent_id
        self.slot_suborder[packer_id][last] = suborder_id
        return last, self.slots[packer_id][last]

    def acquire_slot(self, packer_id: int, agent_id: int,
                     suborder_id: Optional[int] = None
                     ) -> Optional[Tuple[int, Tuple[int, int]]]:
        """
        Занимает наибольший свободный слот (новый агент — в конец очереди).
        Используется только для агентов ТЕКУЩЕГО подзаказа.
        """
        owners = self.slot_owners.get(packer_id, {})
        free_slots = sorted([sn for sn, oid in owners.items() if oid is None], reverse=True)
        if not free_slots: return None
        sn = free_slots[0]
        owners[sn] = agent_id
        self.slot_suborder[packer_id][sn] = suborder_id
        return sn, self.slots[packer_id][sn]

    def release_slot(self, packer_id: int, slot_num: int):
        owners = self.slot_owners.get(packer_id, {})
        if slot_num in owners:
            owners[slot_num] = None
            self.slot_suborder[packer_id][slot_num] = None

    def get_agent_slot(self, packer_id: int, agent_id: int) -> Optional[int]:
        for sn, aid in self.slot_owners.get(packer_id, {}).items():
            if aid == agent_id: return sn
        return None

    def slot_1_free(self, packer_id: int) -> bool:
        return self.slot_owners.get(packer_id, {}).get(1) is None

    def get_exclusive_reservations(self) -> Dict:
        """
        Возвращает словарь резерваций для всех занятых слотов.
        Формат совместим с reservations AgentManager:
          {pos: [{'agent_id': owner_id, 't_start': 0, 't_end': 0}]}
        Это означает: позиция слота постоянно занята для всех КРОМЕ owner_id.
        Передаётся роутеру — A* не прокладывает пути других агентов через эти клетки.
        """
        res: Dict = {}
        for pid, slot_dict in self.slots.items():
            for sn, pos in slot_dict.items():
                owner = self.slot_owners.get(pid, {}).get(sn)
                if owner is not None:
                    res.setdefault(pos, []).append(
                        {'agent_id': owner, 't_start': 0, 't_end': 0})
        return res


# ── AgentManager ─────────────────────────────────────────────────────────────
class AgentManager:
    def __init__(self, env, router: IRouter, map_data: Dict,
                 dispatcher=None, inventory=None,
                 packer_positions=None, packer_delivery_zones=None,
                 num_agents: int = 2):
        self.env                   = env
        self.router                = router
        self.dispatcher            = dispatcher
        self.inventory             = inventory
        self.packer_positions      = packer_positions      or {}
        self.packer_delivery_zones = packer_delivery_zones or {}
        self.num_agents            = num_agents
        self.agents: Dict[int, Agent] = {}
        self.current_tick          = 0

        self.reservations: Dict = {}  # сохраняется между тиками
        self.shelf_states: Dict[Tuple[int, int], str] = {}
        # Тик когда стеллаж был возвращён. Не назначается агентам
        # пока current_tick < cooldown_until, чтобы оранжевый был виден ≥N тиков.
        self._shelf_cooldown: Dict[Tuple[int, int], int] = {}
        self._SHELF_COOLDOWN_TICKS = 3   # минимум тиков оранжевого
        # Счётчик аборт-попыток для каждого стеллажа.
        # Если один стеллаж абортируется слишком много раз подряд —
        # он физически недостижим (заблокирован). Такой стеллаж
        # помечается как STUCK и исключается из подзаказа навсегда,
        # чтобы не зациклить систему в бесконечных retry.
        self._shelf_abort_count: Dict[Tuple[int, int], int] = {}
        self._SHELF_MAX_ABORTS  = 5  # попыток перед признанием недостижимым
        for row in map_data.get('cells', []):
            for c in row:
                if c['type'] == 'S':
                    self.shelf_states[(c['row'], c['col'])] = 'ACTIVE'

        self.slot_manager      = PackerSlotManager(map_data)
        self.deadlock_resolver = DeadlockResolver(stuck_threshold=8, map_data=map_data)

        if self.dispatcher and hasattr(self.dispatcher, 'packer_slots'):
            for pid in self.slot_manager.slots:
                self.dispatcher.packer_slots[pid] = self.slot_manager.total_slots(pid)
                logging.info(f"  Диспетчер: фасовщик {pid} = "
                             f"{self.dispatcher.packer_slots[pid]} слотов")

        self._init_agents(map_data)
        logging.info(f"🤖 Агентов: {len(self.agents)} | "
                     f"Стеллажей: {len(self.shelf_states)}")

    def _init_agents(self, map_data: Dict):
        restricted: set = set()
        for row in map_data.get('cells', []):
            for c in row:
                t = c.get('type', '')
                if t in ('Z', 'B', 'P', 'C', 'E', 'W', 'S'):
                    restricted.add((c['row'], c['col']))
                    if t in ('P', 'Z'):
                        for dr, dc in ((-1,0),(1,0),(0,-1),(0,1)):
                            restricted.add((c['row']+dr, c['col']+dc))
        a_pos = [(c['row'], c['col'])
                 for row in map_data.get('cells', [])
                 for c in row if c.get('text', '').startswith('A|')]
        occupied = set(a_pos)
        f_safe = [(c['row'], c['col'])
                  for row in map_data.get('cells', [])
                  for c in row
                  if c.get('type') == 'F'
                  and (c['row'], c['col']) not in occupied
                  and (c['row'], c['col']) not in restricted]
        f_any  = [(c['row'], c['col'])
                  for row in map_data.get('cells', [])
                  for c in row
                  if c.get('type') == 'F'
                  and (c['row'], c['col']) not in occupied]
        pool, seen = [], set()
        for p in (a_pos + f_safe + f_any):
            if p not in seen:
                seen.add(p); pool.append(p)
        spawn = pool[:self.num_agents]
        if not spawn:
            spawn = [(10, 10)] * self.num_agents
        # Реальный диаметр карты (манхэттен угол→угол = width + height)
        _map_max_dist = map_data.get('width', 100) + map_data.get('height', 100)
        for i, pos in enumerate(spawn, start=1):
            self.agents[i] = Agent(i, pos)
            self.agents[i].map_max_dist = _map_max_dist
            logging.info(f"  Агент {i} → старт {pos}")

    # ── helpers ──────────────────────────────────────────────────────────────
    def _advance_convoy(self, packer_id: int):
        """
        Конвойное продвижение: каждый агент сдвигается ровно на ОДИН слот вперёд,
        строго по условиям:
          1. Целевой слот (N-1) физически свободен — ни один агент не стоит там.
          2. Текущий агент физически стоит в своём слоте (статус WAITING_SLOT).

        Обрабатываем слоты от меньшего к большему:
          сначала продвигаем слот 2→1, потом 3→2 (только если 2 уже пуст), и т.д.
        Это гарантирует порядок вереницы без «обгона».
        """
        slots   = self.slot_manager.slots.get(packer_id, {})
        owners  = self.slot_manager.slot_owners.get(packer_id, {})
        sub_map = self.slot_manager.slot_suborder.get(packer_id, {})
        sorted_slots = sorted(slots.keys())

        for i, sn in enumerate(sorted_slots):
            if i == 0:
                continue  # слот 1 сам не может никуда сдвинуться

            prev_sn  = sorted_slots[i - 1]
            prev_pos = slots[prev_sn]
            cur_pos  = slots[sn]

            # Условие 1: предыдущий слот должен быть свободен в slot_owners
            if owners.get(prev_sn) is not None:
                continue  # слот N-1 ещё занят

            # Условие 1b: позиция N-1 должна быть физически пуста
            if any(ag.pos == prev_pos for ag in self.agents.values()):
                continue  # агент ещё физически стоит там

            # Условие 2: агент в слоте N физически стоит там и ждёт
            cur_owner = owners.get(sn)
            if cur_owner is None:
                continue  # слот N пуст — никого двигать

            ag = self.agents.get(cur_owner)
            if ag is None or ag.status != STATUS_WAITING_SLOT:
                continue  # агент ещё не добрался до своего слота — ждём

            # Физически ли агент стоит в cur_pos?
            if ag.pos != cur_pos:
                continue  # ещё в пути — ждём

            # Всё готово: двигаем агента из слота sn → prev_sn
            owners[prev_sn]  = cur_owner
            sub_map[prev_sn] = sub_map.get(sn)
            owners[sn]       = None
            sub_map[sn]      = None

            ag.assigned_slot_num = prev_sn
            ag.goal              = prev_pos
            ag.status            = STATUS_CARRY_TO_PACKER
            ag.urgency           = max(1, min(10, 11 - prev_sn))
            ag.path              = []

            logging.info(f"  🔄 Агент {ag.id}: слот {sn}→{prev_sn} @ {prev_pos} "
                         f"(конвой, фасовщик {packer_id})")

    def _try_hijack_blocker(self, stuck_ag: 'Agent') -> bool:
        """
        Вызывается когда stuck_ag завис в GO_TO_SHELF и не может добраться до полки.

        Проверяем: стоит ли другой агент прямо на целевой полке?
        Агент без груза может находиться на стеллаже — именно он блокирует путь.

        Стратегия:
          1. Найти агента blocker на позиции целевой полки.
          2. Если blocker IDLE (без задачи) — назначить ЕГО перевозчиком этой полки.
             stuck_ag при этом освобождает полку и уходит в IDLE.
          3. Если blocker занят (GO_TO_SHELF, CARRY...) — не трогаем, делаем обычный abort.

        Возвращает True если проблема решена (abort не нужен).
        """
        shelf_pos = stuck_ag.original_shelf_pos
        if shelf_pos is None:
            return False
        if stuck_ag.current_suborder is None:
            return False

        # Ищем агента, стоящего на целевой полке
        blocker = None
        for other in self.agents.values():
            if other.id != stuck_ag.id and other.pos == shelf_pos:
                blocker = other
                break

        if blocker is None:
            return False  # Полка свободна — другая причина застревания

        sub = stuck_ag.current_suborder
        pid = sub.packer_id

        # Блокирующий агент должен быть без груза и без активной задачи
        if blocker.has_cargo:
            logging.info(
                f"  🔄 Агент {stuck_ag.id}: блокер А{blocker.id} несёт груз — abort штатный")
            return False
        if blocker.status not in (STATUS_IDLE, STATUS_WAITING_SLOT):
            logging.info(
                f"  🔄 Агент {stuck_ag.id}: блокер А{blocker.id} "
                f"занят ({blocker.status}) — abort штатный")
            return False

        # Blocker стоит на полке и свободен → назначаем его перевозчиком
        # Шаг 1: stuck_ag освобождает полку (без возврата в pending — blocker возьмёт её)
        if pid and stuck_ag.assigned_slot_num:
            self.slot_manager.release_slot(pid, stuck_ag.assigned_slot_num)

        # Шаг 2: переносим слот и задачу на blocker
        # Blocker освобождает свой старый слот если был в очереди
        if blocker.assigned_slot_num is not None:
            blocker_pid = (blocker.current_suborder.packer_id
                           if blocker.current_suborder else pid)
            self.slot_manager.release_slot(blocker_pid, blocker.assigned_slot_num)

        # Захватываем слот для blocker
        pre_slot = self.slot_manager.acquire_slot(
            pid, blocker.id, suborder_id=sub.suborder_id)
        if pre_slot is None:
            # Нет свободных слотов — обычный abort
            logging.info(
                f"  🔄 Агент {stuck_ag.id}: блокер А{blocker.id} есть, "
                f"но нет слота → abort штатный")
            return False

        # Назначаем blocker перевозчиком
        self._assign_agent_to_shelf(blocker, pid, sub, shelf_pos, pre_slot)
        # Полка уже в TAKEN (была назначена stuck_ag), статус не меняем

        # Шаг 3: stuck_ag → IDLE, задача снята
        stuck_ag.status             = STATUS_IDLE
        stuck_ag.goal               = None
        stuck_ag.original_shelf_pos = None
        stuck_ag.current_suborder   = None
        stuck_ag.has_cargo          = False
        stuck_ag.assigned_slot_num  = None
        stuck_ag.path               = []
        stuck_ag.waiting_time       = 0
        # Счётчик абортов для этой полки — не увеличиваем: это не настоящий abort
        self._shelf_abort_count.pop(shelf_pos, None)

        logging.info(
            f"  🔀 Hijack: А{stuck_ag.id} застрял у полки {shelf_pos} "
            f"→ передал задачу А{blocker.id} (стоит на полке)")
        return True

    def _abort_go_to_shelf(self, ag: 'Agent'):
        """
        Аварийный сброс GO_TO_SHELF агента → IDLE.
        Освобождает стеллаж (TAKEN→ACTIVE, возвращает в pending_shelves) и слот.
        """
        pid = ag.current_suborder.packer_id if ag.current_suborder else None

        if ag.original_shelf_pos:
            shelf_pos = ag.original_shelf_pos

            # ── Счётчик аборт-попыток ──────────────────────────────────────
            self._shelf_abort_count[shelf_pos] = (
                self._shelf_abort_count.get(shelf_pos, 0) + 1)
            abort_cnt = self._shelf_abort_count[shelf_pos]

            self.shelf_states[shelf_pos] = 'ACTIVE'
            self._shelf_cooldown[shelf_pos] = (
                self.current_tick + self._SHELF_COOLDOWN_TICKS)

            if ag.current_suborder and hasattr(ag.current_suborder, 'pending_shelves'):

                if abort_cnt >= self._SHELF_MAX_ABORTS:
                    # Стеллаж физически недостижим — исключаем из подзаказа.
                    # НЕ возвращаем в pending_shelves — иначе бесконечный retry.
                    ag.current_suborder.pending_shelves.discard(shelf_pos)
                    if ag.current_suborder.active_agents > 0:
                        ag.current_suborder.active_agents -= 1
                    # Помечаем как STUCK (не берётся никем)
                    self.shelf_states[shelf_pos] = 'STUCK'
                    logging.warning(
                        f"  🚫 Стеллаж {shelf_pos} недостижим "
                        f"({abort_cnt} аборт-попыток) — исключён из подзаказа "
                        f"#{ag.current_suborder.suborder_id}")
                    # Если pending_shelves опустел — подзаказ теперь в COMPLETING
                    try:
                        from dispatcher import SubOrderStatus as _SOS
                        sub = ag.current_suborder
                        if (not sub.pending_shelves
                                and sub.status == _SOS.IN_PROGRESS):
                            sub.status = _SOS.COMPLETING
                            logging.info(
                                f"  🔄 Подзаказ #{sub.suborder_id}: "
                                f"→ COMPLETING (последний стеллаж недостижим)")
                    except ImportError:
                        pass
                else:
                    # Обычный abort — возвращаем в очередь
                    ag.current_suborder.pending_shelves.add(shelf_pos)
                    if ag.current_suborder.active_agents > 0:
                        ag.current_suborder.active_agents -= 1
                    # Агент прерван до разгрузки — delivered_agents не трогаем
                    try:
                        from dispatcher import SubOrderStatus as _SOS
                        if ag.current_suborder.status == _SOS.COMPLETING:
                            ag.current_suborder.status = _SOS.IN_PROGRESS
                            if self.dispatcher:
                                pid_sub = ag.current_suborder.packer_id
                                cur_active = self.dispatcher.active_suborder_id.get(pid_sub)
                                if cur_active != ag.current_suborder.suborder_id:
                                    self.dispatcher.active_suborder_id[pid_sub] = (
                                        ag.current_suborder.suborder_id)
                                    logging.info(
                                        f"  ↩️ Подзаказ #{ag.current_suborder.suborder_id}: "
                                        f"COMPLETING→IN_PROGRESS, active_suborder_id восстановлен")
                        elif ag.current_suborder.status == _SOS.IN_PROGRESS:
                            logging.info(
                                f"  ↩️ Подзаказ #{ag.current_suborder.suborder_id}: "
                                f"стеллаж {shelf_pos} возвращён "
                                f"(попытка {abort_cnt}/{self._SHELF_MAX_ABORTS})")
                    except ImportError:
                        pass

        if pid and ag.assigned_slot_num:
            self.slot_manager.release_slot(pid, ag.assigned_slot_num)

        ag.status             = STATUS_IDLE
        ag.goal               = None
        ag.current_suborder   = None
        ag.has_cargo          = False
        ag.original_shelf_pos = None
        ag.assigned_slot_num  = None
        ag.shortest_path_len  = 0
        ag.path               = []
        ag.waiting_time       = 0

    def print_order_summary(self):
        """
        Выводит в консоль текущее состояние заказов и назначений.
        Формат: Заказ → Подзаказы → Агенты.
        Дополнительно: разбивка IDLE/ожидающих агентов для диагностики freeze.
        """
        if not self.dispatcher:
            return
        print("\n" + "═"*60)
        print(f"  📋 ЗАКАЗЫ — тик {self.current_tick}")
        print("═"*60)

        # Карта: suborder_id → список агентов
        sub_agents: Dict[int, List[int]] = {}
        for ag in self.agents.values():
            if ag.current_suborder is not None:
                sid = ag.current_suborder.suborder_id
                sub_agents.setdefault(sid, []).append(ag.id)

        # Карта: статус → список агентов (для диагностики)
        status_groups: Dict[str, List[int]] = {}
        for ag in self.agents.values():
            status_groups.setdefault(ag.status, []).append(ag.id)

        for pid in self.dispatcher.packer_ids:
            act_oid = self.dispatcher.active_order_id.get(pid)
            act_sid = self.dispatcher.active_suborder_id.get(pid)
            print(f"\n  Фасовщик {pid}:")

            if act_oid is None:
                print("    — нет активного заказа")
                continue

            order = self.dispatcher.orders.get(act_oid)
            if order is None:
                continue

            print(f"    Заказ #{act_oid}  ({len(order.suborders)} подзаказов)")

            for sub in order.suborders:
                sid   = sub.suborder_id
                st    = sub.status.value
                agents = sub_agents.get(sid, [])
                is_act = "◀ АКТИВНЫЙ" if sid == act_sid else ""
                pending_n = len(sub.pending_shelves)
                total_n   = len(sub.required_shelves)

                agents_str = (", ".join(f"А{a}" for a in sorted(agents))
                              if agents else "—")

                # Показываем стеллажи с аборт-предупреждениями
                stuck_shelves = [
                    f"{pos}×{cnt}"
                    for pos, cnt in self._shelf_abort_count.items()
                    if pos in sub.pending_shelves and cnt > 0]
                stuck_str = f"  ⚠️abort:{','.join(stuck_shelves)}" if stuck_shelves else ""

                print(f"      Подзаказ #{sid:3d}  [{st:13s}]  "
                      f"{total_n}п({pending_n}ждут/{sub.active_agents}в пути)  "
                      f"агенты: {agents_str}  {is_act}{stuck_str}")

        # ── Диагностика: статусы всех агентов ────────────────────────────
        idle_ids = sorted(status_groups.get(STATUS_IDLE, []))
        wait_ids = sorted(status_groups.get(STATUS_WAITING_SLOT, []))
        go_ids   = sorted(status_groups.get(STATUS_GO_TO_SHELF, []))
        carry_ids= sorted(status_groups.get(STATUS_CARRY_TO_PACKER, []))
        ret_ids  = sorted(status_groups.get(STATUS_RETURN_SHELF, []))
        unl_ids  = sorted(status_groups.get(STATUS_UNLOADING, []))

        parts = []
        if idle_ids:  parts.append(f"IDLE:{','.join(f'А{i}' for i in idle_ids)}")
        if go_ids:    parts.append(f"GO:{','.join(f'А{i}' for i in go_ids)}")
        if carry_ids: parts.append(f"CARRY:{','.join(f'А{i}' for i in carry_ids)}")
        if unl_ids:   parts.append(f"UNLOAD:{','.join(f'А{i}' for i in unl_ids)}")
        if wait_ids:  parts.append(f"WAIT_SLOT:{','.join(f'А{i}' for i in wait_ids)}")
        if ret_ids:   parts.append(f"RETURN:{','.join(f'А{i}' for i in ret_ids)}")
        if parts:
            print(f"\n  🤖 Агенты: {' | '.join(parts)}")

        print("═"*60 + "\n")

    def _assign_agent_to_shelf(self, ag: 'Agent', pid: int, sub, shelf_pos, pre_slot):
        """Вспомогательный: устанавливает агенту задачу GO_TO_SHELF."""
        self.shelf_states[shelf_pos] = 'TAKEN'
        ag.status             = STATUS_GO_TO_SHELF
        ag.goal               = shelf_pos
        ag.original_shelf_pos = shelf_pos
        ag.current_suborder   = sub
        ag.has_cargo          = False
        ag.unload_timer       = 0
        ag.assigned_slot_num  = pre_slot[0]
        ag.shortest_path_len  = 0
        ag.waiting_time       = 0
        ag.urgency            = max(1, min(10, 11 - pre_slot[0]))
        logging.info(f"  🚀 Агент {ag.id} → подзаказ #{sub.suborder_id} "
                     f"стеллаж {shelf_pos} [слот {pre_slot[0]}]")

    def _find_packer_for_agent(self, ag: 'Agent') -> Optional[int]:
        if not self.dispatcher:
            return None
        for pid in self.slot_manager.slots:
            free = self.slot_manager.free_slots_count(pid)
            if free > 0 and self.dispatcher.get_assignable_suborders(
                    pid, free) is not None:
                return pid
            if self.slot_manager.last_slot_free(pid):
                if self.dispatcher.get_next_pending_suborder(pid) is not None:
                    return pid
        return None

    def _pick_free_shelf(self, sub) -> Optional[Tuple[int, int]]:
        for sp in sorted(sub.pending_shelves):
            if self.shelf_states.get(sp) != 'ACTIVE':
                continue
            # Не назначаем стеллаж пока не истёк cooldown после возврата.
            # Это даёт время визуализатору показать оранжевый цвет.
            if self.current_tick < self._shelf_cooldown.get(sp, 0):
                continue
            return sp
        return None

    # ── основной тик ─────────────────────────────────────────────────────────
    def run_tick(self) -> Dict[str, Any]:
        self.current_tick += 1

        # Заполняем reservations перед планированием.
        # Статичные агенты (IDLE/UNLOADING/WAITING_SLOT или без цели) —
        # t_start=t_end=0 → _is_blocked интерпретирует как постоянную блокировку
        # на всех временны́х шагах. Движущиеся агенты резервируются только на t=0
        # (их пути добавятся через _reserve после solve).
        reservations = self.reservations
        reservations.clear()
        _static_statuses = (STATUS_IDLE, STATUS_UNLOADING, STATUS_WAITING_SLOT)
        for _ag in self.agents.values():
            reservations.setdefault(_ag.pos, []).append(
                {'agent_id': _ag.id, 't_start': 0, 't_end': 0})

        # Эксклюзивные резервации слотов: каждый занятый слот — постоянный блок
        # для всех КРОМЕ владельца. Роутер не прокладывает чужие пути через них.
        # Это гарантирует физическую эксклюзивность слота.
        for slot_res_pos, slot_res_entry in self.slot_manager.get_exclusive_reservations().items():
            existing = reservations.get(slot_res_pos, [])
            # Если позиция уже в reservations (агент стоит там) — добавляем поверх
            for entry in slot_res_entry:
                if not any(e['agent_id'] == entry['agent_id'] for e in existing):
                    existing.append(entry)
            reservations[slot_res_pos] = existing

        # ── 0. Восстановление осиротевших стеллажей ─────────────────────────
        # Стеллаж 'TAKEN'/'RETURNING' должен иметь живого агента.
        # Если агент завершил цикл или переназначен — стеллаж зависает серым.
        occupied_shelves: set = set()
        for ag in self.agents.values():
            if ag.original_shelf_pos:
                occupied_shelves.add(ag.original_shelf_pos)
        for pos, state in list(self.shelf_states.items()):
            if state in ('TAKEN', 'RETURNING') and pos not in occupied_shelves:
                self.shelf_states[pos] = 'ACTIVE'
                logging.debug(f"  🔄 Стеллаж {pos}: {state}→ACTIVE (агент пропал)")

        # ── 0b. Время ожидания ───────────────────────────────────────────────
        for ag in self.agents.values():
            if ag.status in (STATUS_WAITING_SLOT, STATUS_IDLE):
                ag.waiting_time = min(1000, ag.waiting_time + 1)
            else:
                ag.waiting_time = 0

        # ── 0c. Ранняя обработка UNLOADING ───────────────────────────────────
        # КРИТИЧНО: завершение разгрузки должно происходить ДО шага 2 (назначение),
        # иначе шаг 2 никогда не увидит освобождённые слоты — они появлялись в шаге 3
        # (после назначения) и следующий подзаказ запускался только в следующем тике.
        #
        # Также отслеживаем "vacating_slots" — позиции слотов которые освобождаются
        # ПРЯМО СЕЙЧАС: агент уходит в RETURN_SHELF и физически ещё стоит на месте
        # слота, но бронь снята. Шаг 2 должен игнорировать физическое присутствие
        # этих агентов при проверке свободности слота.
        vacating_slot_positions: set = set()

        for ag in self.agents.values():
            if ag.status != STATUS_UNLOADING:
                continue
            ag.unload_timer -= 1
            if ag.unload_timer > 0:
                continue

            pid = ag.current_suborder.packer_id if ag.current_suborder else None
            # Запоминаем позицию слота ДО освобождения
            if pid and ag.assigned_slot_num:
                slot_pos_vac = self.slot_manager.slots.get(pid, {}).get(
                    ag.assigned_slot_num)
                if slot_pos_vac:
                    vacating_slot_positions.add(slot_pos_vac)
                self.slot_manager.release_slot(pid, ag.assigned_slot_num)
            if self.dispatcher and ag.current_suborder:
                self.dispatcher.mark_delivery_complete(
                    ag.current_suborder.suborder_id)
            ag.status            = STATUS_RETURN_SHELF
            ag.goal              = ag.original_shelf_pos
            ag.has_cargo         = True
            ag.assigned_slot_num = None
            ag.shortest_path_len = 0
            ag.path              = []
            if ag.original_shelf_pos:
                self.shelf_states[ag.original_shelf_pos] = 'RETURNING'


        # Каждый агент сдвигается на один слот только когда предыдущая позиция
        # физически пуста и он сам физически в своём слоте (WAITING_SLOT).
        for pid in self.slot_manager.slots:
            self._advance_convoy(pid)

        # ── 2. Назначение задач ──────────────────────────────────────────────
        #
        # Два режима назначения:
        #   ТЕКУЩИЙ подзаказ: acquire_slot (любой свободный слот, кроме last)
        #     → агент встаёт в хвост очереди текущего подзаказа
        #   СЛЕДУЮЩИЙ подзаказ: acquire_last_slot (ТОЛЬКО последний слот)
        #     → обеспечивает непрерывный поток к фасовщику
        #     → номер в подзаказе = 1, но физически — в последней позиции очереди
        #
        if self.dispatcher:
            from dispatcher import SubOrderStatus as _SOS
            free_agents = sorted(
                [a for a in self.agents.values()
                 if a.status == STATUS_IDLE and not a.path],
                key=lambda a: -a.waiting_time)

            for ag in free_agents:
                assigned = False

                # Позиции всех агентов — строим один раз на итерацию (O(N))
                _all_positions = {a.pos for a in self.agents.values()} - vacating_slot_positions

                for pid in list(self.slot_manager.slots.keys()):
                    if assigned: break

                    # ── 2a. Попытка войти в ТЕКУЩИЙ подзаказ ──────────────
                    # Используем тот же принцип что и 2b: слот должен быть
                    # свободен и в slot_owners, и физически. Это важно когда
                    # mark_delivery_complete переключил active_suborder_id раньше
                    # чем предыдущие агенты физически покинули свои слоты.
                    free_slots = self.slot_manager.free_slots_count(pid)
                    if free_slots > 0:
                        cur_sub = self.dispatcher.get_assignable_suborders(pid, free_slots)
                        if cur_sub is not None:
                            target_shelf = self._pick_free_shelf(cur_sub)
                            if target_shelf is not None:
                                # Ищем слот свободный И по owners, И физически
                                _owners_2a = self.slot_manager.slot_owners.get(pid, {})
                                _slots_2a  = self.slot_manager.slots.get(pid, {})
                                _free_sn_2a = None
                                for _sn in sorted(_owners_2a.keys(), reverse=True):
                                    if _owners_2a[_sn] is not None:
                                        continue
                                    if _slots_2a.get(_sn) in _all_positions:
                                        continue  # физически занят
                                    _free_sn_2a = _sn
                                    break
                                if _free_sn_2a is not None:
                                    _owners_2a[_free_sn_2a] = ag.id
                                    self.slot_manager.slot_suborder.get(
                                        pid, {})[_free_sn_2a] = cur_sub.suborder_id
                                    pre_slot = (_free_sn_2a, _slots_2a[_free_sn_2a])
                                    if self.dispatcher.take_shelf_from_suborder(
                                            cur_sub.suborder_id, target_shelf, ag.id):
                                        self._assign_agent_to_shelf(
                                            ag, pid, cur_sub, target_shelf, pre_slot)
                                        assigned = True
                                        break
                                    else:
                                        _owners_2a[_free_sn_2a] = None
                                        self.slot_manager.slot_suborder.get(
                                            pid, {})[_free_sn_2a] = None

                    # ── 2b. Конвейер следующего подзаказа ─────────────────
                    # ТЗ: «когда освобождаются слоты у фасовщика, в них могут
                    # ехать агенты из следующего подзаказа. При этом слот
                    # не должен быть занят или забронирован агентами текущего
                    # подзаказа. При переназначении агента в текущем подзаказе
                    # сначала сдаётся текущий — следующий не имеет права занимать
                    # слоты нужные текущему».
                    #
                    # Алгоритм:
                    #   A. Считаем действительно свободные слоты:
                    #      slot_owners[N] is None  И  позиция физически пуста
                    #      (конвой обнуляет slot_owners раньше физического хода)
                    #   B. Считаем сколько из них НУЖНЫ текущему подзаказу:
                    #      = len(cur_sub.pending_shelves)
                    #      (каждому pending-стеллажу нужен один свободный слот)
                    #   C. Доступно следующему = max(0, A - B)
                    #   D. Берём слот с наибольшим номером (хвост очереди)
                    #      — агент следующего подзаказа встаёт позади всех текущих
                    if not assigned:
                        cur_sid_2b = self.dispatcher.active_suborder_id.get(pid)
                        if cur_sid_2b is not None:
                            # Находим текущий активный подзаказ
                            cur_sub_2b = None
                            for _ord2 in self.dispatcher.active_queue:
                                for _s2 in _ord2.suborders:
                                    if _s2.suborder_id == cur_sid_2b:
                                        cur_sub_2b = _s2
                                        break
                                if cur_sub_2b:
                                    break

                            if cur_sub_2b is not None:
                                owners_2b      = self.slot_manager.slot_owners.get(pid, {})
                                slots_2b       = self.slot_manager.slots.get(pid, {})
                                agent_pos_set  = {a.pos for a in self.agents.values()} - vacating_slot_positions

                                # A. Все реально свободные слоты (sorted desc = хвост первым)
                                free_slots_2b = sorted(
                                    [sn for sn, oid in owners_2b.items()
                                     if oid is None
                                     and slots_2b.get(sn) not in agent_pos_set],
                                    reverse=True)

                                # B. Сколько нужно текущему подзаказу
                                reserved_for_current = len(cur_sub_2b.pending_shelves)

                                # C. Сколько доступно следующему
                                available_for_next = len(free_slots_2b) - reserved_for_current

                                if available_for_next > 0:
                                    next_sub_2b = self.dispatcher.get_next_pending_suborder(pid)
                                    if next_sub_2b is not None:
                                        target_2b = self._pick_free_shelf(next_sub_2b)
                                        if target_2b is not None:
                                            # D. Берём наибольший свободный слот
                                            sn_2b      = free_slots_2b[0]
                                            slot_pos_2b = slots_2b[sn_2b]
                                            # Бронируем
                                            owners_2b[sn_2b] = ag.id
                                            self.slot_manager.slot_suborder.get(
                                                pid, {})[sn_2b] = next_sub_2b.suborder_id
                                            pre_slot_2b = (sn_2b, slot_pos_2b)
                                            if self.dispatcher.take_shelf_from_suborder(
                                                    next_sub_2b.suborder_id,
                                                    target_2b, ag.id):
                                                self._assign_agent_to_shelf(
                                                    ag, pid, next_sub_2b,
                                                    target_2b, pre_slot_2b)
                                                assigned = True
                                                logging.info(
                                                    f"  ⏩ Конвейер: А{ag.id} → подзаказ "
                                                    f"#{next_sub_2b.suborder_id} слот {sn_2b} "
                                                    f"(текущему нужно {reserved_for_current} слотов, "
                                                    f"свободно {len(free_slots_2b)})")
                                            else:
                                                # Откат
                                                owners_2b[sn_2b] = None
                                                self.slot_manager.slot_suborder.get(
                                                    pid, {})[sn_2b] = None

        # ── 3. Переходы состояний ────────────────────────────────────────────
        # Примечание: STATUS_UNLOADING обрабатывается в шаге 0c (до назначения),
        # чтобы освобождённые слоты были видны шагу 2 в том же тике.
        for ag in self.agents.values():

            if ag.status == STATUS_GO_TO_SHELF and ag.goal and ag.pos == ag.goal:
                # Успешно добрались до стеллажа — сбрасываем счётчик аборт-попыток
                if ag.original_shelf_pos in self._shelf_abort_count:
                    self._shelf_abort_count.pop(ag.original_shelf_pos, None)
                pid = ag.current_suborder.packer_id if ag.current_suborder else None
                if pid is None:
                    ag.path = []; continue

                # Слот pre-assigned при назначении задачи.
                # Проверяем что слот всё ещё за этим агентом после возможных reorder_queue.
                current_sn = self.slot_manager.get_agent_slot(pid, ag.id)
                if current_sn is None:
                    # Слот потерян (reorder убрал агента из очереди).
                    # Пробуем взять заново.
                    _sub_id = ag.current_suborder.suborder_id if ag.current_suborder else None
                    slot_res = self.slot_manager.acquire_slot(pid, ag.id, suborder_id=_sub_id)
                    if slot_res is None:
                        # Все слоты заняты — аварийный сброс.
                        # Продолжение ожидания = вечная заморозка: стеллаж TAKEN,
                        # агент в GO_TO_SHELF без пути, подзаказ не продвигается.
                        logging.warning(
                            f"  ⚠️ Агент {ag.id}: прибыл на стеллаж "
                            f"{ag.original_shelf_pos} но нет слота → сброс в IDLE")
                        self._abort_go_to_shelf(ag)
                        continue
                    current_sn = slot_res[0]

                ag.assigned_slot_num = current_sn  # синхронизируем с реальным слотом
                slot_pos = self.slot_manager.slots.get(pid, {}).get(current_sn)
                if slot_pos is None:
                    ag.path = []; continue
                ag.goal              = slot_pos
                ag.urgency           = max(1, min(10, 11 - current_sn))
                ag.status            = STATUS_CARRY_TO_PACKER
                ag.has_cargo         = True
                ag.shortest_path_len = 0
                ag.path              = []
                logging.info(f"  📦 Агент {ag.id} несёт товар → слот {current_sn} @ {ag.goal}")

            elif ag.status == STATUS_CARRY_TO_PACKER and ag.goal and ag.pos == ag.goal:
                if ag.assigned_slot_num == 1:
                    # Ворота разгрузки: разгружаемся только если наш подзаказ активен.
                    # Защищает от ситуации когда агент следующего подзаказа по конвою
                    # добрался до слота 1 пока текущий подзаказ ещё в COMPLETING.
                    _pid_gate   = ag.current_suborder.packer_id if ag.current_suborder else None
                    _my_sid     = ag.current_suborder.suborder_id if ag.current_suborder else None
                    _active_sid = (self.dispatcher.active_suborder_id.get(_pid_gate)
                                   if self.dispatcher and _pid_gate else None)
                    _can_unload = (_active_sid is None or _active_sid == _my_sid)
                    if _can_unload:
                        ag.status       = STATUS_UNLOADING
                        ag.unload_timer = 5
                        logging.info(f"  📦 Агент {ag.id} разгружается "
                                     f"(слот 1, фасовщик {_pid_gate})")
                    # else: ждём в слоте 1 — остаёмся STATUS_CARRY_TO_PACKER,
                    # следующий тик проверит снова когда переключится active_suborder_id
                else:
                    # Агент достиг своего слота в очереди — ждёт продвижения
                    ag.status = STATUS_WAITING_SLOT
                    logging.info(f"  ⏳ Агент {ag.id} ждёт в слоте "
                                 f"{ag.assigned_slot_num} @ {ag.pos}")
                ag.path = []

            elif ag.status == STATUS_RETURN_SHELF and ag.goal and ag.pos == ag.goal:
                if ag.original_shelf_pos:
                    self.shelf_states[ag.original_shelf_pos] = 'ACTIVE'
                    # Защита от немедленного переназначения: стеллаж будет
                    # оранжевым минимум _SHELF_COOLDOWN_TICKS тиков.
                    self._shelf_cooldown[ag.original_shelf_pos] = (
                        self.current_tick + self._SHELF_COOLDOWN_TICKS)
                # Освобождаем слот ДО того как обнуляем assigned_slot_num,
                # иначе slot_owners[N] останется с ID агента и 2a/2b не увидят
                # свободного слота в следующем тике → freeze следующего подзаказа.
                if ag.current_suborder and ag.assigned_slot_num is not None:
                    _ret_pid = ag.current_suborder.packer_id
                    self.slot_manager.release_slot(_ret_pid, ag.assigned_slot_num)
                if self.dispatcher and ag.current_suborder:
                    self.dispatcher.complete_agent_in_suborder(
                        ag.current_suborder.suborder_id)
                ag.status             = STATUS_IDLE
                ag.goal               = None
                ag.current_suborder   = None
                ag.has_cargo          = False
                ag.unload_timer       = 0
                ag.original_shelf_pos = None
                ag.assigned_slot_num  = None
                ag.shortest_path_len  = 0
                ag.path               = []

        # ── 4. DeadlockResolver ──────────────────────────────────────────────
        # update_last_move вызывается ПОСЛЕ движения (в конце тика — шаг 6),
        # но check_and_resolve нужен ДО планирования — поэтому вызываем здесь
        # с данными прошлого тика (позиции уже актуальны после move).
        actions = self.deadlock_resolver.check_and_resolve(
            self.agents, reservations, self.current_tick)

        # ── 5. Формирование задач для роутера ────────────────────────────────
        tasks = []
        for ag in self.agents.values():
            if ag.goal is None: continue
            if ag.status in (STATUS_UNLOADING, STATUS_IDLE, STATUS_WAITING_SLOT):
                continue

            # GO_TO_SHELF + REPLAN = агент застрял.
            if ag.status == STATUS_GO_TO_SHELF and actions.get(ag.id) == 'REPLAN':
                # Сначала проверяем: стоит ли другой агент на целевой полке?
                # Агент без груза может находиться на стеллаже — он и блокирует путь.
                # В этом случае назначаем блокирующего агента перевозчиком этой полки.
                hijacked = self._try_hijack_blocker(ag)
                if not hijacked:
                    logging.warning(f"  ⚠️ Агент {ag.id}: GO_TO_SHELF застрял → abort")
                    self._abort_go_to_shelf(ag)
                continue

            # RETURN_SHELF + REPLAN = агент застрял возвращая стеллаж.
            # Принудительно завершаем возврат: стеллаж → ACTIVE, агент → IDLE.
            # Без этого active_agents никогда не обнулится → подзаказ вечно COMPLETING.
            if ag.status == STATUS_RETURN_SHELF and actions.get(ag.id) == 'REPLAN':
                logging.warning(f"  ⚠️ Агент {ag.id}: RETURN_SHELF застрял → принудительное завершение")
                if ag.original_shelf_pos:
                    self.shelf_states[ag.original_shelf_pos] = 'ACTIVE'
                    self._shelf_cooldown[ag.original_shelf_pos] = (
                        self.current_tick + self._SHELF_COOLDOWN_TICKS)
                # Освобождаем слот явно (иначе slot_owners остаётся с ID агента)
                if ag.current_suborder and ag.assigned_slot_num is not None:
                    self.slot_manager.release_slot(
                        ag.current_suborder.packer_id, ag.assigned_slot_num)
                if self.dispatcher and ag.current_suborder:
                    self.dispatcher.complete_agent_in_suborder(
                        ag.current_suborder.suborder_id)
                ag.status             = STATUS_IDLE
                ag.goal               = None
                ag.current_suborder   = None
                ag.has_cargo          = False
                ag.original_shelf_pos = None
                ag.assigned_slot_num  = None
                ag.shortest_path_len  = 0
                ag.path               = []
                continue

            returning_shelf = (ag.status == STATUS_RETURN_SHELF)
            if ag.shortest_path_len == 0 and hasattr(self.router, 'path_length'):
                ag.shortest_path_len = self.router.path_length(
                    ag.pos, ag.goal, ag.has_cargo, returning_shelf)

            ag.compute_score()
            tasks.append({
                'agent_id':        ag.id,
                'start':           ag.pos,
                'goal':            ag.goal,
                'score':           ag.score,
                'has_cargo':       ag.has_cargo,
                'returning_shelf': returning_shelf,
                'action':          actions.get(ag.id, 'OK'),
            })

        # ── 6. Планирование и движение ───────────────────────────────────────
        paths = self.router.solve(tasks, reservations)

        # 6_L2. WFG-обнаружение циклов + локальный CBS (Уровень 2).
        # Запускается ПОСЛЕ router.solve() и ДО записи путей в агентов.
        # Перепланирует только агентов, попавших в циклический дедлок,
        # оставляя пути остальных нетронутыми.
        paths = self.deadlock_resolver.resolve_local_deadlocks(
            self.agents, paths, tasks, self.current_tick,
            reservations=reservations)

        # 6a. Записываем пути (ещё не двигаемся)
        for aid, path in paths.items():
            ag = self.agents[aid]
            actual_len = len(path) - 1 if len(path) > 1 else 0
            ag.compute_score(actual_path_len=actual_len)
            if actual_len > 0 and actual_len != ag.shortest_path_len:
                ag.shortest_path_len = 0
            ag.path               = path
            ag.current_path_index = 1  # path[0]=cur pos, path[1]=next step

        # 6b. Финальный enforcement: vertex + swap + IDLE-уступка.
        # Возвращает (allowed_to_move, idle_displacements):
        #   allowed_to_move   — агенты с путями, которым разрешён шаг
        #   idle_displacements — {idle_id: new_pos} физические смещения IDLE
        allowed_to_move, idle_displacements = self.deadlock_resolver.resolve_movement_conflicts(
            self.agents, paths)

        # 6c. move() только для разрешённых активных агентов (есть путь от роутера)
        for aid, path in paths.items():
            ag = self.agents[aid]
            if aid in allowed_to_move and len(path) >= 2:
                ag.move()
            else:
                ag.current_path_index = 0  # перепланировать в следующем тике

        # 6d. Физические смещения IDLE-агентов (уступили дорогу активному агенту).
        # IDLE не имеют путей от роутера → ag.move() их не затрагивает.
        # Применяем позиции напрямую — controlled swap уже гарантирует отсутствие
        # коллизий (IDLE уходит на клетку, которую активный агент покидает).
        for idle_id, new_pos in idle_displacements.items():
            self.agents[idle_id].pos = new_pos

        # DeadlockResolver запоминает позиции ПОСЛЕ движения
        self.deadlock_resolver.update_last_move(self.agents, self.current_tick)

        # ── 7. Сборка state ───────────────────────────────────────────────────
        return {
            'agents': {
                ag.id: {
                    'pos':          ag.pos,
                    'status':       ag.status,
                    'score':        ag.score,
                    'id':           ag.id,
                    'has_cargo':    ag.has_cargo,
                    'unload_timer': ag.unload_timer,
                    'path':         ag.path,
                }
                for ag in self.agents.values()
            },
            'reservations': reservations,
            'shelf_states': self.shelf_states,
        }


# Обратная совместимость
from router_interface import PrioritizedAStarSolver as AStarRouter