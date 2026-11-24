"""Independent serialized interlocking model. The train retains its route identity."""
def replay(events, *, faulty=False, layout=(1,2)):
    reservation = train = switch = 0
    observations = []
    for event in events:
        index, kind = divmod(event,4)
        route = index + 1
        verdict = 0
        if index in (0,1):
            if kind == 0 and reservation == 0:
                reservation, switch, verdict = route, layout[index], 1
            elif kind == 1 and reservation == route and (not train or faulty):
                reservation, verdict = 0, 2
            elif kind == 2 and reservation == route and not train:
                train, verdict = route, 3
            elif kind == 3 and train == route:
                reservation, train, verdict = 0, 0, 4
        observations.extend([verdict,reservation,train,switch])
    return observations
