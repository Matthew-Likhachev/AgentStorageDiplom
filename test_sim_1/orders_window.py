"""
orders_window.py
Оверлейная панель заказов, рисуемая ПОВЕРХ основного экрана до pygame.display.flip().
Открывается клавишей O. Поддерживает раскрывающиеся списки и прокрутку колесом.
"""
import pygame
from typing import Dict, Any, Optional, Tuple

PRODUCT_COLORS = {0:(50,205,50), 1:(30,144,255), 2:(255,215,0)}
PRODUCT_NAMES  = {0:"Зел", 1:"Синий", 2:"Жёлт"}
STATUS_COLORS  = {'pending':(160,160,160),'in_progress':(80,200,120),
                  'completing':(255,180,50),'completed':(90,90,90)}
STATUS_LABELS  = {'pending':'ожидает','in_progress':'выполн.',
                  'completing':'завершает','completed':'готов'}

WIN_W  = 500
ROW_H  = 24
INDENT = 14
PAD    = 8


class OrdersWindow:
    def __init__(self):
        self.visible    = False
        self._state:    Dict[str, Any] = {}
        self._surface:  Optional[pygame.Surface] = None
        self._scroll_y  = 0
        self._content_h = 0
        self._ox = 0
        self._oy = 10
        self._hit_zones: Dict = {}

        self._exp_packer: Dict[str, bool] = {}
        self._exp_order:  Dict[int,  bool] = {}
        self._exp_sub:    Dict[int,  bool] = {}

        self._fonts_ok = False

    def _init_fonts(self):
        if self._fonts_ok: return
        b = 14
        self.ft  = pygame.font.Font(None, b+8)
        self.fh  = pygame.font.Font(None, b+4)
        self.fb  = pygame.font.Font(None, b+2)
        self.fs  = pygame.font.Font(None, b)
        self._fonts_ok = True

    # ── публичный API ─────────────────────────────────────────────────────────
    def toggle(self):
        self.visible = not self.visible

    def on_state_change(self, state: Dict[str, Any]):
        self._state = state

    def handle_event(self, event):
        """Принимает pygame-события, отфильтрованные из основного цикла."""
        if not self.visible:
            return
        if event.type == pygame.MOUSEWHEEL:
            max_s = max(0, self._content_h - self._win_h() + 50)
            self._scroll_y = max(0, min(max_s, self._scroll_y - event.y * 18))
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._handle_click(event.pos)

    def _handle_click(self, pos):
        wx = pos[0] - self._ox
        wy = pos[1] - self._oy - 38 + self._scroll_y
        for key, (rx, ry, rw, rh) in self._hit_zones.items():
            if rx <= wx <= rx+rw and ry <= wy <= ry+rh:
                kind, val = key[0], key[1]
                if kind == 'p': self._exp_packer[val] = not self._exp_packer.get(val, True)
                elif kind == 'o': self._exp_order[val] = not self._exp_order.get(val, True)
                elif kind == 's': self._exp_sub[val]   = not self._exp_sub.get(val, False)
                break

    # ── отрисовка (вызывать ДО display.flip) ─────────────────────────────────
    def update(self):
        if not self.visible:
            return
        self._init_fonts()
        ds = pygame.display.get_surface()
        if ds is None:
            return

        win_h      = self._win_h()
        self._ox   = ds.get_width() - WIN_W - 6
        self._oy   = 10

        if self._surface is None or self._surface.get_size() != (WIN_W, win_h):
            self._surface = pygame.Surface((WIN_W, win_h), pygame.SRCALPHA)

        surf = self._surface
        surf.fill((20, 20, 32, 230))

        # Заголовок
        pygame.draw.rect(surf, (40,40,70), (0, 0, WIN_W, 38))
        surf.blit(self.ft.render("📋 Заказы", True, (255,255,255)), (PAD, 8))
        hint = self.fs.render("O — закрыть  |  колесо — прокрутка  |  клик — раскрыть", True, (140,140,160))
        surf.blit(hint, (WIN_W - hint.get_width() - PAD, 12))

        # Контент
        content_surf = pygame.Surface((WIN_W, max(win_h, 4000)), pygame.SRCALPHA)
        content_surf.fill((0,0,0,0))

        y = 0
        self._hit_zones = {}
        disp = self._state.get('dispatcher', {})
        ob   = disp.get('orders_by_packer', {})
        ai   = disp.get('active_info', {})

        if not ob:
            content_surf.blit(self.fb.render("Нет активных заказов", True, (140,140,140)), (PAD,10))
            y = 40
        else:
            for pid_str in sorted(ob.keys(), key=lambda x: int(x)):
                orders = ob.get(pid_str, {}).get('orders', [])
                y = self._draw_packer(content_surf, pid_str, orders, ai.get(pid_str,{}), y)
                y += 4

        self._content_h = y

        # Скроллируем контент в surf
        max_s = max(0, y - (win_h - 38))
        self._scroll_y = min(self._scroll_y, max_s)
        surf.blit(content_surf, (0, 38), (0, self._scroll_y, WIN_W, win_h - 38))

        # Скроллбар
        if y > win_h - 38:
            vh  = win_h - 38
            bh  = max(20, int(vh * vh / y))
            by  = int(self._scroll_y / y * vh)
            pygame.draw.rect(surf, (50,50,70),   (WIN_W-5, 38, 5, vh))
            pygame.draw.rect(surf, (120,120,150), (WIN_W-5, 38+by, 5, bh))

        # Рамка
        pygame.draw.rect(surf, (80,80,120), (0,0,WIN_W,win_h), 2, border_radius=4)

        ds.blit(surf, (self._ox, self._oy))

    def _win_h(self):
        ds = pygame.display.get_surface()
        return (ds.get_height() - 20) if ds else 600

    # ── блоки рендера ─────────────────────────────────────────────────────────
    def _blit(self, surf, text_surf, x, y):
        surf.blit(text_surf, (x, y))

    def _draw_packer(self, surf, pid_str, orders, ai, y):
        pid      = int(pid_str)
        expanded = self._exp_packer.get(pid_str, True)
        act_oid  = ai.get('order_id')
        total    = ai.get('slots_total', 0)
        free     = ai.get('slots_free',  0)

        pygame.draw.rect(surf, (50,50,90), (0, y, WIN_W, ROW_H+2))
        pygame.draw.rect(surf, (80,80,130),(0, y, WIN_W, ROW_H+2), 1)

        arr = "▼" if expanded else "▶"
        lbl = f"{arr}  Фасовщик {pid}"
        if act_oid: lbl += f"   Заказ #{act_oid}"
        surf.blit(self.fh.render(lbl, True, (255,220,100)), (PAD, y+5))

        sl = self.fs.render(f"{total-free}/{total} слотов", True, (180,180,180))
        surf.blit(sl, (WIN_W - sl.get_width() - PAD, y+7))
        self._hit_zones[('p', pid_str)] = (0, y, WIN_W, ROW_H+2)
        y += ROW_H + 4

        if not expanded:
            return y

        # Группируем по order_id
        by_order: Dict[int, list] = {}
        for oi in orders:
            by_order.setdefault(oi['order_id'], []).append(oi)

        for oid, subs in sorted(by_order.items()):
            y = self._draw_order(surf, oid, subs, act_oid, ai.get('suborder_id'), y)

        return y

    def _draw_order(self, surf, oid, subs, act_oid, act_sid, y):
        expanded = self._exp_order.get(oid, True)
        active   = (oid == act_oid)
        bg       = (35,70,35) if active else (30,30,50)
        pygame.draw.rect(surf, bg, (INDENT, y, WIN_W-INDENT, ROW_H))

        arr    = "▼" if expanded else "▶"
        marker = "★ " if active else "   "
        col    = (140,255,140) if active else (190,190,190)
        ns_tot = sum(oi.get('num_shelves',0) for oi in subs)
        ni_tot = sum(oi.get('num_items',0)   for oi in subs)
        lbl = f"{arr} {marker}Заказ #{oid}  [{len(subs)} подзаказов  {ns_tot}п/{ni_tot}тов]"
        surf.blit(self.fb.render(lbl, True, col), (INDENT+PAD, y+4))
        self._hit_zones[('o', oid)] = (INDENT, y, WIN_W-INDENT, ROW_H)
        y += ROW_H + 2

        if expanded:
            for oi in subs:
                y = self._draw_sub(surf, oi, act_sid, y)
        return y

    def _draw_sub(self, surf, oi, act_sid, y):
        sid      = oi.get('suborder_id', 0)
        active   = oi.get('active', False)
        status   = oi.get('status', 'pending')
        expanded = self._exp_sub.get(sid, False)
        bg       = (25,55,25) if active else (25,25,40)
        pygame.draw.rect(surf, bg, (INDENT*2, y, WIN_W-INDENT*2, ROW_H))

        scol = STATUS_COLORS.get(status, (130,130,130))
        pygame.draw.rect(surf, scol, (INDENT*2, y, 4, ROW_H))

        arr    = "▼" if expanded else "▶"
        mk     = "▶ " if active else "   "
        col    = (110,255,110) if active else (170,170,170)
        ns     = oi.get('num_shelves','?')
        ni     = oi.get('num_items','?')
        slbl   = STATUS_LABELS.get(status, status)
        lbl    = f"{arr} {mk}Под#{sid}  {ns}п/{ni}тов  [{slbl}]"
        surf.blit(self.fb.render(lbl, True, col), (INDENT*2+PAD+4, y+4))
        self._hit_zones[('s', sid)] = (INDENT*2, y, WIN_W-INDENT*2, ROW_H)
        y += ROW_H + 1

        if expanded:
            items = oi.get('items', {})
            if items:
                pygame.draw.rect(surf, (18,18,30), (INDENT*2, y, WIN_W-INDENT*2, ROW_H))
                ox = INDENT*3
                for pid2, qty in sorted(items.items()):
                    pcol = PRODUCT_COLORS.get(int(pid2),(200,200,200))
                    pygame.draw.rect(surf, pcol, (ox, y+5, 11, 11))
                    pygame.draw.rect(surf, (0,0,0),(ox, y+5, 11, 11),1)
                    t = self.fs.render(f"{PRODUCT_NAMES.get(int(pid2),'?')}×{qty}",
                                       True,(210,210,210))
                    surf.blit(t, (ox+14, y+5))
                    ox += t.get_width()+28
                    if ox > WIN_W-40:
                        ox = INDENT*3; y += ROW_H-6
                y += ROW_H
        return y