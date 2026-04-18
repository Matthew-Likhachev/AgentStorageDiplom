# !/usr/bin/env python3
"""
Starter - Точка входа для симулятора роботизированного склада
Запускает загрузчик, валидатор и визуализацию в Pygame
ПРОГРАММА: Визуализация карты
"""

# ✅ ИМПОРТ ДЛЯ ОТЛОЖЕННЫХ АННОТАЦИЙ
from __future__ import annotations

import sys
import json
import pygame
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Добавляем путь к модулям
sys.path.insert(0, str(Path(__file__).parent))

from excelloader.excelloader import ExcelLoader
from excelloader.mapvalidator import MapValidator


# ==============================================================================
# КОНФИГУРАЦИЯ
# ==============================================================================
class Config:
    SCREEN_WIDTH = 1400
    SCREEN_HEIGHT = 900
    FPS = 60
    CELL_TEXT_SIZE = 14
    CELL_ID_SIZE = 11
    AGENT_SIZE_RATIO = 0.65

    COLORS = {
        '000000': (0, 0, 0), 'FF0000': (255, 0, 0), 'FFFFFF': (255, 255, 255),
        'FF9900': (255, 153, 0), '980000': (152, 0, 0), '0000FF': (0, 0, 255),
        '7030A0': (112, 48, 160), 'FFFF00': (255, 255, 0), '00B050': (0, 176, 80),
    }

    TYPE_NAMES = {
        'E': 'Граница', 'W': 'Запрет', 'F': 'Пол', 'S': 'Стеллаж',
        'P': 'Фасовщик', 'C': 'Зарядка', 'Z': 'Зона заказа', 'B': 'Буфер', 'A': 'Агент'
    }

    STATUS_PRIORITY = {
        'CHARGING_MOVE': 6, 'RETURNING': 5, 'TO_PACKER': 4,
        'TO_SHELF': 3, 'CHARGING': 2, 'IDLE': 1
    }


