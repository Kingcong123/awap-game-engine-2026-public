from collections import deque
from typing import Tuple, Optional, List, Dict, Any

from game_constants import FoodType, ShopCosts
from robot_controller import RobotController
from item import Pan, Plate, Food


class BotPlayer:
    def __init__(self, map_copy):
        self.map = map_copy
        self.locations = self.find_important_locations(self.map)

        # Current order tracking
        self.current_order = None
        self.current_order_id = None

        # Ingredients lists
        self.cooked_ingredients = []  # Ingredients that need cooking
        self.chop_only_ingredients = []  # Ingredients that need chopping but not cooking
        self.simple_ingredients = []   # Ingredients that don't need cooking or chopping

        # Bot assignments - which cooked ingredient each bot is working on
        self.bot_assignments = {}  # bot_id -> {'ingredient': FoodType, 'state': int, 'cooker': (x,y)}

        # Track which cookers have pans
        self.cooker_pans = {}  # (x,y) -> True if has pan

        # Assembler state (when a bot switches to assembling)
        self.assembler_bot_id = None
        self.assembler_state = 0

        # Cached locations per bot
        self.bot_locations = {}  # bot_id -> {'shop': (x,y), 'cooker': (x,y), ...}

        # Per-turn cache
        self.cached_turn = -1
        self.cached_bot_positions = set()

        # Order expiration tracking
        self.order_expired = False
        self.bot_needs_trash = set()

        # Helper bot state for preparing chop-only ingredients
        self.helper_assignments = {}  # bot_id -> {'ingredient': FoodType, 'state': int, 'counter': (x,y)}
        self.prepared_chop_ingredients = []  # List of (counter_loc, ingredient) ready for assembler

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

        queue = deque([(start, None)])
        visited = {start}
        w, h = self.map.width, self.map.height

        while queue:
            (cx, cy), first_step = queue.popleft()

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

    def score_order(self, order: Dict[str, Any], current_turn: int) -> float:
        """Score an order for priority. Higher = better to work on."""
        if not order.get('is_active'):
            return -9999

        reward = order.get('reward', 0)
        penalty = order.get('penalty', 0)
        expires = order.get('expires_turn', 999)
        turns_left = expires - current_turn

        if turns_left <= 0:
            return -9999  # Already expired

        # Count ingredients to estimate complexity
        required = order.get('required', [])
        num_cooked = sum(1 for f in required if self.get_food_type_by_name(f) and self.get_food_type_by_name(f).can_cook)
        complexity = len(required) + num_cooked  # Cooked items count double

        # Prioritize: high reward, high penalty (don't want to miss), enough time
        # Score = (reward + penalty) / complexity, boosted if running out of time
        urgency = 1.0
        if turns_left < complexity * 15:  # Getting tight on time
            urgency = 2.0
        if turns_left < complexity * 8:   # Very urgent
            urgency = 3.0

        return (reward + penalty) * urgency / max(complexity, 1)

    def select_best_order(self, controller: RobotController) -> Optional[Dict[str, Any]]:
        """Select the best order to work on."""
        team = controller.get_team()
        orders = controller.get_orders(team)
        current_turn = controller.get_turn()

        active_orders = [o for o in orders if o.get('is_active')]
        if not active_orders:
            return None

        # Score and sort orders
        scored = [(self.score_order(o, current_turn), o) for o in active_orders]
        scored.sort(reverse=True, key=lambda x: x[0])

        return scored[0][1] if scored[0][0] > -9999 else None

    def analyze_order(self, order: Dict[str, Any]) -> None:
        """Analyze order and split ingredients."""
        self.current_order = order
        self.current_order_id = order.get('order_id')
        self.cooked_ingredients = []
        self.chop_only_ingredients = []
        self.simple_ingredients = []

        for food_name in order.get('required', []):
            ft = self.get_food_type_by_name(food_name)
            if ft:
                if ft.can_cook:
                    self.cooked_ingredients.append(ft)
                elif ft.can_chop:
                    self.chop_only_ingredients.append(ft)
                else:
                    self.simple_ingredients.append(ft)

    def assign_cooking_tasks(self, controller: RobotController):
        """Assign cooked ingredients to bots."""
        my_bots = controller.get_team_bot_ids(controller.get_team())
        team = controller.get_team()

        # Find available cookers
        cookers = self.locations["COOKER"]

        # Initialize bot locations if needed
        for bot_id in my_bots:
            if bot_id not in self.bot_locations:
                state = controller.get_bot_state(bot_id)
                bx, by = state['x'], state['y']

                def nearest(locs):
                    if not locs:
                        return None
                    return min(locs, key=lambda p: abs(p[0]-bx) + abs(p[1]-by))

                self.bot_locations[bot_id] = {
                    'shop': nearest(self.locations["SHOP"]),
                    'cooker': nearest(cookers),
                    'counter': nearest(self.locations["COUNTER"]),
                    'submit': nearest(self.locations["SUBMIT"]),
                    'trash': nearest(self.locations["TRASH"]),
                }

        # Clear old assignments
        self.bot_assignments = {}
        self.assembler_bot_id = None
        self.assembler_state = 0

        # Assign cooked ingredients to bots
        for i, ingredient in enumerate(self.cooked_ingredients):
            bot_id = my_bots[i % len(my_bots)]

            # Find a cooker for this bot
            bot_cooker = self.bot_locations[bot_id]['cooker']

            self.bot_assignments[bot_id] = {
                'ingredient': ingredient,
                'state': 0,  # 0=init, 1=buy_pan, 2=place_pan, 3=buy_food, 4=chop_place, 5=chop, 6=pickup, 7=place_pan, 8=cooking, 9=done
                'cooker': bot_cooker,
                'done': False
            }

    def play_turn(self, controller: RobotController):
        my_bots = controller.get_team_bot_ids(controller.get_team())
        if not my_bots:
            return

        current_turn = controller.get_turn()
        team = controller.get_team()

        # Check if current order has expired
        if self.current_order is not None:
            expires = self.current_order.get('expires_turn', 999)
            if current_turn >= expires:
                # Order expired - need to trash and reset
                self.order_expired = True
                # Mark all bots for trashing
                for bot_id in my_bots:
                    state = controller.get_bot_state(bot_id)
                    if state and state['holding']:
                        self.bot_needs_trash = self.bot_needs_trash if hasattr(self, 'bot_needs_trash') else set()
                        self.bot_needs_trash.add(bot_id)

        # Handle trashing for bots that need it
        if hasattr(self, 'bot_needs_trash') and self.bot_needs_trash:
            trash_loc = self.locations["TRASH"][0] if self.locations["TRASH"] else None
            counters = self.locations["COUNTER"]
            for bot_id in list(self.bot_needs_trash):
                state = controller.get_bot_state(bot_id)
                if state and state['holding']:
                    holding_type = state['holding'].get('type') if state['holding'] else None

                    # If holding a Pan or Plate that's already empty/clean, place it on counter
                    if holding_type == 'Pan':
                        pan_food = state['holding'].get('food')
                        if pan_food is None:
                            # Empty pan - place on counter or cooker
                            cookers = self.locations["COOKER"]
                            place_loc = cookers[0] if cookers else (counters[0] if counters else None)
                            if place_loc:
                                if self.move_towards(controller, bot_id, place_loc[0], place_loc[1], current_turn):
                                    if controller.place(bot_id, place_loc[0], place_loc[1]):
                                        self.bot_needs_trash.discard(bot_id)
                        elif trash_loc:
                            # Pan has food - trash it first
                            if self.move_towards(controller, bot_id, trash_loc[0], trash_loc[1], current_turn):
                                controller.trash(bot_id, trash_loc[0], trash_loc[1])
                    elif holding_type == 'Plate':
                        plate_food = state['holding'].get('food', [])
                        is_dirty = state['holding'].get('dirty', False)
                        if not plate_food and is_dirty:
                            # Dirty empty plate - put in sink
                            sinks = self.locations["SINK"]
                            if sinks:
                                sink_loc = sinks[0]
                                if self.move_towards(controller, bot_id, sink_loc[0], sink_loc[1], current_turn):
                                    if controller.put_dirty_plate_in_sink(bot_id, sink_loc[0], sink_loc[1]):
                                        self.bot_needs_trash.discard(bot_id)
                        elif not plate_food and not is_dirty:
                            # Clean empty plate - place on counter for reuse
                            if counters:
                                place_loc = counters[0]
                                if self.move_towards(controller, bot_id, place_loc[0], place_loc[1], current_turn):
                                    if controller.place(bot_id, place_loc[0], place_loc[1]):
                                        self.bot_needs_trash.discard(bot_id)
                        elif plate_food and trash_loc:
                            # Has food - trash it (becomes dirty empty plate)
                            if self.move_towards(controller, bot_id, trash_loc[0], trash_loc[1], current_turn):
                                controller.trash(bot_id, trash_loc[0], trash_loc[1])
                                # After trashing, it becomes dirty empty plate - handle next turn
                    elif trash_loc:
                        # Regular food item - just trash it
                        if self.move_towards(controller, bot_id, trash_loc[0], trash_loc[1], current_turn):
                            if controller.trash(bot_id, trash_loc[0], trash_loc[1]):
                                self.bot_needs_trash.discard(bot_id)
                elif state and not state['holding']:
                    self.bot_needs_trash.discard(bot_id)

            # If all bots done trashing, reset order state
            if not self.bot_needs_trash:
                self.current_order = None
                self.current_order_id = None
                self.bot_assignments = {}
                self.helper_assignments = {}
                self.prepared_chop_ingredients = []
                self.assembler_bot_id = None
                self.assembler_state = 0
                self.order_expired = False
            return  # Don't do anything else while trashing

        # Select order if we don't have one
        if self.current_order is None:
            best_order = self.select_best_order(controller)
            if best_order:
                self.analyze_order(best_order)
                self.assign_cooking_tasks(controller)

        if self.current_order is None:
            return  # No active orders

        # Check if all cooking is done
        all_cooking_done = all(
            self.bot_assignments.get(bid, {}).get('done', True)
            for bid in my_bots if bid in self.bot_assignments
        )

        # If no cooked ingredients needed, go straight to assembly
        if not self.cooked_ingredients:
            all_cooking_done = True

        # If all cooking done and no assembler yet, assign one
        if all_cooking_done and self.assembler_bot_id is None:
            self.assembler_bot_id = my_bots[-1]  # Last bot becomes assembler
            self.assembler_state = 0

        # Run each bot
        for bot_id in my_bots:
            if bot_id == self.assembler_bot_id:
                self.play_assembler(controller, bot_id, current_turn)
            elif bot_id in self.bot_assignments:
                self.play_cook(controller, bot_id, current_turn)
            else:
                # Idle bot - help by preparing chop-only ingredients
                self.play_helper(controller, bot_id, current_turn)

    def play_cook(self, controller: RobotController, bot_id: int, current_turn: int):
        """Bot works on cooking its assigned ingredient."""
        assignment = self.bot_assignments.get(bot_id)
        if not assignment or assignment['done']:
            return

        state_info = controller.get_bot_state(bot_id)
        bx, by = state_info['x'], state_info['y']
        holding = state_info['holding']
        team = controller.get_team()
        money = controller.get_team_money(team)

        ingredient = assignment['ingredient']
        cooker_loc = assignment['cooker']
        locs = self.bot_locations[bot_id]
        shop_loc = locs['shop']
        counter_loc = locs['counter']
        trash_loc = locs['trash']

        state = assignment['state']

        # State 0: Check if cooker has pan
        if state == 0:
            if cooker_loc:
                tile = controller.get_tile(team, cooker_loc[0], cooker_loc[1])
                if tile and isinstance(tile.item, Pan):
                    assignment['state'] = 3  # Has pan, buy ingredient
                else:
                    assignment['state'] = 1  # Need to buy pan

        # State 1: Buy pan
        elif state == 1:
            if holding:
                assignment['state'] = 2
            elif shop_loc and money >= ShopCosts.PAN.buy_cost:
                if self.move_towards(controller, bot_id, shop_loc[0], shop_loc[1], current_turn):
                    controller.buy(bot_id, ShopCosts.PAN, shop_loc[0], shop_loc[1])

        # State 2: Place pan on cooker
        elif state == 2:
            if cooker_loc:
                if self.move_towards(controller, bot_id, cooker_loc[0], cooker_loc[1], current_turn):
                    if controller.place(bot_id, cooker_loc[0], cooker_loc[1]):
                        assignment['state'] = 3

        # State 3: Buy ingredient
        elif state == 3:
            if holding:
                if ingredient.can_chop:
                    assignment['state'] = 4
                else:
                    assignment['state'] = 7
            elif shop_loc and money >= ingredient.buy_cost:
                if self.move_towards(controller, bot_id, shop_loc[0], shop_loc[1], current_turn):
                    controller.buy(bot_id, ingredient, shop_loc[0], shop_loc[1])

        # State 4: Place ingredient for chopping
        elif state == 4:
            if counter_loc:
                tile = controller.get_tile(team, counter_loc[0], counter_loc[1])
                if tile and getattr(tile, 'item', None) is None:
                    if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                        if controller.place(bot_id, counter_loc[0], counter_loc[1]):
                            assignment['state'] = 5

        # State 5: Chop
        elif state == 5:
            if counter_loc:
                if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                    if controller.chop(bot_id, counter_loc[0], counter_loc[1]):
                        assignment['state'] = 6

        # State 6: Pick up chopped ingredient
        elif state == 6:
            if counter_loc:
                if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                    if controller.pickup(bot_id, counter_loc[0], counter_loc[1]):
                        assignment['state'] = 7

        # State 7: Place in pan
        elif state == 7:
            if cooker_loc:
                if self.move_towards(controller, bot_id, cooker_loc[0], cooker_loc[1], current_turn):
                    if controller.place(bot_id, cooker_loc[0], cooker_loc[1]):
                        assignment['state'] = 8

        # State 8: Wait for cooking
        elif state == 8:
            if cooker_loc:
                tile = controller.get_tile(team, cooker_loc[0], cooker_loc[1])
                if tile and isinstance(tile.item, Pan) and tile.item.food:
                    if tile.item.food.cooked_stage == 1:
                        assignment['state'] = 9
                        assignment['done'] = True
                    elif tile.item.food.cooked_stage == 2:
                        # Burnt! Take it out and trash it
                        if self.move_towards(controller, bot_id, cooker_loc[0], cooker_loc[1], current_turn):
                            if controller.take_from_pan(bot_id, cooker_loc[0], cooker_loc[1]):
                                assignment['state'] = 99  # Go to trash

        # State 9: Done cooking, wait for assembler
        elif state == 9:
            # Move away from cooker to let assembler access
            if shop_loc:
                self.move_towards(controller, bot_id, shop_loc[0], shop_loc[1], current_turn)

        # State 99: Trash burnt food and restart
        elif state == 99:
            if trash_loc:
                if self.move_towards(controller, bot_id, trash_loc[0], trash_loc[1], current_turn):
                    if controller.trash(bot_id, trash_loc[0], trash_loc[1]):
                        assignment['state'] = 3  # Try again

    def play_helper(self, controller: RobotController, bot_id: int, current_turn: int):
        """Idle bot helps by preparing chop-only ingredients."""
        # Initialize bot locations if needed
        if bot_id not in self.bot_locations:
            state = controller.get_bot_state(bot_id)
            bx, by = state['x'], state['y']
            def nearest(locs):
                if not locs:
                    return None
                return min(locs, key=lambda p: abs(p[0]-bx) + abs(p[1]-by))
            self.bot_locations[bot_id] = {
                'shop': nearest(self.locations["SHOP"]),
                'cooker': nearest(self.locations["COOKER"]),
                'counter': nearest(self.locations["COUNTER"]),
                'submit': nearest(self.locations["SUBMIT"]),
                'trash': nearest(self.locations["TRASH"]),
            }

        state_info = controller.get_bot_state(bot_id)
        holding = state_info['holding']
        team = controller.get_team()
        money = controller.get_team_money(team)
        locs = self.bot_locations[bot_id]
        shop_loc = locs['shop']

        # Check if this bot has an existing helper assignment
        if bot_id not in self.helper_assignments:
            # Find an unassigned chop-only ingredient to work on
            assigned_ingredients = {a['ingredient'] for a in self.helper_assignments.values()}
            prepared_ingredients = {ing for _, ing in self.prepared_chop_ingredients}

            for ingredient in self.chop_only_ingredients:
                if ingredient not in assigned_ingredients and ingredient not in prepared_ingredients:
                    # Find a free counter for this bot
                    counters = self.locations["COUNTER"]
                    used_counters = {a.get('counter') for a in self.helper_assignments.values() if a.get('counter')}
                    free_counter = None
                    for c in counters:
                        if c not in used_counters:
                            tile = controller.get_tile(team, c[0], c[1])
                            if tile and getattr(tile, 'item', None) is None:
                                free_counter = c
                                break

                    if free_counter:
                        self.helper_assignments[bot_id] = {
                            'ingredient': ingredient,
                            'state': 0,  # 0=buy, 1=place, 2=chop, 3=done
                            'counter': free_counter
                        }
                        break

            if bot_id not in self.helper_assignments:
                return  # Nothing to do

        assignment = self.helper_assignments[bot_id]
        ingredient = assignment['ingredient']
        counter_loc = assignment['counter']
        state = assignment['state']

        # State 0: Buy ingredient
        if state == 0:
            if holding:
                assignment['state'] = 1
            elif shop_loc and money >= ingredient.buy_cost:
                if self.move_towards(controller, bot_id, shop_loc[0], shop_loc[1], current_turn):
                    controller.buy(bot_id, ingredient, shop_loc[0], shop_loc[1])

        # State 1: Place on counter
        elif state == 1:
            if counter_loc:
                if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                    if controller.place(bot_id, counter_loc[0], counter_loc[1]):
                        assignment['state'] = 2

        # State 2: Chop
        elif state == 2:
            if counter_loc:
                if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                    if controller.chop(bot_id, counter_loc[0], counter_loc[1]):
                        assignment['state'] = 3
                        # Mark as prepared for assembler
                        self.prepared_chop_ingredients.append((counter_loc, ingredient))

        # State 3: Done, wait or help with another
        elif state == 3:
            # Clear assignment so bot can help with another ingredient
            del self.helper_assignments[bot_id]

    def play_assembler(self, controller: RobotController, bot_id: int, current_turn: int):
        """Bot assembles the plate and submits."""
        state_info = controller.get_bot_state(bot_id)
        bx, by = state_info['x'], state_info['y']
        holding = state_info['holding']
        team = controller.get_team()
        money = controller.get_team_money(team)

        locs = self.bot_locations.get(bot_id, {})
        shop_loc = locs.get('shop')
        counter_loc = locs.get('counter')
        submit_loc = locs.get('submit')

        # State 0: Buy plate
        if self.assembler_state == 0:
            if holding:
                self.assembler_state = 1
            elif shop_loc and money >= ShopCosts.PLATE.buy_cost:
                if self.move_towards(controller, bot_id, shop_loc[0], shop_loc[1], current_turn):
                    controller.buy(bot_id, ShopCosts.PLATE, shop_loc[0], shop_loc[1])

        # State 1: Place plate on counter
        elif self.assembler_state == 1:
            if counter_loc:
                tile = controller.get_tile(team, counter_loc[0], counter_loc[1])
                if tile and getattr(tile, 'item', None) is None:
                    if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                        if controller.place(bot_id, counter_loc[0], counter_loc[1]):
                            self.assembler_state = 2

        # State 2: Collect cooked ingredients from all cookers
        elif self.assembler_state == 2:
            # Find a cooker with cooked food
            for other_bot_id, assignment in self.bot_assignments.items():
                if assignment.get('done') and assignment.get('state') == 9:
                    cooker_loc = assignment['cooker']
                    tile = controller.get_tile(team, cooker_loc[0], cooker_loc[1])
                    if tile and isinstance(tile.item, Pan) and tile.item.food:
                        if tile.item.food.cooked_stage == 1:
                            if self.move_towards(controller, bot_id, cooker_loc[0], cooker_loc[1], current_turn):
                                if controller.take_from_pan(bot_id, cooker_loc[0], cooker_loc[1]):
                                    self.assembler_state = 3
                            return
            # No more cooked ingredients to collect
            self.assembler_state = 4

        # State 3: Add cooked ingredient to plate
        elif self.assembler_state == 3:
            if counter_loc:
                if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                    if controller.add_food_to_plate(bot_id, counter_loc[0], counter_loc[1]):
                        self.assembler_state = 2  # Check for more cooked

        # State 4: Handle chop-only ingredients (check for pre-prepared first)
        elif self.assembler_state == 4:
            if self.chop_only_ingredients:
                ingredient = self.chop_only_ingredients[0]

                # First check if helper has prepared this ingredient
                prepared_loc = None
                for i, (loc, prep_ing) in enumerate(self.prepared_chop_ingredients):
                    if prep_ing == ingredient:
                        prepared_loc = loc
                        self.prepared_chop_ingredients.pop(i)
                        break

                if prepared_loc:
                    # Pick up pre-prepared ingredient
                    if holding:
                        self.assembler_state = 7  # Go add to plate
                    else:
                        if self.move_towards(controller, bot_id, prepared_loc[0], prepared_loc[1], current_turn):
                            if controller.pickup(bot_id, prepared_loc[0], prepared_loc[1]):
                                self.assembler_state = 7  # Go add to plate (state 7 will pop)
                elif holding:
                    self.assembler_state = 5  # Go place it for chopping
                elif shop_loc and money >= ingredient.buy_cost:
                    if self.move_towards(controller, bot_id, shop_loc[0], shop_loc[1], current_turn):
                        controller.buy(bot_id, ingredient, shop_loc[0], shop_loc[1])
            else:
                self.assembler_state = 8  # Skip to simple ingredients

        # State 5: Place chop-only ingredient on counter for chopping
        elif self.assembler_state == 5:
            # Find a different counter than where the plate is (use second counter if available)
            counters = self.locations["COUNTER"]
            chop_counter = None
            for c in counters:
                if c != counter_loc:
                    chop_counter = c
                    break
            if chop_counter is None:
                chop_counter = counter_loc  # Fallback, but plate may be there

            if chop_counter:
                tile = controller.get_tile(team, chop_counter[0], chop_counter[1])
                if tile and getattr(tile, 'item', None) is None:
                    if self.move_towards(controller, bot_id, chop_counter[0], chop_counter[1], current_turn):
                        if controller.place(bot_id, chop_counter[0], chop_counter[1]):
                            self.chop_counter = chop_counter  # Remember where we put it
                            self.assembler_state = 6

        # State 6: Chop the ingredient
        elif self.assembler_state == 6:
            chop_counter = getattr(self, 'chop_counter', None)
            if chop_counter:
                if self.move_towards(controller, bot_id, chop_counter[0], chop_counter[1], current_turn):
                    if controller.chop(bot_id, chop_counter[0], chop_counter[1]):
                        self.assembler_state = 7

        # State 7: Pick up chopped ingredient and add to plate
        elif self.assembler_state == 7:
            chop_counter = getattr(self, 'chop_counter', None)
            if holding:
                # Add to plate
                if counter_loc:
                    if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                        if controller.add_food_to_plate(bot_id, counter_loc[0], counter_loc[1]):
                            self.chop_only_ingredients.pop(0)
                            self.assembler_state = 4  # Check for more chop-only
            elif chop_counter:
                if self.move_towards(controller, bot_id, chop_counter[0], chop_counter[1], current_turn):
                    controller.pickup(bot_id, chop_counter[0], chop_counter[1])

        # State 8: Add simple ingredients
        elif self.assembler_state == 8:
            if self.simple_ingredients:
                ingredient = self.simple_ingredients[0]
                if holding:
                    if counter_loc:
                        if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                            if controller.add_food_to_plate(bot_id, counter_loc[0], counter_loc[1]):
                                self.simple_ingredients.pop(0)
                elif shop_loc and money >= ingredient.buy_cost:
                    if self.move_towards(controller, bot_id, shop_loc[0], shop_loc[1], current_turn):
                        controller.buy(bot_id, ingredient, shop_loc[0], shop_loc[1])
            else:
                self.assembler_state = 9

        # State 9: Pick up plate
        elif self.assembler_state == 9:
            if counter_loc:
                if self.move_towards(controller, bot_id, counter_loc[0], counter_loc[1], current_turn):
                    if controller.pickup(bot_id, counter_loc[0], counter_loc[1]):
                        self.assembler_state = 10

        # State 10: Submit
        elif self.assembler_state == 10:
            if submit_loc:
                if self.move_towards(controller, bot_id, submit_loc[0], submit_loc[1], current_turn):
                    if controller.submit(bot_id, submit_loc[0], submit_loc[1]):
                        self.assembler_state = 11

        # State 11: Done, get next order
        elif self.assembler_state == 11:
            self.current_order = None
            self.current_order_id = None
            self.bot_assignments = {}
            self.helper_assignments = {}
            self.prepared_chop_ingredients = []
            self.assembler_bot_id = None
            self.assembler_state = 0
