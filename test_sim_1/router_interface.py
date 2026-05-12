"""
router_interface.py
Архитектура маршрутизации: Strategy Pattern + Registry.

Доступные алгоритмы:
  "prioritized_astar" — жадный последовательный A* (реактивный)
  "cbs"               — Conflict-Based Search (централизованный, оптимальный)
  "lns1_cbs"          — LNS (верхний уровень) + CBS (нижний, разрешает конфликты)
  "lns2_simple"       — LNS только верхний уровень + A* нижний (без CBS, быстрее)
  Алиасы: "lns1" → lns1_cbs | "lns2" → lns2_simple | "lns" → lns1_cbs

СРАВНЕНИЕ ДЛЯ ДИПЛОМА:
  A*         — базовая реактивная линия (быстро, субоптимально, нужен WFG)
  CBS        — централизованный оптимум для малых групп (≤8-10 агентов)
  LNS-1/CBS  — компромисс CBS-качества для больших групп (8-20 агентов)
  LNS-2/A*   — быстрый централизованный для очень больших групп (20+ агентов)
"""
from __future__ import annotations
import heapq, logging, random, copy
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple, Set, Optional, Type

# Брони reservations: три уровня приоритета.
# t_end == 0          → движущийся агент; блокирует только t=0
# t_end == _IDLE_PERM → IDLE-агент; cargo проезжает, no-cargo огибает
# t_end == _PERM      → жёсткий блок (WAITING_SLOT / UNLOADING / слоты)
_PERM:      int = 99_999
_IDLE_PERM: int = 99_998   # = _PERM - 1; IDLE: мягкая для cargo, жёсткая для no-cargo


class IRouter(ABC):
    is_centralized: bool = False   # True → WFG не нужен, план кэшируется

    @abstractmethod
    def solve(self, tasks: List[Dict], reservations: Dict
              ) -> Dict[int, List[Tuple[int, int]]]: ...


class RoutingAlgorithmRegistry:
    _registry: Dict[str, Type[IRouter]] = {}

    @classmethod
    def register(cls, name: str):
        def decorator(algo_cls):
            cls._registry[name] = algo_cls
            logging.info(f"  [Router] зарегистрирован: '{name}'")
            return algo_cls
        return decorator

    @classmethod
    def create(cls, name: str, map_data: Dict, **kw) -> IRouter:
        if name not in cls._registry:
            raise ValueError(f"Алгоритм '{name}' не найден. "
                             f"Доступны: {list(cls._registry)}")
        return cls._registry[name](map_data, **kw)

    @classmethod
    def list_algorithms(cls) -> List[str]:
        return list(cls._registry.keys())


