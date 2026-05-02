import starter
import simulation.map_viewer

from excelloader.excelloader import ExcelLoader
from excelloader.mapvalidator import MapValidator

def validation(file_name):
    # Загрузка
    loader = ExcelLoader(folder_name='maps', file_name=file_name)
    if not loader.load():
        print("❌ Ошибки:", loader.get_errors())
        return 1
    map_data = loader.get_map_data()
    print(f"✅ {map_data['width']}×{map_data['height']}")
    loader.save_to_json('map_data.json')

    # Валидация
    validator = MapValidator(map_data, auto_fix=True)
    validator.validate()
    report = validator.get_report()
    print(f"✅ {'Валидно' if report['is_valid'] else 'Есть ошибки'} | Исправлено: {report['fixed_count']}")

if __name__ == "__main__":
   # starter.main()
   validation(file_name = 'map1.xlsx')
   simulation.map_viewer.main(json_path="A:\projects in programming\python\AgentStorage\AgentStorageDiplom\excelloader\map_data_fixed.json")

