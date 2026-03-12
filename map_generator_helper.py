#полки через одну
start_id = 99  #номер начальной клетки
columns_left = 10    #количество рядов полок слева направо
rows = 9    #количество полок в одном ряду
step_left = 1   #шаг между колоннами

temp_id = 0
result = []
for row in range(rows):
    line = ['S|---|n-w-s-e|{"shelf_id":'+str(start_id+i)+'}' for i in range (row,columns_left*rows,rows)]
    result.append(line)

for el in result:
    empty = ';'*(step_left+1)
    print(empty.join(el))
