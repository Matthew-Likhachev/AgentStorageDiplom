"""
visualizer.py
"""
import pygame
import json
import math
import logging
from typing import Dict, List, Optional, Tuple, Set, Any

CELL_COLORS = {
    'E': (0, 0, 0),       'W': (255, 0, 0),      'F': (255, 255, 255),
    'S': (255, 153, 0),   'P': (152, 0, 0),       'C': (0, 0, 255),
    'Z': (112, 48, 160),  'B': (255, 255, 0),     'A': (0, 176, 80),
}
AGENT_STATUS_COLORS = {
    'IDLE':            (0,   176,  80),
    'GO_TO_SHELF':     (0,   176,  80),
    'CARRY_TO_PACKER': (0,   120, 215),
    'UNLOADING':       (255, 140,   0),
    'WAITING_SLOT':    (100, 100, 100),
    'RETURN_SHELF':    (255, 140,   0),
}
PRODUCT_COLORS = {
    0: (50,  205,  50),
    1: (30,  144, 255),
    2: (255, 215,   0),
}
PRODUCT_NAMES = {0: "Зелёный", 1: "Синий", 2: "Жёлтый"}

# Направления: имя → (dr, dc, угол_pygame_для_стрелки)
DIR_META = {
    'n': (-1,  0, 270),
    's': ( 1,  0,  90),
    'w': ( 0, -1, 180),
    'e': ( 0,  1,   0),
}

DEFAULT_CELL_SIZE = 30
FPS = 60
AXIS_WIDTH  = 50
AXIS_HEIGHT = 40
RIGHT_PANEL = 340


