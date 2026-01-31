import random
from collections import deque
from typing import Tuple, Optional, List

from game_constants import Team, TileType, FoodType, ShopCosts
from robot_controller import RobotController
from item import Pan, Plate, Food

from helpers import locations

class BotPlayer:
    def __init__(self, map_copy):
        self.map = map_copy
        self.locations = locations.find_important_locations(self.map)
        self.assembler_bot_id = None
        self.provider_bot_id = None

        # The integer defines the ingredient that is currently stored in the box. Each box can store multiple of one ingredient
        self.boxes = [(-1, x,y) for (x,y) in self.locations["BOXES"]]

        # First boolean is if a pan is there
        # Second boolean is if it is cooking
        self.cookers = [(False, False, x,y) for (x,y) in self.locations["COOKER"]]

        self.bot_states = {}     # Tracks what each bot is doing
        self.current_order_target = None
        self.ingredients_processed_count = 0
        self.provider_processed_count = 0

        self.invading = False
        self.state = 0

        self.clean_plates = 0
        self.dirty_plates = 0

    def get_bfs_path(self, controller: RobotController, start: Tuple[int, int], target_predicate) -> Optional[Tuple[int, int]]:
        queue = deque([(start, [])]) 
        visited = set([start])
        w, h = self.map.width, self.map.height

        while queue:
            (curr_x, curr_y), path = queue.popleft()
            tile = controller.get_tile(controller.get_team(), curr_x, curr_y)
            if target_predicate(curr_x, curr_y, tile):
                if not path: return (0, 0) 
                return path[0] 

            for dx in [0, -1, 1]:
                for dy in [0, -1, 1]:
                    if dx == 0 and dy == 0: continue
                    nx, ny = curr_x + dx, curr_y + dy
                    if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in visited:
                        if controller.get_map().is_tile_walkable(nx, ny):
                            visited.add((nx, ny))
                            queue.append(((nx, ny), path + [(dx, dy)]))
        return None

    def move_towards(self, controller: RobotController, bot_id: int, target_x: int, target_y: int) -> bool:
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

    def find_nearest_tile(self, controller: RobotController, bot_x: int, bot_y: int, tile_name: str) -> Optional[Tuple[int, int]]:
        best_dist = 9999
        best_pos = None
        m = controller.get_map()
        for x in range(m.width):
            for y in range(m.height):
                tile = m.tiles[x][y]
                if tile.tile_name == tile_name:
                    dist = max(abs(bot_x - x), abs(bot_y - y))
                    if dist < best_dist:
                        best_dist = dist
                        best_pos = (x, y)
        return best_pos

    def play_turn(self, controller: RobotController):
        my_bots = controller.get_team_bot_ids()
        if not my_bots: return
    
        self.provider_bot_id = my_bots[0]
        provider_bot_id = self.my_bot_id

        self.assembler_bot = my_bots[1]
        assembly_bot_id = self.assembly_bot_id

        if self.invading:
            ...
        else:
            self.play_assembler_bot(assembly_bot_id, controller)
            self.play_provider_bot(provider_bot_id, controller)
        
    def play_assembler_bot(self, bot_id, controller: RobotController):
        """
        Role: Sourcing & Prep (The "Sous Chef")
        Responsibilities: 
        1. Check the current order.
        2. Buy the necessary ingredient.
        3. Chop it (if required).
        4. Place it on the Cooker (if cookable) or Assembly Counter (if just prep).
        """
        if bot_id not in self.bot_states:
            self.bot_states[bot_id] = 0
            
        state = self.bot_states[bot_id]
        bot_info = controller.get_bot_state(bot_id)

        sx, sy = self.locations["SHOP"][0]
        cx, cy = self.locations["COUNTER"][0]

        def is_holding(name):
            h = bot_info.get('holding')
            if not h: 
                return False
            if isinstance(h, dict):
                return h.get('food_name', '').upper() == name.upper()
            return False

        # 2. Determine Target Ingredient
        # We look at the shared order target and the count of items we've already prepped.
        if not self.current_order_target:
            return # Wait for game logic to pick an order
            
        required_items = self.current_order_target['required']

        # Get the specific ingredient we need right now
        found_item = False
        while(not found_item and self.ingredients_processed_count < len(required_items)):
            target_name = required_items[self.ingredients_processed_count]
            if target_name in ["NOODLES", "EGG"]:
                self.ingredients_processed_count += 1
            else:
                found_item = True

        # If we have finished all ingredients, go to Waiting Zone
        if self.ingredients_processed_count >= len(required_items):
            return

        target_enum = self.name_to_enum.get(target_name.upper())
        
        if not target_enum:
            # Skip invalid/unknown ingredients to prevent freezing
            self.ingredients_processed_count += 1
            return

        # --- STATE 0: Buy Ingredient ---
        if state == 0:
            # If we already have it, skip to processing
            if is_holding(target_name):
                self.bot_states[bot_id] = 1
                return

            # Go to Shop
            if self.move_towards(controller, bot_id, sx, sy):
                # Check funds
                if controller.get_team_money() >= target_enum.buy_cost:
                    if controller.buy(bot_id, target_enum, sx, sy):
                        if target_enum.food_name in ["MEAT", "ONIONS"]:
                            self.bot_states[bot_id] = 1 # Go chop
                        else:
                            self.bot_states[bot_id] = 2 # Cook


        # --- STATE 1: Route to Station ---
        elif state == 1:
            # Sanity Check: Did we lose the item?
            if not is_holding(target_name):
                self.bot_states[bot_id] = 0 # Retry buy
                self.ingredients_processed_count -= 1
                return

            item_name = target_name.upper()

            # ROUTE A: Needs Chopping (Meat, Onion) -> Go to Chop Counter
            if item_name in ["MEAT", "ONION"]:
                if self.move_towards(controller, bot_id, cx, cy):
                    # Place it on the counter to chop
                    if controller.chop(bot_id, cx, cy):
                        self.bot_states[bot_id] = 2
                        if item_name == "ONION":
                            self.bot_states[bot_id] = 4
                        else:
                            self.bot_states[bot_id] = 2

        # --- STATE 2: Cook Item
        elif state == 2:
            if 

        # --- STATE 4: Deliver Chopped Item to boxes---
        elif state == 4:
            
    
    def play_provider_bot(self, controller, bot_id):
        if controller.get_turn() == 1: # if starting state
            self.get_pans(controller, bot_id)
        pass
    
    def get_pans(self, controller, bot_id):
        bot_state = controller.get_bot_state(bot_id)
        bx, by = bot_state['x'], bot_state['y']
        if not bot_state['holding']:
            shop_x, shop_y = self.find_nearest_tile(controller, bx, by, '$')

            if (abs(shop_x-bx) <= 1 and abs(shop_y-by) <= 1): # can access shop
                controller.buy(bot_id, "PANS", shop_x, shop_y)
            else:
                self.move_towards(controller, bot_id, shop_x, shop_y)
        else:
            # for now just choose first available stove
            for has_pan, _, x, y in self.cookers:
                if not has_pan:
                    stove_x, stove_y = x, y
                    break

            if (abs(stove_x-bx) <= 1 and abs(stove_y-by) <= 1): # can access stove
                controller.place(bot_id, stove_x, stove_y)
            else:
                self.move_towards(controller, bot_id, stove_x, stove_y)

    def wash_dishes(self, controller: RobotController, bot_id):
        bot_state = controller.get_bot_state(bot_id)
        bx, by = bot_state['x'], bot_state['y']
        sinkx, sinky = self.find_nearest_tile(controller, bx, by, 'S')

        if (abs(sinkx-bx) <= 1 and abs(sinky-by) <= 1): #can access sink
            controller.wash_sink(bot_id, sinkx, sinky)
        else:
            self.move_towards(controller, bot_id, sinkx, sinky)