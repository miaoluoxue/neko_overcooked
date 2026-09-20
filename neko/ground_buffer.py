"""Ground handoff geometry. Terrain is a live observation, never a receipt.

Mechanics: ServerPlayerControlsImpl_Default.ReceiveThrowEvent IL_003a..008d
uses forward; ServerThrowableItem.HandleThrow sets flying and allows landing.
Live trajectory ground-buffer-live.jsonl (9160..9165): floor landing is about
eight world units away. Catch range is shorter and must not be reused here.
This is only a planning estimate. Confirm real stock.
"""
import math


def landing_plan(tm, own, other):
    """Choose a reachable launch cell and an interior receiving floor point."""
    if not own or not other or set(own).intersection(other):
        return None
    targets = []
    for d in other:
        tx,tz = tm.world_of(*d)
        nx,_ = tm.world_of(d[0]+1,d[1]); _,nz = tm.world_of(d[0],d[1]+1)
        # A moving raft can occupy only two grid rows. Their midpoint is safe
        # even though neither row has four walkable grid neighbours.
        for ox,oz in ((0,0),(.5,0),(0,.5),(.5,.5)):
            px,pz = tx+(nx-tx)*ox,tz+(nz-tz)*oz
            if all(tm.cell_of(px+dx,pz+dz) in other
                   for dx,dz in ((.65,0),(-.65,0),(0,.65),(0,-.65))):
                targets.append((px,pz))
    best = None
    centre = tuple(sum(tm.world_of(*d)[axis] for d in other)/len(other) for axis in (0,1))
    for c, steps in own.items():
        x, z = tm.world_of(*c)
        for tx,tz in targets:
            distance = math.hypot(tx-x, tz-z)
            if not 8.2 <= distance <= 9.4:
                continue
            ux, uz = (tx-x)/distance, (tz-z)/distance
            # TurnSpeed is finite (PlayerControlsHelper.RotateTowards). Reserve
            # one step of safe launch-side floor for a .18s facing movement.
            if tm.cell_of(x+ux*1.1,z+uz*1.1) not in own:
                continue
            # Include direction/range error and the short facing movement.
            samples = [(x+ux*r-uz*s, z+uz*r+ux*s)
                       for r in (8.2, 8.8, 9.4) for s in (-.35, .35)]
            if not all(tm.cell_of(a,b) in other for a,b in samples):
                continue
            # Prefer the platform's middle over its leading/trailing edge;
            # the raft keeps translating while the food is airborne.
            rank = steps + abs(distance-8.8) + .15*((tx-centre[0])**2+(tz-centre[1])**2)
            if best is None or rank < best[0]:
                best = rank, c, (tx,tz)
    return None if best is None else best[1:]