# ==============================================================================
# КЛАСС АГЕНТА (логические параметры)
# ==============================================================================
class Agent:
    """Логическое представление агента"""

    def __init__(self, agent_id: int, charger_id: int, start_pos: Tuple[int, int]):
        self.id = agent_id
        self.charger_id = charger_id
        self.position = start_pos
        self.charge = 1000
        self.max_charge = 1000
        self.cargo = False
        self.cargo_shelf_id = None
        self.status = 'IDLE'
        self.priority = Config.STATUS_PRIORITY[self.status]
        self.path: List[Tuple[int, int]] = []
        self.reservations: Dict[int, Tuple[int, int]] = {}
        self.current_order_id = None
        self.penalty = 0
        self.last_action_tick = 0

    # ... остальные методы Agent без изменений ...

    def get_score(self, order_priority: int, distance: int) -> int:
        """Расчет приоритета для разрешения конфликтов"""
        return (self.priority * 10000) + (order_priority * 10000) + (10000 - distance) + (self.id + 1)

    def needs_charging(self) -> bool:
        """Проверяет необходимость зарядки (<15%)"""
        return self.charge < self.max_charge * 0.15

    def can_complete_task(self, cells_to_cargo: int, cells_packer_round: int, cells_to_charger: int) -> bool:
        """Проверяет, хватит ли заряда на задачу"""
        total_needed = cells_to_cargo + cells_packer_round * 2 + cells_to_charger
        return (self.charge - total_needed) > self.max_charge * 0.15

    def to_dict(self) -> Dict:
        """Экспорт параметров агента"""
        return {
            'id': self.id,
            'charger_id': self.charger_id,
            'position': self.position,
            'charge': self.charge,
            'max_charge': self.max_charge,
            'cargo': self.cargo,
            'cargo_shelf_id': self.cargo_shelf_id,
            'status': self.status,
            'priority': self.priority,
            'path': self.path,
            'reservations': {str(k): v for k, v in self.reservations.items()},
            'current_order_id': self.current_order_id,
            'penalty': self.penalty,
            'last_action_tick': self.last_action_tick
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'Agent':
        """Импорт параметров агента"""
        agent = cls(data['id'], data['charger_id'], tuple(data['position']))
        agent.charge = data.get('charge', 1000)
        agent.max_charge = data.get('max_charge', 1000)
        agent.cargo = data.get('cargo', False)
        agent.cargo_shelf_id = data.get('cargo_shelf_id')
        agent.status = data.get('status', 'IDLE')
        agent.priority = data.get('priority', 1)
        agent.path = data.get('path', [])
        agent.reservations = {int(k): tuple(v) for k, v in data.get('reservations', {}).items()}
        agent.current_order_id = data.get('current_order_id')
        agent.penalty = data.get('penalty', 0)
        agent.last_action_tick = data.get('last_action_tick', 0)
        return agent


# ==============================================================================
# ВИЗУАЛИЗАТОР КАРТЫ С ПРОКРУТКОЙ
# ==============================================================================
class MapVisualizer:
    """Визуализация карты с поддержкой прокрутки"""

    def __init__(self, map_data: Dict, validation_report: Dict = None, agents: List[Agent] = None):
        pygame.init()
        pygame.display.set_caption("🏭 Симулятор склада | Визуализация")

        self.map_data = map_data
        self.validation_report = validation_report or {}
        self.agents = {a.id: a for a in agents} if agents else {}

        self.map_width = map_data.get('width', 0)
        self.map_height = map_data.get('height', 0)
        self.cells = map_data.get('cells', [])

        # Настройки отображения
        self.base_cell_size = 40
        self.offset_x = 20
        self.offset_y = 20

        # ✅ Панель информации справа (фиксированная ширина) — ДОБАВЛЕНО
        self.info_panel_width = 320
        self.info_panel_x = Config.SCREEN_WIDTH - self.info_panel_width

        # Камера для прокрутки
        self.camera_x = 0
        self.camera_y = 0
        self.dragging = False
        self.last_mouse_pos = (0, 0)

        # Экран
        self.screen = pygame.display.set_mode((Config.SCREEN_WIDTH, Config.SCREEN_HEIGHT))
        self.clock = pygame.time.Clock()

        # Шрифты
        self.font_main = pygame.font.SysFont('Arial', Config.CELL_TEXT_SIZE, bold=True)
        self.font_small = pygame.font.SysFont('Arial', Config.CELL_ID_SIZE)
        self.font_ui = pygame.font.SysFont('Arial', 16)
        self.font_title = pygame.font.SysFont('Arial', 20, bold=True)

        # Состояние
        self.running = True
        self.selected_cell = None
        self.show_grid = True
        self.show_info = True
        self.show_agents = True
        self.tick = 0



    def _get_visible_area(self) -> Tuple[int, int, int, int]:
        """Возвращает видимую область карты с учетом камеры и панели справа"""
        # Доступная ширина для карты (без панели)
        available_width = self.info_panel_x - self.offset_x - 10
        cols_visible = available_width // self.base_cell_size + 2
        rows_visible = Config.SCREEN_HEIGHT // self.base_cell_size + 2

        start_col = max(0, self.camera_x)
        start_row = max(0, self.camera_y)
        end_col = min(self.map_width, start_col + cols_visible)
        end_row = min(self.map_height, start_row + rows_visible)

        return start_row, end_row, start_col, end_col

    def world_to_screen(self, row: int, col: int) -> Tuple[int, int]:
        """Конвертирует координаты мира в экранные"""
        x = self.offset_x + (col - self.camera_x) * self.base_cell_size
        y = self.offset_y + (row - self.camera_y) * self.base_cell_size
        return x, y

    def screen_to_world(self, screen_x: int, screen_y: int) -> Optional[Tuple[int, int]]:
        """Конвертирует экранные координаты в мировые (игнорирует панель справа)"""
        # ✅ Если клик по панели информации — не возвращаем координаты
        if screen_x >= self.info_panel_x:
            return None

        col = self.camera_x + (screen_x - self.offset_x) // self.base_cell_size
        row = self.camera_y + (screen_y - self.offset_y) // self.base_cell_size

        if 0 <= row < self.map_height and 0 <= col < self.map_width:
            return (row, col)
        return None

    def hex_to_rgb(self, hex_color: str) -> Tuple[int, int, int]:
        if hex_color in Config.COLORS:
            return Config.COLORS[hex_color]
        if hex_color is None:
            return (255, 255, 255)
        try:
            h = hex_color.lstrip('#')
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except:
            return (255, 255, 255)

    def _parse_cell(self, cell: Dict) -> Tuple[Optional[Dict], Optional[str]]:
        text = cell.get('text')
        if text is None or not isinstance(text, str):
            return {'type': '?', 'logic': {}}, None
        parts = text.split('|')
        if len(parts) != 4:
            return {'type': 'ERR', 'logic': {}}, None
        cell_type, _, _, logic_str = parts
        try:
            logic = json.loads(logic_str) if logic_str and logic_str != '{}' else {}
        except:
            logic = {}
        return {'type': cell_type, 'logic': logic}, None

    def _get_text_color(self, bg_color: Tuple[int, int, int]) -> Tuple[int, int, int]:
        brightness = (bg_color[0] * 299 + bg_color[1] * 587 + bg_color[2] * 114) / 1000
        return (0, 0, 0) if brightness > 128 else (255, 255, 255)

    def draw_grid(self):
        """Рисует видимую часть карты"""
        start_row, end_row, start_col, end_col = self._get_visible_area()

        for row_idx in range(start_row, end_row):
            for col_idx in range(start_col, end_col):
                cell = self.cells[row_idx][col_idx]
                color_hex = cell.get('color')
                rgb = self.hex_to_rgb(color_hex)

                x, y = self.world_to_screen(row_idx, col_idx)
                rect = pygame.Rect(x, y, self.base_cell_size, self.base_cell_size)

                # Цвет клетки
                pygame.draw.rect(self.screen, rgb, rect)

                # Сетка
                if self.show_grid:
                    pygame.draw.rect(self.screen, (180, 180, 180), rect, 1)

                # Выделение
                if self.selected_cell == (row_idx, col_idx):
                    pygame.draw.rect(self.screen, (0, 255, 0), rect, 3)

                # Тип клетки
                parsed, _ = self._parse_cell(cell)
                type_char = parsed.get('type', '?') if parsed else '?'
                text_surf = self.font_main.render(type_char, True, self._get_text_color(rgb))
                text_rect = text_surf.get_rect(center=rect.center)
                self.screen.blit(text_surf, text_rect)

                # ID из логики
                logic = parsed.get('logic', {}) if parsed else {}
                info_text = ""
                if 'packer' in logic:
                    info_text = f"P{logic['packer']}"
                elif 'shelf' in logic:
                    info_text = f"S{logic['shelf']}"
                elif 'charger' in logic:
                    info_text = f"C{logic['charger']}"
                elif 'slot' in logic:
                    info_text = f"#{logic['slot']}"

                if info_text and self.base_cell_size >= 30:
                    id_surf = self.font_small.render(info_text, True, (255, 255, 255))
                    id_shadow = self.font_small.render(info_text, True, (0, 0, 0))
                    self.screen.blit(id_shadow, (x + 3, y + 3))
                    self.screen.blit(id_surf, (x + 2, y + 2))

    def draw_agents(self):
        """Рисует агентов (меньше клетки)"""
        if not self.show_agents:
            return

        agent_size = int(self.base_cell_size * Config.AGENT_SIZE_RATIO)
        offset = (self.base_cell_size - agent_size) // 2

        for agent in self.agents.values():
            row, col = agent.position
            x, y = self.world_to_screen(row, col)

            # Рисуем агента как меньший квадрат поверх клетки
            agent_rect = pygame.Rect(x + offset, y + offset, agent_size, agent_size)

            # Цвет зависит от статуса
            if agent.status == 'CHARGING_MOVE':
                color = (0, 100, 255)
            elif agent.cargo:
                color = (200, 0, 0)
            elif agent.status == 'CHARGING':
                color = (0, 200, 0)
            else:
                color = Config.COLORS['00B050']  # Зеленый

            pygame.draw.rect(self.screen, color, agent_rect)
            pygame.draw.rect(self.screen, (255, 255, 255), agent_rect, 1)  # Белая рамка

            # Номер агента
            if agent_size >= 20:
                id_text = self.font_small.render(str(agent.id), True, (255, 255, 255))
                id_shadow = self.font_small.render(str(agent.id), True, (0, 0, 0))
                self.screen.blit(id_shadow, (agent_rect.x + 3, agent_rect.y + 2))
                self.screen.blit(id_text, (agent_rect.x + 2, agent_rect.y + 1))

    def draw_minimap(self):
        """Рисует мини-карту в левом нижнем углу области карты"""
        minimap_size = 120
        minimap_x = self.offset_x + 10
        minimap_y = Config.SCREEN_HEIGHT - minimap_size - 50  # С запасом под header

        # Фон мини-карты
        pygame.draw.rect(self.screen, (40, 50, 70),
                         (minimap_x, minimap_y, minimap_size, minimap_size), border_radius=4)
        pygame.draw.rect(self.screen, (200, 200, 200),
                         (minimap_x, minimap_y, minimap_size, minimap_size), 2)

        # Масштаб
        scale = minimap_size / max(self.map_width, self.map_height)

        # Рисуем клетки (упрощенно)
        for row in range(0, self.map_height, 3):
            for col in range(0, self.map_width, 3):
                cell = self.cells[row][col]
                color = self.hex_to_rgb(cell.get('color', 'FFFFFF'))
                mx = minimap_x + int(col * scale)
                my = minimap_y + int(row * scale)
                pygame.draw.rect(self.screen, color,
                                 (mx, my, max(1, int(scale * 2)), max(1, int(scale * 2))))

        # Рамка видимой области
        available_width = self.info_panel_x - self.offset_x - 10
        view_w = int((available_width // self.base_cell_size) * scale)
        view_h = int((Config.SCREEN_HEIGHT // self.base_cell_size) * scale)
        view_x = minimap_x + int(self.camera_x * scale)
        view_y = minimap_y + int(self.camera_y * scale)
        pygame.draw.rect(self.screen, (0, 255, 100),
                         (view_x, view_y, view_w, view_h), 2)

        # Подпись
        label = self.font_small.render("🗺️  Обзор", True, (200, 220, 255))
        self.screen.blit(label, (minimap_x + 5, minimap_y + 3))

    def draw_info_panel(self):
        """Рисует панель информации как отдельный белый блок справа"""
        if not self.show_info:
            return

        x = self.info_panel_x + 10  # Отступ внутри панели
        y = 15
        width = self.info_panel_width - 30  # Полезная ширина

        # ✅ Белый фон панели
        pygame.draw.rect(self.screen, (255, 255, 255),
                         (self.info_panel_x, 0, self.info_panel_width, Config.SCREEN_HEIGHT))

        # ✅ Тень/разделитель слева от панели
        pygame.draw.line(self.screen, (150, 150, 150),
                         (self.info_panel_x, 0), (self.info_panel_x, Config.SCREEN_HEIGHT), 3)

        # Заголовок
        title = self.font_title.render("📊 ИНФОРМАЦИЯ", True, (30, 60, 120))
        self.screen.blit(title, (x, y))
        y += 40

        # Разделитель
        pygame.draw.line(self.screen, (220, 220, 220), (x - 10, y), (x + width, y), 2)
        y += 15

        # Основная статистика
        stats = [
            ("🗺️  Размер карты", f"{self.map_width} × {self.map_height}"),
            ("📦 Всего клеток", str(self.map_width * self.map_height)),
            ("🤖 Агентов", str(len(self.agents))),
            ("🎯 Текущий tick", str(self.tick)),
            ("👁️  Камера", f"({self.camera_x}, {self.camera_y})"),
        ]

        for label, value in stats:
            self.screen.blit(self.font_ui.render(label, True, (60, 80, 110)), (x, y))
            self.screen.blit(self.font_ui.render(value, True, (30, 50, 90)), (x + 170, y))
            y += 24

        y += 10
        pygame.draw.line(self.screen, (220, 220, 220), (x - 10, y), (x + width, y), 2)
        y += 15

        # Статус валидации
        if self.validation_report:
            self.screen.blit(self.font_ui.render("✅ Валидация:", True, (60, 80, 110)), (x, y))
            y += 22

            is_valid = self.validation_report.get('is_valid', True)
            status_color = (0, 140, 0) if is_valid else (180, 50, 50)
            status_text = "ВАЛИДНО ✓" if is_valid else "ЕСТЬ ОШИБКИ ⚠"

            self.screen.blit(self.font_ui.render(status_text, True, status_color), (x + 5, y))
            y += 22

            errors = len(self.validation_report.get('errors', []))
            warnings = len(self.validation_report.get('warnings', []))
            fixed = self.validation_report.get('fixed_count', 0)

            self.screen.blit(self.font_small.render(f"• Ошибок: {errors}", True, (140, 40, 40)), (x + 5, y));
            y += 18
            self.screen.blit(self.font_small.render(f"• Предупреждений: {warnings}", True, (160, 120, 30)), (x + 5, y));
            y += 18
            self.screen.blit(self.font_small.render(f"• Исправлено: {fixed}", True, (40, 140, 40)), (x + 5, y));
            y += 20

        y += 10
        pygame.draw.line(self.screen, (220, 220, 220), (x - 10, y), (x + width, y), 2)
        y += 15

        # Выбранная клетка
        self.screen.blit(self.font_ui.render("🔍 Выбранная клетка:", True, (60, 80, 110)), (x, y))
        y += 22

        if self.selected_cell:
            row, col = self.selected_cell
            cell = self.cells[row][col]
            parsed, _ = self._parse_cell(cell)

            # Фон для выбранной клетки
            pygame.draw.rect(self.screen, (240, 248, 255), (x - 5, y - 5, width + 15, 95), border_radius=4)
            pygame.draw.rect(self.screen, (180, 200, 230), (x - 5, y - 5, width + 15, 95), 1)

            self.screen.blit(self.font_ui.render(f"Координаты: ({row}, {col})", True, (40, 60, 100)), (x + 5, y))
            y += 20

            if parsed:
                cell_type = parsed.get('type', '?')
                type_name = Config.TYPE_NAMES.get(cell_type, cell_type)
                self.screen.blit(self.font_ui.render(f"Тип: {cell_type} ({type_name})", True, (40, 60, 100)),
                                 (x + 5, y))
                y += 20

                # Логика
                logic = parsed.get('logic', {})
                if logic:
                    self.screen.blit(self.font_small.render("Параметры:", True, (80, 100, 130)), (x + 5, y))
                    y += 18
                    for key, value in list(logic.items())[:4]:
                        kv = self.font_small.render(f"  • {key}: {value}", True, (100, 120, 150))
                        self.screen.blit(kv, (x + 10, y))
                        y += 16
        else:
            self.screen.blit(self.font_small.render("  (кликните ЛКМ по клетке)", True, (140, 140, 160)), (x + 5, y))
            y += 20

        y += 15
        pygame.draw.line(self.screen, (220, 220, 220), (x - 10, y), (x + width, y), 2)
        y += 15

        # Управление
        self.screen.blit(self.font_ui.render("🎮 Управление:", True, (60, 80, 110)), (x, y))
        y += 22

        controls = [
            "• WASD / Стрелки — прокрутка",
            "• Shift + ЛКМ — перетащить карту",
            "• ЛКМ — выбрать клетку",
            "• ПКМ — снять выделение",
            "• G — сетка вкл/выкл",
            "• I — панель вкл/выкл",
            "• V — агенты вкл/выкл",  # ✅ ИЗМЕНЕНО: A → V
            "• ESC — выход",
        ]

        for ctrl in controls:
            self.screen.blit(self.font_small.render(ctrl, True, (100, 110, 130)), (x + 5, y))
            y += 17

        # Футер панели
        footer_y = Config.SCREEN_HEIGHT - 35
        pygame.draw.line(self.screen, (220, 220, 220), (x - 10, footer_y), (x + width, footer_y), 2)
        footer_text = self.font_small.render("v1.0 | Дипломная работа", True, (150, 150, 170))
        self.screen.blit(footer_text, (x, footer_y + 8))

    def draw_header(self):
        y = Config.SCREEN_HEIGHT - 35
        pygame.draw.line(self.screen, (200, 200, 200), (0, y), (Config.SCREEN_WIDTH, y), 2)
        text = f"Tick: {self.tick} | FPS: {int(self.clock.get_fps())} | Агентов: {len(self.agents)}"
        self.screen.blit(self.font_ui.render(text, True, (100, 100, 100)), (20, y + 8))

    def _clamp_camera(self):
        """Ограничивает камеру в пределах карты"""
        max_x = max(0, self.map_width - (Config.SCREEN_WIDTH - self.offset_x) // self.base_cell_size)
        max_y = max(0, self.map_height - (Config.SCREEN_HEIGHT - self.offset_y) // self.base_cell_size)
        self.camera_x = max(0, min(max_x, self.camera_x))
        self.camera_y = max(0, min(max_y, self.camera_y))

    def handle_events(self):
        """Обрабатывает события Pygame"""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.key == pygame.K_g:
                    self.show_grid = not self.show_grid
                elif event.key == pygame.K_i:
                    self.show_info = not self.show_info
                # ✅ ИЗМЕНЕНО: A → V для переключения агентов
                elif event.key == pygame.K_v:
                    self.show_agents = not self.show_agents
                elif event.key == pygame.K_r:
                    return 'reload'
                elif event.key == pygame.K_SPACE:
                    self.tick += 1
                # Прокрутка клавишами (WASD теперь работает корректно)
                elif event.key in (pygame.K_UP, pygame.K_w):
                    self.camera_y = max(0, self.camera_y - 1)
                elif event.key in (pygame.K_DOWN, pygame.K_s):
                    self.camera_y = min(self.map_height - 10, self.camera_y + 1)
                elif event.key in (pygame.K_LEFT, pygame.K_a):
                    self.camera_x = max(0, self.camera_x - 1)
                elif event.key in (pygame.K_RIGHT, pygame.K_d):
                    self.camera_x = min(self.map_width - 15, self.camera_x + 1)

            elif event.type == pygame.MOUSEBUTTONDOWN:
                # ✅ Используем pygame.key.get_mods() для модификаторов
                mods = pygame.key.get_mods()

                # ✅ УБРАНЫ ПРОВЕРКИ ОВЕРЛЕЯ — теперь панель справа фиксированная

                if event.button == 1:  # ЛКМ - выбор или перетаскивание
                    world_pos = self.screen_to_world(*event.pos)
                    if mods & pygame.KMOD_SHIFT:
                        self.dragging = True
                        self.last_mouse_pos = event.pos
                    elif world_pos:
                        self.selected_cell = world_pos
                elif event.button == 3:  # ПКМ - снять выделение
                    self.selected_cell = None
                elif event.button == 4:  # Колесо вверх
                    self.camera_y = max(0, self.camera_y - 2)
                elif event.button == 5:  # Колесо вниз
                    self.camera_y = min(self.map_height - 10, self.camera_y + 2)

            elif event.type == pygame.MOUSEBUTTONUP:
                if event.button == 1:
                    self.dragging = False

            elif event.type == pygame.MOUSEMOTION and self.dragging:
                dx = event.pos[0] - self.last_mouse_pos[0]
                dy = event.pos[1] - self.last_mouse_pos[1]
                self.camera_x = max(0, min(self.map_width - 15, self.camera_x - dx // self.base_cell_size))
                self.camera_y = max(0, min(self.map_height - 10, self.camera_y - dy // self.base_cell_size))
                self.last_mouse_pos = event.pos

        self._clamp_camera()
        return None

    def run(self) -> bool:
        print("\n🎮 Визуализация запущена | Стрелки/WASD - прокрутка, Shift+ЛКМ - перетаскивание")

        while self.running:
            self.clock.tick(Config.FPS)
            action = self.handle_events()
            if action == 'reload':
                return True

            # ✅ 1. Фон области карты (серый)
            self.screen.fill((240, 240, 240),
                             (0, 0, self.info_panel_x, Config.SCREEN_HEIGHT))

            # ✅ 2. Рисуем карту
            self.draw_grid()
            self.draw_agents()

            # ✅ 3. Рисуем мини-карту
            self.draw_minimap()

            # ✅ 4. Рисуем белую панель справа
            self.draw_info_panel()

            # ✅ 5. Заголовок внизу
            self.draw_header()

            pygame.display.flip()

        pygame.quit()
        return False


# ==============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ==============================================================================
def print_header(text: str): print("\n" + "=" * 60 + f"\n{text}\n" + "=" * 60)


def create_agents_from_map(map_data: Dict) -> List[Agent]:
    """Создает агентов на основе позиций зарядок из карты"""
    agents = []
    charger_positions = {}  # charger_id -> (row, col)

    # Сначала находим все зарядки
    for row_idx, row in enumerate(map_data.get('cells', [])):
        for col_idx, cell in enumerate(row):
            parsed, _ = MapValidator(map_data, auto_fix=False).parse_cell_text(cell)
            if parsed and parsed['type'] == 'C' and 'charger' in parsed['logic']:
                charger_positions[parsed['logic']['charger']] = (row_idx, col_idx)

    # Создаем агентов для каждой зарядки
    for charger_id, pos in charger_positions.items():
        agent = Agent(agent_id=charger_id, charger_id=charger_id, start_pos=pos)
        agent.status = 'CHARGING'
        agent.charge = agent.max_charge
        agents.append(agent)

    return agents


def main():
    print_header("🏭 СИМУЛЯТОР РОБОТИЗИРОВАННОГО СКЛАДА")
    print("Программа 1/4: Визуализация карты")

    # Загрузка
    print_header("1. Загрузка карты")
    loader = ExcelLoader(folder_name='maps', file_name='map3.xlsx')
    if not loader.load():
        print("❌ Ошибки:", loader.get_errors());
        return 1
    map_data = loader.get_map_data()
    print(f"✅ {map_data['width']}×{map_data['height']}")
    loader.save_to_json('map_data.json')

    # Валидация
    print_header("2. Валидация")
    validator = MapValidator(map_data, auto_fix=True)
    validator.validate()
    report = validator.get_report()
    print(f"✅ {'Валидно' if report['is_valid'] else 'Есть ошибки'} | Исправлено: {report['fixed_count']}")

    # Создание агентов
    agents = create_agents_from_map(validator.map_data)
    print(f"✅ Создано агентов: {len(agents)} (на позициях зарядок)")

    # Визуализация
    print_header("3. Визуализация")
    visualizer = MapVisualizer(validator.map_data, report, agents)
    if visualizer.run(): return main()

    print_header("✅ Завершено")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⚠️ Прервано"); pygame.quit(); sys.exit(1)
    except Exception as e:
        print(f"\n❌ {e}"); import traceback; traceback.print_exc(); pygame.quit(); sys.exit(1)