class RouterContext:
    """
    Контекст алгоритма + двухрежимное кэширование путей.

    ┌─────────────────────────────────────────────────────────────────────┐
    │ РЕЖИМ A: Centralized (CBS / LNS-1 / LNS-2)                         │
    │                                                                     │
    │ Единый глобальный план на всех агентов. Пересчёт — только если     │
    │ агент отклонился от плана или поменял цель. WFG не нужен.          │
    ├─────────────────────────────────────────────────────────────────────┤
    │ РЕЖИМ B: Per-agent (Prioritized A*)                                 │
    │                                                                     │
    │ Каждый агент хранит свой отдельный кэшированный путь.              │
    │                                                                     │
    │ Порядок разрешения конфликта (от мягкого к жёсткому):              │
    │   1. Level-3 (resolve_movement_conflicts) блокирует движение       │
    │      — агент остаётся на месте. Следующий тик: RouterContext        │
    │      возвращает тот же кэшированный путь (retry без A*).           │
    │   2. Level-2 (WFG) обнаруживает цикл ожидания → force_wait         │
    │      для лузера. Лузер ждёт N тиков и повторяет тот же путь.      │
    │   3. Level-1 (check_and_resolve) фиксирует: агент не двигался      │
    │      stuck_threshold тиков → действие REPLAN → RouterContext        │
    │      вызывает A* только для этого агента; остальные — из кэша.     │
    │                                                                     │
    │ A* НЕ вызывается каждый тик. Вызывается только при REPLAN          │
    │ (локальное разрешение исчерпано) или при первом планировании.      │
    └─────────────────────────────────────────────────────────────────────┘

    Поля:
      plan_was_recomputed — True если на этом тике хотя бы один агент
                            был перепланирован (любой режим).
      swap_losers         — агенты, проигравшие swap в Level-3.
    """
    def __init__(self, algorithm: IRouter):
        self._algorithm               = algorithm
        self._map_data: Optional[Dict] = None
        self._cached_paths: Dict[int, List[Tuple]] = {}
        self._cached_has_cargo: Dict[int, bool]    = {}
        self.plan_was_recomputed: bool = True
        self.swap_losers: Set[int]    = set()
        # Агенты, заблокированные Level-3 на прошлом тике.
        # Для централизованных: блокировка любого = план устарел → пересчёт.
        # (Причина: MAPF-план строится в time-space. Если агент не сделал шаг,
        # а остальные продолжили, временны́е координаты плана нарушены → коллизии.)
        self._blocked_agents: Set[int] = set()

    def notify_blocked(self, agent_id: int):
        """
        Сообщает роутеру что Level-3 заблокировал агента (он не сделал шаг).
        Для централизованных алгоритмов: следующий тик → полный пересчёт плана.
        """
        self._blocked_agents.add(agent_id)

    @property
    def is_centralized(self) -> bool:
        return getattr(self._algorithm, 'is_centralized', False)

    def set_algorithm(self, name: str, **kw):
        if self._map_data is None:
            raise RuntimeError("Вызовите set_map_data() сначала.")
        self._algorithm = RoutingAlgorithmRegistry.create(name, self._map_data, **kw)
        self._cached_paths.clear()
        self._cached_has_cargo.clear()
        self.plan_was_recomputed = True

    def set_map_data(self, map_data: Dict):
        self._map_data = map_data

    def invalidate_plan(self, agent_id: Optional[int] = None):
        """Сброс кэша. agent_id=None → все агенты; иначе только указанный."""
        if agent_id is not None:
            self._cached_paths.pop(agent_id, None)
            self._cached_has_cargo.pop(agent_id, None)
        else:
            self._cached_paths.clear()
            self._cached_has_cargo.clear()
        self.plan_was_recomputed = True

    def solve(self, tasks: List[Dict], reservations: Dict
              ) -> Dict[int, List[Tuple]]:
        if self.is_centralized:
            return self._solve_centralized(tasks, reservations)
        return self._solve_astar_cached(tasks, reservations)

    # ── Режим A: централизованный (CBS / LNS) ────────────────────────────────
    def _solve_centralized(self, tasks: List[Dict],
                           reservations: Dict) -> Dict[int, List[Tuple]]:
        task_map    = {t['agent_id']: t for t in tasks}
        current_ids = set(task_map)
        result: Dict[int, List[Tuple]] = {}

        # Если Level-3 заблокировал хотя бы одного агента в прошлом тике →
        # план MAPF устарел (time-space координаты нарушены) → полный пересчёт.
        any_blocked = bool(self._blocked_agents)
        self._blocked_agents.clear()

        # Если Level-1 выдал REPLAN хотя бы одному агенту → план требует пересчёта.
        # БАГ Б: ранее REPLAN игнорировался (кэш возвращался без проверки action),
        # из-за чего застрявшие агенты никогда не получали новый план.
        any_replan = any(t.get('action') == 'REPLAN' for t in tasks)

        can_reuse = (
            bool(self._cached_paths)
            and not any_blocked          # никто не был заблокирован прошлый тик
            and not any_replan           # Level-1 не требует пересчёта
            and current_ids == set(self._cached_paths)
        )

        if can_reuse:
            for aid, task in task_map.items():
                cached = self._cached_paths.get(aid, [])
                start, goal = task['start'], task['goal']
                if not cached or cached[-1] != goal:
                    can_reuse = False; break
                if cached[0] == start:
                    result[aid] = cached
                elif len(cached) >= 2 and cached[1] == start:
                    result[aid] = cached[1:]
                else:
                    can_reuse = False; break

        if can_reuse:
            self._cached_paths       = result
            self.plan_was_recomputed = False
            for aid, path in result.items():
                for i, pos in enumerate(path[1:], 1):
                    reservations.setdefault(pos, []).append(
                        {'agent_id': aid, 't_start': i, 't_end': i})
            logging.debug("[RouterContext/CBS] кэш переиспользован")
            return result

        paths = self._algorithm.solve(tasks, reservations)

        # БАГ: не кэшировать провальные пути (len ≤ 1 = алгоритм не нашёл маршрут).
        # Провальный путь в кэше → следующий тик снова возвращает провальный кэш
        # (can_reuse=False из-за goal mismatch, но полный re-solve даёт тот же результат).
        # Без кэша → re-solve каждый тик до разрешения ситуации.
        self._cached_paths = {aid: p for aid, p in paths.items() if len(p) > 1}
        self.plan_was_recomputed = True
        return paths

    # ── Режим B: per-agent A* с кэшем ────────────────────────────────────────
    def _solve_astar_cached(self, tasks: List[Dict],
                             reservations: Dict) -> Dict[int, List[Tuple]]:
        """
        Кэш на агента для Prioritized A* с приоритетом груза (cargo-first).

        CARGO-FIRST — двухфазное планирование:
        ─────────────────────────────────────────
        Фаза 1 — агенты С ГРУЗОМ (CARRY_TO_PACKER / RETURN_SHELF):
          1a. В reservations добавляются пути кэшированных cargo-агентов.
          1b. Cargo-агенты с REPLAN запускают A* — видят только _PERM блоки
              и позиции других cargo-агентов. No-cargo пути ещё НЕ добавлены
              → cargo получает свободные corridor-позиции.

        Фаза 2 — агенты БЕЗ ГРУЗА (GO_TO_SHELF):
          2a. В reservations добавляются пути кэшированных no-cargo-агентов.
          2b. No-cargo с REPLAN планируются В ОБХОД всего cargo.

        Итог: груз ВСЕГДА имеет приоритет в пространстве путей.
        Level-3 (resolve_movement_conflicts) дополнительно обеспечивает
        физический приоритет cargo в конфликтах (cargo +500 к score).
        """
        result:         Dict[int, List[Tuple]] = {}
        cargo_replan:   List[Dict]             = []
        nocargo_replan: List[Dict]             = []

        for task in tasks:
            aid       = task['agent_id']
            action    = task.get('action', 'OK')
            start     = task['start']
            goal      = task['goal']
            has_cargo = task.get('has_cargo', False)

            if action == 'WAIT':
                result[aid] = [start]
                continue

            cached    = self._cached_paths.get(aid)
            need_plan = (action == 'REPLAN' or cached is None or cached[-1] != goal)

            if not need_plan:
                if cached[0] == start:
                    result[aid]                 = cached
                    self._cached_has_cargo[aid] = has_cargo
                elif len(cached) >= 2 and cached[1] == start:
                    advanced                    = cached[1:]
                    self._cached_paths[aid]     = advanced
                    self._cached_has_cargo[aid] = has_cargo
                    result[aid]                 = advanced
                else:
                    need_plan = True

            if need_plan:
                (cargo_replan if has_cargo else nocargo_replan).append(task)

        def _cache_paths(new_paths: Dict, is_cargo: bool):
            for aid, path in new_paths.items():
                self._cached_has_cargo[aid] = is_cargo
                if len(path) > 1:
                    self._cached_paths[aid] = path
                else:
                    self._cached_paths.pop(aid, None)
                result[aid] = path

        def _reserve_cached(cargo_only: bool):
            for aid, path in result.items():
                if self._cached_has_cargo.get(aid, False) == cargo_only:
                    for i, pos in enumerate(path[1:], 1):
                        reservations.setdefault(pos, []).append(
                            {'agent_id': aid, 't_start': i, 't_end': i})

        # Фаза 1: cargo ───────────────────────────────────────────────────────
        _reserve_cached(cargo_only=True)                          # 1a
        if cargo_replan:                                          # 1b
            _cache_paths(self._algorithm.solve(cargo_replan, reservations), True)

        # Фаза 2: no-cargo (планируется В ОБХОД cargo) ────────────────────────
        _reserve_cached(cargo_only=False)                         # 2a
        if nocargo_replan:                                        # 2b
            _cache_paths(self._algorithm.solve(nocargo_replan, reservations), False)

        n_replan = len(cargo_replan) + len(nocargo_replan)
        if n_replan:
            logging.debug(f"[RouterContext/A*] cargo={len(cargo_replan)} "
                          f"no-cargo={len(nocargo_replan)} replanned / {len(tasks)} total")
        self.plan_was_recomputed = n_replan > 0
        return result


    def path_length(self, start, goal, has_cargo=False, returning_shelf=False) -> int:
        if hasattr(self._algorithm, 'path_length'):
            return self._algorithm.path_length(start, goal, has_cargo, returning_shelf)
        return 0


