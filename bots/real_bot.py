import random
from collections import deque
from typing import Tuple, Optional, List

from game_constants import Team, TileType, FoodType, ShopCosts
from robot_controller import RobotController
from item import Pan, Plate, Food

class BotPlayer:
    def __init__(self, map_copy):
        self.map = map_copy
        self.locations = self.find_important_locations(self.map)
        self.assembler_bot_id = None
        self.provider_bot_id = None

        self.boxes = [(-1, x,y) for (x,y) in self.locations["BOXES"]]
        self.cookers = [(True, False, x,y) for (x,y) in self.locations["COOKER"]]

        self.bot_states = {}     # Tracks what each bot is doing
        self.current_order_target = None
        self.ingredients_processed_count = 0

        self.invading = False
        self.state = 0

        self.clean_plates = 0
        self.dirty_plates = 0

        self.pans = 0


    def find_important_locations(self, map_instance):
        locations = {
            "COOKER": [],           # 'K'
            "SINK": [],             # 'S'
            "SINKTABLE": [],        # 'T'
            "SUBMIT": [],           # 'U'
            "SHOP": [],             # '$'
            "TRASH": [],            # 'R'
            "COUNTER": [],          # 'C'
            "BOXES": []               # 'B'
        }

        # 1. Scan the grid for fixed stations
        for x in range(map_instance.width):
            for y in range(map_instance.height):
                tile_name = map_instance.tiles[x][y].tile_name
                
                # Map the exact tile names to our dictionary keys
                if tile_name in locations:
                    locations[tile_name].append((x, y))

        return locations

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
                        if controller.get_map(controller.get_team()).is_tile_walkable(nx, ny):
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
        m = controller.get_map(controller.get_team())
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
        my_bots = controller.get_team_bot_ids(controller.get_team())
        if not my_bots: return
    
        self.provider_bot_id = my_bots[0]
        provider_bot_id = self.provider_bot_id

        self.play_provider_bot(controller, provider_bot_id)
     
    def play_provider_bot(self, controller, bot_id):
        if bot_id not in self.bot_states:
            self.bot_states[bot_id] = 0 #init state

        if self.bot_states[bot_id] == 0:
            if self.clean_plates<1:
                self.bot_states[bot_id] = 1

            elif self.dirty_plates == 1:
                self.bot_states[bot_id] = 2


        if self.bot_states[bot_id] == 1:
            if self.get_plate(controller, bot_id):
                self.bot_states[bot_id] = 0

        elif self.bot_states[bot_id] == 2:
            if self.wash_dishes(controller, bot_id):
                self.bot_states[bot_id] = 0
    
    
    def get_plate(self, controller, bot_id):
        bot_state = controller.get_bot_state(bot_id)
        bx, by = bot_state['x'], bot_state['y']
        if not bot_state['holding']:
            shop_x, shop_y = self.find_nearest_tile(controller, bx, by, 'SHOP')

            self.move_towards(controller, bot_id, shop_x, shop_y)

            if (abs(shop_x-bx) <= 1 and abs(shop_y-by) <= 1): # can access shop
                controller.buy(bot_id, ShopCosts.PLATE, shop_x, shop_y)
                return False
            
        else:
            x, y = self.locations["SINKTABLE"][0]

            if (abs(x-bx) <= 1 and abs(y-by) <= 1): # can access stove
                controller.place(bot_id, x, y)
                self.clean_plates+=1
                return True
            else:
                self.move_towards(controller, bot_id, x, y)
                return False

    def wash_dishes(self, controller: RobotController, bot_id):
        bot_state = controller.get_bot_state(bot_id)
        bx, by = bot_state['x'], bot_state['y']
        sinkx, sinky = self.find_nearest_tile(controller, bx, by, 'SINK')

        self.move_towards(controller, bot_id, sinkx, sinky)

        if (abs(sinkx-bx) <= 1 and abs(sinky-by) <= 1): #can access sink
            if controller.wash_sink(bot_id, sinkx, sinky):
                self.clean_plates+=1
                self.dirty_plates-=1
                return True
        return False