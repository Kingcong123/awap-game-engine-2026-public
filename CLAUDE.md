# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Carnegie Cookoff - a turn-based competitive cooking game engine where two teams (RED and BLUE) control robots in separate kitchen maps to complete cooking orders and maximize profit.

**Language:** Python 3
**Rendering:** Pygame (optional)

## Commands

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate  # Mac/Linux
pip install -r requirements.txt

# Run game (results only)
python src/game.py --red bots/duo_noodle_bot.py --blue bots/duo_noodle_bot.py --map maps/map1.txt

# Run with pygame rendering
python src/game.py --red bots/duo_noodle_bot.py --blue bots/duo_noodle_bot.py --map maps/map1.txt --render

# Save replay
python src/game.py --red bots/duo_noodle_bot.py --blue bots/duo_noodle_bot.py --map maps/map1.txt --replay replay_path.json
```

CLI flags: `--turns`, `--timeout`, `--fps`, `--render`, `--replay`

## Architecture

- **`src/game.py`** - Main entry point. Loads maps, initializes bots, runs game loop with per-turn timeouts (0.5s default)
- **`src/game_state.py`** - Core game logic: bot positions, maps, orders, team money, turn progression, cooking/plating rules
- **`src/robot_controller.py`** - Bot API. Key constraint: each bot gets 1 move + 1 action per turn; actions must target Chebyshev distance 1
- **`src/game_constants.py`** - Game parameters (TOTAL_TURNS=500, COOK_PROGRESS=20, BURN_PROGRESS=40, etc.)
- **`src/map_processor.py`** - Parses map text files with tile layouts and optional SWITCH/ORDERS sections
- **`src/render.py`** - Pygame visualization showing both maps side-by-side with HUD

## Bot Implementation

Each bot file must define:

```python
class BotPlayer:
    def __init__(self, map_copy):
        # Initialize with deep copy of team's map
        pass

    def play_turn(self, controller: RobotController):
        # Called once per turn; use controller API
        pass
```

Key controller methods: `move()`, `pickup()`, `place()`, `buy()`, `chop()`, `start_cook()`, `take_from_pan()`, `wash_sink()`, `submit()`, `switch_maps()`

## Map Format

Tile characters: `.` Floor, `#` Wall, `C` Counter, `K` Cooker, `S` Sink, `T` SinkTable, `R` Trash, `U` Submit, `$` Shop, `B` Box, `b` Bot spawn

Maps can include optional `SWITCH:` and `ORDERS:` sections.

## External Documentation

[Bot API Google Doc](https://docs.google.com/document/d/1nUkWxDJRSEe4xSbe1q4rNd6GeMOpzQO-H_nWJHBnP14/edit)
