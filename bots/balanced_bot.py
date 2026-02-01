from collections import deque
from typing import Tuple, Optional, List, Dict, Any

from game_constants import FoodType, ShopCosts
from robot_controller import RobotController
from item import Pan, Plate, Food

class BotPlayer:
    def __init__(self, map_copy):
        self.map = map_copy
        self.locations = self.find_important_locations(self.map)

        # Bot assignments
        self.provider_bot_id = None
        self.assembler_bot_id = None

        # State machines
        self.provider_state = 0
        self.assembler_state = 0
        
        # Watchdog timers (Prevent infinite loops)
        self.provider_state_timer = 0
        self.last_provider_state = -1
        self.assembler_state_timer = 0
        self.last_assembler_state = -1

        # Order tracking
        self.current_order = None
        self.current_order_id = None
        self.cooked_ingredients = []
        self.cooked_queue = []
        self.simple_ingredients = []
        self.chopped_ingredients = []
        self.chop_queue = []
        self.cooked_count = 0
        self.cooked_total = 0
        self.current_chop_ingredient = None

        # Flags
        self.pan_on_cooker = False
        self.current_cooking_ingredient = None

        # Locations (Dynamic locks)
        self.active_chop_loc = None
        self.active_assemble_loc = None
        self.cached_idle_loc = None

        # Locations (Static)
        self.cooker_loc = None
        self.shop_loc = None
        self.submit_loc = None
        self.trash_loc = None

        # Deterministic Movement
        self.future_positions = {}
        self.current_positions = {}

        # Weights
        self.order_cost_weight = 0.5
        self.order_time_weight = 1.0
        self.time_simple = 1
        self.time_chop = 2
        self.time_cook = 4

    def find_important_locations(self, map_instance) -> Dict[str, List[Tuple[int, int]]]:
        locations = {
            "COOKER": [], "SINK": [], "SINKTABLE": [], "SUBMIT": [],
            "SHOP": [], "TRASH": [], "COUNTER": [], "BOX": []
        }
        for x in range(map_instance.width):
            for y in range(map_instance.height):
                tile_name = map_instance.tiles[x][y].tile_name
                if tile_name in locations:
                    locations[tile_name].append((x, y))
        return locations

    # --- DETERMINISTIC PATHFINDING ---
    def get_bfs_path(self, start: Tuple[int, int], target_x: int, target_y: int, blocked_tiles: set) -> Optional[Tuple[int, int]]:
        queue = deque([(start, None)])
        visited = {start}
        w, h = self.map.width, self.map.height

        while queue:
            (cx, cy), first_step = queue.popleft()
            if max(abs(cx - target_x), abs(cy - target_y)) <= 1:
                return first_step if first_step else (0, 0)

            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0: continue
                    nx, ny = cx + dx, cy + dy
                    if (nx, ny) in visited: continue
                    if not (0 <= nx < w and 0 <= ny < h): continue
                    if not self.map.is_tile_walkable(nx, ny): continue
                    if (nx, ny) in blocked_tiles: continue
                    
                    visited.add((nx, ny))
                    new_first = first_step if first_step else (dx, dy)
                    queue.append(((nx, ny), new_first))
        return None

    def move_towards(self, controller: RobotController, bot_id: int, target_x: int, target_y: int) -> bool:
        bx, by = self.current_positions[bot_id]

        if max(abs(bx - target_x), abs(by - target_y)) <= 1:
            self.future_positions[bot_id] = (bx, by)
            return True

        blocked = set()
        for other_id, pos in self.future_positions.items():
            if other_id != bot_id: blocked.add(pos)
        
        for other_id, pos in self.current_positions.items():
             if other_id != bot_id and other_id not in self.future_positions:
                 blocked.add(pos)

        step = self.get_bfs_path((bx, by), target_x, target_y, blocked)
        
        if step and step != (0, 0):
            if controller.move(bot_id, step[0], step[1]):
                self.future_positions[bot_id] = (bx + step[0], by + step[1])
                return False 
        
        self.future_positions[bot_id] = (bx, by)
        return False

    def get_best_counter(self, controller: RobotController, bot_id: int, require_empty: bool = True) -> Optional[Tuple[int, int]]:
        bx, by = self.current_positions[bot_id]
        team = controller.get_team()
        valid = []
        for cx, cy in self.locations.get("COUNTER", []):
            if require_empty:
                tile = controller.get_tile(team, cx, cy)
                if tile and getattr(tile, 'item', None) is None:
                    valid.append((cx, cy))
            else:
                valid.append((cx, cy))
        return min(valid, key=lambda p: abs(p[0]-bx) + abs(p[1]-by)) if valid else None

    # Helpers
    def get_food_type_by_name(self, name: str) -> Optional[FoodType]:
        for ft in FoodType:
            if ft.food_name == name: return ft
        return None

    def find_plate_counter(self, controller: RobotController, bot_id: int) -> Optional[Tuple[int, int]]:
        bx, by = self.current_positions[bot_id]
        team = controller.get_team()
        best_loc = None
        best_dist = float('inf')
        for cx, cy in self.locations.get("COUNTER", []):
            tile = controller.get_tile(team, cx, cy)
            item = getattr(tile, 'item', None)
            if isinstance(item, Plate):
                dist = abs(cx - bx) + abs(cy - by)
                if dist < best_dist:
                    best_dist = dist
                    best_loc = (cx, cy)
        return best_loc

    def get_idle_tile(self, controller: RobotController, bot_id: int) -> Optional[Tuple[int, int]]:
        if self.cached_idle_loc: return self.cached_idle_loc
        team = controller.get_team()
        critical = set(filter(None, [self.shop_loc, self.cooker_loc, self.submit_loc]))
        for x in range(self.map.width):
            for y in range(self.map.height):
                tile = controller.get_tile(team, x, y)
                if not tile or not tile.is_walkable: continue
                if any(max(abs(x - cx), abs(y - cy)) <= 1 for cx, cy in critical): continue
                self.cached_idle_loc = (x, y)
                return (x, y)
        return None

    def find_counter_food(self, controller: RobotController, bot_id: int, food_type: FoodType, require_chopped: bool) -> Optional[Tuple[int, int]]:
        bx, by = self.current_positions[bot_id]
        team = controller.get_team()
        best_loc = None
        best_dist = float('inf')
        for cx, cy in self.locations.get("COUNTER", []):
            tile = controller.get_tile(team, cx, cy)
            item = getattr(tile, 'item', None)
            if isinstance(item, Food) and item.food_name == food_type.food_name:
                if require_chopped and not item.chopped: continue
                dist = abs(cx - bx) + abs(cy - by)
                if dist < best_dist:
                    best_dist = dist
                    best_loc = (cx, cy)
        return best_loc

    def holding_is_plate(self, holding: Any) -> bool:
        return isinstance(holding, dict) and holding.get('type') == 'Plate'

    def estimate_order_cost(self, order: Dict[str, Any]) -> int:
        total = 0
        for food_name in order.get('required', []):
            ft = self.get_food_type_by_name(food_name)
            if ft: total += int(ft.buy_cost)
        return total

    def estimate_order_time(self, order: Dict[str, Any]) -> int:
        total = 0
        for food_name in order.get('required', []):
            ft = self.get_food_type_by_name(food_name)
            if not ft: continue
            total += self.time_simple
            if ft.can_chop: total += self.time_chop
            if ft.can_cook: total += self.time_cook
        return total

    def order_score(self, order: Dict[str, Any], current_turn: int) -> Tuple[float, int]:
        reward = float(order.get('reward', 0))
        cost = float(self.estimate_order_cost(order))
        time_est = float(self.estimate_order_time(order))
        score = reward - (self.order_cost_weight * cost) - (self.order_time_weight * time_est)
        time_left = max(0, order.get('expires_turn', 0) - current_turn)
        return (score, -int(time_left))

    def select_best_order(self, orders: List[Dict[str, Any]], current_turn: int) -> Optional[Dict[str, Any]]:
        active = [o for o in orders if o.get('is_active')]
        if not active: return None
        return max(active, key=lambda o: self.order_score(o, current_turn))

    def get_order_by_id(self, orders: List[Dict[str, Any]], order_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if order_id is None: return None
        for o in orders:
            if o.get('order_id') == order_id: return o
        return None

    def is_order_expired(self, order: Optional[Dict[str, Any]], current_turn: int) -> bool:
        if not order: return False
        if order.get('completed_turn') is not None: return False
        expires = order.get('expires_turn')
        return expires is not None and current_turn > int(expires)
    
    def analyze_order(self, order: Dict[str, Any]) -> None:
        if self.current_order_id == order.get('order_id'): return
        self.current_order = order
        self.current_order_id = order.get('order_id')
        self.active_chop_loc = None
        self.active_assemble_loc = None
        self.cooked_ingredients = []
        self.cooked_queue = []
        self.simple_ingredients = []
        self.chopped_ingredients = []
        self.chop_queue = []
        self.current_chop_ingredient = None
        for food_name in order['required']:
            ft = self.get_food_type_by_name(food_name)
            if not ft: continue
            if ft.can_cook: self.cooked_ingredients.append(ft)
            elif ft.can_chop: self.chopped_ingredients.append(ft)
            else: self.simple_ingredients.append(ft)
        self.cooked_count = 0
        self.cooked_total = len(self.cooked_ingredients)
        self.cooked_queue = list(self.cooked_ingredients)
        self.chop_queue = list(self.chopped_ingredients)

    def play_turn(self, controller: RobotController):
        my_bots = controller.get_team_bot_ids(controller.get_team())
        if not my_bots: return
        current_turn = controller.get_turn()

        self.future_positions = {}
        self.current_positions = {}
        for bid in my_bots:
             st = controller.get_bot_state(bid)
             self.current_positions[bid] = (st['x'], st['y'])

        self.provider_bot_id = my_bots[0]
        self.assembler_bot_id = my_bots[1] if len(my_bots) > 1 else None

        if self.shop_loc is None:
            px, py = self.current_positions[self.provider_bot_id]
            def nearest(locs):
                if not locs: return None
                return min(locs, key=lambda p: abs(p[0]-px) + abs(p[1]-py))
            self.cooker_loc = nearest(self.locations["COOKER"])
            self.shop_loc = nearest(self.locations["SHOP"])
            self.submit_loc = nearest(self.locations["SUBMIT"])
            self.trash_loc = nearest(self.locations["TRASH"])

        orders = controller.get_orders(controller.get_team())
        best_order = self.select_best_order(orders, current_turn)
        current_order_dict = self.get_order_by_id(orders, self.current_order_id)
        current_active = bool(current_order_dict and current_order_dict.get('is_active'))
        current_expired = self.is_order_expired(current_order_dict, current_turn)

        # Logic to switch orders if current is invalid
        if best_order is not None:
            should_switch = False
            if self.current_order is None:
                should_switch = True
            elif current_expired:
                should_switch = True
            elif best_order.get('order_id') != self.current_order_id:
                if not current_active:
                    should_switch = True
                else:
                    curr_score, _ = self.order_score(self.current_order, current_turn)
                    best_score, _ = self.order_score(best_order, current_turn)
                    if best_score > curr_score:
                        should_switch = True
            
            if should_switch:
                self.analyze_order(best_order)
                self.provider_state = 0
                self.assembler_state = 0

        self.play_provider_bot(controller, self.provider_bot_id, current_turn)
        if self.assembler_bot_id is not None:
            self.play_assembler_bot(controller, self.assembler_bot_id, current_turn)

    def play_provider_bot(self, controller: RobotController, bot_id: int, current_turn: int):
        # WATCHDOG: If stuck in same state for too long, reset.
        if self.provider_state == self.last_provider_state:
            self.provider_state_timer += 1
        else:
            self.provider_state_timer = 0
        self.last_provider_state = self.provider_state

        if self.provider_state_timer > 15: # 15 turn timeout
            self.provider_state = 100
            self.provider_state_timer = 0

        state = controller.get_bot_state(bot_id)
        holding = state['holding']
        team = controller.get_team()
        money = controller.get_team_money(team)
        
        def stay():
            self.future_positions[bot_id] = self.current_positions[bot_id]

        def abort_order():
            # If we are broke and stuck, drop order and reset
            self.current_order_id = None
            self.provider_state = 100
            stay()

        # Interruption logic
        if self.provider_state in {30, 31, 32, 33, 34} and not self.chop_queue and self.current_chop_ingredient is None:
            self.provider_state = 100
            stay()
            return

        # STATE MACHINE
        if self.provider_state == 0:
            if not self.cooked_ingredients:
                if self.chop_queue:
                    self.current_chop_ingredient = self.chop_queue[0]
                    self.provider_state = 30
                else:
                    self.provider_state = 100
                stay()
                return
            self.current_cooking_ingredient = self.cooked_ingredients[0]
            if self.cooker_loc:
                tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                if tile and isinstance(tile.item, Pan):
                    self.pan_on_cooker = True
                    self.provider_state = 3
                else:
                    self.provider_state = 1
            else:
                self.provider_state = 100
            stay()

        elif self.provider_state == 1: # Buy Pan
            if holding:
                self.provider_state = 2
                stay()
            elif self.shop_loc:
                if money >= ShopCosts.PAN.buy_cost:
                    if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, ShopCosts.PAN, self.shop_loc[0], self.shop_loc[1])
                else:
                    abort_order() # REPLACED "stay()" with ABORT
            else:
                stay()

        elif self.provider_state == 2: # Place Pan
            if self.cooker_loc:
                if self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                    if controller.place(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                        self.pan_on_cooker = True
                        self.provider_state = 3
            else:
                stay()

        elif self.provider_state == 3: # Buy Ingredient
            if holding:
                if self.current_cooking_ingredient and self.current_cooking_ingredient.can_chop:
                    self.provider_state = 4
                else:
                    self.provider_state = 7
                stay()
            elif self.shop_loc and self.current_cooking_ingredient:
                if money >= self.current_cooking_ingredient.buy_cost:
                    if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, self.current_cooking_ingredient, self.shop_loc[0], self.shop_loc[1])
                else:
                    abort_order() # REPLACED "stay()" with ABORT
            else:
                stay()

        elif self.provider_state == 4: # Place for Chopping
            if self.active_chop_loc is None:
                self.active_chop_loc = self.get_best_counter(controller, bot_id)
            
            if self.active_chop_loc:
                if self.move_towards(controller, bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                    if controller.place(bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                        self.provider_state = 5
                    else:
                        self.active_chop_loc = None 
            else:
                stay()

        elif self.provider_state == 5: # Chop
            if self.active_chop_loc:
                if self.move_towards(controller, bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                    if controller.chop(bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                        self.provider_state = 6
            else:
                stay()

        elif self.provider_state == 6: # Pickup Chopped
            if self.active_chop_loc:
                if self.move_towards(controller, bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                    if controller.pickup(bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                        self.active_chop_loc = None 
                        self.provider_state = 7
            else:
                stay()

        elif self.provider_state == 7: # Place in Pan
            if self.cooker_loc:
                if self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                    if controller.place(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                        self.provider_state = 8
            else:
                stay()

        elif self.provider_state == 8: # Wait for cooking
            if self.cooker_loc:
                tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                if tile and isinstance(tile.item, Pan) and tile.item.food:
                    if tile.item.food.cooked_stage == 1:
                        self.cooked_count += 1
                        self.provider_state = 9
                        stay()
                    elif tile.item.food.cooked_stage == 2: # Burnt
                        if self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                            if controller.take_from_pan(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                                self.provider_state = 99
                    else:
                        idle = self.get_idle_tile(controller, bot_id)
                        if idle: self.move_towards(controller, bot_id, idle[0], idle[1])
                        else: stay()
                else: stay()
            else: stay()

        elif self.provider_state == 9: # Transition
            if self.cooker_loc:
                tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                if tile and isinstance(tile.item, Pan) and tile.item.food is None:
                    if self.cooked_count < self.cooked_total:
                        self.current_cooking_ingredient = self.cooked_ingredients[self.cooked_count]
                        self.provider_state = 3
                    else:
                        if self.chop_queue:
                            self.current_chop_ingredient = self.chop_queue[0]
                            self.provider_state = 30
                        else:
                            self.provider_state = 100
            stay()

        elif self.provider_state == 99: # Trash
            if self.trash_loc:
                if self.move_towards(controller, bot_id, self.trash_loc[0], self.trash_loc[1]):
                    if controller.trash(bot_id, self.trash_loc[0], self.trash_loc[1]):
                        self.provider_state = 3
            else:
                stay()

        elif self.provider_state == 100: # Idle
            idle = self.get_idle_tile(controller, bot_id)
            if idle: self.move_towards(controller, bot_id, idle[0], idle[1])
            else: stay()

        elif self.provider_state == 30: # Buy Chop-only
            if holding:
                self.provider_state = 31
                stay()
            elif self.shop_loc and self.current_chop_ingredient:
                if money >= self.current_chop_ingredient.buy_cost:
                    if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, self.current_chop_ingredient, self.shop_loc[0], self.shop_loc[1])
                else:
                    abort_order() # REPLACED "stay()" with ABORT
            else:
                stay()

        elif self.provider_state == 31: # Place Chop-only
            if self.active_chop_loc is None:
                self.active_chop_loc = self.get_best_counter(controller, bot_id)

            if self.active_chop_loc:
                if self.move_towards(controller, bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                    if controller.place(bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                        self.provider_state = 32
                    else:
                        self.active_chop_loc = None
            else:
                stay()

        elif self.provider_state == 32: # Chop
            if self.active_chop_loc:
                tile = controller.get_tile(team, self.active_chop_loc[0], self.active_chop_loc[1])
                item = getattr(tile, 'item', None)
                if not isinstance(item, Food) or not item.can_chop:
                    self.active_chop_loc = None
                    self.provider_state = 31 if holding else 30
                    stay()
                    return
                if self.move_towards(controller, bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                    if controller.chop(bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                        self.provider_state = 33
            else:
                stay()

        elif self.provider_state == 33: # Pickup Chopped
            if self.active_chop_loc:
                tile = controller.get_tile(team, self.active_chop_loc[0], self.active_chop_loc[1])
                item = getattr(tile, 'item', None)
                if not isinstance(item, Food) or not item.chopped:
                    self.active_chop_loc = None
                    self.provider_state = 32
                    stay()
                    return
                if self.move_towards(controller, bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                    if controller.pickup(bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                        self.active_chop_loc = None 
                        self.provider_state = 34
            else:
                stay()

        elif self.provider_state == 34: # Handoff
            target_counter = self.get_best_counter(controller, bot_id)
            if target_counter:
                if self.move_towards(controller, bot_id, target_counter[0], target_counter[1]):
                    if controller.place(bot_id, target_counter[0], target_counter[1]):
                        if self.chop_queue:
                            self.chop_queue.pop(0)
                        if self.chop_queue:
                            self.current_chop_ingredient = self.chop_queue[0]
                            self.provider_state = 30
                        else:
                            self.provider_state = 100
            else:
                stay()
        else:
             stay()

    def play_assembler_bot(self, controller: RobotController, bot_id: int, current_turn: int):
        state = controller.get_bot_state(bot_id)
        holding = state['holding']
        team = controller.get_team()
        money = controller.get_team_money(team)

        def stay():
            self.future_positions[bot_id] = self.current_positions[bot_id]
        
        # Idle check
        if self.provider_state in {3, 4, 5, 6, 7, 30, 31, 32, 33, 34} and holding is None and self.assembler_state == 0:
            idle_tile = self.get_idle_tile(controller, bot_id)
            if idle_tile:
                self.move_towards(controller, bot_id, idle_tile[0], idle_tile[1])
            else:
                stay()
            return

        if self.assembler_state == 0: # Buy Plate
            if holding:
                self.assembler_state = 1
                stay()
            elif self.provider_state >= 7 or not self.cooked_queue:
                if self.shop_loc and money >= ShopCosts.PLATE.buy_cost:
                    if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, ShopCosts.PLATE, self.shop_loc[0], self.shop_loc[1])
                else:
                    stay() # Plate is cheap; ok to wait? Or should we abort? 
                           # Plates are 2 coins. If we can't afford that, we are dead anyway.
                           # Let's just wait.
            else:
                idle_tile = self.get_idle_tile(controller, bot_id)
                if idle_tile:
                    self.move_towards(controller, bot_id, idle_tile[0], idle_tile[1])
                else: stay()

        elif self.assembler_state == 1: # Place Plate
            if not self.holding_is_plate(holding):
                self.assembler_state = 0
                stay()
                return
            
            if self.active_assemble_loc is None:
                self.active_assemble_loc = self.get_best_counter(controller, bot_id, require_empty=True)

            if self.active_assemble_loc:
                if self.move_towards(controller, bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                    if controller.place(bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                        if self.cooked_queue:
                            self.assembler_state = 2
                        else:
                            self.assembler_state = 4
                    else:
                        self.active_assemble_loc = None 
            else:
                stay()

        elif self.assembler_state == 2: # Get Cooked
            if not self.cooked_queue:
                self.assembler_state = 4
                stay()
                return
            
            if not self.active_assemble_loc:
                 self.active_assemble_loc = self.find_plate_counter(controller, bot_id)
            
            if holding:
                self.assembler_state = 3
                stay()
                return

            if self.cooker_loc:
                if self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                    tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                    if tile and isinstance(tile.item, Pan) and tile.item.food:
                        if tile.item.food.cooked_stage == 1:
                            if controller.take_from_pan(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                                self.assembler_state = 3
            else:
                stay()

        elif self.assembler_state == 3: # Add to plate
            if self.active_assemble_loc:
                if self.move_towards(controller, bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                    if controller.add_food_to_plate(bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                        if self.cooked_queue:
                            self.cooked_queue.pop(0)
                        if self.cooked_queue:
                            self.assembler_state = 2
                        else:
                            self.assembler_state = 4
            else:
                stay()

        elif self.assembler_state == 4: # Add simple
            target_ing = None
            if self.simple_ingredients:
                target_ing = self.simple_ingredients[0]
            elif self.chopped_ingredients:
                target_ing = self.chopped_ingredients[0]
            
            if not target_ing:
                self.assembler_state = 5 
                stay()
                return

            if holding:
                if self.active_assemble_loc:
                    if self.move_towards(controller, bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                        if controller.add_food_to_plate(bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                            if self.simple_ingredients: self.simple_ingredients.pop(0)
                            elif self.chopped_ingredients: self.chopped_ingredients.pop(0)
                else: stay()
            else:
                found_loc = self.find_counter_food(controller, bot_id, target_ing, target_ing.can_chop)
                if found_loc:
                    if self.move_towards(controller, bot_id, found_loc[0], found_loc[1]):
                        controller.pickup(bot_id, found_loc[0], found_loc[1])
                elif self.shop_loc and not target_ing.can_chop and money >= target_ing.buy_cost:
                     if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, target_ing, self.shop_loc[0], self.shop_loc[1])
                else:
                    stay() # Here we might also want to abort, but simple ingredients are cheap.

        elif self.assembler_state == 5: # Submit
            if holding and self.holding_is_plate(holding):
                if self.submit_loc:
                    if self.move_towards(controller, bot_id, self.submit_loc[0], self.submit_loc[1]):
                        if controller.submit(bot_id, self.submit_loc[0], self.submit_loc[1]):
                            self.assembler_state = 0
                else: stay()
            else:
                if self.active_assemble_loc:
                     if self.move_towards(controller, bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                        controller.pickup(bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1])
                else: stay()
        else:
            stay()