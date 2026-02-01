from collections import deque
from typing import Tuple, Optional, List, Dict, Any, Set

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
        
        # Watchdog timers
        self.provider_state_timer = 0
        self.last_provider_state = -1
        self.assembler_state_timer = 0
        self.last_assembler_state = -1

        # Order tracking - these are the REMAINING items to process
        self.current_order = None
        self.current_order_id = None
        self.cooked_queue = []        # Cooked items still to cook (provider handles)
        self.chop_queue = []          # Chop-only items still to chop (provider handles)
        self.simple_queue = []        # Simple items still to add (assembler handles)
        self.chopped_queue = []       # Chopped items still to add (assembler handles after provider chops)
        self.cooked_count = 0         # Items that have finished cooking (provider increments)
        self.cooked_total = 0         # Total cooked items needed
        self.cooked_added_to_plate = 0  # Cooked items added to plate (assembler increments)
        self.items_on_plate = 0       # Total items added to plate
        self.current_chop_ingredient = None
        self.current_cooking_ingredient = None

        # Flags
        self.pan_on_cooker = False

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

        # Inventory awareness
        self.map_inventory: Dict[str, List[Dict]] = {}

        # Time estimates
        self.time_per_tile = 1.2
        self.time_chop = 2
        self.time_cook = 20
        self.time_pickup_place = 2

        # Pipelining - completely separate state
        self.pipeline_state = 0
        self.pipeline_chop_loc = None
        self.pipeline_ingredient = None
        self.pipeline_queue = []

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

    def scan_map_for_ingredients(self, controller: RobotController) -> Dict[str, List[Dict]]:
        team = controller.get_team()
        inventory: Dict[str, List[Dict]] = {}
        
        search_tiles = self.locations.get("COUNTER", []) + self.locations.get("BOX", [])
        
        for x, y in search_tiles:
            tile = controller.get_tile(team, x, y)
            if not tile:
                continue
            item = getattr(tile, 'item', None)
            if isinstance(item, Food):
                food_name = item.food_name
                if food_name not in inventory:
                    inventory[food_name] = []
                inventory[food_name].append({
                    'loc': (x, y),
                    'chopped': item.chopped,
                    'cooked_stage': item.cooked_stage,
                    'can_chop': item.can_chop,
                    'can_cook': item.can_cook
                })
        
        self.map_inventory = inventory
        return inventory

    def find_ingredient_on_map(self, controller: RobotController, bot_id: int, food_type: FoodType, 
                                require_chopped: bool = False) -> Optional[Tuple[int, int]]:
        bx, by = self.current_positions[bot_id]
        food_name = food_type.food_name
        
        if food_name not in self.map_inventory:
            return None
        
        best_loc = None
        best_dist = float('inf')
        
        for item_info in self.map_inventory[food_name]:
            loc = item_info['loc']
            if require_chopped and not item_info['chopped']:
                continue
            dist = abs(loc[0] - bx) + abs(loc[1] - by)
            if dist < best_dist:
                best_dist = dist
                best_loc = loc
        
        return best_loc

    def count_available_ingredients(self, order: Dict[str, Any]) -> Tuple[int, int]:
        available = 0
        total = 0
        
        for food_name in order.get('required', []):
            total += 1
            ft = self.get_food_type_by_name(food_name)
            if not ft:
                continue
            
            if food_name in self.map_inventory:
                for item_info in self.map_inventory[food_name]:
                    if ft.can_cook:
                        if item_info['cooked_stage'] == 1:
                            available += 1
                            break
                        elif ft.can_chop and item_info['chopped']:
                            available += 0.5
                            break
                    elif ft.can_chop:
                        if item_info['chopped']:
                            available += 1
                            break
                        else:
                            available += 0.5
                            break
                    else:
                        available += 1
                        break
        
        return (available, total)

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

    def get_distance_bfs(self, start: Tuple[int, int], target: Tuple[int, int]) -> int:
        if max(abs(start[0] - target[0]), abs(start[1] - target[1])) <= 1:
            return 0
        
        queue = deque([(start, 0)])
        visited = {start}
        w, h = self.map.width, self.map.height
        
        while queue:
            (cx, cy), dist = queue.popleft()
            
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0: continue
                    nx, ny = cx + dx, cy + dy
                    if (nx, ny) in visited: continue
                    if not (0 <= nx < w and 0 <= ny < h): continue
                    if not self.map.is_tile_walkable(nx, ny): continue
                    
                    if max(abs(nx - target[0]), abs(ny - target[1])) <= 1:
                        return dist + 1
                    
                    visited.add((nx, ny))
                    queue.append(((nx, ny), dist + 1))
        
        return -1

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

    def find_unchopped_food_on_counter(self, controller: RobotController, bot_id: int, food_type: FoodType) -> Optional[Tuple[int, int]]:
        bx, by = self.current_positions[bot_id]
        team = controller.get_team()
        best_loc = None
        best_dist = float('inf')
        for cx, cy in self.locations.get("COUNTER", []):
            tile = controller.get_tile(team, cx, cy)
            item = getattr(tile, 'item', None)
            if isinstance(item, Food) and item.food_name == food_type.food_name:
                if item.can_chop and not item.chopped:
                    dist = abs(cx - bx) + abs(cy - by)
                    if dist < best_dist:
                        best_dist = dist
                        best_loc = (cx, cy)
        return best_loc

    def holding_is_plate(self, holding: Any) -> bool:
        return isinstance(holding, dict) and holding.get('type') == 'Plate'

    def holding_is_food(self, holding: Any) -> bool:
        return isinstance(holding, dict) and holding.get('type') == 'Food'

    def holding_food_name(self, holding: Any) -> Optional[str]:
        if isinstance(holding, dict) and holding.get('type') == 'Food':
            return holding.get('food_name')
        return None

    def estimate_turns_to_complete(self, order: Dict[str, Any], bot_pos: Tuple[int, int]) -> float:
        total_turns = 0.0
        current_pos = bot_pos
        
        for food_name in order.get('required', []):
            ft = self.get_food_type_by_name(food_name)
            if not ft:
                continue
            
            ingredient_turns = 0.0
            existing_loc = None
            
            if food_name in self.map_inventory:
                for item_info in self.map_inventory[food_name]:
                    loc = item_info['loc']
                    if ft.can_cook and item_info['cooked_stage'] == 1:
                        existing_loc = loc
                        break
                    elif ft.can_chop and item_info['chopped'] and not ft.can_cook:
                        existing_loc = loc
                        break
                    elif not ft.can_cook and not ft.can_chop:
                        existing_loc = loc
                        break
                    elif existing_loc is None:
                        existing_loc = loc
            
            if existing_loc:
                dist = self.get_distance_bfs(current_pos, existing_loc)
                if dist >= 0:
                    ingredient_turns += dist * self.time_per_tile
                    current_pos = existing_loc
                    item_info = next((i for i in self.map_inventory[food_name] if i['loc'] == existing_loc), None)
                    if item_info:
                        if ft.can_chop and not item_info['chopped']:
                            ingredient_turns += self.time_chop + self.time_pickup_place
                        if ft.can_cook and item_info['cooked_stage'] != 1:
                            ingredient_turns += self.time_cook + self.time_pickup_place
            else:
                if self.shop_loc:
                    dist = self.get_distance_bfs(current_pos, self.shop_loc)
                    if dist >= 0:
                        ingredient_turns += dist * self.time_per_tile + self.time_pickup_place
                        current_pos = self.shop_loc
                if ft.can_chop:
                    ingredient_turns += self.time_chop + self.time_pickup_place * 2
                if ft.can_cook:
                    ingredient_turns += self.time_cook + self.time_pickup_place
            
            total_turns += ingredient_turns
        
        if self.submit_loc:
            dist = self.get_distance_bfs(current_pos, self.submit_loc)
            if dist >= 0:
                total_turns += dist * self.time_per_tile + self.time_pickup_place * 2
        
        return max(total_turns, 1.0)

    def order_score(self, order: Dict[str, Any], current_turn: int) -> float:
        reward = float(order.get('reward', 0))
        bot_pos = self.current_positions.get(self.provider_bot_id, (0, 0))
        estimated_turns = self.estimate_turns_to_complete(order, bot_pos)
        
        roi = reward / estimated_turns
        
        available, total = self.count_available_ingredients(order)
        if total > 0:
            inventory_bonus = (available / total) * 0.3
            roi *= (1 + inventory_bonus)
        
        expires_turn = order.get('expires_turn', current_turn + 100)
        turns_left = max(1, expires_turn - current_turn)
        if turns_left < estimated_turns * 1.5:
            roi *= 0.8
        elif turns_left < estimated_turns:
            roi *= 0.1
        
        return roi

    def select_best_order(self, orders: List[Dict[str, Any]], current_turn: int) -> Optional[Dict[str, Any]]:
        active = [o for o in orders if o.get('is_active')]
        if not active:
            return None
        scored_orders = [(o, self.order_score(o, current_turn)) for o in active]
        return max(scored_orders, key=lambda x: x[1])[0]

    def get_order_by_id(self, orders: List[Dict[str, Any]], order_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if order_id is None: return None
        for o in orders:
            if o.get('order_id') == order_id: return o
        return None

    def is_order_expired(self, order: Optional[Dict[str, Any]], current_turn: int) -> bool:
        """Check if order has expired (can no longer be submitted)."""
        if not order: return False
        if order.get('completed_turn') is not None: return False
        expires = order.get('expires_turn')
        # Game's is_active is: created_turn <= turn <= expires_turn
        # So order is still active ON the expiration turn, only expired AFTER
        return expires is not None and current_turn > int(expires)
    
    def should_continue_order(self, current_turn: int) -> bool:
        """Check if we should continue working on current order (not expired, enough time)."""
        if self.current_order is None:
            return False
        expires = self.current_order.get('expires_turn')
        if expires is None:
            return True
        
        turns_left = int(expires) - current_turn
        
        # Game allows submissions on the expiration turn (is_active includes it)
        # So we should work as long as turns_left >= 0
        # For complex orders, we need a buffer since they take longer
        if self.cooked_total == 0 and not self.chop_queue:
            # Simple order - can complete quickly, work until expired
            return turns_left >= 0
        elif self.cooked_total == 0:
            # Chopping but no cooking - need a bit more time
            return turns_left >= 0
        else:
            # Has cooking - need more time buffer since cooking takes 20 ticks
            return turns_left >= 0
    
    def can_switch_orders(self) -> bool:
        """Check if it's safe to switch orders without wasting work."""
        # Don't switch if assembler has placed a plate (has items on it)
        if self.items_on_plate > 0:
            return False
        # Don't switch if assembler is mid-assembly (states 2-6)
        if self.assembler_state >= 2:
            return False
        return True
    
    def analyze_order(self, order: Dict[str, Any]) -> None:
        if self.current_order_id == order.get('order_id'): 
            return
        
        self.current_order = order
        self.current_order_id = order.get('order_id')
        self.active_chop_loc = None
        self.active_assemble_loc = None
        self.cooked_queue = []
        self.chop_queue = []
        self.simple_queue = []
        self.chopped_queue = []
        self.current_chop_ingredient = None
        self.current_cooking_ingredient = None
        self.pipeline_state = 0
        self.pipeline_chop_loc = None
        self.pipeline_ingredient = None
        self.pipeline_queue = []
        self.items_on_plate = 0
        
        for food_name in order.get('required', []):
            ft = self.get_food_type_by_name(food_name)
            if not ft:
                continue
            
            if ft.can_cook:
                self.cooked_queue.append(ft)
            elif ft.can_chop:
                self.chop_queue.append(ft)
                self.chopped_queue.append(ft)
            else:
                self.simple_queue.append(ft)
        
        self.cooked_count = 0
        self.cooked_total = len(self.cooked_queue)
        self.cooked_added_to_plate = 0
        self.pipeline_queue = list(self.chop_queue)

    def has_work_to_do(self) -> bool:
        """Check if there's any work remaining for the current order."""
        # Check if there are items still to process in queues
        if self.cooked_queue or self.chop_queue or self.simple_queue or self.chopped_queue:
            return True
        # Check if there are cooked items that still need to be plated
        # (cooked_queue is emptied when cooking finishes, but assembler still needs to plate)
        if self.cooked_added_to_plate < self.cooked_total:
            return True
        return False

    def _clear_current_order(self) -> None:
        """Clear current order and all related state - called when order expires or is completed."""
        self.current_order = None
        self.current_order_id = None
        self.cooked_queue = []
        self.chop_queue = []
        self.simple_queue = []
        self.chopped_queue = []
        self.cooked_count = 0
        self.cooked_total = 0
        self.cooked_added_to_plate = 0
        self.items_on_plate = 0
        self.current_chop_ingredient = None
        self.current_cooking_ingredient = None
        self.active_chop_loc = None
        self.active_assemble_loc = None
        self.pipeline_state = 0
        self.pipeline_chop_loc = None
        self.pipeline_ingredient = None
        self.pipeline_queue = []
        # Set bots to idle
        self.provider_state = 100
        self.assembler_state = 0

    def get_total_items_needed(self) -> int:
        """Get total number of items needed for the current order."""
        return self.cooked_total + len(self.chopped_queue) + len(self.simple_queue)

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

        self.scan_map_for_ingredients(controller)

        orders = controller.get_orders(controller.get_team())
        best_order = self.select_best_order(orders, current_turn)
        current_order_dict = self.get_order_by_id(orders, self.current_order_id)
        current_active = bool(current_order_dict and current_order_dict.get('is_active'))
        current_expired = self.is_order_expired(current_order_dict, current_turn)

        # FIRST: Check if current order is no longer valid (expired or inactive)
        current_order_invalid = (self.current_order is not None and 
                                  (current_expired or not current_active))
        
        if current_order_invalid:
            # Current order expired or became inactive - must stop working on it
            if best_order is not None:
                # Switch to new order
                self.analyze_order(best_order)
                self.provider_state = 0
                self.assembler_state = 0
            else:
                # No orders available - clear current order and go idle
                self._clear_current_order()
        elif best_order is not None:
            # Current order still valid, check if we should switch to a better one
            should_switch = False
            if self.current_order is None:
                should_switch = True
            elif best_order.get('order_id') != self.current_order_id:
                if self.can_switch_orders():
                    curr_score = self.order_score(self.current_order, current_turn)
                    best_score = self.order_score(best_order, current_turn)
                    if best_score > curr_score * 1.5:
                        should_switch = True
            
            if should_switch:
                self.analyze_order(best_order)
                self.provider_state = 0
                self.assembler_state = 0

        self.play_provider_bot(controller, self.provider_bot_id, current_turn)
        if self.assembler_bot_id is not None:
            self.play_assembler_bot(controller, self.assembler_bot_id, current_turn)

    def play_provider_bot(self, controller: RobotController, bot_id: int, current_turn: int):
        if self.provider_state == self.last_provider_state:
            self.provider_state_timer += 1
        else:
            self.provider_state_timer = 0
        self.last_provider_state = self.provider_state

        if self.provider_state_timer > 20:
            self.provider_state = 100
            self.provider_state_timer = 0
            self.active_chop_loc = None

        state = controller.get_bot_state(bot_id)
        holding = state['holding']
        team = controller.get_team()
        money = controller.get_team_money(team)
        
        def stay():
            self.future_positions[bot_id] = self.current_positions[bot_id]
        
        # Check if order is still valid before doing any buying
        order_valid = self.should_continue_order(current_turn)
        if not order_valid and self.provider_state not in {100, 99}:
            # Order expired or about to expire - go idle
            # If holding something, go to state 100 which will handle it
            self.provider_state = 100
            self.active_chop_loc = None

        def abort_order():
            self.current_order_id = None
            self.current_order = None
            self.cooked_queue = []
            self.chop_queue = []
            self.simple_queue = []
            self.chopped_queue = []
            self.pipeline_queue = []
            self.provider_state = 100
            stay()

        # Interruption logic
        if self.provider_state in {30, 31, 32, 33, 34} and not self.chop_queue and self.current_chop_ingredient is None:
            self.provider_state = 100
            stay()
            return

        # STATE MACHINE
        if self.provider_state == 0:
            if self.cooked_queue:
                self.current_cooking_ingredient = self.cooked_queue[0]
                if self.cooker_loc:
                    tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                    if tile and isinstance(tile.item, Pan):
                        self.pan_on_cooker = True
                        self.provider_state = 3
                    else:
                        self.provider_state = 1
                else:
                    self.provider_state = 100
            elif self.chop_queue:
                self.current_chop_ingredient = self.chop_queue[0]
                self.provider_state = 30
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
                    stay()
            else:
                stay()

        elif self.provider_state == 2: # Place Pan
            if not holding:
                self.provider_state = 1
                stay()
                return
            if self.cooker_loc:
                if self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                    if controller.place(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                        self.pan_on_cooker = True
                        self.provider_state = 3
                    else:
                        stay()
            else:
                stay()

        elif self.provider_state == 3: # Get Ingredient for cooking
            if not self.current_cooking_ingredient:
                if self.cooked_queue:
                    self.current_cooking_ingredient = self.cooked_queue[0]
                else:
                    self.provider_state = 0
                    stay()
                    return
                
            if holding:
                if self.current_cooking_ingredient.can_chop:
                    if self.holding_is_food(holding) and holding.get('chopped', False):
                        self.provider_state = 7
                    else:
                        self.provider_state = 4
                else:
                    self.provider_state = 7
                stay()
                return
            
            if self.current_cooking_ingredient.can_chop:
                chopped_loc = self.find_counter_food(controller, bot_id, self.current_cooking_ingredient, require_chopped=True)
                if chopped_loc:
                    self.move_towards(controller, bot_id, chopped_loc[0], chopped_loc[1])
                    if max(abs(self.current_positions[bot_id][0] - chopped_loc[0]), 
                           abs(self.current_positions[bot_id][1] - chopped_loc[1])) <= 1:
                        controller.pickup(bot_id, chopped_loc[0], chopped_loc[1])
                    return
                
                unchopped_loc = self.find_unchopped_food_on_counter(controller, bot_id, self.current_cooking_ingredient)
                if unchopped_loc:
                    self.move_towards(controller, bot_id, unchopped_loc[0], unchopped_loc[1])
                    if max(abs(self.current_positions[bot_id][0] - unchopped_loc[0]), 
                           abs(self.current_positions[bot_id][1] - unchopped_loc[1])) <= 1:
                        controller.pickup(bot_id, unchopped_loc[0], unchopped_loc[1])
                    return
            else:
                existing_loc = self.find_ingredient_on_map(controller, bot_id, self.current_cooking_ingredient)
                if existing_loc:
                    self.move_towards(controller, bot_id, existing_loc[0], existing_loc[1])
                    if max(abs(self.current_positions[bot_id][0] - existing_loc[0]), 
                           abs(self.current_positions[bot_id][1] - existing_loc[1])) <= 1:
                        controller.pickup(bot_id, existing_loc[0], existing_loc[1])
                    return
            
            if self.shop_loc and self.current_cooking_ingredient:
                if money >= self.current_cooking_ingredient.buy_cost:
                    if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, self.current_cooking_ingredient, self.shop_loc[0], self.shop_loc[1])
                else:
                    stay()
            else:
                stay()

        elif self.provider_state == 4: # Place for Chopping
            if not holding:
                self.provider_state = 3
                stay()
                return
                
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
                tile = controller.get_tile(team, self.active_chop_loc[0], self.active_chop_loc[1])
                item = getattr(tile, 'item', None)
                if not isinstance(item, Food):
                    self.active_chop_loc = None
                    self.provider_state = 3
                    stay()
                    return
                if item.chopped:
                    self.provider_state = 6
                    stay()
                    return
                if self.move_towards(controller, bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                    controller.chop(bot_id, self.active_chop_loc[0], self.active_chop_loc[1])
            else:
                self.provider_state = 3
                stay()

        elif self.provider_state == 6: # Pickup Chopped
            if holding:
                self.active_chop_loc = None 
                self.provider_state = 7
                stay()
                return
                
            if self.active_chop_loc:
                if self.move_towards(controller, bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                    controller.pickup(bot_id, self.active_chop_loc[0], self.active_chop_loc[1])
            else:
                self.provider_state = 3
                stay()

        elif self.provider_state == 7: # Place in Pan
            if not holding:
                self.provider_state = 3
                stay()
                return
                
            if self.cooker_loc:
                if self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                    if controller.place(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                        self.provider_state = 8
                        self.pipeline_state = 0
                        self.pipeline_chop_loc = None
                        self.pipeline_ingredient = None
            else:
                stay()

        elif self.provider_state == 8: # Wait for cooking
            if self.cooker_loc:
                tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                if tile and isinstance(tile.item, Pan) and tile.item.food:
                    if tile.item.food.cooked_stage == 1:
                        self.cooked_count += 1
                        if self.cooked_queue:
                            self.cooked_queue.pop(0)
                        self.provider_state = 9
                        stay()
                    elif tile.item.food.cooked_stage == 2:
                        if self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                            if controller.take_from_pan(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                                self.provider_state = 99
                    else:
                        if self.pipeline_queue and not holding:
                            self._do_pipeline_work(controller, bot_id, team, money, stay)
                        else:
                            idle = self.get_idle_tile(controller, bot_id)
                            if idle: self.move_towards(controller, bot_id, idle[0], idle[1])
                            else: stay()
                elif tile and isinstance(tile.item, Pan) and tile.item.food is None:
                    self.provider_state = 3
                    stay()
                else:
                    stay()
            else:
                stay()

        elif self.provider_state == 9: # Transition after cooking
            if self.cooker_loc:
                tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                if tile and isinstance(tile.item, Pan):
                    if tile.item.food is None:
                        if self.cooked_queue:
                            self.current_cooking_ingredient = self.cooked_queue[0]
                            self.provider_state = 3
                        elif self.chop_queue:
                            self.current_chop_ingredient = self.chop_queue[0]
                            self.provider_state = 30
                        else:
                            self.provider_state = 100
                        stay()
                    else:
                        if self.pipeline_queue and not holding:
                            self._do_pipeline_work(controller, bot_id, team, money, stay)
                        else:
                            idle = self.get_idle_tile(controller, bot_id)
                            if idle: self.move_towards(controller, bot_id, idle[0], idle[1])
                            else: stay()
                else:
                    stay()
            else:
                stay()

        elif self.provider_state == 99: # Trash burnt food
            if not holding:
                self.provider_state = 3
                stay()
                return
            if self.trash_loc:
                if self.move_towards(controller, bot_id, self.trash_loc[0], self.trash_loc[1]):
                    if controller.trash(bot_id, self.trash_loc[0], self.trash_loc[1]):
                        self.provider_state = 3
            else:
                stay()

        elif self.provider_state == 100: # Idle
            if holding:
                # Place held item on counter before going idle
                target_counter = self.get_best_counter(controller, bot_id)
                if target_counter:
                    if self.move_towards(controller, bot_id, target_counter[0], target_counter[1]):
                        controller.place(bot_id, target_counter[0], target_counter[1])
                else:
                    stay()
            else:
                idle = self.get_idle_tile(controller, bot_id)
                if idle: self.move_towards(controller, bot_id, idle[0], idle[1])
                else: stay()

        elif self.provider_state == 30: # Get Chop-only ingredient
            if not self.current_chop_ingredient:
                if self.chop_queue:
                    self.current_chop_ingredient = self.chop_queue[0]
                else:
                    self.provider_state = 100
                    stay()
                    return
                    
            if holding:
                self.provider_state = 31
                stay()
                return
            
            existing_unchopped = self.find_unchopped_food_on_counter(controller, bot_id, self.current_chop_ingredient)
            if existing_unchopped:
                self.move_towards(controller, bot_id, existing_unchopped[0], existing_unchopped[1])
                if max(abs(self.current_positions[bot_id][0] - existing_unchopped[0]), 
                       abs(self.current_positions[bot_id][1] - existing_unchopped[1])) <= 1:
                    controller.pickup(bot_id, existing_unchopped[0], existing_unchopped[1])
                return
            
            if self.shop_loc and self.current_chop_ingredient:
                if money >= self.current_chop_ingredient.buy_cost:
                    if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, self.current_chop_ingredient, self.shop_loc[0], self.shop_loc[1])
                else:
                    stay()
            else:
                stay()

        elif self.provider_state == 31: # Place Chop-only
            if not holding:
                self.provider_state = 30
                stay()
                return
                
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
                if not isinstance(item, Food):
                    self.active_chop_loc = None
                    self.provider_state = 30
                    stay()
                    return
                if item.chopped:
                    self.provider_state = 33
                    stay()
                    return
                if self.move_towards(controller, bot_id, self.active_chop_loc[0], self.active_chop_loc[1]):
                    controller.chop(bot_id, self.active_chop_loc[0], self.active_chop_loc[1])
            else:
                self.provider_state = 30
                stay()

        elif self.provider_state == 33: # Done chopping - leave for assembler
            if self.chop_queue:
                self.chop_queue.pop(0)
            self.active_chop_loc = None
            
            if self.chop_queue:
                self.current_chop_ingredient = self.chop_queue[0]
                self.provider_state = 30
            else:
                self.provider_state = 100
            stay()

        elif self.provider_state == 34: # (No longer used)
            self.provider_state = 100
            stay()
            
        else:
             stay()

    def _do_pipeline_work(self, controller: RobotController, bot_id: int, team, money: int, stay_fn):
        """Pipeline: Chop items while waiting for cooking. Uses separate pipeline_queue."""
        state = controller.get_bot_state(bot_id)
        holding = state['holding']
        
        if self.pipeline_state == 0:
            if not self.pipeline_queue:
                idle = self.get_idle_tile(controller, bot_id)
                if idle: self.move_towards(controller, bot_id, idle[0], idle[1])
                else: stay_fn()
                return
            
            self.pipeline_ingredient = self.pipeline_queue[0]
            self.pipeline_state = 1
        
        if self.pipeline_state == 1:
            if holding:
                self.pipeline_state = 2
                stay_fn()
                return
            
            if self.pipeline_ingredient:
                existing_unchopped = self.find_unchopped_food_on_counter(controller, bot_id, self.pipeline_ingredient)
                if existing_unchopped:
                    self.move_towards(controller, bot_id, existing_unchopped[0], existing_unchopped[1])
                    if max(abs(self.current_positions[bot_id][0] - existing_unchopped[0]), 
                           abs(self.current_positions[bot_id][1] - existing_unchopped[1])) <= 1:
                        controller.pickup(bot_id, existing_unchopped[0], existing_unchopped[1])
                    return
                
                if self.shop_loc and money >= self.pipeline_ingredient.buy_cost:
                    if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, self.pipeline_ingredient, self.shop_loc[0], self.shop_loc[1])
                else:
                    stay_fn()
            else:
                stay_fn()
        
        elif self.pipeline_state == 2:
            if not holding:
                self.pipeline_state = 1
                stay_fn()
                return
                
            if self.pipeline_chop_loc is None:
                self.pipeline_chop_loc = self.get_best_counter(controller, bot_id)
            
            if self.pipeline_chop_loc:
                if self.move_towards(controller, bot_id, self.pipeline_chop_loc[0], self.pipeline_chop_loc[1]):
                    if controller.place(bot_id, self.pipeline_chop_loc[0], self.pipeline_chop_loc[1]):
                        self.pipeline_state = 3
                    else:
                        self.pipeline_chop_loc = None
            else:
                stay_fn()
        
        elif self.pipeline_state == 3:
            if self.pipeline_chop_loc:
                tile = controller.get_tile(team, self.pipeline_chop_loc[0], self.pipeline_chop_loc[1])
                item = getattr(tile, 'item', None)
                if not isinstance(item, Food):
                    self.pipeline_chop_loc = None
                    self.pipeline_state = 1
                    stay_fn()
                    return
                if item.chopped:
                    if self.pipeline_queue:
                        self.pipeline_queue.pop(0)
                    if self.chop_queue and self.pipeline_ingredient and self.chop_queue[0].food_name == self.pipeline_ingredient.food_name:
                        self.chop_queue.pop(0)
                    self.pipeline_state = 0
                    self.pipeline_chop_loc = None
                    self.pipeline_ingredient = None
                    stay_fn()
                    return
                if self.move_towards(controller, bot_id, self.pipeline_chop_loc[0], self.pipeline_chop_loc[1]):
                    controller.chop(bot_id, self.pipeline_chop_loc[0], self.pipeline_chop_loc[1])
            else:
                self.pipeline_state = 1
                stay_fn()

    def play_assembler_bot(self, controller: RobotController, bot_id: int, current_turn: int):
        if self.assembler_state == self.last_assembler_state:
            self.assembler_state_timer += 1
        else:
            self.assembler_state_timer = 0
        self.last_assembler_state = self.assembler_state

        if self.assembler_state_timer > 25:
            # Watchdog - stuck, reset completely
            self.assembler_state = 0
            self.assembler_state_timer = 0
            self.active_assemble_loc = None
            self.items_on_plate = 0
            self.cooked_added_to_plate = 0

        state = controller.get_bot_state(bot_id)
        holding = state['holding']
        team = controller.get_team()
        money = controller.get_team_money(team)

        def stay():
            self.future_positions[bot_id] = self.current_positions[bot_id]
        
        # Check if order is still valid
        order_valid = self.should_continue_order(current_turn)
        if not order_valid and self.assembler_state != 0:
            # Order expired - trash what we're holding and reset
            if holding:
                if self.trash_loc:
                    if self.move_towards(controller, bot_id, self.trash_loc[0], self.trash_loc[1]):
                        controller.trash(bot_id, self.trash_loc[0], self.trash_loc[1])
                    return
                else:
                    target = self.get_best_counter(controller, bot_id)
                    if target:
                        if self.move_towards(controller, bot_id, target[0], target[1]):
                            controller.place(bot_id, target[0], target[1])
                    return
            self.assembler_state = 0
            self.active_assemble_loc = None
            self.items_on_plate = 0
            self.cooked_added_to_plate = 0
        
        def trash_held_item():
            """Trash whatever we're holding and reset to state 0."""
            if self.trash_loc:
                if self.move_towards(controller, bot_id, self.trash_loc[0], self.trash_loc[1]):
                    controller.trash(bot_id, self.trash_loc[0], self.trash_loc[1])
            else:
                # No trash, try to place on counter
                target = self.get_best_counter(controller, bot_id)
                if target:
                    if self.move_towards(controller, bot_id, target[0], target[1]):
                        controller.place(bot_id, target[0], target[1])
        
        # Check provider status
        provider_cooking = self.provider_state in {8, 9}
        provider_done_cooking = self.cooked_count >= self.cooked_total
        
        # Ready for plate when cooking is happening/done or no cooking needed
        ready_for_plate = provider_cooking or provider_done_cooking or (self.cooked_total == 0)
        
        if self.assembler_state == 0:
            if holding:
                if self.holding_is_plate(holding):
                    self.assembler_state = 1
                else:
                    # Holding food - trash it, we shouldn't have random food in state 0
                    trash_held_item()
                return
            
            # Not holding anything
            if ready_for_plate and self.has_work_to_do() and order_valid:
                if self.shop_loc and money >= ShopCosts.PLATE.buy_cost:
                    if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, ShopCosts.PLATE, self.shop_loc[0], self.shop_loc[1])
                else:
                    stay()
            else:
                idle_tile = self.get_idle_tile(controller, bot_id)
                if idle_tile:
                    self.move_towards(controller, bot_id, idle_tile[0], idle_tile[1])
                else:
                    stay()

        elif self.assembler_state == 1: # Place Plate
            if not holding:
                self.assembler_state = 0
                stay()
                return
            if not self.holding_is_plate(holding):
                # Holding wrong item - trash it
                trash_held_item()
                self.assembler_state = 0
                return
            
            if self.active_assemble_loc is None:
                self.active_assemble_loc = self.get_best_counter(controller, bot_id, require_empty=True)

            if self.active_assemble_loc:
                if self.move_towards(controller, bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                    if controller.place(bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                        # Determine next state based on what's been ADDED to plate
                        if self.cooked_added_to_plate < self.cooked_total:
                            self.assembler_state = 2  # Get cooked food
                        elif self.simple_queue or self.chopped_queue:
                            self.assembler_state = 4  # Add other ingredients
                        else:
                            self.assembler_state = 5  # Submit
                    else:
                        self.active_assemble_loc = None
            else:
                stay()

        elif self.assembler_state == 2: # Get Cooked food from pan
            if self.cooked_added_to_plate >= self.cooked_total:
                if self.simple_queue or self.chopped_queue:
                    self.assembler_state = 4
                else:
                    self.assembler_state = 5
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
                            controller.take_from_pan(bot_id, self.cooker_loc[0], self.cooker_loc[1])
            else:
                stay()

        elif self.assembler_state == 3: # Add cooked food to plate
            if not holding:
                self.assembler_state = 2
                stay()
                return
                
            if self.active_assemble_loc:
                if self.move_towards(controller, bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                    result = controller.add_food_to_plate(bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1])
                    if result:
                        self.items_on_plate += 1
                        self.cooked_added_to_plate += 1
                        # Check if more cooked items to add
                        if self.cooked_added_to_plate < self.cooked_total:
                            self.assembler_state = 2
                        elif self.simple_queue or self.chopped_queue:
                            self.assembler_state = 4
                        else:
                            self.assembler_state = 5
                    else:
                        # Failed to add to plate - trash the food and try again
                        trash_held_item()
            else:
                # Lost plate location - find it
                self.active_assemble_loc = self.find_plate_counter(controller, bot_id)
                if not self.active_assemble_loc:
                    # No plate found - trash food and restart
                    trash_held_item()
                    self.assembler_state = 0
                    self.items_on_plate = 0
                    self.cooked_added_to_plate = 0

        elif self.assembler_state == 4: # Add simple/chopped ingredients
            target_ing = None
            need_chopped = False
            
            if self.simple_queue:
                target_ing = self.simple_queue[0]
                need_chopped = False
            elif self.chopped_queue:
                target_ing = self.chopped_queue[0]
                need_chopped = True
            
            if not target_ing:
                self.assembler_state = 5 
                stay()
                return

            if holding:
                if self.active_assemble_loc:
                    if self.move_towards(controller, bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                        result = controller.add_food_to_plate(bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1])
                        if result:
                            self.items_on_plate += 1
                            if not need_chopped and self.simple_queue: 
                                self.simple_queue.pop(0)
                            elif need_chopped and self.chopped_queue: 
                                self.chopped_queue.pop(0)
                        else:
                            # Failed - check if we're holding the wrong item
                            held_name = self.holding_food_name(holding)
                            if held_name != target_ing.food_name:
                                # Wrong item - trash it
                                trash_held_item()
                            else:
                                # Right item but still failed - maybe plate is full?
                                # Trash and go to submit
                                trash_held_item()
                                self.assembler_state = 5
                else:
                    # Lost plate - find it
                    self.active_assemble_loc = self.find_plate_counter(controller, bot_id)
                    if not self.active_assemble_loc:
                        trash_held_item()
                        self.assembler_state = 0
                        self.items_on_plate = 0
                        self.cooked_added_to_plate = 0
            else:
                found_loc = self.find_counter_food(controller, bot_id, target_ing, need_chopped)
                if found_loc:
                    self.move_towards(controller, bot_id, found_loc[0], found_loc[1])
                    if max(abs(self.current_positions[bot_id][0] - found_loc[0]), 
                           abs(self.current_positions[bot_id][1] - found_loc[1])) <= 1:
                        controller.pickup(bot_id, found_loc[0], found_loc[1])
                elif not need_chopped and self.shop_loc and money >= target_ing.buy_cost:
                    if self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1]):
                        controller.buy(bot_id, target_ing, self.shop_loc[0], self.shop_loc[1])
                else:
                    # Wait for provider to prepare chopped ingredient
                    stay()

        elif self.assembler_state == 5: # Submit
            if not self.active_assemble_loc:
                self.active_assemble_loc = self.find_plate_counter(controller, bot_id)
            
            # Check if there are still items that need to be added to plate
            has_more_cooked = self.cooked_added_to_plate < self.cooked_total and self.cooked_count > self.cooked_added_to_plate
            has_more_items = self.simple_queue or self.chopped_queue or has_more_cooked
                
            if holding and self.holding_is_plate(holding):
                if self.submit_loc:
                    if self.move_towards(controller, bot_id, self.submit_loc[0], self.submit_loc[1]):
                        result = controller.submit(bot_id, self.submit_loc[0], self.submit_loc[1])
                        if result:
                            self.assembler_state = 0
                            self.active_assemble_loc = None
                            self.items_on_plate = 0
                            self.cooked_added_to_plate = 0
                        else:
                            # Submit failed - plate is incomplete
                            # Check if there are items to add
                            if has_more_items:
                                # Place plate back on counter and go add items
                                self.assembler_state = 6  # New state: place plate back
                            else:
                                # No items to add but still incomplete?
                                # This shouldn't happen, but trash plate and start over
                                trash_held_item()
                                self.assembler_state = 0
                                self.items_on_plate = 0
                                self.cooked_added_to_plate = 0
                else:
                    stay()
            elif holding:
                # Holding food, not plate - trash it
                trash_held_item()
            else:
                # Need to pick up plate
                if self.active_assemble_loc:
                    self.move_towards(controller, bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1])
                    if max(abs(self.current_positions[bot_id][0] - self.active_assemble_loc[0]), 
                           abs(self.current_positions[bot_id][1] - self.active_assemble_loc[1])) <= 1:
                        result = controller.pickup(bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1])
                        if not result:
                            # Pickup failed - maybe plate is gone?
                            self.active_assemble_loc = self.find_plate_counter(controller, bot_id)
                            if not self.active_assemble_loc:
                                # No plate anywhere - start over
                                self.assembler_state = 0
                                self.items_on_plate = 0
                                self.cooked_added_to_plate = 0
                else:
                    # No plate location known - start over
                    self.assembler_state = 0
                    self.items_on_plate = 0
                    self.cooked_added_to_plate = 0
        
        elif self.assembler_state == 6: # Place plate back to add more items
            if not holding:
                # Lost the plate somehow
                self.active_assemble_loc = self.find_plate_counter(controller, bot_id)
                if self.active_assemble_loc:
                    # Plate is on counter - figure out what to add
                    if self.cooked_added_to_plate < self.cooked_total and self.cooked_count > self.cooked_added_to_plate:
                        self.assembler_state = 2
                    elif self.simple_queue or self.chopped_queue:
                        self.assembler_state = 4
                    else:
                        self.assembler_state = 5
                else:
                    # No plate - start over
                    self.assembler_state = 0
                    self.items_on_plate = 0
                    self.cooked_added_to_plate = 0
                stay()
                return
            
            # Find a counter to place the plate
            if self.active_assemble_loc is None:
                self.active_assemble_loc = self.get_best_counter(controller, bot_id, require_empty=True)
            
            if self.active_assemble_loc:
                if self.move_towards(controller, bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                    if controller.place(bot_id, self.active_assemble_loc[0], self.active_assemble_loc[1]):
                        # Plate placed - figure out what to add
                        if self.cooked_added_to_plate < self.cooked_total and self.cooked_count > self.cooked_added_to_plate:
                            self.assembler_state = 2  # Get more cooked food
                        elif self.simple_queue or self.chopped_queue:
                            self.assembler_state = 4  # Add other ingredients
                        else:
                            self.assembler_state = 5  # Try submit again
                    else:
                        self.active_assemble_loc = None
            else:
                stay()
                
        else:
            stay()
