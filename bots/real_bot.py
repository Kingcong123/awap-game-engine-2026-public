import random
from collections import deque
from typing import Tuple, Optional, List, Set

from game_constants import Team, TileType, FoodType, ShopCosts
from robot_controller import RobotController
from item import Pan, Plate, Food
from tiles import SinkTable, Sink

class BotPlayer:
    def __init__(self, map_copy):
        self.map = map_copy
        self.locations = self.find_important_locations(self.map)
        
        # Role assignment
        self.provider_bot_id = None
        self.assembler_bot_id = None

        self.bot_states = {} 
        
        # COLLISION: Track where bots will be next turn to prevent crashes
        self.future_positions = set()

    def find_important_locations(self, map_instance):
        locations = {k: [] for k in ["COOKER", "SINK", "SINKTABLE", "SUBMIT", "SHOP", "TRASH", "COUNTER", "BOXES"]}
        for x in range(map_instance.width):
            for y in range(map_instance.height):
                tile = map_instance.tiles[x][y]
                if tile.tile_name in locations:
                    locations[tile.tile_name].append((x, y))
        return locations

    def play_turn(self, controller: RobotController):
        my_bots = controller.get_team_bot_ids(controller.get_team())
        if not my_bots: return
        
        # 1. Assign Roles dynamically
        if self.provider_bot_id is None and len(my_bots) >= 1:
            self.provider_bot_id = my_bots[0]
        if self.assembler_bot_id is None and len(my_bots) >= 2:
            self.assembler_bot_id = my_bots[1]

        # 2. Reset collision reservations for this turn
        self.future_positions = set()
        # Mark current positions as occupied initially (bots that don't move stay there)
        for bid in my_bots:
            state = controller.get_bot_state(bid)
            self.future_positions.add((state['x'], state['y']))

        # 3. Execute Bot Logic
        # We iterate specifically to prioritize roles (Provider moves first, then Assembler works around them)
        if self.provider_bot_id:
            self.play_provider_bot(controller, self.provider_bot_id)
        
        if self.assembler_bot_id:
            # You would implement play_assembler_bot here
            # self.play_assembler_bot(controller, self.assembler_bot_id)
            pass

    def play_provider_bot(self, controller, bot_id):
        if bot_id not in self.bot_states:
            self.bot_states[bot_id] = 0 

        # DATA: Read actual map data instead of internal counters
        # Find the SinkTable object to check plate count
        sink_table_loc = self.locations["SINKTABLE"][0]
        sink_table_tile = controller.get_map(controller.get_team()).tiles[sink_table_loc[0]][sink_table_loc[1]]
        
        # Check actual game state
        num_clean = sink_table_tile.num_clean_plates if isinstance(sink_table_tile, SinkTable) else 0
        
        # Simple State Machine
        if self.bot_states[bot_id] == 0:
            if num_clean < 2: # Keep a buffer of 2 plates
                self.bot_states[bot_id] = 1 # Fetch Plate
            else:
                self.bot_states[bot_id] = 2 # Wash Dishes (Idle action)

        if self.bot_states[bot_id] == 1:
            if self.get_plate(controller, bot_id):
                self.bot_states[bot_id] = 0

        elif self.bot_states[bot_id] == 2:
            if self.wash_dishes(controller, bot_id):
                # If we washed successfully, re-evaluate
                self.bot_states[bot_id] = 0

    def get_bfs_path(self, controller: RobotController, start: Tuple[int, int], target_predicate) -> Optional[Tuple[int, int]]:
        queue = deque([(start, [])]) 
        visited = set([start])
        w, h = self.map.width, self.map.height

        while queue:
            (curr_x, curr_y), path = queue.popleft()
            tile = controller.get_tile(controller.get_team(), curr_x, curr_y)
            
            if target_predicate(curr_x, curr_y, tile):
                if not path: return (0, 0) # Already there
                return path[0] # Return the first step (dx, dy)

            for dx in [0, -1, 1]:
                for dy in [0, -1, 1]:
                    if abs(dx) + abs(dy) != 1: continue # strictly up/down/left/right (Manhattan/Von Neumann) usually safer, or use Chebyshev if allowed
                    
                    nx, ny = curr_x + dx, curr_y + dy
                    
                    # COLLISION CHECK:
                    # 1. Bounds & Walkability
                    # 2. "future_positions": Don't step where a friend is going/staying
                    if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in visited:
                        is_walkable = controller.get_map(controller.get_team()).is_tile_walkable(nx, ny)
                        is_occupied = (nx, ny) in self.future_positions
                        
                        # Note: We allow entering "occupied" tiles ONLY if it's the target 
                        # (because we might be interacting with the bot/station there), 
                        # but standard pathfinding usually blocks it. 
                        # Safest: Treat future_positions as walls.
                        if is_walkable and not is_occupied:
                            visited.add((nx, ny))
                            queue.append(((nx, ny), path + [(dx, dy)]))
        return None

    def move_towards(self, controller: RobotController, bot_id: int, target_x: int, target_y: int) -> bool:
        bot_state = controller.get_bot_state(bot_id)
        bx, by = bot_state['x'], bot_state['y']
        
        def is_adjacent_to_target(x, y, tile):
            return max(abs(x - target_x), abs(y - target_y)) <= 1
            
        if is_adjacent_to_target(bx, by, None): return True
        
        # Calculate step
        step = self.get_bfs_path(controller, (bx, by), is_adjacent_to_target)
        
        if step and (step[0] != 0 or step[1] != 0):
            new_x, new_y = bx + step[0], by + step[1]
            
            # EXECUTE MOVE
            controller.move(bot_id, step[0], step[1])
            
            # UPDATE COLLISION TRACKING
            # Remove old position reservation, add new position reservation
            if (bx, by) in self.future_positions:
                self.future_positions.remove((bx, by))
            self.future_positions.add((new_x, new_y))
            
            return False 
        return False 

    def get_plate(self, controller, bot_id):
        bot_state = controller.get_bot_state(bot_id)
        bx, by = bot_state['x'], bot_state['y']
        
        if not bot_state['holding']:
            shop_pos = self.find_nearest_tile(controller, bx, by, 'SHOP')
            if not shop_pos: return False # Handle missing shop
            
            shop_x, shop_y = shop_pos

            # Move returns True if we are adjacent
            if self.move_towards(controller, bot_id, shop_x, shop_y): 
                # Adjacent, now buy
                controller.buy(bot_id, ShopCosts.PLATE, shop_x, shop_y)
                return False
            
        else:
            if not self.locations["SINKTABLE"]: return False
            x, y = self.locations["SINKTABLE"][0]

            if self.move_towards(controller, bot_id, x, y):
                controller.place(bot_id, x, y)
                return True
        return False

    def wash_dishes(self, controller: RobotController, bot_id):
        bot_state = controller.get_bot_state(bot_id)
        bx, by = bot_state['x'], bot_state['y']
        
        sink_pos = self.find_nearest_tile(controller, bx, by, 'SINK')
        if not sink_pos: return False
        
        sinkx, sinky = sink_pos

        if self.move_towards(controller, bot_id, sinkx, sinky):
            # Only wash if there are dirty plates?
            # Good optimization: check sink tile for dirty plates before washing
            controller.wash_sink(bot_id, sinkx, sinky)
            return True
        return False

    def find_nearest_tile(self, controller: RobotController, bot_x: int, bot_y: int, tile_name: str) -> Optional[Tuple[int, int]]:
        # Your existing logic is fine, just ensure it handles empty lists
        if tile_name not in self.locations or not self.locations[tile_name]:
            return None
        
        best_pos = None
        best_dist = 9999
        for (x, y) in self.locations[tile_name]:
            dist = max(abs(bot_x - x), abs(bot_y - y))
            if dist < best_dist:
                best_dist = dist
                best_pos = (x, y)
        return best_pos