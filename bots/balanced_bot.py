from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from game_constants import FoodType, ShopCosts, Team
from robot_controller import RobotController
from item import Food, Plate, Pan


@dataclass(frozen=True)
class Task:
    kind: str
    target: Tuple[int, int]
    priority: int
    meta: Dict[str, Any]


@dataclass
class Snapshot:
    turn: int
    team: Team
    money: int
    bots: Dict[int, Dict[str, Any]]
    counters: Dict[Tuple[int, int], Any]
    cookers: Dict[Tuple[int, int], Any]
    sinktables: Dict[Tuple[int, int], Any]
    sinks: Dict[Tuple[int, int], Any]
    shops: List[Tuple[int, int]]
    submits: List[Tuple[int, int]]
    trashes: List[Tuple[int, int]]


class BotPlayer:
    def __init__(self, map_copy):
        self.map = map_copy
        self.locations = self._find_important_locations(map_copy)

        self.current_order_id: Optional[int] = None
        self.assembly_counter: Optional[Tuple[int, int]] = None
        self.idle_tile: Optional[Tuple[int, int]] = None
        self.last_tasks: Dict[int, Tuple[str, Tuple[int, int]]] = {}

    # ----------------------------
    # Map helpers
    # ----------------------------
    def _find_important_locations(self, map_instance) -> Dict[str, List[Tuple[int, int]]]:
        locations = {
            "COOKER": [],
            "SINK": [],
            "SINKTABLE": [],
            "SUBMIT": [],
            "SHOP": [],
            "TRASH": [],
            "COUNTER": [],
            "BOX": [],
        }
        for x in range(map_instance.width):
            for y in range(map_instance.height):
                tile_name = map_instance.tiles[x][y].tile_name
                if tile_name in locations:
                    locations[tile_name].append((x, y))
        return locations

    # ----------------------------
    # Per-turn snapshot
    # ----------------------------
    def _build_snapshot(self, controller: RobotController, bot_ids: List[int]) -> Snapshot:
        team = controller.get_team()
        bots: Dict[int, Dict[str, Any]] = {}
        for bot_id in bot_ids:
            state = controller.get_bot_state(bot_id)
            if state:
                bots[bot_id] = state

        counters = {}
        for pos in self.locations["COUNTER"]:
            counters[pos] = controller.get_tile(team, pos[0], pos[1])

        cookers = {}
        for pos in self.locations["COOKER"]:
            cookers[pos] = controller.get_tile(team, pos[0], pos[1])

        sinktables = {}
        for pos in self.locations["SINKTABLE"]:
            sinktables[pos] = controller.get_tile(team, pos[0], pos[1])

        sinks = {}
        for pos in self.locations["SINK"]:
            sinks[pos] = controller.get_tile(team, pos[0], pos[1])

        return Snapshot(
            turn=controller.get_turn(),
            team=team,
            money=controller.get_team_money(team),
            bots=bots,
            counters=counters,
            cookers=cookers,
            sinktables=sinktables,
            sinks=sinks,
            shops=list(self.locations["SHOP"]),
            submits=list(self.locations["SUBMIT"]),
            trashes=list(self.locations["TRASH"]),
        )

    # ----------------------------
    # Order utilities
    # ----------------------------
    def _foodtype_by_name(self, name: str) -> Optional[FoodType]:
        try:
            return FoodType[name]
        except KeyError:
            return None

    def _order_signature(self, required: List[FoodType]) -> List[Tuple[str, bool, int]]:
        sig = [
            (ft.food_name, bool(ft.can_chop), 1 if ft.can_cook else 0)
            for ft in required
        ]
        sig.sort()
        return sig

    def _plate_signature(self, plate_foods: List[Tuple[str, bool, int]]) -> List[Tuple[str, bool, int]]:
        sig = list(plate_foods)
        sig.sort()
        return sig

    def _extract_plate_foods(self, plate_obj: Any) -> List[Tuple[str, bool, int]]:
        foods: List[Tuple[str, bool, int]] = []
        if plate_obj is None:
            return foods

        if isinstance(plate_obj, Plate):
            for f in plate_obj.food:
                if isinstance(f, Food):
                    foods.append((f.food_name, bool(f.chopped), int(f.cooked_stage)))
            return foods

        if isinstance(plate_obj, dict) and plate_obj.get("type") == "Plate":
            for f in plate_obj.get("food", []):
                foods.append((f.get("food_name"), bool(f.get("chopped")), int(f.get("cooked_stage", 0))))
            return foods

        return foods

    def _plate_matches_order(self, plate_obj: Any, required: List[FoodType]) -> bool:
        plate_foods = self._extract_plate_foods(plate_obj)
        return self._plate_signature(plate_foods) == self._order_signature(required)

    def _available_food_names(self, snapshot: Snapshot) -> Counter:
        counts: Counter = Counter()
        for state in snapshot.bots.values():
            holding = state.get("holding")
            if isinstance(holding, dict) and holding.get("type") == "Food":
                if holding.get("food_name"):
                    counts[holding["food_name"]] += 1

        for tile in snapshot.counters.values():
            item = getattr(tile, "item", None)
            if isinstance(item, Food):
                counts[item.food_name] += 1

        for tile in snapshot.cookers.values():
            pan = getattr(tile, "item", None)
            if isinstance(pan, Pan) and isinstance(pan.food, Food):
                counts[pan.food.food_name] += 1

        return counts

    def _has_accessible_plate(self, snapshot: Snapshot) -> bool:
        plate_obj, _, plate_holder_id = self._find_plate(snapshot)
        if plate_obj is not None or plate_holder_id is not None:
            return True
        for tile in snapshot.sinktables.values():
            if getattr(tile, "num_clean_plates", 0) > 0:
                return True
        return False

    def _choose_order(self, controller: RobotController, snapshot: Snapshot) -> Optional[Dict[str, Any]]:
        orders = controller.get_orders(snapshot.team)
        active = [o for o in orders if o.get("is_active")]
        if not active:
            self.current_order_id = None
            return None

        if self.current_order_id is not None:
            for o in active:
                if o.get("order_id") == self.current_order_id:
                    return o

        available_foods = self._available_food_names(snapshot)
        plate_available = self._has_accessible_plate(snapshot)

        def missing_cost(order: Dict[str, Any]) -> int:
            required = [self._foodtype_by_name(n) for n in order.get("required", [])]
            needed: Counter = Counter()
            for ft in required:
                if ft is not None:
                    needed[ft.food_name] += 1

            cost = 0
            for food_name, cnt in needed.items():
                have = available_foods.get(food_name, 0)
                missing = max(0, cnt - have)
                if missing > 0:
                    ft = self._foodtype_by_name(food_name)
                    if ft is not None:
                        cost += ft.buy_cost * missing
            if not plate_available:
                cost += ShopCosts.PLATE.buy_cost
            return cost

        def score(order: Dict[str, Any]) -> float:
            req_cost = missing_cost(order)
            time_left = max(1, order.get("expires_turn", snapshot.turn) - snapshot.turn)
            reward = order.get("reward", 0)
            penalty = 100000 if req_cost > snapshot.money and snapshot.money < 0 else 0
            return (reward - req_cost - penalty) / time_left

        active.sort(key=score, reverse=True)
        self.current_order_id = active[0].get("order_id")
        return active[0]

    # ----------------------------
    # Task building
    # ----------------------------
    def _empty_counters(self, snapshot: Snapshot) -> List[Tuple[int, int]]:
        return [pos for pos, tile in snapshot.counters.items() if getattr(tile, "item", None) is None]

    def _select_assembly_counter(self, snapshot: Snapshot) -> Optional[Tuple[int, int]]:
        if self.assembly_counter in snapshot.counters:
            tile = snapshot.counters.get(self.assembly_counter)
            if tile is not None and getattr(tile, "item", None) is None:
                return self.assembly_counter

        if snapshot.submits:
            sx, sy = snapshot.submits[0]
        else:
            sx, sy = 0, 0

        candidates = self._empty_counters(snapshot)
        if not candidates:
            candidates = list(snapshot.counters.keys())
        if not candidates:
            return None
        candidates.sort(key=lambda p: abs(p[0] - sx) + abs(p[1] - sy))
        self.assembly_counter = candidates[0]
        return self.assembly_counter

    def _find_plate(self, snapshot: Snapshot) -> Tuple[Optional[Any], Optional[Tuple[int, int]], Optional[int]]:
        # returns (plate_obj, plate_pos_on_counter, bot_id_holding_plate)
        for bot_id, state in snapshot.bots.items():
            holding = state.get("holding")
            if isinstance(holding, dict) and holding.get("type") == "Plate" and not holding.get("dirty"):
                return holding, None, bot_id

        for pos, tile in snapshot.counters.items():
            plate = getattr(tile, "item", None)
            if isinstance(plate, Plate) and not plate.dirty:
                return plate, pos, None
        return None, None, None

    def _missing_required(self, required: List[FoodType], plate_obj: Any) -> List[FoodType]:
        needed = [ft for ft in required if ft is not None]
        if plate_obj is None:
            return needed

        plate_foods = self._extract_plate_foods(plate_obj)
        req_sig = Counter(self._order_signature(needed))
        plate_sig = Counter(self._plate_signature(plate_foods))

        missing: List[FoodType] = []
        for ft in needed:
            key = (ft.food_name, bool(ft.can_chop), 1 if ft.can_cook else 0)
            if plate_sig.get(key, 0) < req_sig.get(key, 0):
                missing.append(ft)
                plate_sig[key] = plate_sig.get(key, 0) + 1
        return missing

    def _food_matches_requirement(self, food: Food, req: FoodType) -> bool:
        if food.food_name != req.food_name:
            return False
        if req.can_chop and not food.chopped:
            return False
        if req.can_cook and food.cooked_stage != 1:
            return False
        if not req.can_cook and food.cooked_stage != 0:
            return False
        return True

    def _holding_matches_requirement(self, holding: Dict[str, Any], req: FoodType) -> bool:
        if holding.get("food_name") != req.food_name:
            return False
        if req.can_chop and not holding.get("chopped"):
            return False
        if req.can_cook and holding.get("cooked_stage") != 1:
            return False
        if not req.can_cook and holding.get("cooked_stage") != 0:
            return False
        return True

    def _build_tasks(self, controller: RobotController, snapshot: Snapshot, order: Optional[Dict[str, Any]]) -> List[Task]:
        tasks: List[Task] = []
        if order is None:
            return tasks

        required = [self._foodtype_by_name(n) for n in order.get("required", [])]
        plate_obj, plate_pos, plate_holder_id = self._find_plate(snapshot)
        missing = self._missing_required(required, plate_obj)

        # Submit tasks
        if plate_obj is not None:
            if self._plate_matches_order(plate_obj, [ft for ft in required if ft is not None]):
                if plate_holder_id is not None:
                    for submit_pos in snapshot.submits:
                        tasks.append(Task("submit", submit_pos, 90, {}))
                elif plate_pos is not None:
                    tasks.append(Task("pickup_plate", plate_pos, 80, {}))

        # Cooker monitoring
        for pos, tile in snapshot.cookers.items():
            pan = getattr(tile, "item", None)
            if isinstance(pan, Pan) and isinstance(pan.food, Food):
                if pan.food.cooked_stage == 1:
                    tasks.append(Task("take_from_pan", pos, 100, {}))
                elif pan.food.cooked_stage == 2:
                    tasks.append(Task("take_from_pan", pos, 95, {"burnt": True}))

        # If holding burnt food, trash it quickly
        for bot_id, state in snapshot.bots.items():
            holding = state.get("holding")
            if isinstance(holding, dict) and holding.get("type") == "Food":
                if holding.get("cooked_stage") == 2:
                    for trash_pos in snapshot.trashes:
                        tasks.append(Task("trash", trash_pos, 85, {}))

        # Plate acquisition and placement
        if plate_obj is None:
            # Try sinktable first, then shop
            for pos, tile in snapshot.sinktables.items():
                if getattr(tile, "num_clean_plates", 0) > 0:
                    tasks.append(Task("take_clean_plate", pos, 55, {}))
            for pos in snapshot.shops:
                tasks.append(Task("buy_plate", pos, 50, {}))
        else:
            if plate_holder_id is not None:
                assembly = self._select_assembly_counter(snapshot)
                empty_counters = self._empty_counters(snapshot)
                if assembly and assembly in empty_counters:
                    tasks.append(Task("place_plate", assembly, 60, {}))
                else:
                    for pos in empty_counters:
                        tasks.append(Task("place_plate", pos, 55, {}))

        # Add prepared food to plate (if plate on counter)
        if plate_pos is not None:
            for bot_id, state in snapshot.bots.items():
                holding = state.get("holding")
                if isinstance(holding, dict) and holding.get("type") == "Food":
                    ft = self._foodtype_by_name(holding.get("food_name", ""))
                    if ft and ft in missing:
                        if self._holding_matches_requirement(holding, ft):
                            tasks.append(Task("add_food_to_plate", plate_pos, 80, {"mode": "holding_food"}))

            for pos, tile in snapshot.counters.items():
                item = getattr(tile, "item", None)
                if isinstance(item, Food):
                    ft = self._foodtype_by_name(item.food_name)
                    if ft and ft in missing and self._food_matches_requirement(item, ft):
                        tasks.append(Task("pickup_food", pos, 65, {"reason": "plate"}))

        # Add prepared food to plate (if plate held by a bot)
        if plate_holder_id is not None:
            for pos, tile in snapshot.counters.items():
                item = getattr(tile, "item", None)
                if isinstance(item, Food):
                    ft = self._foodtype_by_name(item.food_name)
                    if ft and ft in missing and self._food_matches_requirement(item, ft):
                        tasks.append(Task("add_food_to_plate", pos, 75, {"mode": "holding_plate"}))

        # Missing ingredient pipeline tasks
        if missing:
            missing_sorted = sorted(
                missing,
                key=lambda ft: (0 if ft.can_cook else 1, 0 if ft.can_chop else 1),
            )
            for ft in missing_sorted[:2]:
                # If any bot already holds it, advance that item
                for bot_id, state in snapshot.bots.items():
                    holding = state.get("holding")
                    if isinstance(holding, dict) and holding.get("type") == "Food":
                        if holding.get("food_name") == ft.food_name:
                            if ft.can_chop and not holding.get("chopped"):
                                for pos, tile in snapshot.counters.items():
                                    if getattr(tile, "item", None) is None:
                                        tasks.append(Task("place_for_chop", pos, 60, {"food": ft}))
                            elif ft.can_cook and holding.get("cooked_stage") == 0:
                                if ft.can_chop and not holding.get("chopped"):
                                    continue
                                for pos, tile in snapshot.cookers.items():
                                    pan = getattr(tile, "item", None)
                                    if isinstance(pan, Pan) and pan.food is None:
                                        tasks.append(Task("place_in_cooker", pos, 70, {"food": ft}))
                            else:
                                if plate_pos is not None:
                                    tasks.append(Task("add_food_to_plate", plate_pos, 80, {"mode": "holding_food"}))

                # Check counters for this ingredient
                for pos, tile in snapshot.counters.items():
                    item = getattr(tile, "item", None)
                    if isinstance(item, Food) and item.food_name == ft.food_name:
                        if ft.can_chop and not item.chopped:
                            tasks.append(Task("chop", pos, 60, {"food": ft}))
                        elif ft.can_cook and item.cooked_stage == 0:
                            tasks.append(Task("pickup_food", pos, 65, {"reason": "cook"}))
                        elif self._food_matches_requirement(item, ft):
                            tasks.append(Task("pickup_food", pos, 65, {"reason": "plate"}))

                # If no staged item, buy
                for pos in snapshot.shops:
                    tasks.append(Task("buy_food", pos, 40, {"food": ft}))

        # Wash dishes opportunistically (low priority)
        for pos, tile in snapshot.sinks.items():
            if getattr(tile, "num_dirty_plates", 0) > 0:
                tasks.append(Task("wash_sink", pos, 10, {}))

        return tasks

    # ----------------------------
    # Pathing helpers
    # ----------------------------
    def _adjacent_distance(self, bx: int, by: int, tx: int, ty: int) -> int:
        return max(abs(bx - tx), abs(by - ty)) - 1

    def _next_step(self, start: Tuple[int, int], target: Tuple[int, int], blocked: set) -> Optional[Tuple[int, int]]:
        if max(abs(start[0] - target[0]), abs(start[1] - target[1])) <= 1:
            return (0, 0)

        queue = deque([(start, None)])
        visited = {start}
        w, h = self.map.width, self.map.height

        while queue:
            (cx, cy), first = queue.popleft()
            if max(abs(cx - target[0]), abs(cy - target[1])) <= 1:
                return first if first is not None else (0, 0)

            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if not (0 <= nx < w and 0 <= ny < h):
                        continue
                    if (nx, ny) in visited:
                        continue
                    if not self.map.is_tile_walkable(nx, ny):
                        continue
                    if first is None and (nx, ny) in blocked:
                        continue
                    visited.add((nx, ny))
                    new_first = first if first is not None else (dx, dy)
                    queue.append(((nx, ny), new_first))
        return None

    # ----------------------------
    # Task assignment and execution
    # ----------------------------
    def _task_feasible(self, snapshot: Snapshot, bot_state: Dict[str, Any], task: Task) -> bool:
        holding = bot_state.get("holding")
        if task.kind == "buy_plate":
            return holding is None and snapshot.money >= ShopCosts.PLATE.buy_cost
        if task.kind == "take_clean_plate":
            return holding is None
        if task.kind == "buy_food":
            ft = task.meta.get("food")
            return holding is None and ft is not None and snapshot.money >= ft.buy_cost
        if task.kind == "place_for_chop":
            return holding is not None and holding.get("type") == "Food"
        if task.kind == "chop":
            return holding is None
        if task.kind == "pickup_food":
            return holding is None
        if task.kind == "place_in_cooker":
            return holding is not None and holding.get("type") == "Food"
        if task.kind == "take_from_pan":
            return holding is None
        if task.kind == "add_food_to_plate":
            if task.meta.get("mode") == "holding_food":
                return holding is not None and holding.get("type") == "Food"
            return holding is not None and holding.get("type") == "Plate" and not holding.get("dirty")
        if task.kind == "place_plate":
            return holding is not None and holding.get("type") == "Plate" and not holding.get("dirty")
        if task.kind == "pickup_plate":
            return holding is None
        if task.kind == "submit":
            return holding is not None and holding.get("type") == "Plate" and not holding.get("dirty")
        if task.kind == "trash":
            return holding is not None
        if task.kind == "wash_sink":
            return True
        return False

    def _task_score(self, snapshot: Snapshot, bot_state: Dict[str, Any], task: Task) -> int:
        if not self._task_feasible(snapshot, bot_state, task):
            return -10**9
        bx, by = bot_state["x"], bot_state["y"]
        dist = self._adjacent_distance(bx, by, task.target[0], task.target[1])
        dist = max(0, dist)
        score = task.priority * 100 - dist
        last = self.last_tasks.get(bot_state["bot_id"])
        if last and last[0] == task.kind and last[1] == task.target:
            score += 25
        return score

    def _assign_tasks(self, snapshot: Snapshot, tasks: List[Task], bot_ids: List[int]) -> Dict[int, Task]:
        assigned: Dict[int, Task] = {}
        reserved_targets = set()

        candidates: List[Tuple[int, int, Task]] = []
        for bot_id in bot_ids:
            state = snapshot.bots.get(bot_id)
            if not state:
                continue
            for task in tasks:
                score = self._task_score(snapshot, state, task)
                if score > -10**8:
                    candidates.append((score, bot_id, task))

        candidates.sort(key=lambda t: t[0], reverse=True)

        for score, bot_id, task in candidates:
            if bot_id in assigned:
                continue
            if task.target in reserved_targets and task.kind not in {"trash", "wash_sink"}:
                continue
            assigned[bot_id] = task
            reserved_targets.add(task.target)
            if len(assigned) == len(bot_ids):
                break

        self.last_tasks = {bid: (task.kind, task.target) for bid, task in assigned.items()}
        return assigned

    def _execute_task(
        self,
        controller: RobotController,
        snapshot: Snapshot,
        bot_id: int,
        task: Task,
        blocked: set,
        planned_positions: Dict[int, Tuple[int, int]],
    ) -> None:
        state = snapshot.bots.get(bot_id)
        if not state:
            return
        bx, by = state["x"], state["y"]
        tx, ty = task.target

        adjacent = max(abs(bx - tx), abs(by - ty)) <= 1
        if adjacent:
            if task.kind == "buy_plate":
                controller.buy(bot_id, ShopCosts.PLATE, tx, ty)
                return
            if task.kind == "take_clean_plate":
                controller.take_clean_plate(bot_id, tx, ty)
                return
            if task.kind == "buy_food":
                controller.buy(bot_id, task.meta["food"], tx, ty)
                return
            if task.kind == "place_for_chop":
                controller.place(bot_id, tx, ty)
                return
            if task.kind == "chop":
                controller.chop(bot_id, tx, ty)
                return
            if task.kind == "pickup_food":
                controller.pickup(bot_id, tx, ty)
                return
            if task.kind == "place_in_cooker":
                controller.place(bot_id, tx, ty)
                return
            if task.kind == "take_from_pan":
                controller.take_from_pan(bot_id, tx, ty)
                return
            if task.kind == "add_food_to_plate":
                controller.add_food_to_plate(bot_id, tx, ty)
                return
            if task.kind == "place_plate":
                controller.place(bot_id, tx, ty)
                return
            if task.kind == "pickup_plate":
                controller.pickup(bot_id, tx, ty)
                return
            if task.kind == "submit":
                controller.submit(bot_id, tx, ty)
                return
            if task.kind == "trash":
                controller.trash(bot_id, tx, ty)
                return
            if task.kind == "wash_sink":
                controller.wash_sink(bot_id, tx, ty)
                return

        step = self._next_step((bx, by), (tx, ty), blocked)
        if step and step != (0, 0):
            success = controller.move(bot_id, step[0], step[1])
            if success:
                planned_positions[bot_id] = (bx + step[0], by + step[1])

    def _idle_target(self, snapshot: Snapshot, bot_state: Dict[str, Any]) -> Optional[Tuple[int, int]]:
        critical = set(snapshot.shops + snapshot.submits + list(snapshot.cookers.keys()))
        start = (bot_state["x"], bot_state["y"])
        w, h = self.map.width, self.map.height

        queue = deque([start])
        visited = {start}
        while queue:
            cx, cy = queue.popleft()
            if self.map.is_tile_walkable(cx, cy):
                too_close = False
                for tx, ty in critical:
                    if max(abs(cx - tx), abs(cy - ty)) <= 1:
                        too_close = True
                        break
                if not too_close:
                    return (cx, cy)

            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if not (0 <= nx < w and 0 <= ny < h):
                        continue
                    if (nx, ny) in visited:
                        continue
                    if not self.map.is_tile_walkable(nx, ny):
                        continue
                    visited.add((nx, ny))
                    queue.append((nx, ny))
        return None

    # ----------------------------
    # Main entry
    # ----------------------------
    def play_turn(self, controller: RobotController):
        bot_ids = controller.get_team_bot_ids(controller.get_team())
        if not bot_ids:
            return

        snapshot = self._build_snapshot(controller, bot_ids)
        order = self._choose_order(controller, snapshot)
        tasks = self._build_tasks(controller, snapshot, order)
        assignments = self._assign_tasks(snapshot, tasks, bot_ids)

        # Execute tasks in bot-id order to reduce oscillation
        planned_positions = {bid: (snapshot.bots[bid]["x"], snapshot.bots[bid]["y"]) for bid in bot_ids if bid in snapshot.bots}
        for bot_id in sorted(bot_ids):
            task = assignments.get(bot_id)
            if task is None:
                state = snapshot.bots.get(bot_id)
                if state is None:
                    continue
                idle = self._idle_target(snapshot, state)
                if idle is None:
                    continue
                task = Task("idle_move", idle, 0, {})
            blocked = set(planned_positions.values()) - {planned_positions.get(bot_id)}
            self._execute_task(controller, snapshot, bot_id, task, blocked, planned_positions)
