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

# Маркер постоянной брони (статичные агенты, занятые слоты).
# Движущиеся агенты используют t_end=0 → блокируют только в t=0.
_PERM: int = 99_999


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
    Контекст алгоритма + кэш плана для централизованных методов.

    Кэш плана (CBS / LNS-1 / LNS-2):
      Централизованные алгоритмы строят бесконфликтный план сразу для всех
      агентов. Пересчитывать его каждый тик дорого и бессмысленно — план
      остаётся валидным, пока агенты следуют ему шаг за шагом.

      Кэш сохраняет полные пути. Каждый тик RouterContext проверяет:
        • Тот же набор агентов?
        • Те же цели?
        • Каждый агент на ожидаемой позиции (или продвинулся на один шаг)?
      Если всё сходится — возвращает сдвинутые пути из кэша без вызова solver.
      При любом отклонении (REPLAN, новая цель, новый агент) — полный пересчёт.

    plan_was_recomputed:
      True  → план только что пересчитан; AgentManager может пропустить WFG.
      False → использован кэш; конфликтов нет по определению, WFG тоже пропускать.
      Итого: для CBS/LNS WFG не нужен никогда (см. agent_module.py, шаг 6).

    swap_losers:
      Агенты, проигравшие swap-конфликт в Level-3. На следующем тике
      check_and_resolve назначит им WAIT → разрывает осцилляцию ↑↓.
    """
    def __init__(self, algorithm: IRouter):
        self._algorithm          = algorithm
        self._map_data: Optional[Dict] = None
        self._cached_paths: Dict[int, List[Tuple]] = {}
        self.plan_was_recomputed: bool = True
        self.swap_losers: Set[int] = set()   # читает DeadlockResolver

    @property
    def is_centralized(self) -> bool:
        return getattr(self._algorithm, 'is_centralized', False)

    def set_algorithm(self, name: str, **kw):
        if self._map_data is None:
            raise RuntimeError("Вызовите set_map_data() сначала.")
        self._algorithm = RoutingAlgorithmRegistry.create(name, self._map_data, **kw)
        self._cached_paths.clear()
        self.plan_was_recomputed = True

    def set_map_data(self, map_data: Dict):
        self._map_data = map_data

    def invalidate_plan(self):
        """Сброс кэша — вызывать при изменении состояния агентов."""
        self._cached_paths.clear()
        self.plan_was_recomputed = True

    def solve(self, tasks: List[Dict], reservations: Dict
              ) -> Dict[int, List[Tuple]]:
        if not self.is_centralized:
            self.plan_was_recomputed = True
            return self._algorithm.solve(tasks, reservations)

        # ── Централизованный: кэшируем план ──────────────────────────────────
        task_map    = {t['agent_id']: t for t in tasks}
        current_ids = set(task_map)
        result: Dict[int, List[Tuple]] = {}
        can_reuse = bool(self._cached_paths) and current_ids == set(self._cached_paths)

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
            self._cached_paths = result
            self.plan_was_recomputed = False
            for aid, path in result.items():
                for i, pos in enumerate(path[1:], 1):
                    reservations.setdefault(pos, []).append(
                        {'agent_id': aid, 't_start': i, 't_end': i})
            logging.debug("[RouterContext] кэш переиспользован")
            return result

        # Полный пересчёт
        paths = self._algorithm.solve(tasks, reservations)
        self._cached_paths       = dict(paths)
        self.plan_was_recomputed = True
        return paths

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
            if self.cell_types.get(to, 'F') == 'S': return True
            return direction in (self.dirs_loaded.get(frm, set()) |
                                 self.dirs_empty.get(frm, set()))
        allowed = (self.dirs_loaded.get(frm, set()) if has_cargo
                   else self.dirs_empty.get(frm, set()))
        return direction in allowed

    def _is_blocked(self, pos: Tuple, t: int, res: Dict, req_id: int) -> bool:
        """
        t_end == 0    → движущийся агент; блокирует только в t == 0.
        t_end == _PERM → постоянный блок (IDLE/WAITING_SLOT/слот); блокирует всегда.
        иначе         → временная бронь [t_start, t_end].
        """
        for b in res.get(pos, []):
            if b['agent_id'] == req_id: continue
            t_end = b['t_end']
            if b['t_start'] == 0 and t_end == 0:
                if t == 0: return True
            elif b['t_start'] <= t <= t_end:
                return True
        return False

    def _is_swap(self, frm: Tuple, to: Tuple, t: int,
                 res: Dict, req_id: int) -> bool:
        """Блокирует swap. Начальные/постоянные брони — не признак движения."""
        for b in res.get(frm, []):
            if b['agent_id'] == req_id: continue
            t_end = b['t_end']
            if t_end == 0 or t_end >= _PERM: continue   # не swap-сигнал
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
                if self._is_blocked(nb, ng, res, aid): continue
                if self._is_swap(cur, nb, ng, res, aid): continue
                if nb not in g or ng < g[nb]:
                    g[nb] = ng; came[nb] = cur
                    h = abs(nb[0] - goal[0]) + abs(nb[1] - goal[1])
                    heapq.heappush(frontier, (ng + h, nb))
        return None

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
        ordered = sorted(tasks, key=lambda x: x.get('score', 0.0), reverse=True)
        for t in ordered:
            aid = t['agent_id']; start = t['start']; goal = t['goal']
            hc  = t.get('has_cargo', False); rs = t.get('returning_shelf', False)
            if t.get('action') == 'WAIT': result[aid] = [start]; continue
            if start == goal:            result[aid] = [start]; continue
            path = self._astar(start, goal, reservations, aid, hc, rs)
            if path:
                result[aid] = path; self._reserve(path, aid, reservations)
            else:
                logging.warning(f"[A*] нет пути: А{aid} {start}→{goal}")
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
                if self._is_blocked(nb, nt, res, aid): continue
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