class BaseGridRouter(IRouter):
    DIR_OFFSETS = {'n': (-1, 0), 's': (1, 0), 'w': (0, -1), 'e': (0, 1)}

    def __init__(self, map_data: Dict):
        self.width     = map_data['width']
        self.height    = map_data['height']
        self.start_row = map_data['start_row']
        self.start_col = map_data['start_col']
        self.dirs_loaded: Dict[Tuple, Set[str]] = {}
        self.dirs_empty:  Dict[Tuple, Set[str]] = {}
        self.cell_types:  Dict[Tuple, str]      = {}
        for row in map_data['cells']:
            for cell in row:
                pos   = (cell['row'], cell['col'])
                text  = cell.get('text', '')
                parts = text.split('|') if '|' in text else ['F', '', '', '{}']
                self.cell_types[pos]  = parts[0]
                self.dirs_loaded[pos] = self._parse_dirs(parts[1] if len(parts) > 1 else '')
                self.dirs_empty[pos]  = self._parse_dirs(parts[2] if len(parts) > 2 else '')

    def _parse_dirs(self, s: str) -> Set[str]:
        return {d for d in s.split('-') if d in ('n', 'w', 's', 'e')}

    def _is_valid(self, pos: Tuple) -> bool:
        r, c = pos
        if not (self.start_row <= r < self.start_row + self.height): return False
        if not (self.start_col <= c < self.start_col + self.width):  return False
        return self.cell_types.get(pos, 'F') not in ('E', 'W')

    def _can_move(self, frm: Tuple, to: Tuple,
                  has_cargo: bool, returning_shelf: bool = False) -> bool:
        if not self._is_valid(to): return False
        dr, dc = to[0] - frm[0], to[1] - frm[1]
        direction = next(
            (d for d, (odr, odc) in self.DIR_OFFSETS.items()
             if dr == odr and dc == odc), None)
        if direction is None: return False

        if returning_shelf:
            to_type = self.cell_types.get(to, 'F')
            # Целевой стеллаж — всегда доступен
            if to_type == 'S': return True
            # Физические барьеры
            if to_type in ('W', 'E'): return False
            # Специальные ячейки (фасовщик P, очередь Z): стандартные направления
            if to_type in ('P', 'Z'):
                return direction in (self.dirs_loaded.get(frm, set()) |
                                     self.dirs_empty.get(frm, set()))
            # Напольные ячейки (F и другие): ПОЛНАЯ свобода движения.
            #
            # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: ранее использовались dirs_loaded|dirs_empty,
            # что блокировало агентов возврата в "однонаправленных" ячейках.
            # Агент CARRY_TO_PACKER входил в такую ячейку по трафику (E/S),
            # затем RETURN_SHELF не мог выйти в нужном направлении (N/W) —
            # perpetual "нет пути". Теперь возвратные агенты свободны на F-ячейках.
            # Level-3 (resolve_movement_conflicts) обеспечит физическую корректность.
            return True

        allowed = (self.dirs_loaded.get(frm, set()) if has_cargo
                   else self.dirs_empty.get(frm, set()))
        return direction in allowed

    def _is_blocked(self, pos: Tuple, t: int, res: Dict,
                    req_id: int, has_cargo: bool = False) -> bool:
        """
        Проверяет занятость pos в момент t.

        t_end == 0         → движущийся агент; блокирует только в t == 0.
        t_end == _IDLE_PERM → IDLE-агент; блокирует no-cargo; НЕ блокирует cargo
                              (cargo имеет право пути, Level-3 вытолкнет IDLE).
        t_end == _PERM      → постоянный блок (WAITING_SLOT/слот/UNLOADING); блокирует всегда.
        иначе              → временная бронь [t_start, t_end].
        """
        for b in res.get(pos, []):
            if b['agent_id'] == req_id:
                continue
            t_end = b['t_end']
            if b['t_start'] == 0 and t_end == 0:
                # Движущийся агент: блок только в t=0
                if t == 0:
                    return True
            elif t_end == _IDLE_PERM:
                # IDLE-агент: cargo проезжает (будет вытеснен Level-3)
                if not has_cargo:
                    return True
            elif b['t_start'] <= t <= t_end:
                return True
        return False

    def _is_swap(self, frm: Tuple, to: Tuple, t: int,
                 res: Dict, req_id: int) -> bool:
        """Блокирует swap. Начальные/постоянные/IDLE брони — не признак движения."""
        for b in res.get(frm, []):
            if b['agent_id'] == req_id: continue
            t_end = b['t_end']
            # t_end==0 (движущийся), t_end>=_IDLE_PERM (постоянный или IDLE) → не swap
            if t_end == 0 or t_end >= _IDLE_PERM: continue
            if b['t_start'] <= t <= t_end: return True
        return False

    def _astar(self, start, goal, res, aid, has_cargo,
               returning_shelf=False) -> Optional[List[Tuple]]:
        if not self._is_valid(start) or not self._is_valid(goal): return None
        g = {start: 0}; came = {start: None}
        frontier = [(0, start)]; exp = 0
        while frontier and exp < 10_000:
            exp += 1; _, cur = heapq.heappop(frontier)
            if cur == goal:
                path = []
                while cur is not None: path.append(cur); cur = came[cur]
                return path[::-1]
            for _, (dr, dc) in self.DIR_OFFSETS.items():
                nb = (cur[0] + dr, cur[1] + dc)
                if not self._is_valid(nb): continue
                if returning_shelf and self.cell_types.get(nb, 'F') == 'S' and nb != goal: continue
                if not self._can_move(cur, nb, has_cargo, returning_shelf): continue
                ng = g[cur] + 1
                if self._is_blocked(nb, ng, res, aid, has_cargo): continue
                if self._is_swap(cur, nb, ng, res, aid): continue
                if nb not in g or ng < g[nb]:
                    g[nb] = ng; came[nb] = cur
                    h = abs(nb[0] - goal[0]) + abs(nb[1] - goal[1])
                    heapq.heappush(frontier, (ng + h, nb))
        return None

    def diagnose_path_failure(self, start: Tuple, goal: Tuple,
                               res: Dict, aid: int,
                               has_cargo: bool, returning_shelf: bool) -> str:
        """
        Вызывается когда A* не нашёл путь. Возвращает строку-объяснение:
        - Тип и доступные направления стартовой клетки
        - Почему каждый сосед недоступен (стена / запрет направления / агент)
        - Тип целевой клетки и её доступность в принципе
        """
        lines: List[str] = [
            f"  🔍 ДИАГНОСТИКА пути А{aid} {start}→{goal} "
            f"({'cargo' if has_cargo else 'empty'}"
            f"{', ret' if returning_shelf else ''}):"]

        # Тип стартовой клетки и разрешённые направления
        c_type = self.cell_types.get(start, '?')
        d_load = self.dirs_loaded.get(start, set())
        d_empty = self.dirs_empty.get(start, set())
        lines.append(f"    Старт {start}: тип='{c_type}' "
                     f"dirs_loaded={sorted(d_load)} dirs_empty={sorted(d_empty)}")

        # Анализ всех 4 соседей
        for d, (dr, dc) in self.DIR_OFFSETS.items():
            nb = (start[0] + dr, start[1] + dc)
            reasons = []
            if not self._is_valid(nb):
                reasons.append("вне карты/стена")
            else:
                nb_type = self.cell_types.get(nb, 'F')
                if returning_shelf and nb_type == 'S':
                    reasons.append("S-доступна (ret)")
                elif not self._can_move(start, nb, has_cargo, returning_shelf):
                    reasons.append(f"запрет напр '{d}' (из {c_type}→{nb_type})")
                else:
                    blocked = self._is_blocked(nb, 1, res, aid, has_cargo)
                    if blocked:
                        agents_at = [b for b in res.get(nb, [])
                                     if b['agent_id'] != aid]
                        who = [(f"А{b['agent_id']} t_end={b['t_end']}") for b in agents_at]
                        reasons.append(f"занято: {', '.join(who)}")
                    else:
                        reasons.append("СВОБОДНО ✓")

            lines.append(f"    Сосед [{d}] {nb}: {'; '.join(reasons)}")

        # Целевая клетка
        g_type = self.cell_types.get(goal, '?')
        g_valid = self._is_valid(goal)
        g_blocked = self._is_blocked(goal, 99, res, aid, has_cargo) if g_valid else True
        lines.append(f"    Цель  {goal}: тип='{g_type}' "
                     f"valid={g_valid} blocked_t99={g_blocked}")

        return "\n".join(lines)

    def path_length(self, start, goal, has_cargo=False, returning_shelf=False) -> int:
        p = self._astar(start, goal, {}, -1, has_cargo, returning_shelf)
        return len(p) - 1 if p else 0

    def _reserve(self, path: List[Tuple], aid: int, res: Dict):
        for i, p in enumerate(path[1:], 1):
            res.setdefault(p, []).append({'agent_id': aid, 't_start': i, 't_end': i})


