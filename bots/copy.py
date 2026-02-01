from collections import deque
from typing import Tuple, Optional, List, Dict, Any

from game_constants import FoodType, ShopCosts
from robot_controller import RobotController
from item import Pan, Plate, Food
import random

class BotPlayer:
    def __init__(self, map_copy):
        self.map = map_copy
        self.locations = self.find_important_locations(self.map)

        # Bot role assignments
        self.provider_bot_id = None
        self.assembler_bot_id = None

        # State machines
        self.provider_state = 0
        self.assembler_state = 0

        # Current order tracking
        self.current_order = None
        self.current_order_id = None
        self.order_index = 0  # Track position in sorted order list
        self.cooked_ingredients = []
        self.chop_only_ingredients = []
        self.simple_ingredients = []
        self.cooked_count = 0  # How many cooked ingredients provider has made so far
        self.cooked_total = 0  # Total cooked ingredients needed for this order
        self.chopped_count = 0
        self.assembled_cooked_count = 0  # How many cooked ingredients assembler has added to plate


        # Coordination flags
        self.pan_on_cooker = False
        self.current_cooking_ingredient = None
        self.current_chopping_ingredient = None

        # Assigned locations (cached)
        self.cooker_loc = None
        self.chop_counter = None
        self.assembly_counter = None
        self.shop_loc = None
        self.submit_loc = None
        self.trash_loc = None
        self.onion_box = self.locations["BOX"][0]

        self.onion_ready_in_box = 0

        # Per-turn cache
        self.cached_turn = -1
        self.cached_bot_positions = set()

        self.must_move_provider = False
        self.must_move_assembler = False

    def compute_order_heuristic(self, order: Dict[str, Any], controller: RobotController) -> int:
        """
        Compute estimated turns needed to complete an order.
        Returns the heuristic (estimated time in turns).
        """
        current_turn = controller.get_turn()
        
        # Heuristic costs per ingredient
        INGREDIENT_COSTS = {
            "EGG": 20,
            "ONIONS": 30,
            "MEAT": 50,
            "NOODLES": 20,
            "SAUCE": 20,
        }
        
        PLATE_AND_SUBMIT = 5  # Buy plate, assemble, submit overhead
        
        total_cost = PLATE_AND_SUBMIT
        
        # Add cost for each required ingredient
        for food_name in order["required"]:
            cost = INGREDIENT_COSTS.get(food_name, 20)  # Default cost if unknown ingredient
            total_cost += cost
        
        return total_cost

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

    def get_bot_positions(self, controller: RobotController, current_turn: int) -> set:
        if self.cached_turn == current_turn:
            return self.cached_bot_positions
        self.cached_turn = current_turn
        self.cached_bot_positions = set()
        for bot_id in controller.get_team_bot_ids(controller.get_team()):
            state = controller.get_bot_state(bot_id)
            if state:
                self.cached_bot_positions.add((state['x'], state['y']))
        return self.cached_bot_positions

    def get_bfs_path(self, controller: RobotController, start: Tuple[int, int], target_x: int, target_y: int, current_turn: int) -> Optional[Tuple[int, int]]:
        bot_positions = self.get_bot_positions(controller, current_turn)
        bot_positions_copy = bot_positions - {start}

        queue = deque([(start, None)])  # (position, first_step)
        visited = {start}
        w, h = self.map.width, self.map.height

        while queue:
            (cx, cy), first_step = queue.popleft()

            # Check if adjacent to target
            if max(abs(cx - target_x), abs(cy - target_y)) <= 1:
                return first_step if first_step else (0, 0)

            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if (nx, ny) in visited:
                        continue
                    if not (0 <= nx < w and 0 <= ny < h):
                        continue
                    if not self.map.is_tile_walkable(nx, ny):
                        continue
                    if first_step is None and (nx, ny) in bot_positions_copy:
                        continue
                    visited.add((nx, ny))
                    new_first = first_step if first_step else (dx, dy)
                    queue.append(((nx, ny), new_first))
        return None

    def move_towards(self, controller: RobotController, bot_id: int, target_x: int, target_y: int, current_turn: int) -> bool:
        state = controller.get_bot_state(bot_id)
        bx, by = state['x'], state['y']

        if max(abs(bx - target_x), abs(by - target_y)) <= 1:
            return True

        step = self.get_bfs_path(controller, (bx, by), target_x, target_y, current_turn)
        if step and step != (0, 0):
            controller.move(bot_id, step[0], step[1])
        return False

    def get_food_type_by_name(self, name: str) -> Optional[FoodType]:
        for ft in FoodType:
            if ft.food_name == name:
                return ft
        return None

    def analyze_order(self, order: Dict[str, Any]) -> None:
        # Don't re-analyze the same order
        if self.current_order_id == order.get('order_id'):
            return
        self.current_order = order
        self.current_order_id = order.get('order_id')
        self.cooked_ingredients = []
        self.chop_only_ingredients = []
        self.simple_ingredients = []
        for food_name in order['required']:
            ft = self.get_food_type_by_name(food_name)
            if ft:
                if ft.can_cook:
                    self.cooked_ingredients.append(ft)
                elif ft.can_chop:
                    self.chop_only_ingredients.append(ft)
                else:
                    self.simple_ingredients.append(ft)
        self.cooked_count = 0
        self.cooked_total = len(self.cooked_ingredients)
        self.assembled_cooked_count = 0

    def play_turn(self, controller: RobotController):
        my_bots = controller.get_team_bot_ids(controller.get_team())
        if not my_bots:
            return

        current_turn = controller.get_turn()

        self.provider_bot_id = my_bots[0]
        self.assembler_bot_id = my_bots[1] if len(my_bots) > 1 else None

        # Initialize locations once - find nearest tiles to provider bot
        if self.shop_loc is None:
            pstate = controller.get_bot_state(self.provider_bot_id)
            px, py = pstate['x'], pstate['y']

            # Find nearest of each type
            def nearest(locs):
                if not locs:
                    return None
                return min(locs, key=lambda p: abs(p[0]-px) + abs(p[1]-py))

            self.cooker_loc = nearest(self.locations["COOKER"])
            self.shop_loc = nearest(self.locations["SHOP"])
            self.submit_loc = nearest(self.locations["SUBMIT"])
            self.trash_loc = nearest(self.locations["TRASH"])

            counters = self.locations["COUNTER"]
            if counters:
                # Sort by distance and pick two nearest
                sorted_counters = sorted(counters, key=lambda p: abs(p[0]-px) + abs(p[1]-py))
                self.chop_counter = sorted_counters[0]
                self.assembly_counter = sorted_counters[1] if len(sorted_counters) > 1 else sorted_counters[0]

        # Check for active orders
        orders = controller.get_orders(controller.get_team())
        active_orders = [o for o in orders if o['is_active']]

        if active_orders and self.current_order is None:
            self.analyze_order(active_orders[0])
            self.provider_state = 0
            self.assembler_state = 0

        self.play_provider_bot(controller, self.provider_bot_id, current_turn)
        if self.assembler_bot_id is not None:
            self.play_assembler_bot(controller, self.assembler_bot_id, current_turn)

    def play_provider_bot(self, controller: RobotController, bot_id: int, current_turn: int):
        state = controller.get_bot_state(bot_id)
        bx, by = state['x'], state['y']
        holding = state['holding']
        team = controller.get_team()
        money = controller.get_team_money(team)

        print("provider_bot_state: ", self.provider_state)

        # State 0: Init
        if self.provider_state == 0:
            if self.cooked_count < self.cooked_total:
                self.current_cooking_ingredient = self.cooked_ingredients[self.cooked_count]
                self.provider_state = 1
            elif self.chopped_count < len(self.chop_only_ingredients):
                self.current_cooking_ingredient = None
                self.current_chopping_ingredient = self.chop_only_ingredients[0]
                self.provider_state = 3
            else:
                self.provider_state = 100
            
            if self.cooker_loc:
                tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                if tile and isinstance(tile.item, Pan):
                    self.pan_on_cooker = True
                    self.provider_state = 3
                else:
                    self.provider_state = 1
            else:
                self.provider_state = 100

        # State 1: Buy pan
        elif self.provider_state == 1:
            if holding:
                self.provider_state = 2
            elif self.shop_loc and money >= ShopCosts.PAN.buy_cost:
                trying_move_towards = self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1], current_turn)
                self.must_move_assembler = not trying_move_towards
                if trying_move_towards:
                    controller.buy(bot_id, ShopCosts.PAN, self.shop_loc[0], self.shop_loc[1])

        # State 2: Place pan
        elif self.provider_state == 2:
            if self.cooker_loc:
                trying_move_towards = self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1], current_turn)
                self.must_move_assembler = not trying_move_towards
                if trying_move_towards:
                    if controller.place(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                        self.pan_on_cooker = True
                        self.provider_state = 3

        # State 3: Buy ingredient
        elif self.provider_state == 3:
            if holding:
                ingredient = self.current_cooking_ingredient or self.current_chopping_ingredient

                if ingredient.can_chop:
                    self.provider_state = 4   # Must chop first
                elif ingredient.can_cook:
                    self.provider_state = 7   # Can cook immediately
                else:
                    self.provider_state = 99  # Should never happen
            elif self.shop_loc:
                ingredient = self.current_cooking_ingredient or self.current_chopping_ingredient
                if ingredient and money >= ingredient.buy_cost:
                    trying_move_towards = self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1], current_turn)
                    self.must_move_assembler = not trying_move_towards
                    if trying_move_towards:
                        controller.buy(bot_id, ingredient, self.shop_loc[0], self.shop_loc[1])

        # State 4: Place for chopping
        elif self.provider_state == 4:
            if self.chop_counter:
                tile = controller.get_tile(team, self.chop_counter[0], self.chop_counter[1])
                if tile and getattr(tile, 'item', None) is None:
                    trying_move_towards = self.move_towards(controller, bot_id, self.chop_counter[0], self.chop_counter[1], current_turn)
                    self.must_move_assembler = not trying_move_towards 
                    if trying_move_towards:
                        if controller.place(bot_id, self.chop_counter[0], self.chop_counter[1]):
                            self.provider_state = 5

        # State 5: Chop
        elif self.provider_state == 5:
            if self.chop_counter:
                trying_move_towards = self.move_towards(controller, bot_id, self.chop_counter[0], self.chop_counter[1], current_turn)
                self.must_move_assembler = not trying_move_towards 
                if trying_move_towards:
                    if controller.chop(bot_id, self.chop_counter[0], self.chop_counter[1]):
                        self.provider_state = 6

        # State 6: Pick up chopped
        elif self.provider_state == 6:
            if self.chop_counter:
                trying_move_towards = self.move_towards(controller, bot_id, self.chop_counter[0], self.chop_counter[1], current_turn)
                self.must_move_assembler = not trying_move_towards 
                if trying_move_towards:
                    if controller.pickup(bot_id, self.chop_counter[0], self.chop_counter[1]):
                        ingredient = self.current_chopping_ingredient or self.current_cooking_ingredient

                        if ingredient.can_cook:
                            self.provider_state = 7   # Meat: now cook
                        else:
                            self.provider_state = 10  # Onion: stores
        # State 7: Place in pan
        elif self.provider_state == 7:
            if self.cooker_loc:
                trying_move_towards = self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1], current_turn)
                self.must_move_assembler = not trying_move_towards 
                if trying_move_towards:
                    if controller.place(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                        self.provider_state = 8

        # State 8: Wait for cooking - move away from counter to let assembler work
        elif self.provider_state == 8:
            if self.cooker_loc:
                tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                if tile and isinstance(tile.item, Pan) and tile.item.food:
                    if tile.item.food.cooked_stage == 1:
                        self.cooked_count += 1  # This ingredient is done
                        self.provider_state = 9
                    elif tile.item.food.cooked_stage == 2:
                        trying_move_towards = self.move_towards(controller, bot_id, self.cooker_loc[0], self.cooker_loc[1], current_turn)
                        self.must_move_assembler = not trying_move_towards 
                        if trying_move_towards:
                            if controller.take_from_pan(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                                self.provider_state = 99
                    else:
                        # Food is still cooking (stage 0) - move away from counter to let assembler work
                        if self.shop_loc:
                            self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1], current_turn)
                            self.must_move_assembler = not controller.can_move(bot_id, self.shop_loc[0], self.shop_loc[1])

        # State 9: Wait for assembler to take food
        elif self.provider_state == 9:
            if self.cooker_loc:
                tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                if tile and isinstance(tile.item, Pan) and tile.item.food is None:
                    # Check if there are more cooked ingredients to make for this order
                    if self.cooked_count < self.cooked_total:
                        self.current_cooking_ingredient = self.cooked_ingredients[self.cooked_count]
                        self.provider_state = 3  # Go buy next ingredient
                    else:
                        # All cooked ingredients done, wait for next order
                        self.provider_state = 100
        
        elif self.provider_state == 10:
            if self.onion_box:
                trying_move_towards = self.move_towards(controller, bot_id, self.onion_box[0], self.onion_box[1], current_turn)
                self.must_move_assembler = not trying_move_towards 
                if trying_move_towards:
                    if controller.place(bot_id, self.onion_box[0], self.onion_box[1]):
                        self.chopped_count += 1 
                        self.onion_ready_in_box += 1
                        # Finished chop-only ingredient
                        self.current_chopping_ingredient = None

                        # Decide next task
                        if self.cooked_count < self.cooked_total:
                            self.current_cooking_ingredient = self.cooked_ingredients[self.cooked_count]
                            self.provider_state = 3
                        elif self.chopped_count < len(self.chop_only_ingredients):
                            self.current_chopping_ingredient = self.chop_only_ingredients[0]
                            self.provider_state = 3
                        else:
                            self.provider_state = 100

        # State 99: Trash
        elif self.provider_state == 99:
            if self.trash_loc:
                trying_move_towards = self.move_towards(controller, bot_id, self.trash_loc[0], self.trash_loc[1], current_turn)
                self.must_move_assembler = not trying_move_towards 
                if trying_move_towards:
                    self.must_move_assembler = not controller.can_move(bot_id,  self.trash_loc[0], self.trash_loc[1])
                    if controller.trash(bot_id, self.trash_loc[0], self.trash_loc[1]):
                        self.provider_state = 3

        # State 100: Idle - wait for new order
        elif self.provider_state == 100:
            orders = controller.get_orders(team)
            active = [o for o in orders if o['is_active']]
            
            if active:
                # Try to find a feasible order
                found_new_order = False
                
                for order in active:
                    # Check if this is a different order than what we just finished
                    if order.get('order_id') != self.current_order_id:
                        # Compute heuristic for this order
                        heuristic = self.compute_order_heuristic(order, controller)
                        current_turn = controller.get_turn()
                        time_remaining = order['expires_turn'] - current_turn
                        
                        # If we have enough time, take this order
                        if heuristic <= time_remaining:
                            self.current_order_id = None  # Reset so analyze_order works
                            self.analyze_order(order)
                            self.provider_state = 0
                            found_new_order = True
                            break
                        else:
                            # Not enough time, move to next order
                            pass
                
                # If no feasible order found, just idle
                if not found_new_order:
                    if self.must_move_provider:
                        print("moving provider")
                        controller.move(bot_id, random.choice([-1, 1]), random.choice([-1, 1]))
            else:
                if self.must_move_provider:
                    print("moving provider")
                    controller.move(bot_id, random.choice([-1, 1]), random.choice([-1, 1]))

    def play_assembler_bot(self, controller: RobotController, bot_id: int, current_turn: int):
        state = controller.get_bot_state(bot_id)
        bx, by = state['x'], state['y']
        holding = state['holding']
        team = controller.get_team()
        money = controller.get_team_money(team)
        print("assembler_bot_state: ", self.assembler_state)

        # State 0: Buy plate
        if self.assembler_state == 0:
            orders = controller.get_orders(team)
            if not orders:
                if self.must_move_assembler:
                    print("moving assembler")
                    controller.move(bot_id, random.choice([-1, 1]), random.choice([-1, 1]))
                return
            if holding:
                self.assembler_state = 1
            elif self.provider_state >= 7 or self.cooked_total == 0:
                if self.shop_loc and money >= ShopCosts.PLATE.buy_cost:
                    trying_move_towards = self.move_towards(controller, bot_id, self.shop_loc[0], self.shop_loc[1], current_turn)
                    self.must_move_provider = not trying_move_towards 
                    if trying_move_towards:
                        controller.buy(bot_id, ShopCosts.PLATE, self.shop_loc[0], self.shop_loc[1])
            else:
                if self.must_move_assembler:
                    print("moving assembler")
                    controller.move(bot_id, random.choice([-1, 1]), random.choice([-1, 1]))

        # State 1: Place plate
        elif self.assembler_state == 1:
            if self.provider_state < 8 and self.cooked_total > 0:
                return
            if self.assembly_counter:
                tile = controller.get_tile(team, self.assembly_counter[0], self.assembly_counter[1])
                if tile and getattr(tile, 'item', None) is None:
                    trying_move_towards = self.move_towards(controller, bot_id,self.assembly_counter[0], self.assembly_counter[1], current_turn)
                    self.must_move_provider = not trying_move_towards 
                    if trying_move_towards:
                        if controller.place(bot_id, self.assembly_counter[0], self.assembly_counter[1]):
                            # Prioritize cooked ingredients (they can burn!)
                            if self.cooked_total > 0:
                                self.assembler_state = 2  # Get cooked first
                            else:
                                self.assembler_state = 4  # Just simple ingredients

        # State 2: Get cooked ingredient from pan (do this first - it can burn!)
        elif self.assembler_state == 2:
            if self.assembled_cooked_count >= self.cooked_total:
                self.assembler_state = 4  # Move to simple ingredients
            elif self.cooker_loc:
                trying_move_towards = self.move_towards(controller, bot_id,self.cooker_loc[0], self.cooker_loc[1], current_turn)
                self.must_move_provider = not trying_move_towards 
                if trying_move_towards:
                    self.must_move_provider = not controller.can_move(bot_id, self.cooker_loc[0], self.cooker_loc[1])
                    tile = controller.get_tile(team, self.cooker_loc[0], self.cooker_loc[1])
                    if tile and isinstance(tile.item, Pan) and tile.item.food:
                        if tile.item.food.cooked_stage == 1:
                            if controller.take_from_pan(bot_id, self.cooker_loc[0], self.cooker_loc[1]):
                                self.assembler_state = 3

        # State 3: Add cooked ingredient to plate
        elif self.assembler_state == 3:
            if self.assembly_counter:
                trying_move_towards = self.move_towards(controller, bot_id,self.assembly_counter[0], self.assembly_counter[1], current_turn)
                self.must_move_provider = not trying_move_towards 
                if trying_move_towards:
                    if controller.add_food_to_plate(bot_id, self.assembly_counter[0], self.assembly_counter[1]):
                        self.assembled_cooked_count += 1
                        if self.assembled_cooked_count < self.cooked_total:
                            self.assembler_state = 2  # More cooked to get
                        else:
                            self.assembler_state = 4  # Move to simple

        # State 4: Add simple ingredients
        elif self.assembler_state == 4:
            if holding:
                holding_onions = (holding["food_name"] == "ONIONS")
                    
                if self.assembly_counter:
                    trying_move_towards = self.move_towards(controller, bot_id,self.assembly_counter[0], self.assembly_counter[1], current_turn)
                    self.must_move_provider = not trying_move_towards 
                    if trying_move_towards:
                        if controller.add_food_to_plate(bot_id, self.assembly_counter[0], self.assembly_counter[1]):
                            if holding_onions:
                                self.chop_only_ingredients.pop(0)
                            else:
                                self.simple_ingredients.pop(0)
            
            # PRIORITY: chop-only ingredients (from box)
            elif (self.chop_only_ingredients and self.onion_ready_in_box > 0):
                    if self.onion_box:
                        trying_move_towards = self.move_towards(controller, bot_id,self.onion_box[0], self.onion_box[1], current_turn)
                        self.must_move_provider = not trying_move_towards 
                        if trying_move_towards:
                            self.must_move_provider = not controller.can_move(bot_id, self.onion_box[0], self.onion_box[1])
                            if controller.pickup(bot_id, self.onion_box[0], self.onion_box[1]):
                                self.onion_ready_in_box -= 1

            # THEN simple ingredients (buy)
            elif self.simple_ingredients:
                ingredient = self.simple_ingredients[0]

                if self.shop_loc and money >= ingredient.buy_cost:
                    trying_move_towards = self.move_towards(controller, bot_id,self.shop_loc[0], self.shop_loc[1], current_turn)
                    self.must_move_provider = not trying_move_towards 
                    if trying_move_towards:
                        controller.buy(bot_id, ingredient, self.shop_loc[0], self.shop_loc[1])

            elif self.chop_only_ingredients:
                pass
            else:
                self.assembler_state = 5

        # State 5: Pick up plate
        elif self.assembler_state == 5:
            if self.assembly_counter:
                trying_move_towards = self.move_towards(controller, bot_id,self.assembly_counter[0], self.assembly_counter[1], current_turn)
                self.must_move_provider = not trying_move_towards 
                if trying_move_towards:
                    if controller.pickup(bot_id, self.assembly_counter[0], self.assembly_counter[1]):
                        self.assembler_state = 6

        # State 6: Submit
        elif self.assembler_state == 6:
            if self.submit_loc:
                trying_move_towards = self.move_towards(controller, bot_id,self.submit_loc[0], self.submit_loc[1], current_turn)
                self.must_move_provider = not trying_move_towards 
                if trying_move_towards:

                    if controller.submit(bot_id, self.submit_loc[0], self.submit_loc[1]):
                        print("submitted order: ", self.current_order_id)
                        self.current_order = None
                        self.current_order_id = None  # Reset so we can take new orders
                        self.assembler_state = 7

        # State 7: Check for more orders
        elif self.assembler_state == 7:
            orders = controller.get_orders(team)
            active = [o for o in orders if o['is_active']]
            
            if active:
                # Try to find a feasible order using heuristic
                found_order = False
                
                for order in active:
                    # Compute heuristic for this order
                    heuristic = self.compute_order_heuristic(order, controller)
                    current_turn = controller.get_turn()
                    time_remaining = order['expires_turn'] - current_turn
                    
                    # If we have enough time, take this order
                    if heuristic <= time_remaining:
                        self.analyze_order(order)
                        self.provider_state = 0  # Reset provider to start cooking for new order
                        self.assembler_state = 0
                        found_order = True
                        break
                    else:
                        # Not enough time, move to next order
                        print(f"Skipping order {order.get('order_id')}: heuristic={heuristic}, time_remaining={time_remaining}")
                
                # If no feasible order found, just idle
                if not found_order:
                    if self.must_move_assembler:
                        print("moving assembler")
                        controller.move(bot_id, random.choice([-1, 1]), random.choice([-1, 1]))
            else:
                if self.must_move_assembler:
                    print("moving assembler")
                    controller.move(bot_id, random.choice([-1, 1]), random.choice([-1, 1]))