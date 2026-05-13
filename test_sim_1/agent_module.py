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
from typing import Dict, List, Optional, Tuple, Any, Set

# Синхронизировано с router_interface._PERM.
# Статичные агенты (IDLE / WAITING_SLOT / UNLOADING) и занятые слоты
# бронируются с t_end=_PERM — блокируют на всех t≥0.
# Движущиеся агенты бронируются с t_end=0 — блокируют только в t=0;
# CBS / LNS могут прокладывать пути через их позиции в t≥1.
_PERM: int = 99_999
# Импортируем _IDLE_PERM: cargo-агенты не блокируются IDLE позициями в A*
try:
    from router_interface import _IDLE_PERM
except ImportError:
    _IDLE_PERM: int = 99_998

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
        t_end=_PERM → CBS/_is_blocked трактует как постоянную бронь на всех t≥0.
        """
        res: Dict = {}
        for pid, slot_dict in self.slots.items():
            for sn, pos in slot_dict.items():
                owner = self.slot_owners.get(pid, {}).get(sn)
                if owner is not None:
                    res.setdefault(pos, []).append(
                        {'agent_id': owner, 't_start': 0, 't_end': _PERM})
        return res


# ── AgentManager ─────────────────────────────────────────────────────────────
class AgentManager:
    def __init__(self, env, router: IRouter, map_data: Dict,
                 dispatcher=None, inventory=None,
                 packer_positions=None, packer_delivery_zones=None,
                 num_agents: int = 2,
                 metrics=None):          # MetricsCollector | None
        self.env                   = env
        self.router                = router
        self.dispatcher            = dispatcher
        self.inventory             = inventory
        self.packer_positions      = packer_positions      or {}
        self.packer_delivery_zones = packer_delivery_zones or {}
        self.num_agents            = num_agents
        self.metrics               = metrics   # MetricsCollector
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

        # Счётчик consecutive "нет пути" и stuck-counter для диагностики
        self._no_path_count:  Dict[int, int] = {}
        self._last_move_tick: Dict[int, int] = {}
        for row in map_data.get('cells', []):
            for c in row:
                if c['type'] == 'S':
                    self.shelf_states[(c['row'], c['col'])] = 'ACTIVE'

        self.slot_manager      = PackerSlotManager(map_data)
        self.deadlock_resolver = DeadlockResolver(stuck_threshold=8, map_data=map_data,
                                                  metrics=self.metrics)

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

        if hasattr(self.router, 'invalidate_plan'):
            self.router.invalidate_plan(ag.id)

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
        Выводит в консоль текущее состояние заказов и детальную таблицу агентов.
        БАГ 5: добавлена построчная таблица каждого агента (позиция, цель,
        статус, наличие груза, длина пути, score, слот) для диагностики.
        """
        if not self.dispatcher:
            return
        print("\n" + "═"*70)
        print(f"  📋 ЗАКАЗЫ — тик {self.current_tick}")
        print("═"*70)

        sub_agents: Dict[int, List[int]] = {}
        for ag in self.agents.values():
            if ag.current_suborder is not None:
                sub_agents.setdefault(ag.current_suborder.suborder_id, []).append(ag.id)

        for pid in self.dispatcher.packer_ids:
            act_oid = self.dispatcher.active_order_id.get(pid)
            act_sid = self.dispatcher.active_suborder_id.get(pid)
            print(f"\n  Фасовщик {pid}:")
            if act_oid is None:
                print("    — нет активного заказа"); continue
            order = self.dispatcher.orders.get(act_oid)
            if order is None: continue
            print(f"    Заказ #{act_oid}  ({len(order.suborders)} подзаказов)")
            for sub in order.suborders:
                sid       = sub.suborder_id
                st        = sub.status.value
                agents    = sub_agents.get(sid, [])
                is_act    = "◀ АКТИВНЫЙ" if sid == act_sid else ""
                pending_n = len(sub.pending_shelves)
                total_n   = len(sub.required_shelves)
                agents_str = (", ".join(f"А{a}" for a in sorted(agents)) or "—")
                stuck_shelves = [f"{pos}×{cnt}"
                                 for pos, cnt in self._shelf_abort_count.items()
                                 if pos in sub.pending_shelves and cnt > 0
                                 and not str(pos).startswith('ret_')]
                stuck_str = f"  ⚠️{','.join(stuck_shelves)}" if stuck_shelves else ""
                print(f"      #{sid:3d}  [{st:13s}]  "
                      f"{total_n}п({pending_n}ждут/{sub.active_agents}в пути)  "
                      f"агенты: {agents_str}  {is_act}{stuck_str}")

        # ── Детальная таблица агентов (БАГ 5) ────────────────────────────
        STATUS_SHORT = {
            STATUS_IDLE:            "IDLE        ",
            STATUS_GO_TO_SHELF:     "GO_SHELF    ",
            STATUS_CARRY_TO_PACKER: "CARRY_PACK  ",
            STATUS_WAITING_SLOT:    "WAIT_SLOT   ",
            STATUS_UNLOADING:       "UNLOADING   ",
            STATUS_RETURN_SHELF:    "RETURN_SHELF",
        }
        print(f"\n  {'А':>3}  {'Статус':<14} {'Позиция':<10} {'Цель':<10} "
              f"{'Груз':<5} {'Путь':>5} {'Score':>6} {'Слот':>5} {'Кэш':<7} {'Стк':>4} {'НП':>4}")
        print("  " + "─"*82)
        cached_paths  = getattr(self.router, '_cached_paths', {})
        stuck_since   = getattr(self.deadlock_resolver, 'stuck_since', {})
        stuck_agents  = []   # для блока диагностики

        for aid in sorted(self.agents):
            ag       = self.agents[aid]
            st_str   = STATUS_SHORT.get(ag.status, ag.status[:12])
            pos_str  = str(ag.pos)
            goal_str = str(ag.goal) if ag.goal else "—"
            cargo    = "✓" if ag.has_cargo else "—"
            path_len = len(ag.path) - ag.current_path_index if ag.path else 0
            slot_str = str(ag.assigned_slot_num) if ag.assigned_slot_num else "—"
            c        = cached_paths.get(aid)
            cache_str = f"{len(c)}шг" if c else "нет"

            # Стк = тиков без движения (из DeadlockResolver)
            stuck_t  = self.current_tick - stuck_since.get(aid, self.current_tick)
            stk_str  = f"⚠{stuck_t}" if stuck_t >= 5 and ag.goal else (
                       str(stuck_t)   if stuck_t > 0 and ag.goal else "—")

            # НП = consecutive "нет пути" (из _no_path_count)
            no_path  = self._no_path_count.get(aid, 0)
            np_str   = f"⚠{no_path}" if no_path >= 3 else (str(no_path) if no_path else "—")

            if (stuck_t >= 5 or no_path >= 3) and ag.goal:
                stuck_agents.append((aid, ag, stuck_t, no_path))

            print(f"  {aid:>3}  {st_str:<14} {pos_str:<10} {goal_str:<10} "
                  f"{cargo:<5} {path_len:>5} {ag.score:>6.2f} {slot_str:>5} "
                  f"{cache_str:<7} {stk_str:>4} {np_str:>4}")

        # ── Сводка по статусам ────────────────────────────────────────────
        from collections import Counter
        st_cnt = Counter(ag.status for ag in self.agents.values())
        parts  = [f"{k}:{v}" for k, v in sorted(st_cnt.items())]
        print(f"\n  🤖 Итого: {' | '.join(parts)}")

        # ── Диагностика застрявших агентов ────────────────────────────────
        if stuck_agents:
            print(f"\n  ⚠️  ПРОБЛЕМНЫЕ АГЕНТЫ:")
            for aid, ag, stuck_t, no_path in stuck_agents:
                diag = self._diagnose_stuck_agent(ag)
                print(f"    А{aid} [{ag.status}] @ {ag.pos} → {ag.goal} "
                      f"cargo={ag.has_cargo} stuck={stuck_t}т НП={no_path}")
                for line in diag:
                    print(f"      {line}")

        print("═"*70 + "\n")

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
        # Сбрасываем кэш пути: новое задание → новый маршрут
        if hasattr(self.router, 'invalidate_plan'):
            self.router.invalidate_plan(ag.id)
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

    def _try_unblock_cargo_agent(self, ag: 'Agent'):
        """
        Вспомогательный для баг 6: ищет IDLE-агентов вблизи застрявшего
        RETURN_SHELF агента и сбрасывает их waiting_time, чтобы Level-3
        (resolve_movement_conflicts chain-push) вытолкнул их с дороги.
        """
        if not ag.original_shelf_pos:
            return
        nearby = [
            other for other in self.agents.values()
            if other.id != ag.id
            and other.status == STATUS_IDLE
            and (abs(other.pos[0] - ag.pos[0]) +
                 abs(other.pos[1] - ag.pos[1])) <= 4
        ]
        if nearby:
            logging.info(f"  🔓 А{ag.id}: будим {len(nearby)} IDLE-агентов "
                         f"поблизости для освобождения пути к {ag.original_shelf_pos}")
            for other in nearby:
                other.waiting_time = 0   # сбросить → Level-3 даст им уступку

    def _diagnose_stuck_agent(self, ag: 'Agent') -> List[str]:
        """
        Диагностика застрявшего агента: показывает соседние клетки,
        почему они недостижимы (карта / брони / агенты).
        """
        lines = []
        pos  = ag.pos
        goal = ag.goal
        rs   = (ag.status == STATUS_RETURN_SHELF)

        # Направления ячейки
        dirs_l = getattr(self.router._algorithm, 'dirs_loaded', {}).get(pos, set())
        dirs_e = getattr(self.router._algorithm, 'dirs_empty',  {}).get(pos, set())
        c_type = getattr(self.router._algorithm, 'cell_types',  {}).get(pos, '?')
        lines.append(f"тип='{c_type}' dirs_loaded={sorted(dirs_l)} "
                     f"dirs_empty={sorted(dirs_e)}")

        # Соседние клетки
        DIR_OFFSETS = {'n': (-1,0), 's': (1,0), 'w': (0,-1), 'e': (0,1)}
        algo = getattr(self.router, '_algorithm', None)
        for d, (dr, dc) in DIR_OFFSETS.items():
            nb = (pos[0]+dr, pos[1]+dc)
            reasons = []

            # Карта: физический барьер?
            if algo:
                if not algo._is_valid(nb):
                    reasons.append("стена/граница")
                elif not algo._can_move(pos, nb, ag.has_cargo, rs):
                    nb_type = algo.cell_types.get(nb, '?')
                    reasons.append(f"запрет направления (→'{nb_type}')")
                else:
                    # Брони
                    blocked_by = []
                    for b in self.reservations.get(nb, []):
                        if b['agent_id'] == ag.id: continue
                        who = self.agents.get(b['agent_id'])
                        who_str = f"А{b['agent_id']}" + (
                            f"@{who.pos}" if who else "")
                        blocked_by.append(f"{who_str}(t_end={b['t_end']})")
                    if blocked_by:
                        reasons.append(f"занято: {', '.join(blocked_by)}")
                    else:
                        reasons.append("СВОБОДНО ✓")
            else:
                reasons.append("нет алгоритма")

            status = reasons[0] if reasons else "?"
            lines.append(f"  [{d}] {nb}: {status}")

        if goal:
            gr, gc = goal
            pr, pc = pos
            lines.append(f"  Дистанция до цели: {abs(gr-pr)+abs(gc-pc)} клеток")

        return lines

    # ── основной тик ─────────────────────────────────────────────────────────
    def run_tick(self) -> Dict[str, Any]:
        self.current_tick += 1

        # Метрики: начало тика
        if self.metrics:
            self.metrics.on_tick_start(self.current_tick)
            # Отслеживаем заказы и подзаказы через dispatcher
            if self.dispatcher:
                for order in self.dispatcher.orders.values():
                    oid = order.order_id
                    # Подсчитываем стеллажи в заказе (сумма по подзаказам)
                    total_sh = sum(s.num_shelves for s in order.suborders)
                    self.metrics.on_order_start(oid, self.current_tick, total_sh)
                    for sub in order.suborders:
                        self.metrics.on_suborder_start(
                            sub.suborder_id, oid, self.current_tick, sub.num_shelves)
                        # Подзаказ завершён
                        from dispatcher import SubOrderStatus
                        if sub.status == SubOrderStatus.COMPLETED:
                            self.metrics.on_suborder_complete(
                                sub.suborder_id, self.current_tick)
                # Заказ завершён — все подзаказы COMPLETED
                for order in self.dispatcher.orders.values():
                    if all(s.status.value == 'completed'
                           for s in order.suborders):
                        self.metrics.on_order_complete(
                            order.order_id, self.current_tick)

        # Заполняем reservations перед планированием.
        #
        # Классификация агентов по типу блокировки:
        #
        # t_end=_PERM — «жёсткий» блок (физически зафиксирован):
        #   WAITING_SLOT — стоит в конвойном слоте, уступить не может
        #   UNLOADING    — идёт разгрузка, уходить нельзя
        #
        # t_end=0 — «мягкий» блок (может уступить):
        #   IDLE         — свободный агент, может быть вытолкан Level-3 chain push
        #   Движущиеся   — уйдут сами в следующем тике; A*/CBS планирует через них
        #
        # ПОЧЕМУ IDLE МЯГКИЙ (исправление):
        #   Ранее IDLE = _PERM → A* видел стены там, где агенты могут уступить.
        #   Это вызывало "нет пути" когда IDLE агент сидел на целевой ячейке или
        #   в единственном коридоре. Теперь A* планирует ЧЕРЕЗ IDLE позиции (t≥1),
        #   а Level-3 chain push физически сдвигает IDLE агента при необходимости.
        #   CARGO агенты → планируются первыми (см. PrioritizedAStarSolver),
        #   резервируют пути → no-cargo агенты огибают → cargo получает приоритет.
        reservations = self.reservations
        reservations.clear()

        _hard_statuses = (STATUS_UNLOADING, STATUS_WAITING_SLOT)
        for _ag in self.agents.values():
            if _ag.status in _hard_statuses:
                _t_end = _PERM         # жёсткий блок: WAITING_SLOT / UNLOADING
            elif _ag.status == STATUS_IDLE or _ag.goal is None:
                _t_end = _IDLE_PERM   # IDLE: блокирует no-cargo, не блокирует cargo
            else:
                _t_end = 0            # движущийся агент: блок только в t=0
            reservations.setdefault(_ag.pos, []).append(
                {'agent_id': _ag.id, 't_start': 0, 't_end': _t_end})

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

            # ── 2.0 Переназначение CARRY_TO_PACKER без цели ──────────────────
            # Когда AgentManager обнаружил что слот физически недостижим
            # (шаг 5 REPLAN-эскалация сбросил ag.goal=None но оставил has_cargo),
            # пробуем занять последний (входной) слот конвоя.
            # После освобождения старого слота конвой _advance_convoy продвинется
            # и откроет входной слот 10 → агент получит к нему доступ.
            for ag in list(self.agents.values()):
                if ag.status != STATUS_CARRY_TO_PACKER: continue
                if ag.goal is not None: continue    # уже есть цель
                if not ag.has_cargo:    continue    # не с грузом
                pid = ag.current_suborder.packer_id if ag.current_suborder else None
                if pid is None: continue
                _sub_id = ag.current_suborder.suborder_id if ag.current_suborder else None
                new_slot = self.slot_manager.acquire_last_slot(pid, ag.id, _sub_id)
                if new_slot:
                    ag.assigned_slot_num = new_slot[0]
                    ag.goal              = new_slot[1]
                    ag.urgency           = max(1, min(10, 11 - new_slot[0]))
                    ag.shortest_path_len = 0
                    if hasattr(self.router, 'invalidate_plan'):
                        self.router.invalidate_plan(ag.id)
                    logging.info(f"  🔄 А{ag.id}: повторно назначен на слот "
                                 f"{new_slot[0]} @ {new_slot[1]} (предыдущий был недостижим)")

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
                        # Метрика 6: фиксируем выход из очереди (начало разгрузки)
                        if self.metrics:
                            self.metrics.on_exit_packer_queue(ag.id, self.current_tick)
                    # else: ждём в слоте 1 — остаёмся STATUS_CARRY_TO_PACKER,
                    # следующий тик проверит снова когда переключится active_suborder_id
                else:
                    # Агент достиг своего слота в очереди — ждёт продвижения
                    ag.status = STATUS_WAITING_SLOT
                    logging.info(f"  ⏳ Агент {ag.id} ждёт в слоте "
                                 f"{ag.assigned_slot_num} @ {ag.pos}")
                    # Метрика 6: фиксируем вход в очередь
                    if self.metrics and ag.current_suborder:
                        _oid = ag.current_suborder.suborder_id  # берём order_id через suborder
                        _ord_id = getattr(ag.current_suborder, 'order_id', 0)
                        self.metrics.on_enter_packer_queue(ag.id, _ord_id, self.current_tick)
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
                hijacked = self._try_hijack_blocker(ag)
                if not hijacked:
                    logging.warning(f"  ⚠️ Агент {ag.id}: GO_TO_SHELF застрял → abort")
                    self._abort_go_to_shelf(ag)
                continue

            # CARRY_TO_PACKER + REPLAN = агент с грузом не может добраться до слота.
            #
            # ТИПИЧНАЯ ПРИЧИНА: слот N назначен, но физически доступен только через
            # слот N+1 и N+2 (конвойный коридор входит с дальнего конца). Эти слоты
            # заняты WAITING_SLOT агентами (_PERM блок) → A* не находит путь.
            # При этом slot_owners[N] = А{id} → _advance_convoy не может продвинуть
            # конвой вперёд → дедлок: агент ждёт входа, конвой ждёт освобождения слота.
            #
            # РЕШЕНИЕ: после N REPLAN-попыток освобождаем слот N.
            #   → slot_owners[N] = None
            #   → _advance_convoy срабатывает: WAITING_SLOT агенты продвигаются вперёд
            #   → входной слот (последний номер) освобождается физически
            #   → в шаге 2.0 агент получает новый последний слот (входной) → путь есть
            if ag.status == STATUS_CARRY_TO_PACKER and actions.get(ag.id) == 'REPLAN':
                _ckey = f'carry_stuck_{ag.id}'
                _cnt  = self._shelf_abort_count.get(_ckey, 0) + 1
                self._shelf_abort_count[_ckey] = _cnt
                _MAX_CARRY_REPLAN = 3   # попыток REPLAN перед освобождением слота

                if _cnt >= _MAX_CARRY_REPLAN:
                    pid = ag.current_suborder.packer_id if ag.current_suborder else None
                    old_slot = ag.assigned_slot_num
                    if pid and old_slot is not None:
                        self.slot_manager.release_slot(pid, old_slot)
                        logging.warning(
                            f"  🔓 А{ag.id}: слот {old_slot} @ {ag.goal} физически "
                            f"недостижим ({_cnt} REPLAN). Слот освобождён — конвой "
                            f"продвинется, агент получит входной слот.")
                    # Сбрасываем цель: шаг 2.0 переназначит на доступный слот
                    ag.goal              = None
                    ag.assigned_slot_num = None
                    ag.shortest_path_len = 0
                    if hasattr(self.router, 'invalidate_plan'):
                        self.router.invalidate_plan(ag.id)
                    self._shelf_abort_count.pop(_ckey, None)
                    continue   # пропускаем добавление в tasks этот тик
                else:
                    logging.debug(f"  ⏳ А{ag.id}: CARRY слот {ag.assigned_slot_num} "
                                  f"недостижим, попытка {_cnt}/{_MAX_CARRY_REPLAN}")
                    # Продолжаем — tasks получит этот агент, A* попробует снова

            # RETURN_SHELF + REPLAN = агент застрял при возврате стеллажа.
            #
            # БАГ 6 ИСПРАВЛЕНИЕ: агент НЕ ИМЕЕТ ПРАВА бросить груз не на полке.
            # Прежний код принудительно завершал возврат → груз «испарялся» в
            # середине склада, active_agents обнулялся фиктивно.
            #
            # Новое поведение:
            #   • Агент ещё не дошёл до полки → сбрасываем путь, сбрасываем
            #     stuck-счётчик, роутер найдёт новый маршрут в следующем тике.
            #     Уровень-3 (resolve_movement_conflicts) вытолкнет IDLE-агентов
            #     с дороги через механизм chain-push.
            #   • После _MAX_RETURN_STUCK попыток → эскалация: ищем и выдворяем
            #     IDLE-агентов поблизости принудительно.
            #   • Принудительное завершение возврата ТОЛЬКО если агент уже
            #     физически стоит на целевой полке (pos == original_shelf_pos).
            if ag.status == STATUS_RETURN_SHELF and actions.get(ag.id) == 'REPLAN':
                if ag.pos != ag.original_shelf_pos:
                    # ── Агент с грузом, но ещё не на полке ──────────────────
                    _rkey = f'ret_{ag.id}'
                    _cnt  = self._shelf_abort_count.get(_rkey, 0) + 1
                    self._shelf_abort_count[_rkey] = _cnt
                    _MAX  = 15

                    if _cnt < _MAX:
                        logging.warning(
                            f"  ⚠️ А{ag.id}: RETURN_SHELF заблокирован "
                            f"(попытка {_cnt}/{_MAX}). "
                            f"Груз СОХРАНЯЕТСЯ, ищем новый путь "
                            f"{ag.pos}→{ag.original_shelf_pos}.")
                    else:
                        logging.error(
                            f"  🚨 А{ag.id}: RETURN_SHELF критически заблокирован "
                            f"({_cnt} попыток). Груз СОХРАНЯЕТСЯ. "
                            f"Выдворяем IDLE-агентов поблизости.")
                        self._try_unblock_cargo_agent(ag)
                        # Подробная диагностика пути (помогает выявить map-проблемы)
                        if hasattr(self.router, '_algorithm') and \
                           hasattr(self.router._algorithm, 'diagnose_path_failure'):
                            _rs = (ag.status == STATUS_RETURN_SHELF)
                            _diag = self.router._algorithm.diagnose_path_failure(
                                ag.pos, ag.goal, self.reservations,
                                ag.id, ag.has_cargo, _rs)
                            logging.error(_diag)
                        self._shelf_abort_count[_rkey] = _MAX  # не переполняем лог

                    # Сброс пути и stuck-счётчика → новый маршрут в следующем тике
                    ag.path               = []
                    ag.current_path_index = 0
                    ag.shortest_path_len  = 0
                    self.deadlock_resolver.stuck_since[ag.id] = self.current_tick
                    # Инвалидируем кэш плана (маршрут изменится)
                    if hasattr(self.router, 'invalidate_plan'):
                        self.router.invalidate_plan(ag.id)  # только этот агент
                    continue   # остаёмся RETURN_SHELF, tasks не добавляем

                else:
                    # Агент УЖЕ на полке, но DeadlockResolver выдал REPLAN.
                    # Шаг 3 уже обработал или обработает завершение через
                    # (ag.pos == ag.goal), поэтому просто пропускаем добавление в tasks.
                    self._shelf_abort_count.pop(f'ret_{ag.id}', None)
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

        # Обновляем счётчик "нет пути" для диагностики консоли
        for aid, path in paths.items():
            if len(path) <= 1:
                self._no_path_count[aid] = self._no_path_count.get(aid, 0) + 1
            else:
                self._no_path_count.pop(aid, None)

        # 6_L2. WFG-обнаружение циклов + локальный CBS (Уровень 2).
        if not self.router.is_centralized:
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
            ag.current_path_index = 1

        # 6b. Финальный enforcement: vertex + swap + IDLE-уступка.
        allowed_to_move, idle_displacements = self.deadlock_resolver.resolve_movement_conflicts(
            self.agents, paths, self.current_tick)

        # 6c. move() только для разрешённых активных агентов
        for aid, path in paths.items():
            ag = self.agents[aid]
            prev_pos = ag.pos
            if aid in allowed_to_move and len(path) >= 2:
                ag.move()
                if ag.pos != prev_pos:
                    self._last_move_tick[aid] = self.current_tick
            else:
                ag.current_path_index = 0
                if self.router.is_centralized and hasattr(self.router, 'notify_blocked'):
                    self.router.notify_blocked(aid)
                # Метрика 5: активный агент заблокирован Level-3
                if self.metrics and ag.status in (
                        STATUS_GO_TO_SHELF, STATUS_CARRY_TO_PACKER, STATUS_RETURN_SHELF):
                    self.metrics.on_agent_blocked(aid, ag.status)

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