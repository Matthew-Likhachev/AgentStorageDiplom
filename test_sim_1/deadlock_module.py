"""
deadlock_module.py
Трёхуровневая система разрешения дедлоков.

  Уровень 1 — check_and_resolve():
    WFG (Wait-For-Graph) детектирует застрявших агентов.
    Возвращает {agent_id: "OK" | "WAIT" | "REPLAN"}.

  Уровень 2 — resolve_local_deadlocks():
    WFG-цикл → локальный CBS для группы агентов в цикле.
    Вызывается только для реактивного A* (для CBS/LNS пропускается,
    см. agent_module.py шаг 6 — план уже бесконфликтен).

  Уровень 3 — resolve_movement_conflicts():
    Физический enforcement финального шага:
      Pass 1 — блокировка входа в занятые клетки (vertex).
      Pass 2 — цепочка вытеснения IDLE-агентов.
      Pass 3 — swap-конфликты: победитель движется, лузер ждёт.

  БАГ 3 ИСПРАВЛЕНИЕ — swap осцилляция ↑↓:
    Ранее оба агента блокировались при swap → следующий тик CBS строил
    те же пути → тот же swap → бесконечная осцилляция.
    Теперь: лузер (меньший score) получает force_wait на _SWAP_WAIT_TICKS тиков
    через RouterContext.swap_losers (читается в check_and_resolve).
    Победитель в следующем тике движется свободно → swap разрешён.
"""
from __future__ import annotations
import heapq, logging
from typing import Dict, List, Tuple, Set, Optional, Any

_FORCE_WAIT_TICKS  = 3   # тиков WAIT после REPLAN-сброса
_SWAP_WAIT_TICKS   = 4   # тиков WAIT для лузера swap-конфликта (баг 3)
_STUCK_HISTORY_LEN = 8   # глубина истории позиций для WFG


