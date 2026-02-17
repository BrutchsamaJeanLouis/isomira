# Task

Build the classical physics foundation for a 2D world simulation: a **ToyWorld** class that manages a grid with energy sources and obstacles, and computes a potential field using gradient-based attraction/repulsion.

The implementation goes in a single file `toy_world.py`.

## Scope

workspace/toy_world.py

## Domain Knowledge

### Grid and Coordinate System
- The world is a 2D NumPy array of shape `(size, size)` where `size` is configurable (default 128).
- Coordinates are `(row, col)` integers. Row 0 is top, row size-1 is bottom. Column 0 is left.
- The grid stores floating-point potential values. Lower potential = more attractive. Higher potential = repulsive.

### Energy Sources
- An energy source is a tuple `(row, col, strength)` where strength is a positive float.
- Energy sources CREATE attraction: they LOWER the potential in their vicinity.
- The potential contribution of a source at distance `d` from its center is: `-strength / (d + 1.0)`. The `+1.0` prevents division by zero at the source location itself.
- Distance `d` is Euclidean: `sqrt((r2-r1)**2 + (c2-c1)**2)`.
- Multiple sources sum their contributions (superposition).

### Obstacles
- An obstacle is a tuple `(row, col, radius)` where radius is a positive float.
- Obstacles CREATE repulsion: they RAISE the potential in their vicinity.
- Every grid cell whose Euclidean distance from the obstacle center is `<= radius` gets a large positive potential added: `+1000.0` (a wall). Cells outside the radius are unaffected by the obstacle.
- This is a hard barrier, not a smooth falloff. Inside radius = +1000, outside = 0.

### CRITICAL -- Field Shape (how the potential landscape looks)
- The potential field is a LANDSCAPE. Energy sources create WELLS (local minima).
- At the source location itself (d=0): potential contribution is `-strength / 1.0 = -strength`. This is the DEEPEST point (most negative value).
- Cells further from the source have values CLOSER TO ZERO (less negative). Example: strength=10, at d=0 potential=-10.0, at d=1 potential=-5.0, at d=3 potential=-2.5. The value -10.0 is MORE NEGATIVE than -5.0. The source cell has the MOST negative value. Adjacent cells are LESS negative (closer to zero).
- Therefore: `potential[source_row, source_col] < potential[adjacent_row, adjacent_col]` (the source is more negative, so it is LESS THAN neighbors on the number line).
- Obstacle barriers ADD +1000 on top of whatever energy source contributions already exist at that cell. If a cell is inside an obstacle AND near an energy source, its potential = `1000.0 + (negative energy contribution)`, which is LESS than 1000.0. Obstacles do NOT erase energy contributions -- they stack via superposition.

### Potential Field Computation
- `compute_potential_field()` computes the field, stores it as `self.potential` (public attribute), and also returns it.
- For each cell `(r, c)`: `potential[r, c] = sum of all energy source contributions + sum of all obstacle contributions`.
- The field is computed from scratch each call. Sources and obstacles may change between calls.

### Gradient Extraction
- `get_gradient(row, col)` returns a tuple `(dr, dc)` -- the direction of steepest DESCENT (toward lower potential) at a given cell.
- `get_gradient` reads from `self.potential`. The caller must call `compute_potential_field()` first to populate it. If `self.potential` is None, call `compute_potential_field()` internally.
- Interior cells (not on any edge): `dr = -(potential[r+1,c] - potential[r-1,c]) / 2.0`, `dc = -(potential[r,c+1] - potential[r,c-1]) / 2.0`.
- Boundary cells use forward/backward difference with divisor 1.0 (NOT 2.0):
  - Row 0: `dr = -(potential[1, c] - potential[0, c]) / 1.0`
  - Row size-1: `dr = -(potential[size-1, c] - potential[size-2, c]) / 1.0`
  - Col 0: `dc = -(potential[r, 1] - potential[r, 0]) / 1.0`
  - Col size-1: `dc = -(potential[r, size-1] - potential[r, size-2]) / 1.0`
- Do NOT normalize the gradient vector. Return raw magnitude.

### CRITICAL -- "agent" Disambiguation
This code defines a WORLD (environment). It does NOT define agents, AI models, or decision-makers. The word "agent" does not appear in this task. Entities that navigate this world will be built in a separate future task. This file is ONLY the environment: grid, sources, obstacles, potential field, gradient.

## Constraints

- Dependencies: `numpy` only. No scipy, no pygame, no matplotlib in the implementation file.
- All public methods must have type hints.
- The constructor takes `size: int = 128` as its only parameter. Initialize `self.potential = None`.
- `energy_sources` and `obstacles` are stored as `list[tuple[float, float, float]]` -- public attributes, not private.
- `add_energy_source(row, col, strength)` and `add_obstacle(row, col, radius)` append to these lists.
- `compute_potential_field()` stores result in `self.potential` AND returns `numpy.ndarray` of dtype float64.
- `get_gradient(row, col)` returns `tuple[float, float]`. Reads from `self.potential`.
- Do not use any `@jit` or numba decorators -- keep it pure numpy for Phase 1.
- The grid wraps nothing. Out-of-bounds is handled by clamping, not wrapping.
- Tests must only interact through public methods and public attributes. No accessing private/protected state (no `_potential`, no `_internal` anything).
