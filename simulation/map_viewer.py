import pygame
import json
import sys
import os
from typing import Dict, List, Tuple, Optional

# Импорт логики агента (файл agent_logic.py должен быть в той же папке)
from simulation.Agent import create_agent_at_first_charger, STATUSES_WEIGHTS, BATTERY_MAX, BATTERY_LOW_THRESHOLD

# ================= КОНФИГУРАЦИЯ =================
DEFAULT_MAP_FILE = "A:\projects in programming\python\AgentStorage\AgentStorageDiplom\excelloader\map_data_fixed.json"
DEFAULT_CELL_SIZE = 30
BG_COLOR = (255, 255, 255)
FLOOR_COLOR = (210, 210, 210)
OUTPUT_DIR = "parsed_map_data"

# Настройки UI для агента
UI_PANEL_WIDTH = 220
UI_BG_COLOR = (240, 240, 245)
UI_TEXT_COLOR = (30, 30, 40)
UI_ACCENT_COLOR = (0, 120, 215)


# ================= УТИЛИТЫ =================
def hex_to_rgb(hex_color: Optional[str]) -> Tuple[int, int, int]:
    """Преобразует HEX-строку цвета в RGB кортеж."""
    if not hex_color:
        return FLOOR_COLOR
    clean_hex = str(hex_color).strip().lstrip('#')
    if len(clean_hex) != 6:
        return FLOOR_COLOR
    try:
        return tuple(int(clean_hex[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return FLOOR_COLOR


def load_json(filepath: str) -> dict:
    """Загружает и валидирует JSON файл."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Файл {filepath} не найден.")
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def extract_cells(data: dict) -> List[List[dict]]:
    """Извлекает 2D-список ячеек из структуры JSON."""
    for val in data.values():
        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], list):
            return val
    raise ValueError("Не удалось найти структуру карты (список списков ячеек) в файле.")


def _parse_directions(base_pos: Tuple[int, int], dir_string: str,
                      max_row: int, max_col: int) -> List[Tuple[int, int]]:
    """
    Парсит строку направлений, находя буквы n/w/s/e в любом месте строки.
    Работает с форматами: '---', '---e', '--s-', 'n---', 'n-w-s-e' и любыми другими.
    """
    result = []
    r, c = base_pos

    # Словарь: буква направления -> смещение координат
    DIR_LETTERS = {
        'n': (-1, 0),  # North: вверх (уменьшает row)
        'w': (0, -1),  # West: влево (уменьшает col)
        's': (1, 0),  # South: вниз (увеличивает row)
        'e': (0, 1)  # East: вправо (увеличивает col)
    }

    # Перебираем КАЖДЫЙ символ строки (не фиксированные индексы!)
    for char in dir_string.lower():
        if char in DIR_LETTERS:
            dr, dc = DIR_LETTERS[char]
            nr, nc = r + dr, c + dc
            # Проверка границ карты (1-based индексация)
            if 1 <= nr <= max_row and 1 <= nc <= max_col:
                result.append((nr, nc))

    return result


def parse_map(cells: List[List[dict]]) -> Tuple[Dict, Dict, Dict]:
    """Парсит ячейки в матрицы смежности и словарь типов."""
    adj_loaded = {}
    adj_unloaded = {}
    type_dict = {t: [] for t in ['E', 'W', 'F', 'S', 'P', 'C', 'Z', 'B', 'A']}

    max_row = len(cells)
    max_col = max(len(row) for row in cells) if max_row > 0 else 0

    for row in cells:
        for cell in row:
            r = cell.get('row')
            c = cell.get('col')
            text = cell.get('text', '').strip()
            parts = text.split('|')
            # print(r,c,text,parts)

            if len(parts) < 3:
                print('SKIP')
                continue

            cell_type = parts[0].strip()
            dir_loaded = parts[1].strip()
            dir_unloaded = parts[2].strip()

            # Используем вспомогательную функцию для парсинга
            adj_loaded[(r, c)] = _parse_directions((r, c), dir_loaded, max_row, max_col)
            adj_unloaded[(r, c)] = _parse_directions((r, c), dir_unloaded, max_row, max_col)
            # print(adj_unloaded[(r, c)])

            if cell_type in type_dict:
                type_dict[cell_type].append((r, c))

    return adj_loaded, adj_unloaded, type_dict

# ================= ВИЗУАЛИЗАЦИЯ КАРТЫ =================
def render_map_surface(cells: List[List[dict]], cell_size: int) -> Tuple[pygame.Surface, int, int]:
    """
    Отрисовывает статическую карту на поверхности.
    Возвращает: (surface, width_in_cells, height_in_cells)
    """
    map_height = len(cells)
    map_width = max(len(row) for row in cells) if map_height > 0 else 0
    map_surface = pygame.Surface((map_width * cell_size, map_height * cell_size))
    map_surface.fill(FLOOR_COLOR)

    cell_font_size = max(6, int(cell_size * 0.35))
    cell_font = pygame.font.SysFont("arial", cell_font_size, bold=True)

    for row_idx, row in enumerate(cells):
        for col_idx, cell in enumerate(row):
            x = col_idx * cell_size
            y = row_idx * cell_size
            color_hex = cell.get('color') or cell.get('color ')
            color = hex_to_rgb(color_hex)
            pygame.draw.rect(map_surface, color, (x, y, cell_size, cell_size))
            pygame.draw.line(map_surface, (50, 50, 50), (x, y), (x + cell_size, y), 1)
            pygame.draw.line(map_surface, (50, 50, 50), (x, y), (x, y + cell_size), 1)

            text = (cell.get('text') or cell.get('text ', '')).strip()
            parts = text.split('|')
            if len(parts) >= 1 and parts[0].strip() == 'S':
                try:
                    logic_str = parts[3].strip() if len(parts) > 3 else "{}"
                    clean_logic = logic_str.replace(" ", "")
                    logic_dict = json.loads(clean_logic)
                    shelf_id = logic_dict.get("shelf_id")
                    if shelf_id is not None:
                        txt = cell_font.render(str(shelf_id), True, (0, 0, 0))
                        map_surface.blit(txt, (x + (cell_size - txt.get_width()) // 2,
                                               y + (cell_size - txt.get_height()) // 2))
                except Exception:
                    pass

    pygame.draw.line(map_surface, (50, 50, 50), (map_width * cell_size, 0),
                     (map_width * cell_size, map_height * cell_size), 2)
    pygame.draw.line(map_surface, (50, 50, 50), (0, map_height * cell_size),
                     (map_width * cell_size, map_height * cell_size), 2)

    return map_surface, map_width, map_height


def draw_labels(screen: pygame.Surface, map_width: int, map_height: int,
                cell_size: int, label_font: pygame.font.Font,
                label_offset_x: int, label_offset_y: int) -> None:
    """Отрисовывает нумерацию строк и колонок вокруг карты."""
    for col_idx in range(map_width):
        col_num = col_idx + 1
        txt = label_font.render(str(col_num), True, (200, 200, 200))
        cx = label_offset_x + col_idx * cell_size + cell_size // 2
        cy = label_offset_y // 2
        screen.blit(txt, (cx - txt.get_width() // 2, cy - txt.get_height() // 2))
        pygame.draw.line(screen, (100, 100, 100),
                         (cx - cell_size // 2, label_offset_y),
                         (cx + cell_size // 2, label_offset_y), 1)

    for row_idx in range(map_height):
        row_num = row_idx + 1
        txt = label_font.render(str(row_num), True, (200, 200, 200))
        cy = label_offset_y + row_idx * cell_size + cell_size // 2
        cx = label_offset_x // 2
        screen.blit(txt, (cx - txt.get_width() // 2, cy - txt.get_height() // 2))
        pygame.draw.line(screen, (100, 100, 100),
                         (label_offset_x, cy - cell_size // 2),
                         (label_offset_x, cy + cell_size // 2), 1)

    pygame.draw.line(screen, (100, 100, 100), (0, label_offset_y), (label_offset_x, label_offset_y), 2)
    pygame.draw.line(screen, (100, 100, 100), (label_offset_x, 0), (label_offset_x, label_offset_y), 2)


def draw_agent(screen: pygame.Surface, agent_state: dict, cell_size: int,
               offset_x: int, offset_y: int) -> None:
    """Отрисовывает агента на карте с учётом смещения координат."""
    pos = agent_state["pos"]  # (row, col) в 1-based
    x = offset_x + (pos[1] - 1) * cell_size
    y = offset_y + (pos[0] - 1) * cell_size

    # Тело агента
    pygame.draw.rect(screen, agent_state["color_rgb"], (x + 2, y + 2, cell_size - 4, cell_size - 4))
    pygame.draw.rect(screen, (255, 255, 255), (x + 2, y + 2, cell_size - 4, cell_size - 4), 2)

    # Индикатор груза (маленький квадрат в углу)
    if agent_state["has_cargo"]:
        pygame.draw.rect(screen, (255, 215, 0), (x + cell_size - 10, y + 2, 8, 8))
        pygame.draw.rect(screen, (0, 0, 0), (x + cell_size - 10, y + 2, 8, 8), 1)


def draw_ui_panel(screen: pygame.Surface, agent_state: dict, font: pygame.font.Font,
                  panel_x: int, panel_y: int, width: int) -> None:
    """Отрисовывает информационную панель со статусом агента."""
    # Фон панели
    pygame.draw.rect(screen, UI_BG_COLOR, (panel_x, panel_y, width, screen.get_height() - panel_y))
    pygame.draw.line(screen, (150, 150, 150), (panel_x, panel_y), (panel_x, screen.get_height() - panel_y), 2)

    y_offset = panel_y + 15
    lines = [
        (f"АГЕНТ #{agent_state['id']}", UI_ACCENT_COLOR, True),
        ("", UI_TEXT_COLOR, False),
        (f"📍 Координаты: {agent_state['pos']}", UI_TEXT_COLOR, False),
        (f"📦 Груз: {'ДА' if agent_state['has_cargo'] else 'НЕТ'}",
         (200, 150, 0) if agent_state['has_cargo'] else UI_TEXT_COLOR, False),
        (f"🔋 Заряд: {agent_state['battery']}/{BATTERY_MAX} ({agent_state['battery_percent']}%)",
         (0, 180, 0) if agent_state['battery'] > BATTERY_LOW_THRESHOLD else (255, 80, 80), False),
        (f"📊 Статус: {agent_state['status']}", UI_TEXT_COLOR, False),
        (f"⚡ Приоритет: {agent_state['priority']} (из {max(STATUSES_WEIGHTS.values())})", UI_TEXT_COLOR, False),
        ("", UI_TEXT_COLOR, False),
        ("УПРАВЛЕНИЕ:", UI_ACCENT_COLOR, True),
        ("[↑↓←→] Движение", UI_TEXT_COLOR, False),
        ("[C] Сменить груз", UI_TEXT_COLOR, False),
        ("[ESC] Выход", UI_TEXT_COLOR, False),
    ]

    for text, color, is_bold in lines:
        if text:
            f = font  # Можно добавить жирный шрифт для is_bold при необходимости
            txt_surf = f.render(text, True, color)
            screen.blit(txt_surf, (panel_x + 12, y_offset))
        y_offset += 22


# ================= ИНТЕРАКТИВНЫЙ РЕЖИМ =================
def run_interactive_mode(cells: List[List[dict]], adj_loaded: Dict, adj_unloaded: Dict,
                         cell_types: Dict, cell_size: int = DEFAULT_CELL_SIZE) -> None:
    """Основной цикл с отрисовкой карты и управлением агентом."""

    # 1. Создание агента
    agent = create_agent_at_first_charger(
        cell_types=cell_types,
        adj_loaded=adj_loaded,
        adj_unloaded=adj_unloaded,
        agent_id=1,
        initial_battery=BATTERY_MAX,
        has_cargo=False
    )
    print(f"✅ Агент #{agent.id} создан на зарядной станции {agent.charger_pos}")

    # 2. Инициализация Pygame
    pygame.init()
    LABEL_WIDTH, LABEL_HEIGHT = 50, 40
    map_surface, map_w, map_h = render_map_surface(cells, cell_size)

    screen_width = LABEL_WIDTH + map_w * cell_size + UI_PANEL_WIDTH
    screen_height = LABEL_HEIGHT + map_h * cell_size
    screen = pygame.display.set_mode((screen_width, screen_height))
    pygame.display.set_caption(f"🤖 Робосклад | Агент #{agent.id} | Sense-Plan-Act")

    clock = pygame.time.Clock()
    label_font = pygame.font.SysFont("arial", max(10, int(LABEL_HEIGHT * 0.6)), bold=True)
    ui_font = pygame.font.SysFont("consolas", 14)

    # Маппинг клавиш
    KEY_MAP = {
        pygame.K_UP: "UP", pygame.K_DOWN: "DOWN",
        pygame.K_LEFT: "LEFT", pygame.K_RIGHT: "RIGHT",
        pygame.K_c: "TOGGLE_CARGO"
    }

    running = True
    while running:
        action = "NONE"
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
                break
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                    break
                action = KEY_MAP.get(event.key, "NONE")

        # === ЦИКЛ SENSE-PLAN-ACT ===
        target = agent.process_input(action)
        agent.sense(adj_loaded, adj_unloaded)
        move = agent.plan(target)
        agent.act(move)

        # === ОТРИСОВКА ===
        screen.fill((30, 30, 35))  # Тёмный фон окна
        screen.blit(map_surface, (LABEL_WIDTH, LABEL_HEIGHT))
        draw_labels(screen, map_w, map_h, cell_size, label_font, LABEL_WIDTH, LABEL_HEIGHT)

        agent_state = agent.get_render_state()
        draw_agent(screen, agent_state, cell_size, LABEL_WIDTH, LABEL_HEIGHT)
        draw_ui_panel(screen, agent_state, ui_font,
                      panel_x=LABEL_WIDTH + map_w * cell_size + 10,
                      panel_y=10, width=UI_PANEL_WIDTH - 20)

        pygame.display.flip()
        clock.tick(10)  # 10 тактов в секунду

    pygame.quit()
    print(f"🔚 Сессия завершена. Финальное состояние агента: {agent.get_render_state()}")


# ================= ТОЧКА ВХОДА =================
def main(json_path: str = DEFAULT_MAP_FILE, cell_size: int = DEFAULT_CELL_SIZE) -> None:
    try:
        data = load_json(json_path)
        cells = extract_cells(data)
        print(cells)
        print(f"✅ Карта загружена: {len(cells[0])}x{len(cells)}")

        adj_loaded, adj_unloaded, type_dict = parse_map(cells)
        print(f"📊 Типы клеток: { {k: len(v) for k, v in type_dict.items()} }")

        if not type_dict.get('C'):
            print("⚠️  Предупреждение: на карте нет зарядных станций (тип 'C')")

        print (adj_unloaded[(11,7)])

        print("🎮 Запуск интерактивного режима...")
        run_interactive_mode(cells, adj_loaded, adj_unloaded, type_dict, cell_size)

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def test():
    json_path = "./map_data_test.json"
    data = load_json(json_path)
    cells = extract_cells(data)
    print(cells)
    adj_loaded, adj_unloaded, type_dict = parse_map(cells)
    print(adj_loaded)

def test1():
    # Тест для клетки (row=5, col=5) на карте 9x14
    test_cases = [
        ("---", []),
        ("---e", [(5, 6)]),  # East
        ("--s-", [(6, 5)]),  # South
        ("--s-e", [(6, 5), (5, 6)]),  # South + East
        ("-w--", [(5, 4)]),  # West
        ("-w--e", [(5, 4), (5, 6)]),  # West + East
        ("-w-s-e", [(5, 4), (6, 5), (5, 6)]),  # West + South + East
        ("n---", [(4, 5)]),  # North ← ВАШ СЛУЧАЙ
        ("n---e", [(4, 5), (5, 6)]),  # North + East
        ("n--s-", [(4, 5), (6, 5)]),  # North + South
        ("n--s-e", [(4, 5), (6, 5), (5, 6)]),  # North + South + East
        ("n-w--", [(4, 5), (5, 4)]),  # North + West
        ("n-w--e", [(4, 5), (5, 4), (5, 6)]),  # North + West + East
        ("n-w-s-e", [(4, 5), (5, 4), (6, 5), (5, 6)]),  # Все 4
    ]

    print("Тест _parse_directions для клетки (5, 5):")
    for dir_str, expected in test_cases:
        result = _parse_directions((5, 5), dir_str, max_row=9, max_col=14)
        status = "✓" if sorted(result) == sorted(expected) else "❌"
        print(f"{status} '{dir_str:10s}' -> {result}")

if __name__ == "__main__":
    main()
    # test()
    # test1()