# ════════════════════════════════════════════════════════════════════════════
# 1. Prioritized A*
# ════════════════════════════════════════════════════════════════════════════
@RoutingAlgorithmRegistry.register("prioritized_astar")
class PrioritizedAStarSolver(BaseGridRouter):
    """
    Жадный последовательный A* (реактивный).
    Агенты планируются по убыванию score; каждый следующий видит брони предыдущих.

    СРАВНЕНИЕ: самый быстрый. Субоптимален. Требует WFG для устранения дедлоков.
    Рекомендуется как базовая линия для сравнения с CBS и LNS.
    """
    is_centralized = False

    def solve(self, tasks, reservations):
        result  = {}
        ordered = sorted(tasks,
                         key=lambda x: (1 if x.get('has_cargo', False) else 0,
                                        x.get('score', 0.0)),
                         reverse=True)
        if not hasattr(self, '_fail_count'):
            self._fail_count: Dict[int, int] = {}

        for t in ordered:
            aid = t['agent_id']; start = t['start']; goal = t['goal']
            hc  = t.get('has_cargo', False); rs = t.get('returning_shelf', False)
            if t.get('action') == 'WAIT': result[aid] = [start]; continue
            if start == goal:            result[aid] = [start]; continue

            path = self._astar(start, goal, reservations, aid, hc, rs)

            if path is None and hc:
                # ── ЭКСТРЕННЫЙ A*: попытка без time-indexed броней ─────────────
                # Применяется когда cargo-агент не может найти путь из-за
                # временны́х резерваций других агентов. Оставляем только
                # постоянные блоки (_PERM / _IDLE_PERM) — физические препятствия.
                # Level-3 разрешит физические конфликты при исполнении шага.
                hard_res = {
                    pos: [b for b in blist if b['t_end'] >= _IDLE_PERM]
                    for pos, blist in reservations.items()
                    if any(b['t_end'] >= _IDLE_PERM for b in blist)
                }
                path = self._astar(start, goal, hard_res, aid, hc, rs)
                if path:
                    logging.info(f"[A*] А{aid}: ЭКСТРЕННЫЙ маршрут "
                                 f"(без мягких броней) {start}→{goal}")

            if path:
                self._fail_count.pop(aid, None)
                result[aid] = path
                self._reserve(path, aid, reservations)
            else:
                fc = self._fail_count.get(aid, 0) + 1
                self._fail_count[aid] = fc
                if fc <= 2 or fc % 20 == 0:
                    logging.warning(f"[A*] нет пути: А{aid} {start}→{goal} "
                                    f"(попытка {fc})")
                if fc == 3 and hasattr(self, 'diagnose_path_failure'):
                    # Диагностика — один раз при первых 3 попытках
                    logging.warning(self.diagnose_path_failure(
                        start, goal, reservations, aid, hc, rs))
                result[aid] = [start]

        return result


