from lib.geometry import Document
from lib.settings import LineType, Settings

# the draw role a subpath/object item starts and ends on - a bare Path (subpath item)
# has one lineType for its whole span; a PathObject (whole-object item) takes its first
# and last subpath's lineType, which can differ when the object mixes roles
def _itemLineTypeStart(item) -> LineType:
    lineType = getattr(item, "lineType", None)
    return lineType if lineType is not None else item.geometry[0].lineType

def _itemLineTypeEnd(item) -> LineType:
    lineType = getattr(item, "lineType", None)
    return lineType if lineType is not None else item.geometry[-1].lineType

# estimates the cost of pen-up movement between the points
# accounts for slower z motion and short travel moves that don't lift the pen
# roleA and roleB are used for the role-specific min travel dist threshold
# return units are in mm of equivalent xy travel (vertical travel is rescaled accordingly)
def _travelCost(a: complex, b: complex, roleA: "LineType | None", roleB: "LineType | None", settings: Settings) -> float:
    distXY = abs(a - b)
    threshold = settings.shortTravelThresholds[roleB or LineType.PERIMETER]
    if roleA is not None:
        threshold = min(threshold, settings.shortTravelThresholds[roleA])
    if distXY < threshold:
        return distXY

    travelSpeed = settings.speeds[LineType.TRAVEL]
    verticalSpeed = min(travelSpeed, settings.maxVerticalSpeed)
    if verticalSpeed <= 0 or travelSpeed <= 0:
        return distXY

    travelHeight = settings.heights[LineType.TRAVEL]
    prevHeight = settings.heights[roleA] if roleA is not None else travelHeight
    nextHeight = settings.heights[roleB] if roleB is not None else travelHeight
    zDist = abs(travelHeight - prevHeight) + abs(travelHeight - nextHeight)

    # time spent lifting/lowering, expressed as the equivalent XY distance that would
    # take the same time at travelSpeed - keeps this additive with the XY distance above
    return distXY + zDist * travelSpeed / verticalSpeed

