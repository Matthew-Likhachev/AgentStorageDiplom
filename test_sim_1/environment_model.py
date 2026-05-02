from typing import List, Dict, Tuple
from abc import ABC, abstractmethod

class IObserver(ABC):
    @abstractmethod
    def on_state_change(self, state: dict): pass

class Environment:
    def __init__(self, grid_data: dict, size: Tuple[int, int]):
        self.size = size
        self.grid = self._build_grid(grid_data)
        self.reservations: Dict[Tuple[int, int], List[Dict]] = {} # (x,y) -> [{agent_id, t_start, t_end}]
        self._observers: List[IObserver] = []
        self.current_tick = 0

    def subscribe(self, observer: IObserver):
        self._observers.append(observer)

    def get_cell_state(self, x: int, y: int, t: int) -> bool:
        # Стратегия: Temporal Collision Checking
        # Возвращает True, если клетка свободна в такт t
        return not self._is_reserved(x, y, t)

    def reserve_path(self, agent_id: int, path: List[Tuple[int, int]], horizon: int):
        # Стратегия: Sliding Window Reservation
        for i, (x, y) in enumerate(path[:horizon]):
            t = self.current_tick + i
            self.reservations.setdefault((x, y), []).append({
                "agent_id": agent_id, "t_start": t, "t_end": t
            })

    def step(self):
        self.current_tick += 1
        self._cleanup_old_reservations()
        state_snapshot = self._get_snapshot()
        for obs in self._observers:
            obs.on_state_change(state_snapshot)