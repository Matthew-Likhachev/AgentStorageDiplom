"""
router_interface.py
Архитектура маршрутизации: Strategy Pattern + Registry (горячее добавление).

Как добавить новый алгоритм (3 шага):
─────────────────────────────────────
  1. Создайте класс-наследник BaseGridRouter (или IRouter если нужна полная свобода).
  2. Реализуйте метод solve(tasks, reservations) → Dict[agent_id, path].
  3. Зарегистрируйте: @RoutingAlgorithmRegistry.register("my_algo")

  Пример:
      @RoutingAlgorithmRegistry.register("my_algo")
      class MyAlgo(BaseGridRouter):
          def solve(self, tasks, reservations):
              ...

  Переключение в runtime:
      router_context.set_algorithm("my_algo")   # горячая замена без перезапуска

Доступные алгоритмы:
  "prioritized_astar"  — жадный последовательный A* (по умолчанию)
  "cbs"                — Conflict-Based Search (глобальная оптимизация)
  "lns"                — Large Neighbourhood Search поверх CBS
"""

from __future__ import annotations
import heapq
import logging
import random
import copy
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple, Set, Optional, Type, Any


# ── Интерфейс ────────────────────────────────────────────────────────────────
class IRouter(ABC):
    @abstractmethod
    def solve(self,
              tasks: List[Dict],
              reservations: Dict
              ) -> Dict[int, List[Tuple[int, int]]]:
        """
        tasks: список словарей с ключами:
            agent_id, start, goal, score, has_cargo, returning_shelf, action
        reservations: {pos: [{agent_id, t_start, t_end}]}  — изменяется in-place
        Возвращает: {agent_id: path}  (path включает start)
        """


# ── Реестр (горячее добавление) ───────────────────────────────────────────────
class RoutingAlgorithmRegistry:
    """
    Глобальный реестр алгоритмов маршрутизации.
    Декоратор @RoutingAlgorithmRegistry.register("name") регистрирует класс.
    """
    _registry: Dict[str, Type[IRouter]] = {}

    @classmethod
    def register(cls, name: str):
        """Декоратор: @RoutingAlgorithmRegistry.register("algo_name")"""
        def decorator(algo_cls: Type[IRouter]) -> Type[IRouter]:
            cls._registry[name] = algo_cls
            logging.info(f"  [Router] Зарегистрирован алгоритм: '{name}'")
            return algo_cls
        return decorator

    @classmethod
    def create(cls, name: str, map_data: Dict, **kwargs) -> IRouter:
        if name not in cls._registry:
            available = list(cls._registry.keys())
            raise ValueError(f"Алгоритм '{name}' не найден. Доступны: {available}")
        return cls._registry[name](map_data, **kwargs)

    @classmethod
    def list_algorithms(cls) -> List[str]:
        return list(cls._registry.keys())


# ── Контекст (горячая замена стратегии) ──────────────────────────────────────
class RouterContext:
    """
    Хранит текущий алгоритм и делегирует solve().
    Поддерживает горячую замену через set_algorithm().
    """
    def __init__(self, algorithm: IRouter):
        self._algorithm  = algorithm
        self._map_data:  Optional[Dict] = None

    def set_algorithm(self, name: str, **kwargs):
        """Горячая замена алгоритма без перезапуска симуляции."""
        if self._map_data is None:
            raise RuntimeError("map_data не установлен. Вызовите set_map_data() сначала.")
        self._algorithm = RoutingAlgorithmRegistry.create(name, self._map_data, **kwargs)
        logging.info(f"  [Router] Алгоритм переключён на '{name}'")

    def set_map_data(self, map_data: Dict):
        self._map_data = map_data

    def solve(self, tasks: List[Dict], reservations: Dict) -> Dict[int, List[Tuple[int, int]]]:
        return self._algorithm.solve(tasks, reservations)

    # Обратная совместимость с agent_module (там вызывают router.solve напрямую)
    def path_length(self, start, goal, has_cargo=False, returning_shelf=False) -> int:
        if hasattr(self._algorithm, 'path_length'):
            return self._algorithm.path_length(start, goal, has_cargo, returning_shelf)
        return 0


