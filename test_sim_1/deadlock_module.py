"""
deadlock_module.py
3-уровневая реактивная система разрешения дедлоков.

══════════════════════════════════════════════════════════════════
НАУЧНАЯ ОСНОВА:
  Deadlock-Free Hybrid RL-MAPF (arXiv:2511.22685) +
  Wait-For Graph detection (Coffman et al., 1971)

АРХИТЕКТУРА (вызовы строго по порядку в run_tick):

  ┌─────────────────────────────────────────────────────────┐
  │  УРОВЕНЬ 1 — check_and_resolve()      ДО router.solve() │
  │    Таймер застревания → REPLAN                          │
  │    Изолированные случаи: агент не двигается N тактов    │
  ├─────────────────────────────────────────────────────────┤
  │  router.solve(tasks)  ← вызов маршрутизатора            │
  ├─────────────────────────────────────────────────────────┤
  │  УРОВЕНЬ 2 — resolve_local_deadlocks()  ПОСЛЕ solve()   │
  │    WFG-цикл → локальный CBS только для виновных агентов │
  │    Корень проблемы: циклическое ожидание                │
  ├─────────────────────────────────────────────────────────┤
  │  УРОВЕНЬ 3 — resolve_movement_conflicts()  ПЕРЕД move() │
  │    Pass1 static / Pass2 vertex / Pass3 swap             │
  │    Финальная гарантия: ни одной коллизии                │
  └─────────────────────────────────────────────────────────┘

Совместимость: все публичные методы прежних версий сохранены.
Чтобы активировать уровень 2 передайте map_data в конструктор.
══════════════════════════════════════════════════════════════════
"""
import logging
from typing import Dict, List, Set, Tuple, Any, Optional

_STATIONARY_STATUSES = {"IDLE", "UNLOADING", "WAITING_SLOT"}