class PygameVisualizer:
    def __init__(self, grid_data: Dict[str, Any],
                 metrics: Optional[Any] = None,
                 cell_size: int = DEFAULT_CELL_SIZE,
                 title: str = "Robot Warehouse Simulator"):
        self.grid_data  = grid_data
        self.metrics    = metrics
        self.cell_size  = cell_size
        self.title      = title
        self.width      = grid_data.get('width', 0)
        self.height     = grid_data.get('height', 0)
        self.start_row  = grid_data.get('start_row', 1)
        self.start_col  = grid_data.get('start_col', 1)

        self.window_width  = self.width * cell_size + AXIS_WIDTH + RIGHT_PANEL
        self.window_height = self.height * cell_size + AXIS_HEIGHT + 70

        self.current_tick     = 0
        self.agents_state:    Dict[int, Dict]              = {}
        self.reservations:    Dict[Tuple, List]            = {}
        self.shelf_states:    Dict[Tuple, str]             = {}
        self.dispatcher_state: Dict                        = {}
        self.inventory_state:  Dict[Tuple, Dict[int, int]] = {}
        self.slot_state:       Dict                        = {}

        # Режим стрелок: True = с грузом, False = без груза
        self.arrows_cargo_mode: bool = False
        # Показывать ли стрелки вообще
        self.show_arrows: bool = True

        self.running = True
        self.paused  = False
        self.speed   = 1.0

        pygame.init()
        pygame.display.set_caption(title)
        self.screen = pygame.display.set_mode((self.window_width, self.window_height))
        self.clock  = pygame.time.Clock()
        self._init_fonts()

        # ── Строим граф из карты (как BaseGridRouter) ─────────────────────────
        # dirs_loaded[pos] = Set[str]  — разрешённые направления С ГРУЗОМ
        # dirs_empty[pos]  = Set[str]  — разрешённые направления БЕЗ ГРУЗА
        # cell_types[pos]  = str
        self._dirs_loaded: Dict[Tuple, Set[str]] = {}
        self._dirs_empty:  Dict[Tuple, Set[str]] = {}
        self._cell_types:  Dict[Tuple, str]      = {}

        def _parse_dirs(s: str) -> Set[str]:
            return {d for d in s.split('-') if d in ('n', 'w', 's', 'e')}

        for row in grid_data.get('cells', []):
            for c in row:
                pos   = (c['row'], c['col'])
                text  = c.get('text', '')
                parts = text.split('|') if '|' in text else ['F', '', '', '{}']
                self._cell_types[pos]  = parts[0]
                self._dirs_loaded[pos] = _parse_dirs(parts[1] if len(parts) > 1 else '')
                self._dirs_empty[pos]  = _parse_dirs(parts[2] if len(parts) > 2 else '')

        # Кэш: предрасчёт стрелок (список (cx, cy, angle) per pos per mode)
        # Будем рисовать "на лету" — кэш не нужен, карта статична
        self._arrow_cache: Dict[Tuple, Dict[str, List]] = {}  # pos -> {'loaded': [...], 'empty': [...]}
        self._build_arrow_cache()

        # ── Кнопка переключения графа ─────────────────────────────────────────
        self._btn_arrow_rect: Optional[pygame.Rect] = None  # будет задан в _draw_right_panel

        logging.info(f"🎨 Визуализатор: {self.width}x{self.height}, cell={cell_size}px")

    def _init_fonts(self):
        base = max(10, int(self.cell_size * 0.4))
        try:
            self.font_axis  = pygame.font.Font(None, base)
            self.font_cell  = pygame.font.Font(None, int(base * 1.1))
            self.font_agent = pygame.font.Font(None, int(base * 0.8))
            self.font_ui    = pygame.font.Font(None, base + 6)
            self.font_title = pygame.font.Font(None, base + 10)
            self.font_small = pygame.font.Font(None, max(11, base + 2))
            self.font_btn   = pygame.font.Font(None, max(13, base + 4))
        except Exception:
            f = pygame.font.Font(None, base)
            self.font_axis = self.font_cell = self.font_agent = \
                self.font_ui = self.font_title = self.font_small = self.font_btn = f

    # ── Предрасчёт стрелок ───────────────────────────────────────────────────
    def _build_arrow_cache(self):
        """
        Для каждой клетки строим список стрелок (pixel_cx, pixel_cy, angle_deg)
        отдельно для графа "с грузом" и "без груза".
        Стрелки строятся из актуального графа маршрутизатора (_dirs_loaded / _dirs_empty).
        Клетки типа E/W пропускаем — они непроходимы.
        """
        cs  = self.cell_size
        for pos, ctype in self._cell_types.items():
            if ctype in ('E', 'W'):
                continue
            r, c = pos
            cx = AXIS_WIDTH  + (c - self.start_col) * cs + cs // 2
            cy = AXIS_HEIGHT + (r - self.start_row) * cs + cs // 2

            loaded_arrows = []
            empty_arrows  = []

            for mode, dirs_map, out_list in (
                    ('loaded', self._dirs_loaded, loaded_arrows),
                    ('empty',  self._dirs_empty,  empty_arrows)):
                dirs = dirs_map.get(pos, set())
                for d in dirs:
                    dr, dc, angle = DIR_META[d]
                    # Смещаем хвост стрелки от центра в сторону направления
                    # чтобы несколько стрелок из одной клетки не перекрывались
                    off = cs // 5
                    ax = cx + dc * off
                    ay = cy + dr * off
                    out_list.append((ax, ay, angle))

            self._arrow_cache[pos] = {
                'loaded': loaded_arrows,
                'empty':  empty_arrows,
            }

    # ── Рисование одной стрелки ───────────────────────────────────────────────
    def _draw_arrow(self, cx: int, cy: int, angle_deg: float,
                    size: int, color: Tuple):
        """
        Рисует сглаженную стрелку.
          angle_deg=0 → вправо, 90 → вниз.
          size — длина от хвоста до острия в пикселях.

        Тело: pygame.draw.aaline (anti-aliased, без зубчиков).
        Наконечник: закрашенный треугольник-полигон (draw.polygon),
          что даёт чёткую форму без алиасинга на диагоналях.
        """
        rad   = math.radians(angle_deg)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)

        head_len  = size * 0.42       # длина наконечника (% от size)
        head_w    = size * 0.28       # полуширина основания наконечника
        body_len  = size - head_len   # длина тела без наконечника

        # Хвост и основание наконечника (конец тела)
        tail_x  = cx - cos_a * size * 0.5
        tail_y  = cy - sin_a * size * 0.5
        base_x  = cx + cos_a * (size * 0.5 - head_len)
        base_y  = cy + sin_a * (size * 0.5 - head_len)
        tip_x   = cx + cos_a * size * 0.5
        tip_y   = cy + sin_a * size * 0.5

        # Перпендикуляр к направлению (для основания треугольника)
        perp_x = -sin_a
        perp_y =  cos_a

        # Три вершины наконечника
        p_tip   = (int(round(tip_x)),  int(round(tip_y)))
        p_left  = (int(round(base_x + perp_x * head_w)),
                   int(round(base_y + perp_y * head_w)))
        p_right = (int(round(base_x - perp_x * head_w)),
                   int(round(base_y - perp_y * head_w)))

        # Тело — сглаженная линия (aaline не имеет width, поэтому одна линия)
        pygame.draw.aaline(self.screen, color,
                           (int(round(tail_x)), int(round(tail_y))),
                           (int(round(base_x)), int(round(base_y))))

        # Наконечник — закрашенный треугольник
        pygame.draw.polygon(self.screen, color, [p_tip, p_left, p_right])

    # ── Отрисовка стрелок на карте ───────────────────────────────────────────
    def _draw_direction_arrows(self):
        if not self.show_arrows:
            return
        cs       = self.cell_size
        arrow_sz = max(4, cs // 3)
        key      = 'loaded' if self.arrows_cargo_mode else 'empty'
        # Полупрозрачный цвет стрелок
        color    = (80, 180, 255) if self.arrows_cargo_mode else (80, 255, 160)

        for pos, cache in self._arrow_cache.items():
            for ax, ay, angle in cache.get(key, []):
                self._draw_arrow(ax, ay, angle, arrow_sz, color)

    # ── on_state_change ───────────────────────────────────────────────────────
    def on_state_change(self, state: Dict[str, Any]):
        self.current_tick     = state.get('tick', self.current_tick)
        self.agents_state     = state.get('agents', {})
        self.reservations     = state.get('reservations', {})
        self.shelf_states     = state.get('shelf_states', {})
        self.dispatcher_state = state.get('dispatcher', {})
        self.inventory_state  = state.get('inventory', {})
        self.slot_state       = state.get('slot_state', {})  # {pid: {slot_num: aid}}

    # ── Главный цикл ─────────────────────────────────────────────────────────
    def update(self):
        self._handle_events()
        if self.paused:
            self._draw_pause_overlay()
            pygame.display.flip()
            return
        self.screen.fill((30, 30, 40))
        self._draw_axes()
        self._draw_grid()
        self._draw_direction_arrows()   # стрелки поверх сетки, под агентами
        self._draw_paths()
        self._draw_agents()
        self._draw_right_panel()
        self._draw_legend()
        pygame.display.flip()
        self.clock.tick(FPS)

    # ── Обработка событий ────────────────────────────────────────────────────
    def _handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False

            elif event.type == pygame.KEYDOWN:
                if   event.key == pygame.K_ESCAPE: self.running = False
                elif event.key == pygame.K_SPACE:  self.paused = not self.paused
                elif event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                    self.speed = min(4.0, self.speed * 2)
                elif event.key == pygame.K_MINUS:
                    self.speed = max(0.25, self.speed / 2)
                # G — переключить тип графа, A — вкл/выкл стрелки
                elif event.key == pygame.K_g:
                    self.arrows_cargo_mode = not self.arrows_cargo_mode
                elif event.key == pygame.K_a:
                    self.show_arrows = not self.show_arrows

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if self._btn_arrow_rect and self._btn_arrow_rect.collidepoint(event.pos):
                    self.arrows_cargo_mode = not self.arrows_cargo_mode

    def _blit(self, surf, x, y):
        self.screen.blit(surf, (x, y))

    # ── Оси ──────────────────────────────────────────────────────────────────
    def _draw_axes(self):
        pygame.draw.rect(self.screen, (45,45,55), (0, 0, AXIS_WIDTH, self.window_height))
        pygame.draw.rect(self.screen, (45,45,55), (0, 0, self.window_width, AXIS_HEIGHT))
        for row in range(self.start_row, self.start_row + self.height):
            y  = AXIS_HEIGHT + (row - self.start_row) * self.cell_size
            bg = (55,55,65) if row % 2 == 0 else (45,45,55)
            pygame.draw.rect(self.screen, bg, (0, y, AXIS_WIDTH, self.cell_size))
            t = self.font_axis.render(str(row), True, (220,220,220))
            self._blit(t, AXIS_WIDTH - t.get_width() - 4,
                       y + self.cell_size//2 - t.get_height()//2)
        for col in range(self.start_col, self.start_col + self.width):
            x  = AXIS_WIDTH + (col - self.start_col) * self.cell_size
            bg = (55,55,65) if col % 2 == 0 else (45,45,55)
            pygame.draw.rect(self.screen, bg, (x, 0, self.cell_size, AXIS_HEIGHT))
            t = self.font_axis.render(str(col), True, (220,220,220))
            self._blit(t, x + self.cell_size//2 - t.get_width()//2,
                       AXIS_HEIGHT//2 - t.get_height()//2)
        corner = self.font_ui.render("Y\\X", True, (255,255,255))
        self._blit(corner, AXIS_WIDTH//2 - corner.get_width()//2,
                   AXIS_HEIGHT//2 - corner.get_height()//2)
        pygame.draw.line(self.screen,(100,100,120),(AXIS_WIDTH,0),(AXIS_WIDTH,self.window_height),2)
        pygame.draw.line(self.screen,(100,100,120),(0,AXIS_HEIGHT),(self.window_width,AXIS_HEIGHT),2)

    # ── Сетка ────────────────────────────────────────────────────────────────
    def _draw_grid(self):
        cs  = self.cell_size
        sq  = max(4, cs // 4)
        gap = 2

        for r_idx, row in enumerate(self.grid_data.get('cells', [])):
            for c_idx, cell_info in enumerate(row):
                abs_row = cell_info.get('row', self.start_row + r_idx)
                abs_col = cell_info.get('col', self.start_col + c_idx)
                x = AXIS_WIDTH  + (abs_col - self.start_col) * cs
                y = AXIS_HEIGHT + (abs_row - self.start_row) * cs

                text  = cell_info.get('text', '')
                parts = text.split('|') if '|' in text else ['F','','','{}']
                cell_type = parts[0]
                logic_str = parts[3] if len(parts) > 3 else '{}'

                if cell_type == 'S':
                    st    = self.shelf_states.get((abs_row, abs_col), 'ACTIVE')
                    color = (110,110,110) if st in ('TAKEN','RETURNING') else (255,153,0)
                else:
                    color = CELL_COLORS.get(cell_type, (200,200,200))

                pygame.draw.rect(self.screen, color, (x, y, cs, cs))
                pygame.draw.rect(self.screen, (90,90,100), (x, y, cs, cs), 1)

                if cell_type in ('S','P','C','Z','B') and cs >= 20:
                    try:
                        logic = json.loads(logic_str) if logic_str.strip() else {}
                        did = None
                        if   cell_type == 'S':
                            did = logic.get('shelf_id')
                            if did is None: did = logic.get('shelf')
                        elif cell_type == 'P':        did = logic.get('packer_id')
                        elif cell_type == 'C':        did = logic.get('charger_id') or logic.get('charger')
                        elif cell_type in ('Z','B'):  did = logic.get('slot')
                        if did is not None:  # 0 тоже валидный ID
                            did_str = str(did)
                            tc = (0,0,0) if color in [(255,255,255),(255,255,0),(255,153,0),(110,110,110)] else (255,255,255)
                            t  = self.font_cell.render(did_str, True, tc)
                            self._blit(t, x+cs//2-t.get_width()//2, y+cs//2-t.get_height()//2)
                    except Exception:
                        pass

                if cell_type == 'S' and cs >= 18:
                    inv = self.inventory_state.get((abs_row, abs_col), {})
                    if inv:
                        ox = x + 2
                        oy = y + cs - sq - 2
                        for pid in sorted(inv.keys()):
                            if ox + sq > x + cs - 1: break
                            pcol = PRODUCT_COLORS.get(pid, (200,200,200))
                            pygame.draw.rect(self.screen, pcol, (ox, oy, sq, sq))
                            pygame.draw.rect(self.screen, (0,0,0), (ox, oy, sq, sq), 1)
                            ox += sq + gap

    # ── Пути агентов ─────────────────────────────────────────────────────────
    def _draw_paths(self):
        for aid, ad in self.agents_state.items():
            path = ad.get('path', [])
            if not path or len(path) < 2: continue
            color = (50,100,255) if ad.get('has_cargo') else (50,200,100)
            pts = []
            for r, c in path:
                px = AXIS_WIDTH  + (c - self.start_col) * self.cell_size + self.cell_size//2
                py = AXIS_HEIGHT + (r - self.start_row) * self.cell_size + self.cell_size//2
                pts.append((px, py))
            if len(pts) > 1:
                pygame.draw.lines(self.screen, color, False, pts, 2)

    # ── Агенты ───────────────────────────────────────────────────────────────
    def _draw_agents(self):
        cs = self.cell_size
        for aid, ad in self.agents_state.items():
            pos    = ad.get('pos', (0,0))
            status = ad.get('status','IDLE')
            cargo  = ad.get('has_cargo', False)
            x = AXIS_WIDTH  + (pos[1] - self.start_col) * cs + cs//2
            y = AXIS_HEIGHT + (pos[0] - self.start_row) * cs + cs//2
            color  = AGENT_STATUS_COLORS.get(status, (0,176,80))
            radius = max(3, cs//2 - 3)
            pygame.draw.circle(self.screen, (0,0,0),       (x+2, y+2), radius)
            pygame.draw.circle(self.screen, color,         (x,   y),   radius)
            pygame.draw.circle(self.screen, (255,255,255), (x,   y),   radius, 2)
            if cargo:
                sq = max(4, cs//4)
                pygame.draw.rect(self.screen, (255,153,0), (x-sq//2, y+sq//2, sq, sq))
                pygame.draw.rect(self.screen, (0,0,0),     (x-sq//2, y+sq//2, sq, sq), 1)
            if cs >= 20:
                t = self.font_agent.render(str(aid), True, (255,255,255))
                self._blit(t, x-t.get_width()//2, y-t.get_height()//2)

    # ── Правая панель ─────────────────────────────────────────────────────────
    def _draw_right_panel(self):
        px = AXIS_WIDTH + self.width * self.cell_size + 10
        bw = RIGHT_PANEL - 20

        # ── Фиксированная раскладка панели ───────────────────────────────────
        # Каждый блок занимает своё выделенное пространство независимо от данных.
        # Блоки никогда не скачут — высоты зафиксированы.
        lh    = 26   # высота строки
        H     = self.window_height

        # Высоты блоков (пикселей)
        METRICS_H  = 8 + 28 + lh*3 + lh*5 + 8   # метрики + статусы (макс 5)
        BUTTONS_H  = 8 + 30 + 4 + 30 + lh + lh + 8   # 2 кнопки + подсказки
        GOODS_H    = lh + lh*3 + 8               # товары (3 вида)
        QUEUE_H    = H - AXIS_HEIGHT - METRICS_H - BUTTONS_H - GOODS_H - 4  # остаток

        # Начала блоков (Y)
        y_metrics  = AXIS_HEIGHT + 8
        y_buttons  = y_metrics + METRICS_H
        y_goods    = y_buttons + BUTTONS_H
        y_queue    = y_goods   + GOODS_H

        # Рисуем разделители
        def hline(y):
            pygame.draw.line(self.screen,(100,100,120),(px,y),(px+bw,y),1)

        # ════════ Блок 1: Метрики ════════════════════════════════════════════
        py = y_metrics

        # ── Метрики ──────────────────────────────────────────────────────────
        self._blit(self.font_title.render("📊 Метрики", True, (255,255,255)), px, py); py += 28
        self._blit(self.font_ui.render(f"Такт: {self.current_tick}", True, (200,200,200)), px, py); py += lh
        status_str = "⏸ ПАУЗА" if self.paused else f"▶ {self.speed}x"
        sc = (255,100,100) if self.paused else (100,255,100)
        self._blit(self.font_ui.render(status_str, True, sc), px, py); py += lh
        self._blit(self.font_ui.render(f"Агентов: {len(self.agents_state)}", True, (200,200,200)), px, py); py += lh

        cnt: Dict[str,int] = {}
        for ad in self.agents_state.values():
            s = ad.get('status','IDLE'); cnt[s] = cnt.get(s,0)+1
        # Выводим не более 5 статусов чтобы блок не переполнился
        for st, n in list(sorted(cnt.items()))[:5]:
            col = AGENT_STATUS_COLORS.get(st,(200,200,200))
            pygame.draw.rect(self.screen, col, (px, py, 14, 14))
            self._blit(self.font_small.render(f"{st}: {n}", True,(220,220,220)), px+18, py); py += lh

        # ════════ Блок 2: Кнопки ════════════════════════════════════════════
        hline(y_buttons); py = y_buttons + 8

        # ── Кнопка переключения графа стрелок ────────────────────────────────
        if self.arrows_cargo_mode:
            btn_label = "Граф: С ГРУЗОМ"
            btn_bg    = (0, 100, 200)
            btn_fg    = (255, 255, 255)
            hint_col  = (80, 180, 255)
        else:
            btn_label = "Граф: БЕЗ ГРУЗА"
            btn_bg    = (0, 160, 80)
            btn_fg    = (255, 255, 255)
            hint_col  = (80, 255, 160)

        btn_h = 30
        btn_rect = pygame.Rect(px, py, bw, btn_h)
        self._btn_arrow_rect = btn_rect

        # Тень
        pygame.draw.rect(self.screen, (20, 20, 30), btn_rect.move(2, 2), border_radius=5)
        # Кнопка
        pygame.draw.rect(self.screen, btn_bg, btn_rect, border_radius=5)
        # Рамка
        pygame.draw.rect(self.screen, (200, 200, 200), btn_rect, 1, border_radius=5)
        # Текст
        t = self.font_btn.render(btn_label, True, btn_fg)
        self._blit(t, btn_rect.centerx - t.get_width()//2,
                      btn_rect.centery - t.get_height()//2)
        py += btn_h + 4

        # Кнопка вкл/выкл стрелок
        vis_label = "Стрелки: ВКЛ" if self.show_arrows else "Стрелки: ВЫКЛ"
        vis_bg    = (60, 60, 80) if not self.show_arrows else (50, 50, 70)
        vis_rect  = pygame.Rect(px, py, bw, btn_h)
        pygame.draw.rect(self.screen, (20,20,30), vis_rect.move(2,2), border_radius=5)
        pygame.draw.rect(self.screen, vis_bg, vis_rect, border_radius=5)
        pygame.draw.rect(self.screen, (160,160,160), vis_rect, 1, border_radius=5)
        t2 = self.font_btn.render(vis_label, True, (220,220,220))
        self._blit(t2, vis_rect.centerx - t2.get_width()//2,
                       vis_rect.centery - t2.get_height()//2)
        # Запоминаем rect для клика
        self._btn_vis_rect = vis_rect
        py += btn_h + 2

        # Подсказки
        self._blit(self.font_small.render("G — переключить граф", True, hint_col), px, py); py += lh - 4
        self._blit(self.font_small.render("A — вкл/выкл стрелки", True, (160,160,160)), px, py); py += lh

        # ════════ Блок 3: Товары ════════════════════════════════════════════
        hline(y_goods); py = y_goods
        self._blit(self.font_ui.render("Товары:", True, (220,220,220)), px, py); py += lh
        for pid, pname in PRODUCT_NAMES.items():
            pcol = PRODUCT_COLORS[pid]
            pygame.draw.rect(self.screen, pcol, (px, py+2, 12, 12))
            pygame.draw.rect(self.screen,(0,0,0),(px,py+2,12,12),1)
            self._blit(self.font_small.render(pname, True,(220,220,220)), px+16, py); py += lh

        # ════════ Блок 4: Очередь ════════════════════════════════════════════
        hline(y_queue); py = y_queue
        panel_bottom = y_queue + QUEUE_H
        disp     = self.dispatcher_state
        packers  = disp.get('packers', {})
        # Fallback к старому формату если packers не заполнен
        if not packers:
            ai = disp.get('active_info', {})
            ob = disp.get('orders_by_packer', {})
            packers = {pid: {'active_order': ai.get(pid,{}).get('order_id'),
                             'active_suborder': ai.get(pid,{}).get('suborder_id'),
                             'slots_total': ai.get(pid,{}).get('slots_total',0),
                             'slots_free': ai.get(pid,{}).get('slots_free',0),
                             'slots': {},
                             'orders': ob.get(pid,{}).get('orders',[])}
                       for pid in set(list(ai)+list(ob))}

        self._blit(self.font_ui.render("Очередь к фасовщику:", True,(255,255,255)), px, py); py+=lh+2

        for pid_str in sorted(packers.keys(), key=lambda x: int(x)):
            if py + lh > panel_bottom: break
            info = packers[pid_str]
            act_oid = info.get('active_order')
            act_sid = info.get('active_suborder')
            total   = info.get('slots_total', 0)
            free    = info.get('slots_free', 0)
            slots   = info.get('slots', {})

            # Заголовок
            pygame.draw.rect(self.screen,(50,50,70),(px-2,py-1,bw+4,lh+2))
            self._blit(self.font_ui.render(
                f"Фасовщик {pid_str}  [{total-free}/{total} слотов]",
                True,(255,200,100)), px, py); py+=lh

            # Активный заказ/подзаказ
            if act_oid and act_sid:
                self._blit(self.font_small.render(
                    f"  Заказ #{act_oid}   Подзаказ #{act_sid}",
                    True,(120,255,120)), px, py); py+=lh-3
            elif act_oid and not act_sid:
                self._blit(self.font_small.render(
                    f"  Заказ #{act_oid}   подзаказы не назначены",
                    True,(255,200,80)), px, py); py+=lh-3
            else:
                self._blit(self.font_small.render(
                    "  Заказов нет — фасовщик свободен",
                    True,(140,140,140)), px, py); py+=lh-3

            # Слоты очереди: каждый слот — строка
            for sn_str in sorted(slots.keys(), key=lambda x: int(x)):
                if py+lh > panel_bottom: break
                sn   = int(sn_str)
                sd   = slots[sn_str]
                aid  = sd.get('agent_id')
                sid  = sd.get('suborder_id')
                stat = sd.get('status') or ''
                stat_short = (stat.replace('STATUS_','')
                                  .replace('_',' ')
                                  .replace('CARRY TO PACKER','→П')
                                  .replace('WAITING SLOT','⏳')
                                  .replace('GO TO SHELF','→С')
                                  .replace('RETURN SHELF','↩')
                                  .replace('UNLOADING','↓')
                                  .replace('IDLE','·'))[:6]

                if aid is None:
                    bg  = (35,35,45)
                    txt = f"  [{sn}] —"
                    tc  = (80,80,80)
                elif sn == 1:
                    bg  = (0,100,50)
                    txt = f"  [{sn}] А{aid} под#{sid}"
                    tc  = (200,255,200)
                else:
                    bg  = (30,60,100)
                    txt = f"  [{sn}] А{aid} под#{sid}"
                    tc  = (180,210,255)

                pygame.draw.rect(self.screen, bg,(px,py,bw,lh-2),border_radius=3)
                self._blit(self.font_small.render(txt,True,tc), px+4, py+1)
                if aid and stat_short:
                    ts = self.font_small.render(stat_short,True,(200,200,200))
                    self._blit(ts, px+bw-ts.get_width()-4, py+1)
                py += lh-1

            py += 5

        # ── Легенда цветов полок ─────────────────────────────────────────────
        if py + lh*3 < panel_bottom:
            pygame.draw.line(self.screen,(100,100,120),(px,py),(px+bw,py),1); py+=4
            self._blit(self.font_small.render("Полки:",True,(180,180,180)), px,py); py+=lh-2
            for col, label in [((255,153,0),"активна (товар есть)"),
                                ((110,110,110),"взята / возвращается")]:
                if py+lh>panel_bottom: break
                pygame.draw.rect(self.screen,col,(px,py+2,10,10))
                pygame.draw.rect(self.screen,(80,80,80),(px,py+2,10,10),1)
                self._blit(self.font_small.render(label,True,(180,180,180)),px+14,py)
                py+=lh-2

    # ── Легенда внизу ─────────────────────────────────────────────────────────
    def _draw_legend(self):
        items = [
            ('E','Стена',(0,0,0)),     ('W','Запрет',(255,0,0)),
            ('F','Пол',(255,255,255)), ('S','Стеллаж',(255,153,0)),
            ('P','Фасовщик',(152,0,0)),('C','Зарядка',(0,0,255)),
            ('Z','Заказ',(112,48,160)),('B','Буфер',(255,255,0)),
        ]
        ly = AXIS_HEIGHT + self.height * self.cell_size + 12
        ox = 10
        for code, label, color in items:
            pygame.draw.rect(self.screen, color, (ox, ly, 18, 18))
            pygame.draw.rect(self.screen,(100,100,100),(ox,ly,18,18),1)
            t = self.font_small.render(f"{code}:{label}", True, (220,220,220))
            self._blit(t, ox+22, ly+3)
            ox += 22 + t.get_width() + 8
            if ox > self.width * self.cell_size + AXIS_WIDTH - 80:
                ox = 10; ly += 24

    # ── Пауза-оверлей ─────────────────────────────────────────────────────────
    def _draw_pause_overlay(self):
        ov = pygame.Surface((self.window_width, self.window_height), pygame.SRCALPHA)
        ov.fill((0,0,0,180))
        self.screen.blit(ov,(0,0))
        t = self.font_title.render("⏸ ПАУЗА (SPACE - продолжить)", True, (255,255,255))
        self._blit(t, self.window_width//2 - t.get_width()//2,
                      self.window_height//2 - t.get_height()//2)

    # ── Обработка клика по кнопке вкл/выкл стрелок ────────────────────────────
    # (вынесена в _handle_events через _btn_vis_rect)
    def _handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False

            elif event.type == pygame.KEYDOWN:
                if   event.key == pygame.K_ESCAPE: self.running = False
                elif event.key == pygame.K_SPACE:  self.paused = not self.paused
                elif event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                    self.speed = min(4.0, self.speed * 2)
                elif event.key == pygame.K_MINUS:
                    self.speed = max(0.25, self.speed / 2)
                elif event.key == pygame.K_g:
                    self.arrows_cargo_mode = not self.arrows_cargo_mode
                elif event.key == pygame.K_a:
                    self.show_arrows = not self.show_arrows

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if self._btn_arrow_rect and self._btn_arrow_rect.collidepoint(event.pos):
                    self.arrows_cargo_mode = not self.arrows_cargo_mode
                vis = getattr(self, '_btn_vis_rect', None)
                if vis and vis.collidepoint(event.pos):
                    self.show_arrows = not self.show_arrows

    def should_close(self) -> bool: return not self.running
    def set_speed(self, m: float):  self.speed = max(0.25, min(4.0, m))
    def toggle_pause(self):         self.paused = not self.paused