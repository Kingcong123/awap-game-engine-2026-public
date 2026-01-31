import sys
import os
import random
from collections import deque
from typing import Tuple, Optional, List, Dict, Any

# --- Path Setup ---
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.append(script_dir)

from game_constants import Team, TileType, FoodType, ShopCosts
from robot_controller import RobotController
from item import Pan, Plate, Food
from helpers import locations

class BotPlayer:
    def __init__(self, map_copy):
        self.map = map_copy
        # 1. Parse Locations
        self.locations = locations.find_important_locations(self.map)
        
        # 2. Select Targets
        self.shop_loc = self.locations["SHOP"][0] if self.locations["SHOP"] else None
        self.chop_counter = self.locations["COUNTER"][0] if self.locations["COUNTER"] else None
        self.assembly_counter = self.locations["COUNTER"][-1] if self.locations["COUNTER"] else None
        
        self.cookers_locs = self.locations["COOKER"]
        self.boxes_locs = self.locations["BOX"]

        self.bot_states = {} 
        self.order_id = None
        self.provider_processed_count = 0
        self.invading = False
        
        self.name_to_enum = {
            "MEAT": FoodType.MEAT, "ONION": FoodType.ONIONS, "ONIONS": FoodType.ONIONS,
            "EGG": FoodType.EGG, "NOODLES": FoodType.NOODLES, "SAUCE": FoodType.SAUCE
        }

    # -------------------------------------------------------------------------
    # SHARED HELPERS
    # -------------------------------------------------------------------------
    def get_ready_item_location(self, item_name: str, controller: RobotController) -> Optional[Tuple[int, int, str]]:
        name = item_name.upper()

        if name in ["MEAT", "EGG"]:
            for (cx, cy) in self.cookers_locs:
                tile = controller.get_tile(controller.get_team(), cx, cy)
                if tile and tile.item and isinstance(tile.item, Pan) and tile.item.food:
                    f = tile.item.food
                    if f.food_name.upper() == name and f.cooked_stage == 1:
                        return (cx, cy, "STOVE")

        elif name in ["ONION", "ONIONS"]:
            for (bx, by) in self.boxes_locs:
                tile = controller.get_tile(controller.get_team(), bx, by)
                if tile and isinstance(tile.item, Food) and tile.item.food_name.upper() in ["ONION", "ONIONS"]:
                    return (bx, by, "BOX")

        for (ax, ay) in [self.assembly_counter, self.chop_counter]:
            if not ax: continue
            tile = controller.get_tile(controller.get_team(), ax, ay)
            if tile and tile.item and isinstance(tile.item, Food):
                if tile.item.food_name.upper() == name:
                    if name in ["MEAT", "ONION", "ONIONS"]:
                         if tile.item.chopped: return (ax, ay, "COUNTER")
                    else:
                        return (ax, ay, "COUNTER")
        return None

    def get_bfs_path(self, controller: RobotController, start: Tuple[int, int], target_predicate) -> Optional[Tuple[int, int]]:
        queue = deque([(start, [])]) 
        visited = set([start])
        map_obj = controller.get_map(controller.get_team())
        w, h = map_obj.width, map_obj.height

        obstacles = set()
        for bot_id in controller.get_team_bot_ids(controller.get_team()):
            b = controller.get_bot_state(bot_id)
            if (b['x'], b['y']) != start:
                obstacles.add((b['x'], b['y']))

        while queue:
            (curr_x, curr_y), path = queue.popleft()
            tile = controller.get_tile(controller.get_team(), curr_x, curr_y)
            
            if target_predicate(curr_x, curr_y, tile):
                if not path: return (0, 0) 
                return path[0] 

            moves = [(0, 1), (0, -1), (1, 0), (-1, 0)]
            random.shuffle(moves)

            for dx, dy in moves:
                nx, ny = curr_x + dx, curr_y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    if (nx, ny) not in visited:
                        if map_obj.is_tile_walkable(nx, ny):
                            if (nx, ny) not in obstacles:
                                visited.add((nx, ny))
                                queue.append(((nx, ny), path + [(dx, dy)]))
        return None

    def move_towards(self, controller: RobotController, bot_id: int, target_x: int, target_y: int) -> bool:
        if target_x is None or target_y is None: return False
        bot_state = controller.get_bot_state(bot_id)
        bx, by = bot_state['x'], bot_state['y']
        
        def is_adjacent_to_target(x, y, tile):
            return max(abs(x - target_x), abs(y - target_y)) <= 1
            
        if is_adjacent_to_target(bx, by, None): return True
        
        step = self.get_bfs_path(controller, (bx, by), is_adjacent_to_target)
        if step and (step[0] != 0 or step[1] != 0):
            controller.move(bot_id, step[0], step[1])
            return False 
        return False 

    # -------------------------------------------------------------------------
    # MAIN LOOP
    # -------------------------------------------------------------------------
    def play_turn(self, controller: RobotController):
        orders = controller.get_orders(controller.get_team())
        
        if self.order_id is None and orders:
            self.order_id = 0 
        
        curr_turn = controller.get_turn()
        while self.order_id is not None and self.order_id < len(orders):
            if orders[self.order_id]["expires_turn"] <= curr_turn:
                self.order_id += 1
                self.provider_processed_count = 0 
            else:
                break
        
        if self.order_id is None or self.order_id >= len(orders):
            return

        my_bots = controller.get_team_bot_ids(controller.get_team())
        if not my_bots: return
    
        if len(my_bots) >= 1:
            self.play_provider_bot(my_bots[0], controller)
        if len(my_bots) >= 2:
            self.play_assembler_bot(my_bots[1], controller)

    # -------------------------------------------------------------------------
    # PROVIDER BOT
    # -------------------------------------------------------------------------
    def play_provider_bot(self, bot_id, controller: RobotController):
        if bot_id not in self.bot_states:
            self.bot_states[bot_id] = 0
            
        state = self.bot_states[bot_id]
        bot_info = controller.get_bot_state(bot_id)

        if not self.shop_loc or not self.chop_counter: return
        sx, sy = self.shop_loc
        cx, cy = self.chop_counter
        ax, ay = self.assembly_counter if self.assembly_counter else self.chop_counter

        def is_holding(name):
            h = bot_info.get('holding')
            if not h: return False
            if isinstance(h, dict):
                return h.get('food_name', '').upper() == name.upper()
            return False

        orders = controller.get_orders(controller.get_team())
        if self.order_id >= len(orders): return
        required_items = orders[self.order_id]['required']

        target_name = None
        if self.provider_processed_count < len(required_items):
            target_name = required_items[self.provider_processed_count]

        if not target_name: return 

        target_enum = self.name_to_enum.get(target_name.upper())

        # --- STATE 0: Buy ---
        if state == 0:
            if is_holding(target_name):
                if target_name in ["MEAT", "ONION", "ONIONS"]:
                    self.bot_states[bot_id] = 1 # Chop
                elif target_name == "EGG":
                    self.bot_states[bot_id] = 2 # Cook
                else:
                    self.bot_states[bot_id] = 4 # Deliver
                return

            if self.move_towards(controller, bot_id, sx, sy):
                if controller.get_team_money(controller.get_team()) >= target_enum.buy_cost:
                    controller.buy(bot_id, target_enum, sx, sy)

        # --- STATE 1: Chop ---
        elif state == 1:
            item_name = target_name.upper()
            if is_holding(item_name):
                # SAFETY CHECK: Only place if counter is empty
                tile = controller.get_tile(controller.get_team(), cx, cy)
                if tile and tile.item is None:
                    if self.move_towards(controller, bot_id, cx, cy):
                        controller.place(bot_id, cx, cy)
            else:
                tile = controller.get_tile(controller.get_team(), cx, cy)
                if tile and tile.item:
                    is_chopped = getattr(tile.item, 'chopped', False)
                    if is_chopped:
                        if controller.pickup(bot_id, cx, cy):
                            if item_name in ["ONION", "ONIONS"]:
                                self.bot_states[bot_id] = 3 
                            else:
                                self.bot_states[bot_id] = 2 
                    else:
                        controller.chop(bot_id, cx, cy)

        # --- STATE 2: Cook ---
        elif state == 2:
            free_stove = None
            for (stove_x, stove_y) in self.cookers_locs:
                tile = controller.get_tile(controller.get_team(), stove_x, stove_y)
                if tile and tile.item and isinstance(tile.item, Pan):
                    if tile.item.food is None: 
                        free_stove = (stove_x, stove_y)
                        break
            
            if not free_stove: return 
            
            gx, gy = free_stove
            if self.move_towards(controller, bot_id, gx, gy):
                # SAFETY CHECK: Re-verify stove is empty before acting
                tile = controller.get_tile(controller.get_team(), gx, gy)
                if tile and tile.item and isinstance(tile.item, Pan) and tile.item.food is None:
                    if controller.place(bot_id, gx, gy):
                        if controller.start_cook(bot_id, gx, gy):
                            self.provider_processed_count += 1
                            self.bot_states[bot_id] = 0

        # --- STATE 3: Store Onions ---
        elif state == 3:
            target_box = None
            for (bx, by) in self.boxes_locs:
                tile = controller.get_tile(controller.get_team(), bx, by)
                if tile:
                    if tile.item is None:
                        target_box = (bx, by)
                        break
                    elif isinstance(tile.item, Food) and tile.item.food_name.upper() == target_name.upper():
                        target_box = (bx, by)
                        break
            
            if not target_box: target_box = (ax, ay)

            tx, ty = target_box
            if self.move_towards(controller, bot_id, tx, ty):
                # SAFETY CHECK: Re-verify box is valid
                tile = controller.get_tile(controller.get_team(), tx, ty)
                can_place = False
                if tile.item is None: can_place = True
                elif isinstance(tile.item, Food) and tile.item.food_name.upper() == target_name.upper(): can_place = True
                
                if can_place:
                    if controller.place(bot_id, tx, ty):
                        self.provider_processed_count += 1
                        self.bot_states[bot_id] = 0

        # --- STATE 4: Deliver Direct ---
        elif state == 4:
            tile = controller.get_tile(controller.get_team(), ax, ay)
            can_place = False
            # Can place if empty OR if it has a plate (adding to plate)
            if tile and tile.item is None: can_place = True
            elif tile and isinstance(tile.item, Plate): can_place = True
                
            if can_place:
                if self.move_towards(controller, bot_id, ax, ay):
                    if controller.place(bot_id, ax, ay):
                        self.provider_processed_count += 1
                        self.bot_states[bot_id] = 0

    # -------------------------------------------------------------------------
    # ASSEMBLER BOT
    # -------------------------------------------------------------------------
    def play_assembler_bot(self, bot_id, controller: RobotController):
        if bot_id not in self.bot_states:
            self.bot_states[bot_id] = 0
            
        state = self.bot_states[bot_id]
        bot_info = controller.get_bot_state(bot_id)

        if not self.shop_loc or not self.assembly_counter: return
        sx, sy = self.shop_loc
        ax, ay = self.assembly_counter
        
        def is_holding(item_type=None, dirty=None):
            h = bot_info.get('holding')
            if not h: return False
            if isinstance(h, dict):
                if item_type and h.get('type') != item_type: return False
                if dirty is not None and h.get('dirty') != dirty: return False
                return True
            return False

        if is_holding(item_type="Plate", dirty=True):
            self.bot_states[bot_id] = 9
            state = 9
        
        # --- STATE 0: Get Clean Plate ---
        if state == 0:
            if is_holding(item_type="Plate", dirty=False):
                self.bot_states[bot_id] = 1 
                return

            stx, sty = self.locations["SINKTABLE"][0] if self.locations["SINKTABLE"] else (None, None)
            if stx is not None:
                tile = controller.get_tile(controller.get_team(), stx, sty)
                if tile and tile.num_clean_plates > 0:
                    if self.move_towards(controller, bot_id, stx, sty):
                        controller.take_clean_plate(bot_id, stx, sty)
                    return

            if self.move_towards(controller, bot_id, sx, sy):
                if controller.get_team_money(controller.get_team()) >= ShopCosts.PLATE.buy_cost:
                    controller.buy(bot_id, ShopCosts.PLATE, sx, sy)

        # --- STATE 1: Assemble ---
        elif state == 1:
            if not is_holding(item_type="Plate"):
                self.bot_states[bot_id] = 0
                return

            if self.order_id is None: return
            orders = controller.get_orders(controller.get_team())
            if self.order_id >= len(orders): return
            required = orders[self.order_id]['required']
            
            h = bot_info.get('holding', {})
            on_plate = [f['food_name'].upper() for f in h.get('food', [])]
            
            missing = None
            for r in required:
                if r.upper() not in on_plate:
                    missing = r.upper()
                    break
            
            if not missing:
                self.bot_states[bot_id] = 2 
                return
            
            self.bot_states[bot_id] = 10 

        # --- STATE 2: Submit ---
        elif state == 2:
            ux, uy = self.locations["SUBMIT"][0] if self.locations["SUBMIT"] else (None, None)
            if not ux: return
            if self.move_towards(controller, bot_id, ux, uy):
                if controller.submit(bot_id, ux, uy):
                    self.bot_states[bot_id] = 0

        # --- STATE 9: Wash ---
        elif state == 9:
            six, siy = self.locations["SINK"][0] if self.locations["SINK"] else (None, None)
            if not six: return
            if self.move_towards(controller, bot_id, six, siy):
                if is_holding(item_type="Plate", dirty=True):
                    controller.put_dirty_plate_in_sink(bot_id, six, siy)
                
                tile = controller.get_tile(controller.get_team(), six, siy)
                if tile and tile.num_dirty_plates > 0:
                    controller.wash_sink(bot_id, six, siy)
                    self.bot_states[bot_id] = 0
                else:
                    self.bot_states[bot_id] = 0

        # --- JUGGLE: STATE 10 (Drop) ---
        elif state == 10:
            if self.move_towards(controller, bot_id, ax, ay):
                # SAFETY CHECK: Only drop if counter is empty
                tile = controller.get_tile(controller.get_team(), ax, ay)
                if tile and tile.item is None:
                    if controller.place(bot_id, ax, ay):
                        self.bot_states[bot_id] = 11

        # --- JUGGLE: STATE 11 (Fetch) ---
        elif state == 11:
            tile = controller.get_tile(controller.get_team(), ax, ay)
            if not tile or not tile.item or not isinstance(tile.item, Plate):
                self.bot_states[bot_id] = 0
                return
            
            orders = controller.get_orders(controller.get_team())
            required = orders[self.order_id]['required']
            on_plate = [f.food_name.upper() for f in tile.item.food]
            
            missing = None
            for r in required:
                if r.upper() not in on_plate:
                    missing = r.upper()
                    break
            
            if not missing:
                self.bot_states[bot_id] = 13 
                return

            loc = self.get_ready_item_location(missing, controller)
            
            if loc:
                tx, ty, source = loc
                if self.move_towards(controller, bot_id, tx, ty):
                    if source == "STOVE":
                        if controller.take_from_pan(bot_id, tx, ty):
                            self.bot_states[bot_id] = 12
                    else: 
                        if controller.pickup(bot_id, tx, ty):
                            self.bot_states[bot_id] = 12

        # --- JUGGLE: STATE 12 (Add) ---
        elif state == 12:
            if self.move_towards(controller, bot_id, ax, ay):
                if controller.add_food_to_plate(bot_id, ax, ay):
                    self.bot_states[bot_id] = 13

        # --- JUGGLE: STATE 13 (Pickup) ---
        elif state == 13:
            if self.move_towards(controller, bot_id, ax, ay):
                if controller.pickup(bot_id, ax, ay):
                    self.bot_states[bot_id] = 1