class DeadlockResolver:
    """
    Реактивный resolver дедлоков.

    Параметры:
        stuck_threshold  — тактов неподвижности до REPLAN (уровень 1)
        map_data         — данные карты для локального CBS (уровень 2).
                           Если None — уровень 2 пропускается.
        local_cbs_iters  — лимит итераций локального CBS (уровень 2).
    """

    def __init__(self,
                 stuck_threshold: int = 8,
                 map_data: Optional[Dict] = None,
                 local_cbs_iters: int = 120):
        self.stuck_threshold = stuck_threshold

        # Level 1: timer
        self.last_pos:    Dict[int, Tuple[int, int]] = {}
        self.stuck_since: Dict[int, int]             = {}

        # Проходимые клетки карты для IDLE-уступания (Pass 1 уровня 3).
        # Тип 'F' — пол, 'Z' — зона ожидания. Остальные непроходимы.
        self._walkable: Set[Tuple[int, int]] = set()
        if map_data is not None:
            _PASSABLE = {'F', 'Z'}
            for row in map_data.get('cells', []):
                for c in row:
                    if c.get('type') in _PASSABLE:
                        self._walkable.add((c['row'], c['col']))

        # Level 2: local CBS
        self._local_cbs = None
        if map_data is not None:
            try:
                from router_interface import CBSSolver
                self._local_cbs = CBSSolver(map_data, max_iterations=local_cbs_iters)
                logging.info("  [DeadlockResolver] ✅ Локальный CBS готов "
                             f"(max_iter={local_cbs_iters})")
            except ImportError:
                logging.warning("  [DeadlockResolver] ⚠️  router_interface не найден — "
                                "уровень 2 (WFG+CBS) недоступен")
            except Exception as exc:
                logging.warning(f"  [DeadlockResolver] ⚠️  CBS init: {exc}")

        # Статистика
        self._stat_cycles_detected: int = 0
        self._stat_cycles_resolved: int = 0

        # Level 1 oscillation detection
        self._POS_HISTORY_LEN: int               = 6     # длина окна истории
        self._OSC_THRESHOLD:   int               = 4     # тактов осцилляции до REPLAN
        self._pos_history: Dict[int, list]       = {}    # {agent_id: [pos, ...]}
        self._osc_since:   Dict[int, int]        = {}    # {agent_id: tick_first_osc}

        # Force-wait: агент принудительно стоит N тиков после REPLAN из-за осцилляции.
        # Ключ = agent_id, значение = тик до которого стоять.
        # Предотвращает повторную осцилляцию: другой агент за это время проходит мимо.
        self._force_wait_until: Dict[int, int]   = {}
        self._FORCE_WAIT_TICKS: int              = 6
        # Счётчик повторных осцилляций: если агент осциллирует снова после wait,
        # wait удлиняется и стагерируется по ID (чтобы не все перезапустились вместе).
        self._osc_repeat_count: Dict[int, int]   = {}

    def _find_chain_push(
            self,
            start_pos:   Tuple[int, int],
            winner_pos:  Tuple[int, int],
            winner_dest: Tuple[int, int],
            occupied:    Set[Tuple[int, int]],
            pos_to_idle: Dict[Tuple[int, int], int],
            max_depth:   int = 6,
    ) -> Optional[List[Tuple[int, Tuple[int, int]]]]:
        """
        BFS-поиск цепного сдвига IDLE-агентов («волновое вытеснение»).

        Проблема одиночного _find_idle_aside: если все соседи IDLE-агента
        заняты другими IDLE — метод возвращал None и мувер стоял вечно.
        При плотной расстановке (30 агентов на старте) это блокирует склад.

        Решение — chain push (аналог push-операции из MAPF-литературы):
          1. BFS от start_pos через клетки, занятые IDLE-агентами.
          2. При достижении свободной клетки — найдена цепочка.
          3. Каждый IDLE в цепочке сдвигается на одну клетку вперёд.
             Все смещения за ОДИН тик: каждый агент занимает клетку,
             которую в этот же тик освобождает следующий — коллизий нет.

        Приоритет первого шага: перпендикулярно направлению мувера.
        Клетка winner_pos исключена всегда (запрет swap).

        Возвращает List[(agent_id, new_pos)] или None.
        """
        from collections import deque

        dr = winner_dest[0] - winner_pos[0]
        dc = winner_dest[1] - winner_pos[1]

        def neighbors_of(pos: Tuple[int, int]) -> List[Tuple[int, int]]:
            r, c = pos
            return [nb for nb in [(r-1,c),(r+1,c),(r,c-1),(r,c+1)]
                    if nb in self._walkable and nb != winner_pos]

        def perp_first(nb: Tuple[int, int], from_pos: Tuple[int, int]) -> int:
            step_r = nb[0] - from_pos[0]
            step_c = nb[1] - from_pos[1]
            return 0 if dr * step_r + dc * step_c == 0 else 1

        visited: Set[Tuple[int, int]] = {start_pos}
        queue: deque = deque()

        for nb in sorted(neighbors_of(start_pos),
                         key=lambda nb: perp_first(nb, start_pos)):
            if nb not in visited:
                visited.add(nb)
                queue.append((nb, [start_pos, nb]))

        while queue:
            current, chain = queue.popleft()
            if len(chain) - 1 > max_depth:
                continue

            if current not in occupied:
                # Свободная клетка найдена — строим список смещений
                result: List[Tuple[int, Tuple[int, int]]] = []
                for i in range(len(chain) - 1):
                    aid = pos_to_idle.get(chain[i])
                    if aid is not None:
                        result.append((aid, chain[i + 1]))
                return result if result else None

            if current not in pos_to_idle:
                continue    # занято UNLOADING/WAITING_SLOT — тупик

            for nb in neighbors_of(current):
                if nb not in visited:
                    visited.add(nb)
                    queue.append((nb, chain + [nb]))

        return None

    # ══════════════════════════════════════════════════════════════════════════
    # УРОВЕНЬ 1 — Timer-based stuck detection
    # ══════════════════════════════════════════════════════════════════════════

    def update_last_move(self, agents: Dict[int, Any], current_tick: int):
        """Вызывать ПОСЛЕ move() в конце тика."""
        from collections import deque
        for aid, ag in agents.items():
            if ag.status in _STATIONARY_STATUSES:
                self.last_pos[aid]    = ag.pos
                self.stuck_since[aid] = current_tick
                self._osc_since.pop(aid, None)
                self._pos_history.pop(aid, None)
                continue

            # Обычный таймер застревания
            if self.last_pos.get(aid) != ag.pos:
                self.stuck_since[aid] = current_tick
                # Агент успешно двинулся — снижаем счётчик повторений осцилляции
                if aid in self._osc_repeat_count:
                    self._osc_repeat_count[aid] = max(0, self._osc_repeat_count[aid] - 1)
                    if self._osc_repeat_count[aid] == 0:
                        self._osc_repeat_count.pop(aid, None)
            elif aid not in self.stuck_since:
                self.stuck_since[aid] = current_tick
            self.last_pos[aid] = ag.pos

            # История позиций для детектирования осцилляции
            hist = self._pos_history.setdefault(aid, [])
            hist.append(ag.pos)
            if len(hist) > self._POS_HISTORY_LEN:
                hist.pop(0)

            # Детектируем осцилляцию: цикл длиной 2 (A B A B A B)
            # или длиной 4 (A B C D A B C D)
            oscillating = False
            if len(hist) >= 4:
                # Цикл-2: pos[i] == pos[i-2] для последних 4 позиций
                if hist[-1] == hist[-3] and hist[-2] == hist[-4]:
                    oscillating = True
            if not oscillating and len(hist) >= self._POS_HISTORY_LEN:
                # Цикл-4: первые 3 == последние 3 в окне 6
                half = self._POS_HISTORY_LEN // 2
                if hist[:half] == hist[half:]:
                    oscillating = True

            if oscillating:
                if aid not in self._osc_since:
                    self._osc_since[aid] = current_tick
            else:
                self._osc_since.pop(aid, None)

    def check_and_resolve(self,
                          agents: Dict[int, Any],
                          reservations: Dict,
                          current_tick: int) -> Dict[int, str]:
        """
        Уровень 1: таймер застревания + детектирование осцилляции.
        Вызывать ДО router.solve().
        Возвращает {agent_id: 'OK' | 'REPLAN' | 'WAIT'}.

        'WAIT' — агент должен стоять на месте этот тик (не планировать путь).
        Используется для прерывания ping-pong осцилляции.
        """
        actions = {aid: "OK" for aid in agents}
        for aid, ag in agents.items():
            if ag.status in _STATIONARY_STATUSES:
                continue

            # Force-wait: агент обязан стоять (после осцилляционного REPLAN)
            if current_tick < self._force_wait_until.get(aid, 0):
                actions[aid] = "WAIT"
                continue

            # Стандартный таймер (агент не двигается)
            stuck_for = current_tick - self.stuck_since.get(aid, current_tick)
            if stuck_for > self.stuck_threshold:
                actions[aid] = "REPLAN"
                logging.debug(f"⚠️ [L1] Агент {aid} застрял {stuck_for} тактов → REPLAN")
                continue

            # Осцилляция (агент движется но не продвигается: A→B→A→B)
            osc_for = current_tick - self._osc_since.get(aid, current_tick)
            if osc_for >= self._OSC_THRESHOLD:
                actions[aid] = "REPLAN"
                # Считаем повторения осцилляции для этого агента
                repeat = self._osc_repeat_count.get(aid, 0) + 1
                self._osc_repeat_count[aid] = repeat
                # Удлиняем wait при повторах: 6, 10, 14, ...
                # Стагерируем по agent_id чтобы группа не перезапускалась одновременно
                wait_base  = self._FORCE_WAIT_TICKS + (repeat - 1) * 4
                wait_jitter = aid % 4        # 0..3 тика смещения по ID
                total_wait  = wait_base + wait_jitter
                self._force_wait_until[aid] = current_tick + total_wait
                # Сбрасываем историю
                self._pos_history.pop(aid, None)
                self._osc_since.pop(aid, None)
                self.stuck_since[aid] = current_tick
                logging.warning(
                    f"⚠️ [L1-OSC] Агент {aid} осциллирует "
                    f"{osc_for} тактов → REPLAN + WAIT {total_wait} тактов "
                    f"(повтор #{repeat})")

        return actions

    # ══════════════════════════════════════════════════════════════════════════
    # УРОВЕНЬ 2 — WFG Cycle Detection + Local CBS Resolution
    # ══════════════════════════════════════════════════════════════════════════

    def _build_wait_for_graph(
            self,
            agents: Dict[int, Any],
            paths:  Dict[int, List[Tuple[int, int]]],
    ) -> Dict[int, Set[int]]:
        """
        Wait-For Graph (WFG): A→B означает «агент A хочет войти в
        клетку, которую занимает B и которую B не планирует покидать».

        Ребро A→B возникает когда:
          • next_pos[A] == cur_pos[B]   (A хочет встать на место B)
          • next_pos[B] == cur_pos[B]   (B остаётся стоять)
        Цикл в WFG ≡ классический circular deadlock.
        """
        # next_pos: первый реальный шаг по пути от роутера
        next_pos: Dict[int, Tuple] = {}
        for aid, ag in agents.items():
            path = paths.get(aid, [])
            next_pos[aid] = path[1] if len(path) >= 2 else ag.pos

        # Обратный индекс: позиция → агент
        pos_to_agent: Dict[Tuple, int] = {ag.pos: aid for aid, ag in agents.items()}

        wfg: Dict[int, Set[int]] = {aid: set() for aid in agents}
        for aid, ag in agents.items():
            if ag.status in _STATIONARY_STATUSES:
                continue
            target = next_pos[aid]
            if target == ag.pos:
                continue  # не движется сам
            blocker_id = pos_to_agent.get(target)
            if blocker_id is None or blocker_id == aid:
                continue
            # B блокирует A если B сам не собирается уйти
            if next_pos.get(blocker_id) == agents[blocker_id].pos:
                wfg[aid].add(blocker_id)

        return wfg

    def _find_wfg_cycles(self, wfg: Dict[int, Set[int]]) -> List[Set[int]]:
        """
        DFS-поиск всех простых циклов в WFG.
        Возвращает список множеств агентов — каждое множество это один цикл.
        """
        visited:   Set[int] = set()
        rec_stack: Set[int] = set()
        cycles:    List[Set[int]] = []

        def dfs(node: int, path: List[int]):
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in wfg.get(node, set()):
                if neighbor not in visited:
                    dfs(neighbor, path)
                elif neighbor in rec_stack:
                    # Найден цикл: восстанавливаем его из стека
                    idx   = path.index(neighbor)
                    cycle = set(path[idx:])
                    # Добавляем только если он не поглощён уже найденным
                    if not any(cycle <= existing for existing in cycles):
                        cycles.append(cycle)

            path.pop()
            rec_stack.discard(node)

        for node in wfg:
            if node not in visited:
                dfs(node, [])

        return cycles

    def resolve_local_deadlocks(
            self,
            agents:       Dict[int, Any],
            paths:        Dict[int, List[Tuple[int, int]]],
            tasks:        List[Dict],
            current_tick: int,
            reservations: Optional[Dict] = None,
    ) -> Dict[int, List[Tuple[int, int]]]:
        """
        Уровень 2: WFG-обнаружение + локальный CBS.

        reservations — основной словарь брони AgentManager (t=0 постоянные блоки
        статичных агентов). Передаётся в локальный CBS чтобы пути не проходили
        сквозь IDLE/WAITING_SLOT агентов.

        Алгоритм (Deadlock-Free Hybrid, arXiv:2511.22685, адаптация):
          1. Строим WFG из намерений агентов (next_pos).
          2. Находим циклы в WFG — это и есть настоящие дедлоки.
          3. Для каждого цикла:
             a. Собираем задачи только вовлечённых агентов.
             b. Строим temp_res из путей ОСТАЛЬНЫХ агентов + постоянных брони
                (они не участвуют в локальной оптимизации — не трогаем).
             c. Запускаем локальный CBSSolver только для группы цикла.
             d. Заменяем пути вовлечённых агентов на найденные CBS-решения.
          4. Возвращаем обновлённый словарь путей.

        Вызывать ПОСЛЕ router.solve() и ДО resolve_movement_conflicts().
        """
        if self._local_cbs is None:
            return paths  # Уровень 2 не настроен — пропускаем

        wfg    = self._build_wait_for_graph(agents, paths)
        cycles = self._find_wfg_cycles(wfg)

        if not cycles:
            return paths

        self._stat_cycles_detected += len(cycles)
        logging.info(f"🔒 [L2] Тик {current_tick}: обнаружено {len(cycles)} "
                     f"WFG-цикл(ов), всего за сессию: {self._stat_cycles_detected}")

        result_paths = dict(paths)           # не мутируем оригинал
        task_map     = {t['agent_id']: t for t in tasks}
        main_res     = reservations or {}

        for cycle in cycles:
            cycle_ids = sorted(cycle)
            logging.info(f"  ↺ WFG-цикл: агенты {cycle_ids}")

            # Задачи только для агентов цикла
            cycle_tasks: List[Dict] = []
            for aid in cycle_ids:
                if aid not in task_map:
                    continue
                t = dict(task_map[aid])  # shallow copy — не мутируем task_map
                t['action'] = 'OK'       # снимаем REPLAN/WAIT флаги для CBS
                cycle_tasks.append(t)

            if not cycle_tasks:
                continue

            # Временные резервации:
            #   1) постоянные блоки t=0 из основного reservations
            #      (статичные агенты: IDLE, WAITING_SLOT, UNLOADING)
            #   2) пути НЕ-вовлечённых в цикл активных агентов
            temp_res: Dict = {}

            # Копируем постоянные блоки (t_start==t_end==0), исключая агентов цикла
            for pos, entries in main_res.items():
                for entry in entries:
                    if entry['agent_id'] in cycle:
                        continue
                    if entry['t_start'] == 0 and entry['t_end'] == 0:
                        temp_res.setdefault(pos, []).append(dict(entry))

            # Пути активных агентов не из цикла
            for aid, path in result_paths.items():
                if aid in cycle:
                    continue
                for step_i, pos in enumerate(path):
                    temp_res.setdefault(pos, []).append({
                        'agent_id': aid,
                        't_start':  step_i,
                        't_end':    step_i,
                    })

            # Локальный CBS для группы цикла (передаём temp_res в solve)
            try:
                local_paths = self._local_cbs.solve(cycle_tasks, temp_res)
                improved    = 0
                for aid, lpath in local_paths.items():
                    if lpath and len(lpath) >= 1:
                        result_paths[aid] = lpath
                        improved += 1
                        logging.debug(f"    ✅ Агент {aid}: путь {len(lpath)} шагов")

                if improved:
                    self._stat_cycles_resolved += 1
                    logging.info(f"  ✅ Цикл разрешён локальным CBS "
                                 f"({improved}/{len(cycle_ids)} агентов перепланированы)")

            except Exception as exc:
                logging.warning(f"  ⚠️  Локальный CBS упал для цикла {cycle_ids}: {exc}")

        return result_paths

    def get_stats(self) -> Dict[str, int]:
        """Возвращает статистику работы уровня 2 за сессию."""
        return {
            'cycles_detected': self._stat_cycles_detected,
            'cycles_resolved': self._stat_cycles_resolved,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # УРОВЕНЬ 3 — Final conflict enforcement (без изменений)
    # ══════════════════════════════════════════════════════════════════════════

    def resolve_movement_conflicts(
            self,
            agents:    Dict[int, Any],
            new_paths: Dict[int, List[Tuple[int, int]]],
    ) -> Set[int]:
        """
        Финальный enforcement ПОСЛЕ router.solve() и resolve_local_deadlocks(),
        ДО move().

        Три независимых прохода (static строится ОДИН РАЗ):
          Pass 1 — Static-блокировка: агент не может войти в клетку
                   неподвижного не-IDLE агента.
          Pass 2 — Vertex: из нескольких движущихся в одну клетку
                   побеждает с наибольшим score. Итерировать до стабилизации.
          Pass 3 — Swap: A→B и B→A одновременно — проигрывает с меньшим score.
                   Итерировать до стабилизации.

        Возвращает Set[agent_id] которым РАЗРЕШЕНО сделать шаг.
        """
        log: List[str] = []

        # Намерения: следующая позиция по новым путям от роутера
        next_pos: Dict[int, Tuple[int, int]] = {}
        for aid, ag in agents.items():
            path = new_paths.get(aid, [])
            next_pos[aid] = path[1] if len(path) >= 2 else ag.pos

        # static_origin: позиция → (aid, status) для агентов, остающихся на месте
        static_origin: Dict[Tuple[int, int], Tuple[int, str]] = {}
        for aid, ag in agents.items():
            if next_pos[aid] == ag.pos:
                static_origin[ag.pos] = (aid, ag.status)

        allowed: Set[int] = set(agents.keys())
        idle_displacements: Dict[int, Tuple[int, int]] = {}  # IDLE-смещения

        # ── Pass 1: Static-блокировка + IDLE-уступание ──────────────────────
        #
        # Два класса статичных агентов:
        #   • IDLE            — мягкий блокер: уступает движущемуся.
        #                       Ищет свободную боковую клетку через _find_idle_aside.
        #                       Приоритет: перпендикулярное направлению мувера.
        #   • UNLOADING /     — жёсткий блокер: заблокирован рабочим процессом,
        #     WAITING_SLOT      уйти не может. Движущийся ждёт.
        #
        # Алгоритм для IDLE-клеток:
        #   1. Собираем всех претендентов на клетку.
        #   2. Выбираем победителя по наибольшему score.
        #   3. IDLE ищет боковую свободную клетку (не позицию победителя — не swap).
        #   4. Если клетка найдена — IDLE уходит туда, победитель проходит.
        #   5. Если клетки нет — IDLE остаётся, победитель ждёт.

        # Шаг A: разделяем на жёсткие блоки и конкуренцию за IDLE-клетки
        to_block: Set[int]             = set()
        idle_targets: Dict[Tuple[int, int], List[int]] = {}  # dest → [mover_ids]

        for aid in list(allowed):
            dest = next_pos[aid]
            if dest == agents[aid].pos:
                continue
            entry = static_origin.get(dest)
            if entry is None:
                continue
            occ_aid, occ_status = entry
            if occ_status == "IDLE":
                idle_targets.setdefault(dest, []).append(aid)
            else:
                # UNLOADING / WAITING_SLOT — жёсткий блок
                to_block.add(aid)
                log.append(
                    f"🚫 Агент {aid}→{dest}: клетка занята "
                    f"агентом {occ_aid} ({occ_status})")

        # Шаг B: жёсткие блоки
        for aid in to_block:
            allowed.discard(aid)
            next_pos[aid] = agents[aid].pos

        # Шаг C: IDLE-уступание — победитель проходит, IDLE смещается
        for dest, mover_ids in idle_targets.items():
            idle_aid, _ = static_origin[dest]
            # Из претендентов берём только тех, кого не заблокировали ранее
            candidates = [a for a in mover_ids if a in allowed]
            if not candidates:
                continue
            # Победитель — наибольший score
            winner = max(candidates, key=lambda a: agents[a].score)
            losers = [a for a in candidates if a != winner]
            # Проигравшие ждут следующего тика
            for loser in losers:
                allowed.discard(loser)
                next_pos[loser] = agents[loser].pos

        # Шаг C: IDLE-уступание через цепной сдвиг (_find_chain_push)
        # occupied — текущие позиции агентов; обновляется по мере обработки цепей
        occupied: Set[Tuple[int, int]] = {ag.pos for ag in agents.values()}
        # pos_to_idle: только IDLE-агенты (их можно «толкать» по цепи)
        pos_to_idle: Dict[Tuple[int, int], int] = {
            ag.pos: aid
            for aid, ag in agents.items()
            if ag.status == "IDLE"
        }

        for dest, mover_ids in idle_targets.items():
            idle_aid, _ = static_origin[dest]
            candidates  = [a for a in mover_ids if a in allowed]
            if not candidates:
                continue
            winner = max(candidates, key=lambda a: agents[a].score)
            for loser in candidates:
                if loser != winner:
                    allowed.discard(loser)
                    next_pos[loser] = agents[loser].pos

            # BFS-цепной сдвиг: ищем путь через IDLE-соседей до свободной клетки
            chain = self._find_chain_push(
                start_pos   = agents[idle_aid].pos,
                winner_pos  = agents[winner].pos,
                winner_dest = dest,
                occupied    = occupied,
                pos_to_idle = pos_to_idle,
            )

            if chain:
                # Применяем все смещения цепи
                for aid_chain, new_pos in chain:
                    old_pos = agents[aid_chain].pos
                    next_pos[aid_chain]           = new_pos
                    idle_displacements[aid_chain] = new_pos
                    occupied.add(new_pos)
                    occupied.discard(old_pos)
                    # Обновляем pos_to_idle чтобы следующие цепи видели актуальное состояние
                    pos_to_idle.pop(old_pos, None)
                    pos_to_idle[new_pos] = aid_chain
                chain_str = "→".join(f"{p}" for _, p in chain)
                log.append(
                    f"🟡 IDLE-цепь {len(chain)}: {chain_str} "
                    f"(открывает путь агенту {winner}→{dest})")
            else:
                # Цепь не найдена — IDLE заперт, мувер ждёт
                allowed.discard(winner)
                next_pos[winner] = agents[winner].pos
                log.append(
                    f"⛔ IDLE {idle_aid} заперт у {dest} (цепь не найдена), "
                    f"агент {winner} ждёт")

        # ── Pass 2: Vertex-конфликты ─────────────────────────────────────────
        changed = True
        while changed:
            changed = False
            dest_map: Dict[Tuple[int, int], List[int]] = {}
            for aid in allowed:
                if next_pos[aid] != agents[aid].pos:
                    dest_map.setdefault(next_pos[aid], []).append(aid)

            for dest, movers in dest_map.items():
                if len(movers) < 2:
                    continue
                movers.sort(key=lambda x: agents[x].score, reverse=True)
                for loser in movers[1:]:
                    if loser in allowed:
                        allowed.discard(loser)
                        next_pos[loser] = agents[loser].pos
                        changed = True
                        log.append(
                            f"🔄 Vertex {dest}: {movers[0]} "
                            f"(score={agents[movers[0]].score:.2f}) проходит, "
                            f"{loser} ждёт")

        # ── Pass 3: Swap-конфликты ───────────────────────────────────────────
        # Блокируем ОБОИХ участников swap.
        # Победитель тоже не двигается: его цель (клетка проигравшего) остаётся
        # занята, т.к. проигравший стоит на месте → физическая коллизия.
        # На следующем тике роутер строит объездной маршрут.
        changed = True
        while changed:
            changed = False
            movers = [(aid, agents[aid].pos, next_pos[aid])
                      for aid in allowed if next_pos[aid] != agents[aid].pos]
            for i, (a1, c1, n1) in enumerate(movers):
                for a2, c2, n2 in movers[i + 1:]:
                    if n1 == c2 and n2 == c1:
                        loser  = a2 if agents[a1].score >= agents[a2].score else a1
                        winner = a1 if loser == a2 else a2
                        for blocked in (a1, a2):
                            if blocked in allowed:
                                allowed.discard(blocked)
                                next_pos[blocked] = agents[blocked].pos
                                changed = True
                        log.append(f"🔁 Swap {a1}↔{a2}: оба ждут "
                                   f"({winner} приоритет, переплан следующий тик)")

        if log:
            logging.debug("📋 [L3] Конфликты: " + " | ".join(log))

        return allowed, idle_displacements