# reorders the paths in items to minimize travel distance
# if startPos/endPos are None, the solution will end at any position
# open paths will be reversed as needed
# closed paths will be entered/exited at any of their vertices
# a stock TSP solver is not applicable here because paths can be altered
# this converges in under a second with a few hundred paths
def _orderSequence(items: list, startPos: complex | None, endPos: complex | None, settings: Settings) -> list:
    n = len(items)
    if n <= 1:
        return items

    starts: list[complex] = []
    ends: list[complex] = []
    closed: list[bool] = []
    vertices: list[list[complex]] = []
    lineTypeStarts: list[LineType] = []
    lineTypeEnds: list[LineType] = []
    for item in items:
        starts.append(item.start())
        ends.append(item.end())
        isClosed = item.isClosed()
        closed.append(isClosed)
        vertices.append(item.vertices() if isClosed else [])
        lineTypeStarts.append(_itemLineTypeStart(item))
        lineTypeEnds.append(_itemLineTypeEnd(item))

    rev = [False] * n # whether open item i is currently drawn end->start
    anchor = [0] * n # for closed item i, index into vertices[i] of the chosen entry/exit point

    def startPt(i: int) -> complex:
        if closed[i]:
            return vertices[i][anchor[i]]
        return ends[i] if rev[i] else starts[i]

    def endPt(i: int) -> complex:
        if closed[i]:
            return vertices[i][anchor[i]]
        return starts[i] if rev[i] else ends[i]

    # draw role at the item's current entry/exit point - closed items have a single
    # role regardless of anchor; open items swap with rev, same as startPt/endPt above
    def startRole(i: int) -> LineType:
        if closed[i]:
            return lineTypeStarts[i]
        return lineTypeEnds[i] if rev[i] else lineTypeStarts[i]

    def endRole(i: int) -> LineType:
        if closed[i]:
            return lineTypeStarts[i]
        return lineTypeStarts[i] if rev[i] else lineTypeEnds[i]

    # total pen-up travel for the current `order`/`rev` and a given anchor assignment
    def tourCost(anchorArr: list[int]) -> float:
        def sp(i: int) -> complex:
            return vertices[i][anchorArr[i]] if closed[i] else (ends[i] if rev[i] else starts[i])
        def ep(i: int) -> complex:
            return vertices[i][anchorArr[i]] if closed[i] else (starts[i] if rev[i] else ends[i])
        total = 0.0
        if startPos is not None:
            total += _travelCost(startPos, sp(order[0]), None, startRole(order[0]), settings)
        for idx in range(n-1):
            total += _travelCost(ep(order[idx]), sp(order[idx+1]), endRole(order[idx]), startRole(order[idx+1]), settings)
        if endPos is not None:
            total += _travelCost(ep(order[-1]), endPos, endRole(order[-1]), None, settings)
        return total

    # nearest-neighbor construction (free start just seeds from the first item)
    remaining = set(range(n))
    order: list[int] = []
    current = startPos if startPos is not None else starts[0]
    currentRole: "LineType | None" = None # no draw role behind the very first move
    while remaining:
        def closestDist(i: int) -> float:
            if closed[i]:
                return min(_travelCost(current, v, currentRole, lineTypeStarts[i], settings) for v in vertices[i])
            return min(
                _travelCost(current, starts[i], currentRole, lineTypeStarts[i], settings),
                _travelCost(current, ends[i], currentRole, lineTypeEnds[i], settings),
            )
        best = min(remaining, key=closestDist)
        if closed[best]:
            anchor[best] = min(range(len(vertices[best])), key=lambda k: abs(current-vertices[best][k]))
        else:
            rev[best] = _travelCost(current, ends[best], currentRole, lineTypeEnds[best], settings) < _travelCost(current, starts[best], currentRole, lineTypeStarts[best], settings)
        order.append(best)
        current = endPt(best)
        currentRole = endRole(best)
        remaining.discard(best)

    # 2-opt + anchor-optimization refinement, interleaved until neither helps anymore
    improved = True
    while improved:
        improved = False

        # 2-opt: try reversing every possible run of the tour
        for i in range(n):
            leftPrev = startPos if i == 0 else endPt(order[i-1]) # None only when i==0 and startPos is None
            leftPrevRole = None if i == 0 else endRole(order[i-1])
            for j in range(i, n):
                oldLeft = 0.0 if leftPrev is None else _travelCost(leftPrev, startPt(order[i]), leftPrevRole, startRole(order[i]), settings)
                newLeft = 0.0 if leftPrev is None else _travelCost(leftPrev, endPt(order[j]), leftPrevRole, endRole(order[j]), settings) # order[j] reversed becomes the new start

                if j == n-1:
                    oldRight = 0.0 if endPos is None else _travelCost(endPt(order[j]), endPos, endRole(order[j]), None, settings)
                    newRight = 0.0 if endPos is None else _travelCost(startPt(order[i]), endPos, startRole(order[i]), None, settings) # order[i] reversed becomes the new end
                else:
                    oldRight = _travelCost(endPt(order[j]), startPt(order[j+1]), endRole(order[j]), startRole(order[j+1]), settings)
                    newRight = _travelCost(startPt(order[i]), startPt(order[j+1]), startRole(order[i]), startRole(order[j+1]), settings)

                if newLeft + newRight < oldLeft + oldRight - 1e-9:
                    order[i:j+1] = reversed(order[i:j+1])
                    for k in range(i, j+1):
                        rev[order[k]] = not rev[order[k]]
                    improved = True

        # anchor optimization: for each closed item, try every vertex as the entry/exit point
        for i in range(n):
            itemIdx = order[i]
            if not closed[itemIdx]:
                continue
            leftNeighbor = startPos if i == 0 else endPt(order[i-1]) # None only when i==0 and startPos is None
            leftNeighborRole = None if i == 0 else endRole(order[i-1])
            rightNeighbor = endPos if i == n-1 else startPt(order[i+1]) # None only when i==n-1 and endPos is None
            rightNeighborRole = None if i == n-1 else startRole(order[i+1])
            itemRole = lineTypeStarts[itemIdx] # closed items have a single role regardless of anchor

            def anchorCost(v: complex) -> float:
                cost = 0.0
                if leftNeighbor is not None:
                    cost += _travelCost(leftNeighbor, v, leftNeighborRole, itemRole, settings)
                if rightNeighbor is not None:
                    cost += _travelCost(v, rightNeighbor, itemRole, rightNeighborRole, settings)
                return cost

            bestK = min(range(len(vertices[itemIdx])), key=lambda k: anchorCost(vertices[itemIdx][k]))
            if bestK != anchor[itemIdx] and anchorCost(vertices[itemIdx][bestK]) < anchorCost(vertices[itemIdx][anchor[itemIdx]]) - 1e-9:
                anchor[itemIdx] = bestK
                improved = True

        # rendezvous move: the anchor optimization above is coordinate descent (one
        # anchor at a time vs. its current neighbors), so it can't relocate a group of
        # closed paths that should all be entered/exited near a shared point - that
        # needs several anchors to move together (e.g. concentric infill loops sharing
        # a seam, or the near-coincident double outline of a traced shape). try snapping
        # every closed path's anchor to a common rendezvous point and keep it only if
        # the whole tour gets shorter. candidate rendezvous points are the vertices of
        # the smallest closed path - the loop that most constrains where the group can
        # meet
        closedIdxs = [i for i in range(n) if closed[i]]
        if len(closedIdxs) >= 2:
            smallest = min(closedIdxs, key=lambda i: len(vertices[i]))
            bestCost, bestAnchor = tourCost(anchor), None
            for r in vertices[smallest]:
                trial = anchor.copy()
                for i in closedIdxs:
                    trial[i] = min(range(len(vertices[i])), key=lambda k: abs(vertices[i][k] - r))
                c = tourCost(trial)
                if c < bestCost - 1e-9:
                    bestCost, bestAnchor = c, trial
            if bestAnchor is not None:
                anchor = bestAnchor
                improved = True

    for i in range(n):
        if closed[i]:
            items[i].rotateTo(anchor[i])
        elif rev[i]:
            items[i].reverse()
    return [items[i] for i in order]

# reorders document.objects (and each object's own sub-paths) to minimize travel distance
# no-op if settings.optimizePathOrder is False
def orderPaths(document: Document, settings: Settings):
    if not settings.optimizePathOrder:
        return
    # hendled in 2 passes to reduce runtime
    # pass 1: object subpath order
    for obj in document.objects:
        if len(obj.geometry) > 1:
            obj.geometry = _orderSequence(obj.geometry, None, None, settings)
    # pass 2: object order
    document.objects = _orderSequence(document.objects, complex(settings.startPos["X"], settings.startPos["Y"]), settings.endPos, settings)
