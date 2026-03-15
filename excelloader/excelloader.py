from openpyxl import load_workbook
from pathlib import Path
import json

# --- НАСТРОЙКИ ---
FILE_NAME = 'map1.xlsx'
FOLDER_NAME = 'maps'
OUTPUT_FILE = 'map_data.json'  # Файл для сохранения данных
# -----------------

# 1. Определяем путь к файлу
script_dir = Path(__file__).parent
excel_path = script_dir.parent / FOLDER_NAME / FILE_NAME

if not excel_path.exists():
    print(f"❌ Ошибка: Файл не найден по пути: {excel_path}")
    exit()

# print(f"✅ Загрузка файла: {excel_path}")
wb = load_workbook(excel_path, data_only=True)
ws = wb.active


# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ---
def get_cell_color_hex(cell):
    """Возвращает HEX цвет ячейки или None, если цвета нет"""
    fill = cell.fill
    if fill.fill_type is None:
        return None
    # print(fill.fgColor.type)
    fg_color = fill.fgColor
    if fg_color.type != 'rgb':
        print(fg_color.type)
        return None
    # Убираем первые 2 символа (прозрачность)
    return fg_color.rgb[2:]


# --- 2. НАХОДИМ ГРАНИЦЫ ЧЕРНОГО ПРЯМОУГОЛЬНИКА ---
# Черный цвет в HEX обычно '000000'
BLACK_COLOR = '000000'
START_ROW = 1
START_COL = 1

# Проверяем, что стартовая ячейка действительно черная
first_cell_color = get_cell_color_hex(ws.cell(row=START_ROW, column=START_COL))
if first_cell_color != BLACK_COLOR:
    print(f"❌ Ошибка: Ячейка A1 не черная (цвет: {first_cell_color})")
    print("   Скрипт ожидает, что карта начинается с черного прямоугольника.")
    exit()

# print("🔍 Поиск границ черного прямоугольника...")
# Находим правую границу (ширину)
# Идем вправо по первой строке, пока цвет черный
width = 0
for col in range(START_COL, ws.max_column + 1):
    color = get_cell_color_hex(ws.cell(row=START_ROW, column=col))
    if color == BLACK_COLOR:
        width += 1
    else:
        break

# Находим нижнюю границу (высоту)
# Идем вниз по первому столбцу, пока цвет черный
height = 0
for row in range(START_ROW, ws.max_row + 1):
    color = get_cell_color_hex(ws.cell(row=row, column=START_COL))
    if color == BLACK_COLOR:
        height += 1
    else:
        break

# print(f"✅ Найдено: Ширина = {width}, Высота = {height}")


# --- 3. ИЗВЛЕКАЕМ ОСНОВНУЮ КАРТУ (ВНУТРЕННОСТЬ ПРЯМОУГОЛЬНИКА) ---
map_data = {
    'width': width,
    'height': height,
    'start_row': START_ROW,
    'start_col': START_COL,
    'cells': []
}

for row in range(START_ROW, START_ROW + height):
    row_data = []
    for col in range(START_COL, START_COL + width):
        cell = ws.cell(row=row, column=col)

        # Сохраняем ВСЕ данные ячейки
        cell_info = {
            'row': row,
            'col': col,
            'text': cell.value,
            'color': get_cell_color_hex(cell)
        }
        row_data.append(cell_info)
    map_data['cells'].append(row_data)
# print(f"⏳ Чтение данных карты ({width}x{height})...")
# main_map = []
# for row in range(START_ROW, START_ROW + height):
#     row_data = []
#     for col in range(START_COL, START_COL + width):
#         cell = ws.cell(row=row, column=col)
#         color = get_cell_color_hex(cell)
#
#         # Сохраняем цвет (если None - значит без заливки)
#         row_data.append(color)
#     main_map.append(row_data)


# --- 4. ВЫВОД ---
# for row in main_map:
#     for cell in row:
#         if cell == '000000':
#             print('⬛', end=' ')
#             # print('E', end=' ')
#         elif cell == 'FF0000':
#             print('🟥', end=' ')
#             # print('W', end=' ')
#         elif cell == 'FFFFFF' or cell == None:
#             print('⬜', end=' ')
#             # print('F', end=' ')
#         elif cell == 'FF9900':
#             print('🟧', end=' ')
#             # print('S', end=' ')
#         elif cell == '980000':
#             print('⚛️', end=' ')
#             # print('P', end=' ')
#         elif cell == '0000FF':
#             print('🟦', end=' ')
#             # print('C', end=' ')
#         elif cell == '7030A0':
#             print('🟪', end=' ')
#             # print('Z', end=' ')
#         elif cell == 'FFFF00':
#             print('🟨', end=' ')
#             # print('B', end=' ')
#         elif cell == '00B050':
#             print('🟩', end=' ')
#             # print('A', end=' ')
#         else:
#             print('Error', end = ' ')
#             # print('Error', end=' ')
#     print()
    # print(row)
# Пример: вывести размер полученной карты
# print(f"📊 Размер основной карты: {len(main_map)} строк * {len(main_map[0])} столбцов")

# --- 4. СОХРАНЯЕМ В JSON ---
output_path = script_dir / OUTPUT_FILE
with open(output_path, 'w', encoding='utf-8') as f:
    json.dump(map_data, f, ensure_ascii=False, indent=2)

# --- 5. Валидация ---
# validator = mapvalidator.MapValidator(ws, START_ROW, START_COL, height, width)
# is_valid = validator.validate()
# print(is_valid)