# ════════════════════════════════════════════════════════════════════════════
# 2. CBS — Conflict-Based Search
# ════════════════════════════════════════════════════════════════════════════
@RoutingAlgorithmRegistry.register("cbs")
class CBSSolver(BaseGridRouter):
    """
    Conflict-Based Search (Sharon et al., 2012) — централизованный, оптимальный.

    Двухуровневый алгоритм:
      Нижний уровень: A* с time-space ограничениями (constraints).
      Верхний уровень: CT (дерево ограничений), BFS по конфликтам.

    Конфликты:
      vertex — два агента в одной клетке в один момент.
      swap   — агенты меняются клетками (A→B и B→A в один шаг).

    СРАВНЕНИЕ: оптимален по sum-of-costs. Экспоненциальный в худшем случае.
    На 20+ агентах упирается в лимит итераций — возвращает лучший найденный узел.
    """
    is_centralized = True

    def __init__(self, map_data: Dict, max_iterations: int = 200):
        super().__init__(map_data)
        self.max_iterations = max_iterations

    def solve(self, tasks, reservations):
        if not tasks: return {}
        waiting    = {t['agent_id']: [t['start']] for t in tasks if t.get('action') == 'WAIT'}
        replanning = [dict(t, action='OK') for t in tasks if t.get('action') == 'REPLAN']
        active     = [t for t in tasks if t.get('action') not in ('WAIT', 'REPLAN')]
        active    += replanning
        if not active: return waiting
        result = self._cbs_solve(active, reservations=reservations)
        result.update(waiting)
        for aid, path in result.items():
            self._reserve(path, aid, reservations)
        return result

    def _cbs_solve(self, tasks, reservations=None):
        res = reservations or {}
        root_paths = {
            t['agent_id']: (
                self._astar_constrained(
                    t['start'], t['goal'], {}, t['agent_id'],
                    t.get('has_cargo', False), t.get('returning_shelf', False),
                    reservations=res)
                or [t['start']])
            for t in tasks
        }
        root      = {'constraints': {}, 'paths': root_paths, 'cost': self._cost(root_paths)}
        open_list = [(root['cost'], 0, root)]
        counter   = 1
        best_node = root

        for iteration in range(self.max_iterations):
            if not open_list: break
            _, _, node = heapq.heappop(open_list)
            conflict = self._find_conflict(node['paths'])
            if conflict is None:
                logging.debug(f"[CBS] решение за {iteration} итераций")
                return node['paths']
            if node['cost'] <= best_node['cost']:
                best_node = node

            aid1, aid2, pos_for_1, pos_for_2, t_c, ctype = conflict

            # swap: aid1 запрещается входить в pos_for_1 (позиция aid2),
            #       aid2 запрещается входить в pos_for_2 (позиция aid1).
            # vertex: оба запрещаются в одной клетке.
            if ctype == 'swap':
                clist = [(aid1, pos_for_1), (aid2, pos_for_2)]
            else:
                clist = [(aid1, pos_for_1), (aid2, pos_for_1)]

            for c_aid, c_pos in clist:
                new_c = copy.deepcopy(node['constraints'])
                new_c.setdefault(c_aid, []).append((c_pos, t_c))
                task = next((t for t in tasks if t['agent_id'] == c_aid), None)
                if task is None: continue
                new_path = self._astar_constrained(
                    task['start'], task['goal'],
                    new_c.get(c_aid, []), c_aid,
                    task.get('has_cargo', False), task.get('returning_shelf', False),
                    reservations=res)
                if new_path is None: continue
                new_paths = {**node['paths'], c_aid: new_path}
                new_node  = {'constraints': new_c, 'paths': new_paths,
                             'cost': self._cost(new_paths)}
                heapq.heappush(open_list, (new_node['cost'], counter, new_node))
                counter += 1

        n = len(tasks)
        if n > 8:
            logging.warning(f"[CBS] лимит {self.max_iterations} итераций для {n} агентов")
        return best_node['paths']

    def _astar_constrained(self, start, goal, constraints, aid,
                            has_cargo, returning_shelf, reservations=None):
        forbidden = set(constraints)
        res = reservations or {}
        if not self._is_valid(start) or not self._is_valid(goal): return None
        g = {(start, 0): 0}; came = {(start, 0): None}
        frontier = [(0, start, 0)]; exp = 0
        while frontier and exp < 10_000:
            exp += 1; f, cur, t = heapq.heappop(frontier)
            if cur == goal:
                path = []; state = (cur, t)
                while state: path.append(state[0]); state = came[state]
                return path[::-1]
            for _, (dr, dc) in self.DIR_OFFSETS.items():
                nb = (cur[0] + dr, cur[1] + dc); nt = t + 1
                if not self._is_valid(nb): continue
                if returning_shelf and self.cell_types.get(nb, 'F') == 'S' and nb != goal: continue
                if not self._can_move(cur, nb, has_cargo, returning_shelf): continue
                if (nb, nt) in forbidden: continue
                if self._is_blocked(nb, nt, res, aid, has_cargo): continue
                if self._is_swap(cur, nb, nt, res, aid): continue
                state = (nb, nt)
                ng = g.get((cur, t), float('inf')) + 1
                if state not in g or ng < g[state]:
                    g[state] = ng; came[state] = (cur, t)
                    h = abs(nb[0] - goal[0]) + abs(nb[1] - goal[1])
                    heapq.heappush(frontier, (ng + h, nb, nt))
        return None

    def _find_conflict(self, paths):
        aids  = list(paths)
        max_t = max((len(p) for p in paths.values()), default=0)
        for t in range(max_t):
            pos_at_t = {}
            for aid in aids:
                pos = paths[aid][min(t, len(paths[aid]) - 1)]
                if pos in pos_at_t:
                    return (pos_at_t[pos], aid, pos, pos, t, 'vertex')
                pos_at_t[pos] = aid
            if t + 1 < max_t:
                for i, a1 in enumerate(aids):
                    for a2 in aids[i + 1:]:
                        p1, p2   = paths[a1], paths[a2]
                        p1t  = p1[min(t,     len(p1) - 1)]
                        p1t1 = p1[min(t + 1, len(p1) - 1)]
                        p2t  = p2[min(t,     len(p2) - 1)]
                        p2t1 = p2[min(t + 1, len(p2) - 1)]
                        if p1t == p2t1 and p2t == p1t1:
                            # a1 хочет в p2t, a2 хочет в p1t
                            return (a1, a2, p2t, p1t, t + 1, 'swap')
        return None

    def _cost(self, paths):
        return sum(len(p) for p in paths.values())