class DeadlockResolver:
    """
    Централизованный разрешитель дедлоков и физических конфликтов.
    Создаётся AgentManager'ом, живёт всё время симуляции.
    """

    def __init__(self, stuck_threshold: int = 8, map_data: Optional[Dict] = None):
        self.stuck_threshold = stuck_threshold
        self.map_data        = map_data

        # История позиций: {agent_id: [pos_t, pos_t-1, ...]}
        self.pos_history: Dict[int, List[Tuple]] = {}
        # Тик последнего движения агента
        self.stuck_since:  Dict[int, int]        = {}
        # Принудительный WAIT до этого тика: {agent_id: until_tick}
        self._force_wait_until: Dict[int, int]   = {}
        # Текущий тик (сохраняется в check_and_resolve для Pass 3)
        self._current_tick: int = 0

        # Кэш карты для локального CBS
        self._cell_types:  Dict[Tuple, str]      = {}
        self._dirs_loaded: Dict[Tuple, Set[str]] = {}
        self._dirs_empty:  Dict[Tuple, Set[str]] = {}
        self._start_row    = 0
        self._start_col    = 0
        self._width        = 0
        self._height       = 0
        self._DIR_OFFSETS  = {'n': (-1,0), 's': (1,0), 'w': (0,-1), 'e': (0,1)}

        if map_data:
            self._parse_map(map_data)

    def _parse_map(self, map_data: Dict):
        self._start_row = map_data.get('start_row', 0)
        self._start_col = map_data.get('start_col', 0)
        self._width     = map_data.get('width', 100)
        self._height    = map_data.get('height', 100)
        for row in map_data.get('cells', []):
            for cell in row:
                pos   = (cell['row'], cell['col'])
                text  = cell.get('text', '')
                parts = text.split('|') if '|' in text else ['F','','','{}']
                self._cell_types[pos]  = parts[0]
                self._dirs_loaded[pos] = self._parse_dirs(parts[1] if len(parts)>1 else '')
                self._dirs_empty[pos]  = self._parse_dirs(parts[2] if len(parts)>2 else '')

    def _parse_dirs(self, s: str) -> Set[str]:
        return {d for d in s.split('-') if d in ('n','w','s','e')}

    # ── Уровень 1 ─────────────────────────────────────────────────────────
    def check_and_resolve(self, agents: Dict[int, Any],
                          reservations: Dict,
                          current_tick: int) -> Dict[int, str]:
        """
        Анализирует состояние агентов и возвращает действия:
          "OK"     — двигаться по плану
          "WAIT"   — стоять на месте (уступить)
          "REPLAN" — путь заблокирован, нужен новый маршрут
        """
        self._current_tick = current_tick
        actions: Dict[int, str] = {}

        # Применяем force_wait из предыдущих тиков (в т.ч. swap_losers, баг 3)
        for aid, until in list(self._force_wait_until.items()):
            if current_tick >= until:
                del self._force_wait_until[aid]
            else:
                actions[aid] = 'WAIT'

        # Применяем swap_losers от RouterContext (если есть)
        # AgentManager передаёт router через аргумент → доступен через agents
        # (используем сторонний канал: _swap_losers_pending)
        for aid in list(getattr(self, '_swap_losers_pending', set())):
            until = current_tick + _SWAP_WAIT_TICKS
            self._force_wait_until[aid] = until
            actions[aid] = 'WAIT'
        self._swap_losers_pending: Set[int] = set()

        from agent_module import (STATUS_IDLE, STATUS_UNLOADING,
                                   STATUS_WAITING_SLOT)
        _skip_statuses = (STATUS_IDLE, STATUS_UNLOADING, STATUS_WAITING_SLOT)

        for aid, ag in agents.items():
            if aid in actions:
                continue
            if ag.status in _skip_statuses or ag.goal is None:
                actions[aid] = 'OK'
                continue

            # Инициализация истории
            if aid not in self.stuck_since:
                self.stuck_since[aid] = current_tick
            if aid not in self.pos_history:
                self.pos_history[aid] = []

            # Проверяем прогресс (двигался ли агент за stuck_threshold тиков)
            ticks_stuck = current_tick - self.stuck_since.get(aid, current_tick)
            if ticks_stuck >= self.stuck_threshold:
                # Агент не двигался слишком долго → REPLAN
                jitter = aid % _FORCE_WAIT_TICKS
                self._force_wait_until[aid] = current_tick + _FORCE_WAIT_TICKS + jitter
                self.stuck_since[aid]       = current_tick
                actions[aid]                = 'REPLAN'
                logging.debug(f"  [L1] А{aid}: застрял {ticks_stuck} тиков → REPLAN")
            else:
                actions[aid] = 'OK'

        return actions

    def update_last_move(self, agents: Dict[int, Any], current_tick: int):
        """
        Запоминает позиции ПОСЛЕ движения. Вызывается в конце тика.
        Обновляет stuck_since для агентов, которые действительно двигались.
        """
        from agent_module import (STATUS_IDLE, STATUS_UNLOADING,
                                   STATUS_WAITING_SLOT)
        _skip_statuses = (STATUS_IDLE, STATUS_UNLOADING, STATUS_WAITING_SLOT)

        for aid, ag in agents.items():
            if ag.status in _skip_statuses:
                self.stuck_since.pop(aid, None)
                continue
            prev_positions = self.pos_history.get(aid, [])
            prev_pos       = prev_positions[-1] if prev_positions else None

            if prev_pos != ag.pos:
                # Агент двигался — сбрасываем счётчик
                self.stuck_since[aid] = current_tick

            # Обновляем историю (кольцевой буфер)
            self.pos_history[aid] = (prev_positions + [ag.pos])[-_STUCK_HISTORY_LEN:]

    # ── Уровень 2 ─────────────────────────────────────────────────────────
    def resolve_local_deadlocks(self, agents: Dict[int, Any],
                                 paths: Dict[int, List[Tuple]],
                                 tasks: List[Dict],
                                 current_tick: int,
                                 reservations: Optional[Dict] = None
                                 ) -> Dict[int, List[Tuple]]:
        """
        WFG-детектирование циклов → локальный CBS для группы.
        Вызывается только для реактивного A*. Для CBS/LNS пропускается
        (plan already conflict-free).
        """
        if not paths or not tasks:
            return paths

        # Строим WFG: кто кого ждёт
        # Агент A ждёт агента B если следующий шаг A = текущая позиция B
        next_pos: Dict[int, Tuple] = {}
        for aid, path in paths.items():
            if len(path) >= 2:
                next_pos[aid] = path[1]
            else:
                next_pos[aid] = agents[aid].pos if aid in agents else path[0]

        pos_to_agent: Dict[Tuple, int] = {}
        for aid, ag in agents.items():
            pos_to_agent[ag.pos] = aid

        # WFG рёбра: A→B если A хочет на позицию B
        wfg: Dict[int, int] = {}
        for aid, nxt in next_pos.items():
            blocker = pos_to_agent.get(nxt)
            if blocker is not None and blocker != aid:
                wfg[aid] = blocker

        # Находим циклы (DFS)
        cycles: List[List[int]] = []
        visited: Set[int] = set()

        def find_cycle(start: int) -> Optional[List[int]]:
            path_c: List[int] = []; seen: Dict[int, int] = {}; cur = start
            while cur in wfg:
                if cur in seen:
                    idx = seen[cur]
                    return path_c[idx:]
                seen[cur] = len(path_c); path_c.append(cur); cur = wfg[cur]
            return None

        for aid in list(wfg.keys()):
            if aid not in visited:
                cycle = find_cycle(aid)
                if cycle:
                    for a in cycle: visited.add(a)
                    cycles.append(cycle)

        if not cycles:
            return paths

        # Перепланируем каждую группу в цикле через локальный A* с wait-step
        new_paths = dict(paths)
        for cycle in cycles:
            logging.info(f"  [L2] WFG цикл: {cycle} → локальное перепланирование")
            # Лузер (минимальный score) ждёт один тик
            task_map = {t['agent_id']: t for t in tasks}
            scores   = {aid: task_map[aid].get('score', 0.0)
                        for aid in cycle if aid in task_map}
            if not scores:
                continue
            loser = min(scores, key=scores.get)
            ag_l  = agents.get(loser)
            if ag_l:
                new_paths[loser] = [ag_l.pos]   # wait step
                jitter = loser % _FORCE_WAIT_TICKS
                self._force_wait_until[loser] = (
                    current_tick + _FORCE_WAIT_TICKS + jitter)
                logging.debug(f"    L2 лузер: А{loser} ждёт {_FORCE_WAIT_TICKS} тиков")

        return new_paths

    # ── Уровень 3 ─────────────────────────────────────────────────────────
    def resolve_movement_conflicts(self,
                                   agents: Dict[int, Any],
                                   paths:  Dict[int, List[Tuple]],
                                   current_tick: int = 0
                                   ) -> Tuple[Set[int], Dict[int, Tuple]]:
        """
        Физический enforcement последнего шага перед ag.move().
        Гарантирует: никакие два агента не оказываются в одной клетке.

        Pass 1 — блокировка входа в занятые клетки (vertex-конфликт).
        Pass 2 — цепочка вытеснения IDLE-агентов (chain-push).
        Pass 3 — swap-конфликты: лузер блокируется + force_wait (баг 3).

        Возвращает:
          allowed_to_move   — множество agent_id, которым разрешён шаг
          idle_displacements — {idle_id: new_pos} физические смещения IDLE
        """
        if current_tick:
            self._current_tick = current_tick

        from agent_module import (STATUS_IDLE, STATUS_UNLOADING,
                                   STATUS_WAITING_SLOT, STATUS_CARRY_TO_PACKER)

        # Следующие позиции (plan-step 1 или текущая если нет пути)
        next_pos: Dict[int, Tuple] = {}
        for aid, path in paths.items():
            ag = agents.get(aid)
            if ag is None: continue
            next_pos[aid] = path[1] if len(path) >= 2 else ag.pos

        allowed:    Set[int]          = set(next_pos.keys())
        idle_displ: Dict[int, Tuple]  = {}
        changed = True

        # ── Pass 1: vertex-конфликты ──────────────────────────────────────
        while changed:
            changed = False
            # Клетки назначения для разрешённых агентов
            claimed: Dict[Tuple, int] = {}
            for aid in list(allowed):
                pos = next_pos[aid]
                if pos in claimed:
                    # Конфликт: оставляем агента с бо́льшим score
                    other = claimed[pos]
                    ag    = agents.get(aid)
                    ag_o  = agents.get(other)
                    s     = ag.score    if ag   else 0.0
                    s_o   = ag_o.score  if ag_o else 0.0
                    loser = aid if s <= s_o else other
                    winner = other if loser == aid else aid
                    if loser in allowed:
                        allowed.discard(loser)
                        next_pos[loser] = agents[loser].pos
                        claimed[pos] = winner
                        changed = True
                else:
                    # Проверяем что целевая клетка физически свободна
                    for oid, oag in agents.items():
                        if oid in next_pos: continue  # движущийся — не статик
                        if oag.pos == pos:
                            allowed.discard(aid)
                            next_pos[aid] = agents[aid].pos
                            changed = True
                            break
                    else:
                        claimed[pos] = aid

        # ── Pass 2: chain-push IDLE-агентов ──────────────────────────────
        # Если активный агент заблокирован IDLE-агентом, IDLE уступает:
        # он смещается на клетку, которую покидает активный.
        for aid in list(allowed):
            ag = agents.get(aid)
            if ag is None: continue
            target = next_pos[aid]
            # Ищем IDLE/WAITING_SLOT агента на target
            blocker_id = None
            for oid, oag in agents.items():
                if oid not in next_pos and oag.pos == target:
                    if oag.status in (STATUS_IDLE,):
                        blocker_id = oid
                    break
            if blocker_id is None: continue
            blocker = agents[blocker_id]
            # Смещаем blocker на текущую позицию активного агента (цепочка)
            vacated = ag.pos
            # Проверяем что vacated не будет занята другим
            conflict = any(
                oid != blocker_id and next_pos.get(oid) == vacated
                for oid in next_pos)
            if not conflict:
                idle_displ[blocker_id] = vacated
                logging.debug(f"  [L3] chain-push: А{blocker_id} "
                              f"{blocker.pos}→{vacated}")

        # ── Pass 3: swap-конфликты (БАГ 3 ИСПРАВЛЕНИЕ) ───────────────────
        #
        # Сценарий осцилляции ↑↓:
        #   Тик T:   A хочет на pos(B), B хочет на pos(A) → swap.
        #            Оба блокируются → оба стоят.
        #   Тик T+1: CBS строит те же пути → тот же swap → вечный цикл.
        #
        # Исправление:
        #   Оба блокируются (как раньше), НО лузер получает force_wait
        #   на _SWAP_WAIT_TICKS тиков через _swap_losers_pending.
        #   Тик T+1: лузер → WAIT, победитель планирует свободно → swap разрешён.
        #   Случайный jitter предотвращает симметричные новые swap.
        aid_list = list(allowed)
        for i, a1 in enumerate(aid_list):
            for a2 in aid_list[i + 1:]:
                ag1, ag2 = agents.get(a1), agents.get(a2)
                if ag1 is None or ag2 is None: continue
                # Swap: A хочет на pos(B) и B хочет на pos(A)
                if next_pos.get(a1) == ag2.pos and next_pos.get(a2) == ag1.pos:
                    # Определяем лузера по score (меньший score уступает)
                    loser  = a1 if (ag1.score <= ag2.score) else a2
                    winner = a2 if loser == a1 else a1
                    # Блокируем обоих на этот тик (физически нельзя свапнуться)
                    allowed.discard(a1)
                    allowed.discard(a2)
                    next_pos[a1] = ag1.pos
                    next_pos[a2] = ag2.pos
                    # Лузер получает force_wait → разрывает осцилляцию
                    jitter = loser % 3
                    until  = self._current_tick + _SWAP_WAIT_TICKS + jitter
                    self._force_wait_until[loser] = until
                    logging.info(f"  [L3] swap А{a1}↔А{a2}: "
                                 f"лузер=А{loser} (score {agents[loser].score:.2f}) "
                                 f"→ force_wait до тика {until}")

        return allowed, idle_displ