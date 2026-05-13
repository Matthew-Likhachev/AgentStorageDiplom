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

    def __init__(self, stuck_threshold: int = 8, map_data: Optional[Dict] = None,
                 metrics=None):
        self.stuck_threshold = stuck_threshold
        self.map_data        = map_data
        self.metrics         = metrics   # MetricsCollector | None

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
                jitter = aid % _FORCE_WAIT_TICKS
                self._force_wait_until[aid] = current_tick + _FORCE_WAIT_TICKS + jitter
                self.stuck_since[aid]       = current_tick
                actions[aid]                = 'REPLAN'
                logging.debug(f"  [L1] А{aid}: застрял {ticks_stuck} тиков → REPLAN")
                if self.metrics:
                    self.metrics.on_deadlock()   # Метрика 8: дедлок
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
            if self.metrics:
                self.metrics.on_deadlock()
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

        from agent_module import (STATUS_IDLE, STATUS_UNLOADING,
                                   STATUS_WAITING_SLOT, STATUS_GO_TO_SHELF,
                                   STATUS_CARRY_TO_PACKER, STATUS_RETURN_SHELF)
        _cargo_statuses     = (STATUS_CARRY_TO_PACKER, STATUS_RETURN_SHELF)
        _yieldable_statuses = (STATUS_IDLE, STATUS_GO_TO_SHELF)

        def _is_yieldable(oag) -> bool:
            """
            Агент уступает дорогу cargo-агенту если:
              • Статус IDLE или GO_TO_SHELF (мягкие статусы)
              • ИЛИ goal is None — агент без цели (CARRY без слота, застрявший
                агент), физически стоит на месте и не планирует двигаться.
                БАГ А+В ИСПРАВЛЕНИЕ: CARRY_TO_PACKER с goal=None (потерял слот)
                раньше не считался yieldable → Level-3 не выталкивал его →
                cargo-агент навсегда застревал рядом, хотя CBS строил путь через него.
            """
            if oag.status in _yieldable_statuses:
                return True
            if getattr(oag, 'goal', None) is None:
                return True   # нет цели → агент стоит на месте → должен уступить
            return False

        def _effective_score(ag) -> float:
            """Score агента с бонусом +500 за наличие груза."""
            if ag is None:
                return 0.0
            base = ag.score
            # Груз = абсолютный приоритет над агентами без груза
            if getattr(ag, 'has_cargo', False):
                base += 500.0
            return base

        # ── Pass 1: vertex-конфликты (с приоритетом груза) ───────────────────
        #
        # КРИТИЧЕСКИЙ БАГ ИСПРАВЛЕНИЕ:
        # Ранее: агент А14 (застрял, next_pos==pos) и агент А12 (движется, next_pos=А14.pos)
        # обрабатывались ОДИНАКОВО — победитель определялся по score.
        # А12 (score 6.11 > 4.64) "побеждал" → А14 "проигрывал" → А14 "заблокирован"
        # (остаётся на месте), А12 ДВИЖЕТСЯ ТУДА ЖЕ → физическое наложение!
        #
        # ИСПРАВЛЕНИЕ: статичные агенты (next_pos==pos, т.е. не двигаются)
        # PRE-CLAIM свою позицию ДО того как двигающиеся агенты её займут.
        # Движущийся агент ВСЕГДА проигрывает статичному — нельзя войти в
        # клетку, которую занимает агент без намерения уйти.
        while changed:
            changed = False
            claimed: Dict[Tuple, int] = {}

            # Шаг 1a: статичные агенты (остаются на месте) занимают свои позиции первыми
            for aid in list(allowed):
                if next_pos[aid] == agents[aid].pos:
                    # Этот агент не движется (застрял, на цели, или ждёт)
                    # Его позиция неприкосновенна для движущихся агентов
                    claimed[next_pos[aid]] = aid

            # Шаг 1b: движущиеся агенты проверяют конфликты
            for aid in list(allowed):
                if next_pos[aid] == agents[aid].pos:
                    continue  # статичный: уже обработан выше

                pos = next_pos[aid]
                if pos in claimed:
                    other = claimed[pos]
                    ag    = agents.get(aid)
                    ag_o  = agents.get(other)
                    # Если other статичен (остаётся) → движущийся ВСЕГДА проигрывает
                    other_is_static = (next_pos.get(other) == agents[other].pos
                                       if other in agents else True)
                    if other_is_static:
                        loser  = aid
                        winner = other
                    else:
                        s, s_o = _effective_score(ag), _effective_score(ag_o)
                        loser  = aid if s <= s_o else other
                        winner = other if loser == aid else aid

                    if loser in allowed:
                        allowed.discard(loser)
                        next_pos[loser] = agents[loser].pos
                        claimed[pos]    = winner
                        changed         = True
                else:
                    # Проверяем статичных агентов НЕ из next_pos (IDLE, etc.)
                    blocked_by_static = False
                    for oid, oag in agents.items():
                        if oid in next_pos: continue
                        if oag.pos != pos:  continue
                        ag = agents.get(aid)
                        if getattr(ag, 'has_cargo', False) and _is_yieldable(oag):
                            vacated  = ag.pos
                            conflict = any(
                                oid2 != oid and next_pos.get(oid2) == vacated
                                for oid2 in next_pos)
                            if not conflict:
                                idle_displ[oid] = vacated
                            else:
                                blocked_by_static = True
                        else:
                            blocked_by_static = True
                        break
                    if blocked_by_static:
                        allowed.discard(aid)
                        next_pos[aid] = agents[aid].pos
                        changed = True
                    else:
                        claimed[pos] = aid

        # ── Pass 2: chain-push (cargo вытесняет yieldable агентов) ──────────
        for aid in list(allowed):
            ag = agents.get(aid)
            if ag is None: continue
            # Только cargo-агенты инициируют вытеснение
            if not getattr(ag, 'has_cargo', False): continue

            target = next_pos[aid]
            blocker_id = None
            for oid, oag in agents.items():
                if oid in next_pos:   continue
                if oid in idle_displ: continue
                if oag.pos != target: continue
                if _is_yieldable(oag):          # ← используем расширенную проверку
                    blocker_id = oid
                break

            if blocker_id is None: continue
            vacated  = ag.pos
            conflict = any(
                oid != blocker_id and next_pos.get(oid) == vacated
                for oid in next_pos)
            if not conflict:
                idle_displ[blocker_id] = vacated
                logging.debug(f"  [L3] cargo-push: А{blocker_id} "
                              f"({agents[blocker_id].status}/goal={getattr(agents[blocker_id],'goal',None)}) "
                              f"уступает А{aid} (груз) → {vacated}")

        # ── Pass 3: swap-конфликты (cargo + force_wait для лузера) ──────────
        aid_list = list(allowed)
        for i, a1 in enumerate(aid_list):
            for a2 in aid_list[i + 1:]:
                ag1, ag2 = agents.get(a1), agents.get(a2)
                if ag1 is None or ag2 is None: continue
                if next_pos.get(a1) == ag2.pos and next_pos.get(a2) == ag1.pos:
                    # Груз всегда побеждает агента без груза в swap
                    s1    = _effective_score(ag1)
                    s2    = _effective_score(ag2)
                    loser  = a1 if s1 <= s2 else a2
                    winner = a2 if loser == a1 else a1
                    allowed.discard(a1); allowed.discard(a2)
                    next_pos[a1] = ag1.pos; next_pos[a2] = ag2.pos
                    jitter = loser % 3
                    until  = self._current_tick + _SWAP_WAIT_TICKS + jitter
                    self._force_wait_until[loser] = until
                    logging.info(f"  [L3] swap А{a1}↔А{a2}: "
                                 f"лузер=А{loser} (score-eff {min(s1,s2):.0f}) "
                                 f"→ force_wait до тика {until}")
                    if self.metrics:
                        self.metrics.on_collision()   # Метрика 7: столкновение

        return allowed, idle_displ