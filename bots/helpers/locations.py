
def find_important_locations(map_instance):
        locations = {
            "COOKER": None,
            "SINK": None,
            "SINKTABLE": None,
            "SUBMIT": None,
            "SHOP": None,
            "TRASH": None,
            "CHOP_COUNTER": None,     # Deduced heuristic
            "ASSEMBLY_COUNTER": None, # Deduced heuristic
            "WAITING_ZONE": None      # Deduced heuristic
        }

        counters = []
        floors = []

        # 1. Scan the grid for fixed stations
        for x in range(map_instance.width):
            for y in range(map_instance.height):
                tile_name = map_instance.tiles[x][y].tile_name
                
                # Map the exact tile names to our dictionary keys
                if tile_name in locations:
                    locations[tile_name] = (x, y)
                elif tile_name == "COUNTER":
                    counters.append((x, y))
                elif tile_name == "FLOOR":
                    floors.append((x, y))

        # 2. Heuristic: CHOP_COUNTER
        # The best place to chop is usually the counter closest to the Shop 
        # (since that is where you buy the Meat and Onions).
        if locations["SHOP"] and counters:
            sx, sy = locations["SHOP"]
            # Find counter with minimum Chebyshev distance to Shop
            locations["CHOP_COUNTER"] = min(counters, key=lambda p: max(abs(p[0]-sx), abs(p[1]-sy)))

        # 3. Heuristic: ASSEMBLY_COUNTER
        # The best place to assemble/plate food is the counter closest to the Cooker.
        # Ideally, we pick one that ISN'T the chop counter to reduce bot collisions.
        if locations["COOKER"] and counters:
            cx, cy = locations["COOKER"]
            candidates = [c for c in counters if c != locations["CHOP_COUNTER"]]
            if not candidates: 
                candidates = counters # Fallback if only 1 counter exists on the map
            locations["ASSEMBLY_COUNTER"] = min(candidates, key=lambda p: max(abs(p[0]-cx), abs(p[1]-cy)))

        # 4. Heuristic: WAITING_ZONE
        # A safe place for a bot to stand when it has completed its tasks.
        # We pick the floor tile FURTHEST from the busy Cooker to avoid blocking traffic.
        if locations["COOKER"] and floors:
            cx, cy = locations["COOKER"]
            locations["WAITING_ZONE"] = max(floors, key=lambda p: max(abs(p[0]-cx), abs(p[1]-cy)))
        elif floors:
            locations["WAITING_ZONE"] = floors[0]

        return locations