# agent_logic.py
from typing import Dict, List, Tuple, Optional, Any

# ================= КОНСТАНТЫ ИЗ ТЗ =================
STATUSES_WEIGHTS = {
    "Едет на зарядку": 6,
    "Возвращает груз": 5,
    "Едет к фасовщику": 4,
    "Едет к грузу": 3,
    "Заряжается": 2,
    "Нет задания": 1,
    "Критический разряд": 0
}
BATTERY_MAX = 1000
BATTERY_LOW_THRESHOLD = 150  # 15% от 1000
CHARGE_COST_PER_MOVE = 1


class Agent:
    """
    Логическое ядро интеллектуального агента.
    Реализует цикл Sense-Plan-Act, валидацию по матрице смежности,
    учёт заряда, статусов и приоритетов. Не содержит кода отрисовки.
    """

    def __init__(self, agent_id: int, start_pos: Tuple[int, int], charger_pos: Tuple[int, int],
                 battery: int = BATTERY_MAX, has_cargo: bool = False):
        self.id = agent_id
        self.pos = start_pos  # (row, col) в 1-based индексации
        self.charger_pos = charger_pos
        self.battery = battery
        self.has_cargo = has_cargo
        self.status = "Нет задания"
        self.available_moves: List[Tuple[int, int]] = []
        self._update_status()

    def _update_status(self) -> None:
        """Автоматический пересчёт статуса согласно правилам ТЗ."""
        if self.battery <= 0:
            self.status = "Критический разряд"
        elif self.pos == self.charger_pos and not self.has_cargo:
            self.status = "Заряжается"
        elif self.battery <= BATTERY_LOW_THRESHOLD and not self.has_cargo:
            self.status = "Едет на зарядку"
        elif self.has_cargo:
            self.status = "Едет к грузу"
        else:
            self.status = "Нет задания"

    # ================= SENSE =================
    def sense(self, adj_loaded: Dict, adj_unloaded: Dict, bookings: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Восприятие: запрашивает у среды актуальные данные.
        Формирует список допустимых ходов на основе матрицы смежности.
        """
        adj = adj_loaded if self.has_cargo else adj_unloaded
        self.available_moves = adj.get(self.pos, [])
        return {
            "pos": self.pos,
            "battery": self.battery,
            "has_cargo": self.has_cargo,
            "status": self.status,
            "priority_weight": STATUSES_WEIGHTS.get(self.status, 1),
            "available_moves": self.available_moves,
            "bookings": bookings or {}
        }

    # ================= PLAN =================
    def plan(self, requested_move: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
        """
        Планирование: в ручном режиме валидирует запрошенный ход.
        Для автоматических алгоритмов здесь будет вызов A*/Dijkstra.
        """
        if requested_move and requested_move in self.available_moves:
            return requested_move
        return None

    # ================= ACT =================
    def act(self, move: Optional[Tuple[int, int]]) -> bool:
        """
        Действие: выполняет перемещение, списывает заряд, обновляет статус.
        Возвращает True, если движение выполнено успешно.
        """
        if move is None:
            return False
        self.pos = move
        self.battery = max(0, self.battery - CHARGE_COST_PER_MOVE)
        self._update_status()
        return True

    def process_input(self, action: str) -> Optional[Tuple[int, int]]:
        """
        Преобразует абстрактный ввод от viewer в координаты цели.
        action: 'UP', 'DOWN', 'LEFT', 'RIGHT', 'TOGGLE_CARGO', 'NONE'
        """
        if action == "TOGGLE_CARGO":
            self.has_cargo = not self.has_cargo
            self._update_status()
            return None

        dr, dc = {
            "UP": (-1, 0), "DOWN": (1, 0),
            "LEFT": (0, -1), "RIGHT": (0, 1)
        }.get(action, (0, 0))

        if dr == 0 and dc == 0:
            return None
        return (self.pos[0] + dr, self.pos[1] + dc)

    def get_render_state(self) -> Dict[str, Any]:
        """Возвращает словарь, готовый для передачи в map_viewer.py"""
        return {
            "id": self.id,
            "pos": self.pos,
            "has_cargo": self.has_cargo,
            "battery": self.battery,
            "battery_percent": round(self.battery / BATTERY_MAX * 100, 1),
            "status": self.status,
            "priority": STATUSES_WEIGHTS.get(self.status, 1),
            "color_rgb": (0, 176, 80) if self.battery > BATTERY_LOW_THRESHOLD else (255, 50, 50),
            "available_moves": self.available_moves
        }


# ================= ПОДПРОГРАММА-ФАБРИКА =================
def create_agent_at_first_charger(
        cell_types: Dict[str, List[Tuple[int, int]]],
        adj_loaded: Dict,
        adj_unloaded: Dict,
        agent_id: int = 1,
        initial_battery: int = BATTERY_MAX,
        has_cargo: bool = False
) -> Agent:
    """
    Создаёт агента строго на первой зарядной станции карты.
    Возвращает готовый к работе экземпляр класса Agent.
    """
    if not cell_types.get('C'):
        raise ValueError("❌ На карте отсутствует зарядная станция (тип 'C').")

    charger_pos = cell_types['C'][0]
    return Agent(
        agent_id=agent_id,
        start_pos=charger_pos,
        charger_pos=charger_pos,
        battery=initial_battery,
        has_cargo=has_cargo
    )