from openpyxl import load_workbook
from pathlib import Path
import json
from typing import Dict, List, Optional, Tuple


class ExcelLoader:
    """Загрузчик карты из Excel файла"""

    def __init__(self, folder_name: str = 'maps', file_name: str = 'map1.xlsx'):
        self.folder_name = folder_name
        self.file_name = file_name
        self.map_data = None
        self.errors = []

        # Цвета клеток
        self.BLACK_COLOR = '000000'
        self.COLOR_MAP = {
            '000000': 'E', 'FF0000': 'W', 'FFFFFF': 'F',
            'FF9900': 'S', '980000': 'P', '0000FF': 'C',
            '7030A0': 'Z', 'FFFF00': 'B', '00B050': 'A',
        }

    def get_excel_path(self) -> Path:
        """Возвращает полный путь к Excel файлу"""
        script_dir = Path(__file__).parent
        return script_dir.parent / self.folder_name / self.file_name

    def load(self) -> bool:
        """Загружает Excel и парсит в структуру данных"""
        excel_path = self.get_excel_path()

        if not excel_path.exists():
            self.errors.append(f"Файл не найден: {excel_path}")
            return False

        try:
            wb = load_workbook(excel_path, data_only=True)
            ws = wb.active
        except Exception as e:
            self.errors.append(f"Ошибка открытия Excel: {e}")
            return False

        # Поиск границ карты
        start_row, start_col = 1, 1
        width, height = self._find_map_bounds(ws, start_row, start_col)

        if width == 0 or height == 0:
            self.errors.append("Не удалось определить размеры карты")
            return False

        # Парсинг ячеек
        self.map_data = {
            'width': width,
            'height': height,
            'start_row': start_row,
            'start_col': start_col,
            'cells': self._parse_cells(ws, start_row, start_col, width, height)
        }

        return True

    def _get_cell_color_hex(self, cell) -> Optional[str]:
        """Возвращает HEX цвет ячейки"""
        fill = cell.fill
        if fill.fill_type is None:
            return None
        fg_color = fill.fgColor
        if fg_color.type != 'rgb':
            return None
        return fg_color.rgb[2:]

    def _find_map_bounds(self, ws, start_row: int, start_col: int) -> Tuple[int, int]:
        """Находит ширину и высоту карты по черной рамке"""
        width = 0
        for col in range(start_col, ws.max_column + 1):
            color = self._get_cell_color_hex(ws.cell(row=start_row, column=col))
            if color == self.BLACK_COLOR:
                width += 1
            else:
                break

        height = 0
        for row in range(start_row, ws.max_row + 1):
            color = self._get_cell_color_hex(ws.cell(row=row, column=start_col))
            if color == self.BLACK_COLOR:
                height += 1
            else:
                break

        return width, height

    def _parse_cells(self, ws, start_row: int, start_col: int, width: int, height: int) -> List[List[Dict]]:
        """Парсит все ячейки карты"""
        cells = []
        for row in range(start_row, start_row + height):
            row_data = []
            for col in range(start_col, start_col + width):
                cell = ws.cell(row=row, column=col)
                color = self._get_cell_color_hex(cell)
                text = cell.value

                row_data.append({
                    'row': row,
                    'col': col,
                    'text': text,
                    'color': color
                })
            cells.append(row_data)
        return cells

    def save_to_json(self, output_file: str = 'map_data.json') -> bool:
        """Сохраняет данные карты в JSON"""
        if not self.map_data:
            self.errors.append("Нет данных для сохранения")
            return False

        script_dir = Path(__file__).parent
        output_path = script_dir / output_file

        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(self.map_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            self.errors.append(f"Ошибка сохранения JSON: {e}")
            return False

    def get_map_data(self) -> Optional[Dict]:
        """Возвращает данные карты"""
        return self.map_data

    def get_errors(self) -> List[str]:
        """Возвращает список ошибок"""
        return self.errors



# Для обратной совместимости (если кто-то запускает напрямую)
if __name__ == "__main__":
    loader = ExcelLoader()
    if loader.load():
        print(f"✅ Карта загружена: {loader.map_data['width']}x{loader.map_data['height']}")
        if loader.save_to_json():
            print("✅ Данные сохранены в map_data.json")
    else:
        print("❌ Ошибки загрузки:")
        for err in loader.get_errors():
            print(f"  - {err}")