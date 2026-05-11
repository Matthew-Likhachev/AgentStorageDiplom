import time
from typing import Dict, List, Any

class MetricsCollector:
    def __init__(self):
        self._start_time_ms = 0.0
        self._start_tick = 0
        self._end_tick = 0

        self._orders = {}       # {id: {start, end, shelves}}
        self._suborders = {}    # {id: {start, end, shelves}}

        self._wait_events = 0
        self._packer_wait_ticks = 0
        self._forced_wait_events = 0
        self._deadlock_count = 0

    def on_simulation_start(self, tick):
        self._start_time_ms = time.time() * 1000
        self._start_tick = tick

    def on_simulation_end(self, tick):
        self._end_tick = tick

    def register_order(self, oid, tick, shelves):
        self._orders[oid] = {'start': tick, 'end': None, 'shelves': shelves}

    def complete_order(self, oid, tick):
        if oid in self._orders:
            self._orders[oid]['end'] = tick

    def register_suborder(self, sid, oid, tick, shelves):
        self._suborders[sid] = {'start': tick, 'end': None, 'shelves': shelves}

    def complete_suborder(self, sid, tick):
        if sid in self._suborders:
            self._suborders[sid]['end'] = tick

    def on_agent_wait(self, in_packer_slot):
        if in_packer_slot:
            self._packer_wait_ticks += 1
        else:
            self._wait_events += 1

    def on_forced_wait(self):
        self._forced_wait_events += 1

    def on_deadlock_detected(self):
        self._deadlock_count += 1

    def flush_tick(self):
        pass

    def print_summary(self):
        ticks = self._end_tick - self._start_tick
        ms = time.time() * 1000 - self._start_time_ms

        o_vals = [v for v in self._orders.values() if v['end'] is not None]
        avg_o = sum(v['end']-v['start'] for v in o_vals) / len(o_vals) if o_vals else 0
        avg_o_s = sum(v['shelves'] for v in o_vals) / len(o_vals) if o_vals else 0

        s_vals = [v for v in self._suborders.values() if v['end'] is not None]
        avg_s = sum(v['end']-v['start'] for v in s_vals) / len(s_vals) if s_vals else 0
        avg_s_s = sum(v['shelves'] for v in s_vals) / len(s_vals) if s_vals else 0

        print("\n" + "═"*60)
        print("📊 СВОДКА МЕТРИК")
        print("═"*60)
        print(f"🕐 Сценарий: {ticks} тактов / {ms:.0f} мс")
        print(f"📦 Заказы: среднее время {avg_o:.1f} тиков, стеллажей {avg_o_s:.1f}")
        print(f"📋 Подзаказы: среднее время {avg_s:.1f} тиков, стеллажей {avg_s_s:.1f}")
        print(f"⏳ Ожидания: проезд={self._wait_events}, очередь={self._packer_wait_ticks}, форс={self._forced_wait_events}")
        print(f"🔒 Дедлоки: {self._deadlock_count}")
        print("═"*60 + "\n")