# ── Базовый класс с утилитами карты ──────────────────────────────────────────
class BaseGridRouter(IRouter):
    """
    Базовый класс: разбирает map_data, предоставляет _is_valid, _can_move, _astar.
    Наследуйтесь от него для новых алгоритмов — не нужно дублировать логику карты.
    """
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
        Проверяет занятость pos в момент t.
        Резервации с t_start==t_end==0 считаются постоянными (статичный агент):
        они блокируют pos на ВСЕХ временны́х шагах, включая t>=1.
        """
        for b in res.get(pos, []):
            if b['agent_id'] == req_id:
                continue
            # Постоянная резервация (статичный агент) — блокирует всегда
            if b['t_start'] == 0 and b['t_end'] == 0:
                return True
            # Обычная резервация — проверяем временно́й интервал
            if b['t_start'] <= t <= b['t_end']:
                return True
        return False

    def _is_swap(self, frm: Tuple, to: Tuple, t: int,
                 res: Dict, req_id: int) -> bool:
        """
        Блокирует swap-конфликт: агент A идёт frm→to, агент B идёт to→frm.
        В reservations: если B зарезервировал frm на шаге t (он туда пришёл),
        значит B вышел из to на шаге t-1 → swap.
        Также блокирует вход в to если он постоянно занят (статичный агент).
        """
        # Проверяем что frm не занят другим агентом на шаге t (swap из to в frm)
        for b in res.get(frm, []):
            if b['agent_id'] == req_id:
                continue
            if b['t_start'] == 0 and b['t_end'] == 0:
                # Статичный агент в frm — мы уже там, это нормально (это наша позиция)
                continue
            if b['t_start'] <= t <= b['t_end']:
                return True
        return False

    def _astar(self, start: Tuple, goal: Tuple, res: Dict,
               aid: int, has_cargo: bool,
               returning_shelf: bool = False) -> Optional[List[Tuple]]:
        if not self._is_valid(start) or not self._is_valid(goal):
            return None
        g        = {start: 0}
        came     = {start: None}
        frontier = [(0, start)]
        exp      = 0

        while frontier and exp < 10000:
            exp += 1
            _, cur = heapq.heappop(frontier)
            if cur == goal:
                path = []
                while cur is not None:
                    path.append(cur); cur = came[cur]
                return path[::-1]

            for d, (dr, dc) in self.DIR_OFFSETS.items():
                nb = (cur[0] + dr, cur[1] + dc)
                if not self._is_valid(nb): continue
                # Промежуточные S-клетки непроходимы при возврате стеллажа
                if (returning_shelf
                        and self.cell_types.get(nb, 'F') == 'S'
                        and nb != goal):
                    continue
                if not self._can_move(cur, nb, has_cargo, returning_shelf): continue
                ng = g[cur] + 1
                if self._is_blocked(nb, ng, res, aid): continue
                if self._is_swap(cur, nb, ng, res, aid): continue
                if nb not in g or ng < g[nb]:
                    g[nb] = ng; came[nb] = cur
                    h = abs(nb[0] - goal[0]) + abs(nb[1] - goal[1])
                    heapq.heappush(frontier, (ng + h, nb))
        return None

    def path_length(self, start: Tuple, goal: Tuple,
                    has_cargo: bool = False,
                    returning_shelf: bool = False) -> int:
        path = self._astar(start, goal, {}, -1, has_cargo, returning_shelf)
        return len(path) - 1 if path else 0

    def _reserve(self, path: List[Tuple], aid: int, res: Dict):
        for i, p in enumerate(path[1:], 1):
            res.setdefault(p, []).append(
                {'agent_id': aid, 't_start': i, 't_end': i})


# ════════════════════════════════════════════════════════════════════════════
# АЛГОРИТМ 1: Prioritized A* (жадный последовательный)
# ════════════════════════════════════════════════════════════════════════════
@RoutingAlgorithmRegistry.register("prioritized_astar")
class PrioritizedAStarSolver(BaseGridRouter):
    """
    Планирует агентов последовательно в порядке убывания score.
    Каждый следующий агент видит брони предыдущих и объезжает их.

    Сложность: O(N * A*) где N — число агентов.
    Качество: субоптимально, но быстро. Подходит для большинства случаев.

    Добавление нового алгоритма по этому образцу:
      1. @RoutingAlgorithmRegistry.register("your_name")
      2. class YourSolver(BaseGridRouter):
      3.     def solve(self, tasks, reservations): ...
    """

    def solve(self, tasks: List[Dict], reservations: Dict
              ) -> Dict[int, List[Tuple]]:
        result  = {}
        ordered = sorted(tasks, key=lambda x: x.get('score', 0.0), reverse=True)

        for t in ordered:
            aid             = t['agent_id']
            start           = t['start']
            goal            = t['goal']
            has_cargo       = t.get('has_cargo', False)
            returning_shelf = t.get('returning_shelf', False)

            if t.get('action') == 'WAIT':
                result[aid] = [start]; continue
            if start == goal:
                result[aid] = [start]; continue

            path = self._astar(start, goal, reservations, aid,
                               has_cargo, returning_shelf)
            if path:
                result[aid] = path
                self._reserve(path, aid, reservations)
            else:
                logging.warning(f"[PrioritizedAStar] ❌ Нет пути: агент {aid} "
                                f"{start}→{goal}")
                result[aid] = [start]

        return result


# ════════════════════════════════════════════════════════════════════════════
# АЛГОРИТМ 2: CBS — Conflict-Based Search
# ════════════════════════════════════════════════════════════════════════════
@RoutingAlgorithmRegistry.register("cbs")
class CBSSolver(BaseGridRouter):
    """
    Conflict-Based Search (Sharon et al., 2012).

    Двухуровневый алгоритм:
      Низкий уровень: A* с дополнительными ограничениями (constraints).
      Высокий уровень: CT (Constraint Tree) — дерево ограничений.

    Ограничение (constraint): (agent_id, pos, t) — агент не может быть
    в клетке pos в такт t.

    Алгоритм:
      1. Строим начальные пути без ограничений (каждый агент отдельно).
      2. Ищем первый конфликт (vertex или swap) среди всех пар агентов.
      3. Создаём два дочерних узла CT: в одном ограничение на агента A,
         в другом — на агента B.
      4. В каждом узле перепланируем только затронутого агента.
      5. Повторяем пока конфликтов нет или не достигнут лимит итераций.

    Качество: оптимально по sum-of-costs при малом числе агентов.
    Сложность: экспоненциальная в худшем случае, но на практике быстро.
    """

    def __init__(self, map_data: Dict, max_iterations: int = 200):
        super().__init__(map_data)
        self.max_iterations = max_iterations

    def solve(self, tasks: List[Dict], reservations: Dict
              ) -> Dict[int, List[Tuple]]:
        if not tasks:
            return {}

        # REPLAN = агент застрял → сбрасываем путь к старту, перепланируем как обычный
        # WAIT   = уступает, остаётся на месте
        waiting  = {t['agent_id']: [t['start']]
                    for t in tasks if t.get('action') == 'WAIT'}
        replanning = [dict(t, action='OK') for t in tasks if t.get('action') == 'REPLAN']
        active   = [t for t in tasks if t.get('action') not in ('WAIT', 'REPLAN')]
        active   = active + replanning   # REPLAN-агенты планируются как обычные

        if not active:
            return waiting

        result = self._cbs_solve(active, reservations=reservations)
        result.update(waiting)

        # Применяем найденные пути в reservations
        for aid, path in result.items():
            self._reserve(path, aid, reservations)

        return result

    def _cbs_solve(self, tasks: List[Dict],
                   reservations: Optional[Dict] = None) -> Dict[int, List[Tuple]]:
        """
        Решает MAPF через CBS.
        reservations — постоянные брони (t=0) статичных агентов и пути
        уже запланированных агентов. Передаётся в _astar_constrained чтобы
        CBS-пути не проходили сквозь стоящих агентов.
        """
        res = reservations or {}

        # Начальный узел: пути с учётом reservations, без CBS-ограничений
        root_paths = {}
        for t in tasks:
            path = self._astar_constrained(
                t['start'], t['goal'], {},
                t['agent_id'], t.get('has_cargo', False),
                t.get('returning_shelf', False),
                reservations=res)
            root_paths[t['agent_id']] = path or [t['start']]

        root = {'constraints': {}, 'paths': root_paths,
                'cost': self._cost(root_paths)}

        # Очередь CT (min-heap по стоимости)
        open_list = [(root['cost'], 0, root)]
        counter   = 1
        best_node = root   # лучший найденный узел (минимум конфликтов)

        for iteration in range(self.max_iterations):
            if not open_list:
                break
            _, _, node = heapq.heappop(open_list)

            conflict = self._find_conflict(node['paths'])
            if conflict is None:
                # Решение найдено без конфликтов
                logging.debug(f"[CBS] Решение за {iteration} итераций")
                return node['paths']

            # Обновляем лучший узел (меньше конфликтов = лучше)
            if node['cost'] <= best_node['cost']:
                best_node = node

            aid1, aid2, pos, t_conflict, ctype = conflict

            for constrained_aid in (aid1, aid2):
                new_constraints = copy.deepcopy(node['constraints'])
                new_constraints.setdefault(constrained_aid, []).append(
                    (pos, t_conflict))

                # Перепланируем только затронутого агента
                task = next((t for t in tasks if t['agent_id'] == constrained_aid), None)
                if task is None:
                    continue

                new_path = self._astar_constrained(
                    task['start'], task['goal'],
                    new_constraints.get(constrained_aid, []),
                    constrained_aid,
                    task.get('has_cargo', False),
                    task.get('returning_shelf', False),
                    reservations=res)

                if new_path is None:
                    continue  # нет пути с этим ограничением

                new_paths = dict(node['paths'])
                new_paths[constrained_aid] = new_path
                new_cost = self._cost(new_paths)
                new_node = {'constraints': new_constraints,
                            'paths': new_paths, 'cost': new_cost}
                heapq.heappush(open_list, (new_cost, counter, new_node))
                counter += 1

        # Лимит итераций — возвращаем лучший найденный узел,
        # а не root_paths (который может быть полон конфликтов).
        # Если CBS запущен для большой группы (>8 агентов) — предупреждение.
        n = len(tasks)
        if n > 8:
            logging.warning(f"[CBS] Лимит итераций ({self.max_iterations}) "
                            f"для {n} агентов. Используем лучший найденный узел.")
        else:
            logging.debug(f"[CBS] Лимит итераций ({self.max_iterations}) "
                          f"для {n} агентов.")
        return best_node['paths']

    def _astar_constrained(self, start: Tuple, goal: Tuple,
                            constraints: List[Tuple],
                            aid: int, has_cargo: bool,
                            returning_shelf: bool,
                            reservations: Optional[Dict] = None,
                            ) -> Optional[List[Tuple]]:
        """
        A* с явными ограничениями CBS (pos, t) + проверкой reservations.
        reservations — основной словарь брони (включая постоянные t=0 блоки).
        """
        forbidden: Set[Tuple] = set(constraints)  # {(pos, t)}
        res = reservations or {}

        if not self._is_valid(start) or not self._is_valid(goal):
            return None

        g        = {(start, 0): 0}
        came     = {(start, 0): None}
        frontier = [(0, start, 0)]  # (f, pos, t)
        exp      = 0

        while frontier and exp < 10000:
            exp += 1
            f, cur, t = heapq.heappop(frontier)
            if cur == goal:
                path = []
                state = (cur, t)
                while state is not None:
                    path.append(state[0])
                    state = came[state]
                return path[::-1]

            for d, (dr, dc) in self.DIR_OFFSETS.items():
                nb = (cur[0] + dr, cur[1] + dc)
                nt = t + 1
                if not self._is_valid(nb): continue
                if (returning_shelf
                        and self.cell_types.get(nb, 'F') == 'S'
                        and nb != goal):
                    continue
                if not self._can_move(cur, nb, has_cargo, returning_shelf): continue
                if (nb, nt) in forbidden: continue        # CBS ограничение
                if self._is_blocked(nb, nt, res, aid): continue  # постоянные брони
                if self._is_swap(cur, nb, nt, res, aid): continue
                state = (nb, nt)
                ng = g.get((cur, t), float('inf')) + 1
                if state not in g or ng < g[state]:
                    g[state]    = ng
                    came[state] = (cur, t)
                    h = abs(nb[0] - goal[0]) + abs(nb[1] - goal[1])
                    heapq.heappush(frontier, (ng + h, nb, nt))
        return None

    def _find_conflict(self, paths: Dict[int, List[Tuple]]
                       ) -> Optional[Tuple]:
        """
        Ищет первый конфликт между парами агентов.
        Возвращает (aid1, aid2, pos, t, type) или None.
        """
        aids = list(paths.keys())
        max_t = max((len(p) for p in paths.values()), default=0)

        for t in range(max_t):
            pos_at_t: Dict[Tuple, int] = {}
            for aid in aids:
                path = paths[aid]
                pos  = path[min(t, len(path) - 1)]
                if pos in pos_at_t:
                    return (pos_at_t[pos], aid, pos, t, 'vertex')
                pos_at_t[pos] = aid

            # Swap-конфликты
            if t + 1 < max_t:
                for i, aid1 in enumerate(aids):
                    for aid2 in aids[i+1:]:
                        p1 = paths[aid1]; p2 = paths[aid2]
                        pos1_t  = p1[min(t,   len(p1)-1)]
                        pos1_t1 = p1[min(t+1, len(p1)-1)]
                        pos2_t  = p2[min(t,   len(p2)-1)]
                        pos2_t1 = p2[min(t+1, len(p2)-1)]
                        if pos1_t == pos2_t1 and pos2_t == pos1_t1:
                            return (aid1, aid2, pos1_t, t, 'swap')
        return None

    def _cost(self, paths: Dict[int, List[Tuple]]) -> int:
        return sum(len(p) for p in paths.values())


# ════════════════════════════════════════════════════════════════════════════
# АЛГОРИТМ 3: LNS — Large Neighbourhood Search поверх CBS
# ════════════════════════════════════════════════════════════════════════════
@RoutingAlgorithmRegistry.register("lns")
class LNSSolver(BaseGridRouter):
    """
    Large Neighbourhood Search (метаэвристика поверх базового решения).

    Алгоритм:
      1. Получаем начальное решение от base_solver (CBS или Prioritized A*).
      2. Итерируем:
         a. Случайно выбираем подмножество агентов размером neighbourhood_size.
         b. Удаляем их пути из решения.
         c. Перепланируем выбранных агентов с учётом путей остальных.
         d. Принимаем новое решение если sum-of-costs уменьшился.
      3. Возвращаем лучшее найденное решение.

    Параметры:
      neighbourhood_size — сколько агентов перепланировать за итерацию (default=3)
      max_iterations     — лимит итераций LNS (default=20)
      base_algorithm     — имя базового алгоритма (default="prioritized_astar")
    """

    def __init__(self, map_data: Dict,
                 neighbourhood_size: int = 3,
                 max_iterations: int = 20,
                 base_algorithm: str = "cbs"):
        super().__init__(map_data)
        self.neighbourhood_size = neighbourhood_size
        self.max_iterations     = max_iterations
        # Создаём базовый решатель напрямую (не через реестр чтобы избежать цикла)
        if base_algorithm == "cbs":
            self.base_solver: BaseGridRouter = CBSSolver(map_data)
        else:
            self.base_solver = PrioritizedAStarSolver(map_data)
        logging.info(f"  [LNS] base_algorithm='{base_algorithm}', "
                     f"neighbourhood={neighbourhood_size}, iters={max_iterations}")

    def solve(self, tasks: List[Dict], reservations: Dict
              ) -> Dict[int, List[Tuple]]:
        if not tasks:
            return {}

        waiting = {t['agent_id']: [t['start']]
                   for t in tasks if t.get('action') == 'WAIT'}
        active  = [t for t in tasks if t.get('action') != 'WAIT']

        if not active:
            reservations.update({})
            return waiting

        # Шаг 1: начальное решение
        init_res: Dict = {}
        best_paths = self.base_solver.solve(active, init_res)
        best_cost  = self._total_cost(best_paths)

        task_map = {t['agent_id']: t for t in active}

        # Шаг 2: LNS итерации
        for iteration in range(self.max_iterations):
            if len(active) <= 1:
                break

            # Выбираем neighbourhood
            k = min(self.neighbourhood_size, len(active))
            chosen_ids = random.sample([t['agent_id'] for t in active], k)

            # Строим reservations из путей НЕ выбранных агентов
            partial_res: Dict = {}
            for aid, path in best_paths.items():
                if aid not in chosen_ids:
                    self._reserve(path, aid, partial_res)

            # Перепланируем выбранных
            chosen_tasks = [task_map[aid] for aid in chosen_ids]
            new_sub = self.base_solver.solve(chosen_tasks, partial_res)

            # Собираем полное решение
            candidate = dict(best_paths)
            candidate.update(new_sub)
            new_cost = self._total_cost(candidate)

            if new_cost < best_cost:
                best_paths = candidate
                best_cost  = new_cost
                logging.debug(f"[LNS] Итерация {iteration}: "
                              f"cost {best_cost} → {new_cost} ✅")

        logging.debug(f"[LNS] Финальная стоимость: {best_cost}")

        # Применяем в основные reservations
        for aid, path in best_paths.items():
            self._reserve(path, aid, reservations)

        best_paths.update(waiting)
        return best_paths

    def _total_cost(self, paths: Dict[int, List[Tuple]]) -> int:
        return sum(len(p) for p in paths.values())


# ── Обратная совместимость ────────────────────────────────────────────────────
# Старые имена классов работают как прежде
AStarRouter = PrioritizedAStarSolver
AlgorithmRegistry = RoutingAlgorithmRegistry