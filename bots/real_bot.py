import random
from collections import deque
from typing import Tuple, Optional, List

from game_constants import Team, TileType, FoodType, ShopCosts
from robot_controller import RobotController
from item import Pan, Plate, Food

import os 
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.append(script_dir)

from helpers import locations

class BotPlayer:
    def __init__(self, map_copy):
        self.map = map_copy
        self.locations = locations.find_important_locations(self.map)
        self.assembler_bot_id = None
        self.provider_bot_id = None

        # The integer defines the ingredient that is currently stored in the box. Each box can store multiple of one ingredient
        self.boxes = [(x,y) for (x,y) in self.locations["BOX"]]

        # First boolean is if a pan is there
        # Second boolean is if it is cooking
        self.cookers = [(True, False, x,y) for (x,y) in self.locations["COOKER"]]

        self.goal_stove = None

        self.assembly_counter = self.locations["COUNTER"][-1]

        self.bot_states = {}     # Tracks what each bot is doing
        self.order_id = None
        self.order_target_status = None

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
                        if controller.get_map(controller.get_team).is_tile_walkable(nx, ny):
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
        m = controller.get_map(controller.get_team)
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
        #Get the orders and initialize the order_id
        self.orders = controller.get_orders(controller.get_team())
        if self.order_id == None and self.orders != None:
            self.order_id = 0 
        
        #See if the orders are still active and update accordingly
        while len(self.orders) > self.order_id and self.orders[self.order_id]["expires_turn"] <= controller.get_turn(): 
            self.order_id += 1
        
        #Gone through all orders
        if self.order_id >= len(self.orders):
            return

        my_bots = controller.get_team_bot_ids(controller.get_team())
        if not my_bots: return
    
        self.provider_bot_id = my_bots[0]
        provider_bot_id = self.provider_bot_id

        self.assembler_bot_id = my_bots[1]
        assembler_bot_id = self.assembler_bot_id

        if self.invading:
            ...
        else:
            self.play_assembler_bot(assembler_bot_id, controller)
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
        if self.order_id == None:
            return # Wait for game logic to pick an order
            
        required_items = self.orders[self.order_id]['required']

        # Skip items the Assembler handles (Noodles/Sauce) or items we already did
        target_name = None
        while self.provider_processed_count < len(required_items):
            candidate = required_items[self.provider_processed_count]
            if candidate in ["NOODLES", "SAUCE"]: # Assembler handles these
                self.provider_processed_count += 1
            else:
                target_name = candidate
                break

        # If we have finished all ingredients, go to Waiting Zone
        if not target_name:
            return

        # --- STATE 0: Buy Ingredient ---
        if state == 0:
            if is_holding(target_name):
                # We have it. Where does it go?
                if target_name in ["MEAT", "ONION", "ONIONS"]:
                    self.bot_states[bot_id] = 1 # Go Chop
                else:
                    self.bot_states[bot_id] = 2 # Go Cook (Egg)
                return

            if self.move_towards(controller, bot_id, sx, sy):
                if controller.get_team_money(controller.get_team) >= target_enum.buy_cost:
                    if controller.buy(bot_id, target_enum, sx, sy):
                        # State transition handled next turn by is_holding check
                        pass

        # --- STATE 1: Chop Routine (Meat & Onion) ---
        # Sequence: Place -> Chop -> Pickup
        elif state == 1:
            item_name = target_name.upper()
            
            # Sub-state: Holding raw food -> Place on Counter
            if is_holding(item_name):
                if self.move_towards(controller, bot_id, cx, cy):
                    if controller.place(bot_id, cx, cy):
                        # Placed successfully. Now we need to CHOP.
                        pass 
            
            # Sub-state: Hands empty? Check counter.
            else:
                tile = controller.get_tile(controller.get_team(), cx, cy)
                if tile and tile.item:
                    # Is it chopped yet?
                    if tile.item.chopped:
                        if controller.pickup(bot_id, cx, cy):
                            # Picked up chopped food. Next destination?
                            if item_name == "ONIONS" or item_name == "ONION":
                                self.bot_states[bot_id] = 3 # Store Onion
                            else:
                                self.bot_states[bot_id] = 2 # Cook Meat
                    else:
                        # Not chopped -> Chop it
                        controller.chop(bot_id, cx, cy)

        # --- STATE 2: Cook Item
        elif state == 2:
            if self.goal_stove is None:
                for idx, (has_pan, is_cooking, x, y) in enumerate(self.cookers):
                    # We need a stove that HAS a pan but is NOT cooking
                    # Note: You need to update self.cookers in your Assembler bot 
                    # when it places a pan! For now, assuming pans exist.
                    if has_pan and not is_cooking: 
                        self.goal_stove = (x, y, idx)
                        break
            
            # No stove found? Wait.
            if self.goal_stove is None:
                return
            
            gx, gy, g_idx = self.goal_stove
            if self.move_towards(controller, bot_id,x,y):
                if self.move_towards(controller, bot_id, gx, gy):
                # 3. Place Food (this puts it in the pan)
                    if controller.place(bot_id, gx, gy):
                        # 4. Start Cooking
                        if controller.start_cook(bot_id, gx, gy):
                            # Success! Update tracking
                            self.provider_processed_count += 1
                            self.bot_states[bot_id] = 0
                            self.goal_stove = None
                            
                            # Mark this stove as 'Cooking'
                            self.cookers[g_idx] = (True, True, gx, gy)

                    else: 
                        #STOLEN! INVADERS
                        self.goal_stove = None
                        self.cookers[g_idx] = (False, False, gx, gy)

        # --- STATE 4: Deliver Chopped Item to boxes---
        elif state == 3:
            
            tx, ty= self.boxes[0][0], self.boxes[0][1] 
                
            # If we found a valid place, go there
            if self.move_towards(controller, bot_id, tx, ty):
                if controller.place(bot_id, tx, ty):
                    # Success!
                    self.provider_processed_count += 1
                    self.bot_states[bot_id] = 0
            else:
                # No valid box found (all full of other stuff)? 
                # Wait or stay put to avoid walking into a wall
                pass
            
            
    
    def play_provider_bot(self, controller, bot_id):
        return
        '''if controller.get_turn() == 1: # if starting state
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
            self.move_towards(controller, bot_id, sinkx, sinky)'''