def print_res(arr_2d):
    for el in arr_2d:
        print(''.join(el))

#полки 1 в ряду
def f_shelf1(blank,start_id,rows,columns_left,step_left):
    arr_blank = blank.split('XXX')
    result = []
    for row in range(rows):
        line = [arr_blank[0]+str(start_id+i)+arr_blank[1]+(';' * (step_left + 1)) for i in range (row,columns_left*rows,rows)]
        line[-1]= line[-1][0:(len(line[-1])-(step_left + 1))] #удаление последних символов ; в последнем элементе
        result.append(line)
    return result

#места фасовщика от одного по возрастанию
def f_Zone_asc(blank, start_id, end_id):
    arr_blank = blank.split('XXX')
    result = []
    line = [arr_blank[0]+str(i)+arr_blank[1]+';' for i in range (start_id, end_id+1)]
    line[-1] = line[-1][0:(len(line[-1]) - 1)]  # удаление последних символов ; в последнем элементе
    result.append(line)
    return result

#места фасовщика от 10 по убыванию
def f_Zone_desc(blank, start_id, end_id):
    arr_blank = blank.split('XXX')
    result = []
    line = [arr_blank[0]+str(i)+arr_blank[1]+';' for i in range (end_id, start_id-1, -1)]
    line[-1] = line[-1][0:(len(line[-1]) - 1)]  # удаление последних символов ; в последнем элементе
    result.append(line)
    return result

#blank = заготовка фразы для клетки. вместо ХХХ подставлять номер порядковый.
# Формат текста клетки:
# <Тип>|<Направления_с_грузом>|<Направления_без_груза>|<Логика>

#полки/стеллажи 1 слой в ряду, расстояние между рядами step_left, количество рядов columns_left
blank = 'S|-w--e|n-w-s-e|{"shelf_id":XXX}'
start_id = 0  #номер начальной клетки
rows = 9    #количество полок в одном ряду
columns_left = 11    #количество рядов полок слева направо
step_left = 1   #шаг между колоннами
# print_res(f_shelf1(blank,start_id,rows,columns_left,step_left))

#зона фасовщика по возрастанию
blank = 'Z|n-w-s-e|---|{"packer_id":2,"zone":"order","slot":XXX}'
start_id = 1  #номер начальной клетки
end_id = 10 #номер конечной клетки
# print_res(f_Zone_asc(blank, start_id, end_id))

#зона фасовщика по убыванию
blank = 'Z|n-w-s-e|---|{"packer_id":1,"zone":"order","slot":XXX}'
start_id = 1  #номер начальной (наименьшей по числу) клетки
end_id = 10 #номер конечной (наибольшей по числу) клетки
# print_res(f_Zone_desc(blank, start_id, end_id))

#Буферная зона фасовщика по возрастанию
blank = 'B|n-w-s-e|n-w-s-e|{"packer_id":1,"zone":"buffer","slot":XXX}'
start_id = 11  #номер начальной клетки
end_id = 20 #номер конечной клетки
# print_res(f_Zone_asc(blank, start_id, end_id))

#Буферная зона фасовщика по убыванию
blank = 'B|n-w-s-e|n-w-s-e|{"packer_id":2,"zone":"buffer","slot":XXX}'
start_id = 11  #номер начальной клетки
end_id = 20 #номер конечной клетки
print_res(f_Zone_desc(blank, start_id, end_id))

#полки/стеллажи 1 слой в ряду, расстояние между рядами step_left, количество рядов columns_left
blank = 'C|---|n-w-s-e|{"charger_id":XXX}'
start_id = 1  #номер начальной клетки
rows = 10    #количество полок в одном ряду
columns_left = 1    #количество рядов полок слева направо
step_left = 0   #шаг между колоннами
# print_res(f_shelf1(blank,start_id,rows,columns_left,step_left))


# Свободное пространство (белые клетки)
# F|n-w-s-e|n-w-s-e|{}