# ════════════════════════════════════════════════════════════════════════════
# Базовый класс LNS (общая логика destroy-and-repair)
# ════════════════════════════════════════════════════════════════════════════
class _LNSBase(BaseGridRouter):
    """
    Large Neighbourhood Search — метаэвристика поверх базового решателя.

    Алгоритм:
      1. Получаем начальное решение от base_solver.
      2. Итерируем max_iterations раз:
         a. Случайно выбираем k агентов (neighbourhood).
         b. Удаляем их пути из лучшего решения.
         c. Перепланируем k агентов с учётом путей остальных (через base_solver).
         d. Принимаем новое решение, если sum-of-costs стал меньше.
      3. Возвращаем лучшее найденное решение.

    Разница LNS-1 vs LNS-2 — только в base_solver (CBS vs A*).
    """
    is_centralized = True

    def __init__(self, map_data, neighbourhood_size=3,
                 max_iterations=20, base_solver=None):
        super().__init__(map_data)
        self.neighbourhood_size = neighbourhood_size
        self.max_iterations     = max_iterations
        self.base_solver        = base_solver or PrioritizedAStarSolver(map_data)

    def solve(self, tasks, reservations):
        if not tasks: return {}
        waiting  = {t['agent_id']: [t['start']] for t in tasks if t.get('action') == 'WAIT'}
        active   = [t for t in tasks if t.get('action') != 'WAIT']
        if not active: return waiting

        best_paths = self.base_solver.solve(active, copy.deepcopy(reservations))
        best_cost  = sum(len(p) for p in best_paths.values())
        task_map   = {t['agent_id']: t for t in active}

        for it in range(self.max_iterations):
            if len(active) <= 1: break
            k          = min(self.neighbourhood_size, len(active))
            chosen     = random.sample([t['agent_id'] for t in active], k)
            partial    = copy.deepcopy(reservations)
            for aid, path in best_paths.items():
                if aid not in chosen:
                    self._reserve(path, aid, partial)
            new_sub    = self.base_solver.solve([task_map[a] for a in chosen], partial)
            candidate  = {**best_paths, **new_sub}
            new_cost   = sum(len(p) for p in candidate.values())
            if new_cost < best_cost:
                best_paths = candidate; best_cost = new_cost
                logging.debug(f"[LNS] ит.{it}: cost {best_cost}→{new_cost} ✅")

        for aid, path in best_paths.items():
            self._reserve(path, aid, reservations)
        best_paths.update(waiting)
        return best_paths


