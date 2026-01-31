
def find_important_locations(map_instance):
        locations = {
            "COOKER": [],           # 'K'
            "SINK": [],             # 'S'
            "SINKTABLE": [],        # 'T'
            "SUBMIT": [],           # 'U'
            "SHOP": [],             # '$'
            "TRASH": [],            # 'R'
            "COUNTER": [],          # 'C'
            "BOX": []               # 'B'
        }

        # 1. Scan the grid for fixed stations
        for x in range(map_instance.width):
            for y in range(map_instance.height):
                tile_name = map_instance.tiles[x][y].tile_name
                
                # Map the exact tile names to our dictionary keys
                if tile_name in locations:
                    locations[tile_name].append((x, y))

        return locations