"""
inventory_manager.py
Управление запасами товаров на складе.
Всего 3 вида товаров: 0=зелёный, 1=синий, 2=жёлтый.
"""

import random
from typing import Dict, List, Set, Tuple, Optional
from dataclasses import dataclass, field

# Единые цвета для 3 видов товаров (RGB)
PRODUCT_COLORS = {
    0: (50,  205,  50),   # зелёный
    1: (30,  144, 255),   # синий
    2: (255, 215,   0),   # жёлтый
}
PRODUCT_NAMES = {0: "Зелёный", 1: "Синий", 2: "Жёлтый"}
NUM_PRODUCTS = 3


@dataclass
class Product:
    product_id: int   # 0, 1 или 2
    quantity:   int


@dataclass
class Shelf:
    shelf_id: int
    products: Dict[int, int] = field(default_factory=dict)  # {product_id: qty}

    def add_product(self, product_id: int, quantity: int = 10):
        if product_id in self.products:
            self.products[product_id] = min(10, self.products[product_id] + quantity)
        else:
            self.products[product_id] = min(10, quantity)

    def remove_product(self, product_id: int, quantity: int = 1) -> bool:
        if product_id in self.products and self.products[product_id] >= quantity:
            self.products[product_id] -= quantity
            if self.products[product_id] == 0:
                del self.products[product_id]
            return True
        return False

    def has_product(self, product_id: int) -> bool:
        return product_id in self.products and self.products[product_id] > 0


class InventoryManager:
    """
    Распределяет 3 вида товаров по стеллажам.
    shelf_seed гарантирует воспроизводимость.
    """

    def __init__(self, shelf_seed: int, shelf_positions: List[Tuple[int, int]]):
        self.shelf_seed = shelf_seed
        self.shelves: Dict[Tuple[int, int], Shelf] = {}
        self.product_to_shelves: Dict[int, Set[Tuple[int, int]]] = {}
        self._initialize_inventory(shelf_positions)

    def _initialize_inventory(self, shelf_positions: List[Tuple[int, int]]):
        random.seed(self.shelf_seed)
        for pos in shelf_positions:
            shelf_id = len(self.shelves)
            shelf = Shelf(shelf_id=shelf_id)
            # Каждый стеллаж содержит 1-3 вида товара из 3
            num_types = random.randint(1, NUM_PRODUCTS)
            selected = random.sample(range(NUM_PRODUCTS), num_types)
            for pid in selected:
                qty = random.randint(3, 10)
                shelf.add_product(pid, qty)
                self.product_to_shelves.setdefault(pid, set()).add(pos)
            self.shelves[pos] = shelf

    def get_shelves_with_product(self, product_id: int) -> List[Tuple[int, int]]:
        return list(self.product_to_shelves.get(product_id, set()))

    def remove_product_from_shelf(self, pos: Tuple[int, int], product_id: int) -> bool:
        if pos in self.shelves:
            success = self.shelves[pos].remove_product(product_id)
            if success:
                self.product_to_shelves.get(product_id, set()).discard(pos)
                if not self.product_to_shelves.get(product_id):
                    self.product_to_shelves.pop(product_id, None)
            return success
        return False

    def get_shelf_products(self, pos: Tuple[int, int]) -> Dict[int, int]:
        """Возвращает {product_id: qty} для стеллажа на pos."""
        shelf = self.shelves.get(pos)
        return dict(shelf.products) if shelf else {}

    def get_inventory_state(self) -> Dict:
        return {
            'shelves': {
                str(pos): {'shelf_id': s.shelf_id, 'products': s.products}
                for pos, s in self.shelves.items()
            }
        }

    def get_total_products(self) -> int:
        return sum(sum(s.products.values()) for s in self.shelves.values())