# ════════════════════════════════════════════════════════════════════════════
# 3. LNS-1 / CBS  (LNS верхний уровень + CBS нижний уровень)
# ════════════════════════════════════════════════════════════════════════════
@RoutingAlgorithmRegistry.register("lns1_cbs")
@RoutingAlgorithmRegistry.register("lns1")
@RoutingAlgorithmRegistry.register("lns")
class LNS1CBSSolver(_LNSBase):
    """
    LNS-1: Large Neighbourhood Search (верхний) + CBS (нижний).

    Верхний уровень (LNS):
      Стохастический destroy-and-repair: случайно выбирает подмножество
      агентов, «сносит» их пути и заново решает для них задачу.
      Глобальная стоимость (sum-of-costs) используется как критерий улучшения.

    Нижний уровень (CBS):
      Для каждого выбранного neighbourhood вызывается CBS → он строит
      бесконфликтные пути с гарантией локальной оптимальности.
      Пути остальных агентов зафиксированы как броня в reservations.

    Итог: качество близко к CBS, но масштабируется на бо́льшие группы.
    Конфликты ВНУТРИ neighbourhood гарантированно отсутствуют (CBS).
    Конфликты МЕЖДУ neighbourhood и остальными агентами исключены резервациями.

    СРАВНЕНИЕ: лучше A* и LNS-2 по качеству; медленнее их; слабее чистого CBS
    на малых группах, но масштабируется туда где CBS упирается в лимит итераций.
    Алиасы: "lns1_cbs", "lns1", "lns" (обратная совместимость).
    Рекомендуется для 8-20 агентов.
    """
    is_centralized = True

    def __init__(self, map_data, neighbourhood_size=3, max_iterations=20):
        solver = CBSSolver(map_data)
        super().__init__(map_data, neighbourhood_size, max_iterations, solver)
        logging.info(f"  [LNS-1/CBS] neighbourhood={neighbourhood_size}, "
                     f"iterations={max_iterations}")


