"""Restore empty cookware parked off a cooker, without moving active meals."""
def recovery_plan(km, x, z, held='', available=lambda key: True):
    choices=[]
    for c in km.cooking:
        if not c.is_pot or c.busy or c.state=='Burnt':continue
        if held and held!=c.name:continue
        holder=next((s for s in km.stations.values() if c.name in s.on),None)
        if holder is not None and holder.kind=='CookingStation':continue
        if holder is None and held!=c.name:continue
        if not available('restore:'+c.name):continue
        for s in km.stations.values():
            # CookingStation.CanAddItem IL_001c..002e compares required station type;
            # IL_0030..0048 accepts an empty compatible container.
            if s.kind!='CookingStation' or not s.active or s.n or s.on:continue
            if not c.station or s.sub!=c.station or not available(s.id):continue
            distance=(c.x-x)**2+(c.z-z)**2+(s.x-c.x)**2+(s.z-c.z)**2
            choices.append((distance,c,holder,s))
    return min(choices,key=lambda p:p[0])[1:] if choices else None
