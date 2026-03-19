from pathlib import Path
import json
from typing import Dict, List, Optional, Tuple


class MapValidator:
    """Валидатор данных карты"""

    def __init__(self, map_data: Dict, auto_fix: bool = True):
        self.map_data = map_data
        self.auto_fix = auto_fix
        self.errors = []
        self.warnings = []
        self.fixed_count = 0

        self.width = map_data.get('width', 0)
        self.height = map_data.get('height', 0)
        self.start_row = map_data.get('start_row', 1)
        self.start_col = map_data.get('start_col', 1)
        self.cells = map_data.get('cells', [])

        self.COLOR_MAP = {
            '000000': 'E', 'FF0000': 'W', 'FFFFFF': 'F',
            'FF9900': 'S', '980000': 'P', '0000FF': 'C',
            '7030A0': 'Z', 'FFFF00': 'B', '00B050': 'A',
        }

        self.DIRECTIONS = ['n', 'w', 's', 'e']
        self.DIR_OFFSETS = {'n': (-1, 0), 's': (1, 0), 'w': (0, -1), 'e': (0, 1)}

    def parse_cell_text(self, cell_info: Dict) -> Tuple[Optional[Dict], Optional[str]]:
        """Парсит текст ячейки"""
        text = cell_info.get('text')
        row, col = cell_info['row'], cell_info['col']

        if text is None or not isinstance(text, str):
            return None, f"Ячейка ({row},{col}) пустая или содержит не текст"

        parts = text.split('|')
        if len(parts) != 4:
            return None, f"Ячейка ({row},{col}): неверный формат. Ожидалось 4 части, получено {len(parts)}"

        cell_type, loaded_dirs, empty_dirs, logic_str = parts

        if len(cell_type) != 1 or cell_type not in 'EFWSPCZBA':
            return None, f"Ячейка ({row},{col}): неверный тип '{cell_type}'"

        VALID_DIR_CHARS = set('nwse-')

        if len(loaded_dirs) > 7:
            return None, f"Ячейка ({row},{col}): неверная длина направлений с грузом '{loaded_dirs}'"

        if len(empty_dirs) > 7:
            return None, f"Ячейка ({row},{col}): неверная длина направлений без груза '{empty_dirs}'"

        if not all(c in VALID_DIR_CHARS for c in loaded_dirs):
            return None, f"Ячейка ({row},{col}): недопустимые символы в направлениях с грузом '{loaded_dirs}'"

        if not all(c in VALID_DIR_CHARS for c in empty_dirs):
            return None, f"Ячейка ({row},{col}): недопустимые символы в направлениях без груза '{empty_dirs}'"

        loaded = {'n': '-', 'w': '-', 's': '-', 'e': '-'}
        empty = {'n': '-', 'w': '-', 's': '-', 'e': '-'}

        for dir_char in self.DIRECTIONS:
            if dir_char in loaded_dirs:
                loaded[dir_char] = dir_char
            if dir_char in empty_dirs:
                empty[dir_char] = dir_char

        try:
            logic = json.loads(logic_str) if logic_str and logic_str != '{}' else {}
        except json.JSONDecodeError:
            return None, f"Ячейка ({row},{col}): неверный JSON '{logic_str}'"

        return {
            'type': cell_type,
            'loaded_dirs': loaded,
            'empty_dirs': empty,
            'logic': logic,
            'row': row,
            'col': col,
            'original_text': text
        }, None

    def format_directions(self, dirs_dict: Dict) -> str:
        """Преобразует словарь направлений в строку"""
        result = []
        for dir_char in self.DIRECTIONS:
            result.append(dirs_dict.get(dir_char, '-'))
            result.append('-')
        return ''.join(result[:-1])

    def update_cell_text(self, cell_info: Dict, cell_data: Dict) -> str:
        """Обновляет текст ячейки"""
        loaded_str = self.format_directions(cell_data['loaded_dirs'])
        empty_str = self.format_directions(cell_data['empty_dirs'])
        logic_str = json.dumps(cell_data['logic'], ensure_ascii=False) if cell_data['logic'] else '{}'

        new_text = f"{cell_data['type']}|{loaded_str}|{empty_str}|{logic_str}"

        r_idx = cell_info['row'] - self.start_row
        c_idx = cell_info['col'] - self.start_col
        self.cells[r_idx][c_idx]['text'] = new_text
        self.fixed_count += 1

        return new_text

    def get_cell_info(self, row: int, col: int) -> Optional[Dict]:
        """Получает информацию о ячейке"""
        r_idx = row - self.start_row
        c_idx = col - self.start_col
        if 0 <= r_idx < self.height and 0 <= c_idx < self.width:
            return self.cells[r_idx][c_idx]
        return None

    def get_neighbor(self, row: int, col: int, direction: str) -> Optional[Dict]:
        """Возвращает соседнюю ячейку"""
        dr, dc = self.DIR_OFFSETS[direction]
        new_row, new_col = row + dr, col + dc

        cell_info = self.get_cell_info(new_row, new_col)
        if cell_info:
            cell_data, _ = self.parse_cell_text(cell_info)
            return cell_data
        return None

    def validate_cell_format(self) -> int:
        """Проверяет формат ячеек"""
        valid_count = 0

        for row_data in self.cells:
            for cell_info in row_data:
                cell_data, error = self.parse_cell_text(cell_info)

                if error:
                    self.errors.append(error)
                else:
                    valid_count += 1
                    color = cell_info.get('color')
                    expected_type = self.COLOR_MAP.get(color)

                    if expected_type and cell_data['type'] != expected_type:
                        self.warnings.append(
                            f"Ячейка ({cell_info['row']},{cell_info['col']}): тип '{cell_data['type']}' не соответствует цвету {color}"
                        )

        return valid_count

    def validate_movement_rules(self):
        """Проверяет и исправляет правила движения"""
        for row_data in self.cells:
            for cell_info in row_data:
                cell_data, _ = self.parse_cell_text(cell_info)
                if not cell_data:
                    continue

                row, col = cell_info['row'], cell_info['col']
                modified = False

                for direction in self.DIRECTIONS:
                    neighbor = self.get_neighbor(row, col, direction)
                    if not neighbor:
                        continue

                    neighbor_type = neighbor['type']
                    fixed = False

                    # Нельзя в черные (E) и красные (W)
                    if neighbor_type in ['E', 'W']:
                        if cell_data['loaded_dirs'][direction] != '-':
                            if self.auto_fix:
                                cell_data['loaded_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(
                                    f"Ячейка ({row},{col}): разрешено движение с грузом в {neighbor_type}")

                        if cell_data['empty_dirs'][direction] != '-':
                            if self.auto_fix:
                                cell_data['empty_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(
                                    f"Ячейка ({row},{col}): разрешено движение без груза в {neighbor_type}")

                    # Нельзя с грузом в полки (S) и зарядки (C)
                    if neighbor_type in ['S', 'C']:
                        if cell_data['loaded_dirs'][direction] != '-':
                            if self.auto_fix:
                                cell_data['loaded_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(
                                    f"Ячейка ({row},{col}): разрешено движение с грузом в {neighbor_type}")

                    # Нельзя в фасовщика (P)
                    if neighbor_type == 'P':
                        if cell_data['loaded_dirs'][direction] != '-':
                            if self.auto_fix:
                                cell_data['loaded_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(f"Ячейка ({row},{col}): разрешён заезд с грузом в фасовщика")

                        if cell_data['empty_dirs'][direction] != '-':
                            if self.auto_fix:
                                cell_data['empty_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(f"Ячейка ({row},{col}): разрешён заезд без груза в фасовщика")

                    # Нельзя без груза в зону заказа (Z, slot 1-10)
                    if neighbor_type == 'Z':
                        slot = neighbor['logic'].get('slot', 0)
                        if 1 <= slot <= 10:
                            if cell_data['empty_dirs'][direction] != '-':
                                if self.auto_fix:
                                    cell_data['empty_dirs'][direction] = '-'
                                    modified = True
                                    fixed = True
                                else:
                                    self.errors.append(f"Ячейка ({row},{col}): разрешён заезд без груза в зону заказа")

                if modified and self.auto_fix:
                    self.update_cell_text(cell_info, cell_data)

    def validate_special_rules(self) -> Dict:
        """Проверяет специальные правила (уникальность ID)"""
        packer_ids, charger_ids, shelf_ids = set(), set(), set()

        for row_data in self.cells:
            for cell_info in row_data:
                cell_data, _ = self.parse_cell_text(cell_info)
                if not cell_data:
                    continue

                logic = cell_data['logic']
                cell_type = cell_data['type']
                row, col = cell_info['row'], cell_info['col']

                if cell_type == 'P' and 'packer' in logic:
                    pid = logic['packer']
                    if pid in packer_ids:
                        self.errors.append(f"Ячейка ({row},{col}): дублирующийся packer={pid}")
                    packer_ids.add(pid)

                if cell_type == 'C' and 'charger' in logic:
                    cid = logic['charger']
                    if cid in charger_ids:
                        self.errors.append(f"Ячейка ({row},{col}): дублирующийся charger={cid}")
                    charger_ids.add(cid)

                if cell_type == 'S' and 'shelf' in logic:
                    sid = logic['shelf']
                    if sid in shelf_ids:
                        self.errors.append(f"Ячейка ({row},{col}): дублирующийся shelf={sid}")
                    shelf_ids.add(sid)

        return {
            'packers': len(packer_ids),
            'chargers': len(charger_ids),
            'shelves': len(shelf_ids)
        }

    def save_fixed_data(self, output_file: str = 'map_data_fixed.json') -> bool:
        """Сохраняет исправленные данные"""
        if self.auto_fix and self.fixed_count > 0:
            self.map_data['cells'] = self.cells
            script_dir = Path(__file__).parent
            output_path = script_dir / output_file

            try:
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(self.map_data, f, ensure_ascii=False, indent=2)
                return True
            except Exception:
                return False
        return False

    def validate(self) -> bool:
        """Запускает полную валидацию"""
        valid_count = self.validate_cell_format()
        self.validate_movement_rules()
        stats = self.validate_special_rules()
        self.save_fixed_data()

        return len(self.errors) == 0

    def get_report(self) -> Dict:
        """Возвращает отчет о валидации"""
        return {
            'errors': self.errors,
            'warnings': self.warnings,
            'fixed_count': self.fixed_count,
            'is_valid': len(self.errors) == 0
        }


# Для обратной совместимости
if __name__ == "__main__":
    script_dir = Path(__file__).parent
    input_path = script_dir / 'map_data.json'

    if not input_path.exists():
        print("❌ Файл map_data.json не найден")
        exit(1)

    with open(input_path, 'r', encoding='utf-8') as f:
        map_data = json.load(f)

    validator = MapValidator(map_data, auto_fix=True)
    is_valid = validator.validate()
    report = validator.get_report()

    print(f"\n✅ Валидация завершена")
    print(f"   Ошибок: {len(report['errors'])}")
    print(f"   Предупреждений: {len(report['warnings'])}")
    print(f"   Исправлено: {report['fixed_count']} ячеек")