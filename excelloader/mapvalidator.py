from pathlib import Path
import json

# --- НАСТРОЙКИ ---
INPUT_FILE = 'map_data.json'
OUTPUT_FILE = 'map_data_fixed.json'  # Файл с исправленными данными
AUTO_FIX = True  # Включить автоисправление
# -----------------

script_dir = Path(__file__).parent
input_path = script_dir / INPUT_FILE
output_path = script_dir / OUTPUT_FILE

if not input_path.exists():
    print(f"❌ Ошибка: Файл данных не найден: {input_path}")
    print("   Сначала запустите load_map.py для создания файла данных.")
    exit()


class MapValidator:
    def __init__(self, map_data, auto_fix=True):
        self.map_data = map_data
        self.width = map_data['width']
        self.height = map_data['height']
        self.start_row = map_data['start_row']
        self.start_col = map_data['start_col']
        self.cells = map_data['cells']
        self.errors = []
        self.warnings = []
        self.fixed_count = 0
        self.auto_fix = auto_fix

        self.COLOR_MAP = {
            '000000': 'E', 'FF0000': 'W', 'FFFFFF': 'F',
            'FF9900': 'S', '980000': 'P', '0000FF': 'C',
            '7030A0': 'Z', 'FFFF00': 'B', '00B050': 'A',
        }

        self.DIRECTIONS = ['n', 'w', 's', 'e']
        self.DIR_OFFSETS = {'n': (-1, 0), 's': (1, 0), 'w': (0, -1), 'e': (0, 1)}

    def parse_cell_text(self, cell_info):
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

    def format_directions(self, dirs_dict):
        """Преобразует словарь направлений обратно в строку формата n-w-s-e"""
        result = []
        for dir_char in self.DIRECTIONS:
            result.append(dirs_dict.get(dir_char, '-'))
            result.append('-')
        return ''.join(result[:-1])  # Убираем последний дефис

    def update_cell_text(self, cell_info, cell_data):
        """Обновляет текст ячейки в исходных данных"""
        loaded_str = self.format_directions(cell_data['loaded_dirs'])
        empty_str = self.format_directions(cell_data['empty_dirs'])
        logic_str = json.dumps(cell_data['logic'], ensure_ascii=False) if cell_data['logic'] else '{}'

        new_text = f"{cell_data['type']}|{loaded_str}|{empty_str}|{logic_str}"

        # Находим и обновляем ячейку в исходных данных
        r_idx = cell_info['row'] - self.start_row
        c_idx = cell_info['col'] - self.start_col
        self.cells[r_idx][c_idx]['text'] = new_text
        self.fixed_count += 1

        return new_text

    def get_cell_info(self, row, col):
        """Получает информацию о ячейке по координатам"""
        r_idx = row - self.start_row
        c_idx = col - self.start_col
        if 0 <= r_idx < self.height and 0 <= c_idx < self.width:
            return self.cells[r_idx][c_idx]
        return None

    def get_neighbor(self, row, col, direction):
        """Возвращает соседнюю ячейку"""
        dr, dc = self.DIR_OFFSETS[direction]
        new_row, new_col = row + dr, col + dc

        if not (self.start_row <= new_row < self.start_row + self.height and
                self.start_col <= new_col < self.start_col + self.width):
            return None

        cell_info = self.get_cell_info(new_row, new_col)
        if cell_info:
            cell_data, _ = self.parse_cell_text(cell_info)
            return cell_data
        return None

    def validate_cell_format(self):
        print("🔍 Проверка формата ячеек...")
        valid_count = 0

        for row_idx, row_data in enumerate(self.cells):
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

        print(f"   ✅ Проверено {valid_count} ячеек")

    def validate_movement_rules(self):
        """Проверяет и ИСПРАВЛЯЕТ правила движения"""
        print("🔍 Проверка правил движения..." + (" (с автоисправлением)" if self.auto_fix else ""))

        for row_idx, row_data in enumerate(self.cells):
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

                    # 1. Нельзя в черные (E) и красные (W)
                    if neighbor_type in ['E', 'W']:
                        if cell_data['loaded_dirs'][direction] != '-':
                            msg = f"Ячейка ({row},{col}): закрыто движение с грузом в {neighbor_type} ({neighbor['row']},{neighbor['col']}) направление {direction}"
                            if self.auto_fix:
                                cell_data['loaded_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(msg.replace("закрыто", "разрешено"))

                        if cell_data['empty_dirs'][direction] != '-':
                            msg = f"Ячейка ({row},{col}): закрыто движение без груза в {neighbor_type} ({neighbor['row']},{neighbor['col']}) направление {direction}"
                            if self.auto_fix:
                                cell_data['empty_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(msg.replace("закрыто", "разрешено"))

                    # 2. Нельзя с грузом в полки (S) и зарядки (C)
                    if neighbor_type in ['S', 'C']:
                        if cell_data['loaded_dirs'][direction] != '-':
                            msg = f"Ячейка ({row},{col}): закрыто движение С ГРУЗОМ в {neighbor_type} ({neighbor['row']},{neighbor['col']}) направление {direction}"
                            if self.auto_fix:
                                cell_data['loaded_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(msg.replace("закрыто", "разрешено"))

                    # 3. Нельзя в фасовщика (P)
                    if neighbor_type == 'P':
                        if cell_data['loaded_dirs'][direction] != '-':
                            msg = f"Ячейка ({row},{col}): закрыт заезд с грузом в фасовщика P ({neighbor['row']},{neighbor['col']}) направление {direction}"
                            if self.auto_fix:
                                cell_data['loaded_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(msg.replace("закрыт", "разрешён"))

                        if cell_data['empty_dirs'][direction] != '-':
                            msg = f"Ячейка ({row},{col}): закрыт заезд без груза в фасовщика P ({neighbor['row']},{neighbor['col']}) направление {direction}"
                            if self.auto_fix:
                                cell_data['empty_dirs'][direction] = '-'
                                modified = True
                                fixed = True
                            else:
                                self.errors.append(msg.replace("закрыт", "разрешён"))

                    # 4. Нельзя без груза в зону заказа (Z, slot 1-10)
                    if neighbor_type == 'Z':
                        slot = neighbor['logic'].get('slot', 0)
                        if 1 <= slot <= 10:
                            if cell_data['empty_dirs'][direction] != '-':
                                msg = f"Ячейка ({row},{col}): закрыт заезд БЕЗ ГРУЗА в зону заказа Z slot={slot} ({neighbor['row']},{neighbor['col']}) направление {direction}"
                                if self.auto_fix:
                                    cell_data['empty_dirs'][direction] = '-'
                                    modified = True
                                    fixed = True
                                else:
                                    self.errors.append(msg.replace("закрыт", "разрешён"))

                    if fixed:
                        print(f"   🔧 Исправлено: {msg}")

                # Сохраняем изменения в ячейке
                if modified and self.auto_fix:
                    self.update_cell_text(cell_info, cell_data)

    def validate_special_rules(self):
        print("🔍 Проверка специальных правил...")

        packer_ids, charger_ids, shelf_ids = set(), set(), set()

        for row_data in self.cells:
            for cell_info in row_data:
                cell_data, _ = self.parse_cell_text(cell_info)
                if not cell_data:
                    continue

                logic = cell_data['logic']
                cell_type = cell_data['type']
                row, col = cell_info['row'], cell_info['col']

                if cell_type == 'P' and 'packer_id' in logic:
                    pid = logic['packer_id']
                    if pid in packer_ids:
                        self.errors.append(f"Ячейка ({row},{col}): дублирующийся packer_id={pid}")
                    packer_ids.add(pid)

                if cell_type == 'C' and 'charger_id' in logic:
                    cid = logic['charger_id']
                    if cid in charger_ids:
                        self.errors.append(f"Ячейка ({row},{col}): дублирующийся charger_id={cid}")
                    charger_ids.add(cid)

                if cell_type == 'S' and 'shelf_id' in logic:
                    sid = logic['shelf_id']
                    if sid in shelf_ids:
                        self.errors.append(f"Ячейка ({row},{col}): дублирующийся shelf_id={sid}")
                    shelf_ids.add(sid)

                if cell_type == 'P' and not logic.get('packer_id'):
                    self.errors.append(f"Ячейка ({row},{col}): фасовщик без packer_id")

        print(f"   Найдено фасовщиков: {len(packer_ids)}, зарядок: {len(charger_ids)}, полок: {len(shelf_ids)}")

    def save_fixed_data(self):
        """Сохраняет исправленные данные в новый файл"""
        if self.auto_fix and self.fixed_count > 0:
            self.map_data['cells'] = self.cells
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(self.map_data, f, ensure_ascii=False, indent=2)
            print(f"\n💾 Исправленные данные сохранены в: {output_path}")

    def validate(self):
        print("\n" + "=" * 70)
        print("ЗАПУСК ВАЛИДАТОРА КАРТЫ" + (" (АВТОИСПРАВЛЕНИЕ ВКЛЮЧЕНО)" if self.auto_fix else ""))
        print("=" * 70)

        self.validate_cell_format()
        self.validate_movement_rules()
        self.validate_special_rules()

        # Сохраняем исправления
        self.save_fixed_data()

        print("\n" + "=" * 70)
        print("РЕЗУЛЬТАТЫ ВАЛИДАЦИИ")
        print("=" * 70)

        if self.errors:
            print(f"\n❌ ОШИБКИ ({len(self.errors)}):")
            for i, error in enumerate(self.errors[:20], 1):
                print(f"   {i}. {error}")
            if len(self.errors) > 20:
                print(f"   ... и еще {len(self.errors) - 20} ошибок")

        if self.warnings:
            print(f"\n⚠️  ПРЕДУПРЕЖДЕНИЯ ({len(self.warnings)}):")
            for i, warning in enumerate(self.warnings[:10], 1):
                print(f"   {i}. {warning}")
            if len(self.warnings) > 10:
                print(f"   ... и еще {len(self.warnings) - 10} предупреждений")

        if self.auto_fix:
            print(f"\n🔧 ИСПРАВЛЕНО: {self.fixed_count} ячеек")

        if not self.errors and not self.warnings:
            print("\n✅ Карта валидна! Ошибок и предупреждений не найдено.")
        elif not self.errors:
            print(f"\n✅ Карта валидна! Найдено {len(self.warnings)} предупреждений.")
        else:
            print(f"\n❌ Карта содержит {len(self.errors)} ошибок. Требуется исправление.")

        print("=" * 70 + "\n")

        # Возвращаем True если нет критических ошибок (или всё исправлено)
        return len(self.errors) == 0 if not self.auto_fix else True


# ============================================================================
# ======================== ЗАПУСК ВАЛИДАТОРА =================================
# ============================================================================

if __name__ == "__main__":
    with open(input_path, 'r', encoding='utf-8') as f:
        map_data = json.load(f)

    print(f"📂 Загружены данные карты: {map_data['width']}x{map_data['height']}")
    print(f"🔧 Автоисправление: {'ВКЛЮЧЕНО' if AUTO_FIX else 'ВЫКЛЮЧЕНО'}\n")

    validator = MapValidator(map_data, auto_fix=AUTO_FIX)
    is_valid = validator.validate()

    if not is_valid and not AUTO_FIX:
        exit(1)