# ════════════════════════════════════════════════════════════════════════════
# 4. LNS-2 / Simple  (только верхний уровень, нижний = A*)
# ════════════════════════════════════════════════════════════════════════════
@RoutingAlgorithmRegistry.register("lns2_simple")
@RoutingAlgorithmRegistry.register("lns2")
class LNS2SimpleSolver(_LNSBase):
    """
    LNS-2: Large Neighbourhood Search (верхний) + Prioritized A* (нижний).

    Верхний уровень (LNS): идентичен LNS-1 — destroy-and-repair.

    Нижний уровень (Prioritized A*):
      Вместо CBS используется жадный A* — планирует агентов neighbourhood
      последовательно в порядке убывания score. Никакого CBS на нижнем уровне.
      Конфликты внутри neighbourhood могут возникать (приоритет score решает их),
      но следующая итерация LNS может их устранить через перевыборку.

    Итог: значительно быстрее LNS-1 (нет CBS-дерева). Качество путей ниже,
    чем у LNS-1, но выше чем у чистого A* за счёт LNS-итераций.

    СРАВНЕНИЕ: быстрейший из централизованных. Субоптимален внутри
    neighbourhood. Хорошо масштабируется на 20+ агентов.
    Алиасы: "lns2_simple", "lns2".
    Рекомендуется для 20+ агентов или когда скорость важнее качества.
    """
    is_centralized = True

    def __init__(self, map_data, neighbourhood_size=3, max_iterations=20):
        solver = PrioritizedAStarSolver(map_data)
        super().__init__(map_data, neighbourhood_size, max_iterations, solver)
        logging.info(f"  [LNS-2/A*] neighbourhood={neighbourhood_size}, "
                     f"iterations={max_iterations}")


# Обратная совместимость
AStarRouter       = PrioritizedAStarSolver
AlgorithmRegistry = RoutingAlgorithmRegistry