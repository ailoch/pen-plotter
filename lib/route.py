from lib.geometry import Document

# reorders the paths in items to minimize travel distance
# if startPos/endPos are None, the solution will end at any position
# open paths will be reversed as needed
# closed paths will be entered/exited at any of their vertices
# a stock TSP solver is not applicable here because paths can be altered
# this converges in under a second with a few hundred paths
def _orderSequence(items: list, startPos: complex | None, endPos: complex | None) -> list:
    n = len(items)
    if n <= 1:
        return items

    starts: list[complex] = []
    ends: list[complex] = []
    closed: list[bool] = []
    vertices: list[list[complex]] = []
    for item in items:
        starts.append(item.start())
        ends.append(item.end())
        isClosed = item.isClosed()
        closed.append(isClosed)
        vertices.append(item.vertices() if isClosed else [])

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

    # total pen-up travel for the current `order`/`rev` and a given anchor assignment
    def tourCost(anchorArr: list[int]) -> float:
        def sp(i: int) -> complex:
            return vertices[i][anchorArr[i]] if closed[i] else (ends[i] if rev[i] else starts[i])
        def ep(i: int) -> complex:
            return vertices[i][anchorArr[i]] if closed[i] else (starts[i] if rev[i] else ends[i])
        total = 0.0
        if startPos is not None:
            total += abs(startPos - sp(order[0]))
        for idx in range(n-1):
            total += abs(ep(order[idx]) - sp(order[idx+1]))
        if endPos is not None:
            total += abs(ep(order[-1]) - endPos)
        return total

    # nearest-neighbor construction (free start just seeds from the first item)
    remaining = set(range(n))
    order: list[int] = []
    current = startPos if startPos is not None else starts[0]
    while remaining:
        def closestDist(i: int) -> float:
            if closed[i]:
                return min(abs(current-v) for v in vertices[i])
            return min(abs(current-starts[i]), abs(current-ends[i]))
        best = min(remaining, key=closestDist)
        if closed[best]:
            anchor[best] = min(range(len(vertices[best])), key=lambda k: abs(current-vertices[best][k]))
        else:
            rev[best] = abs(current-ends[best]) < abs(current-starts[best])
        order.append(best)
        current = endPt(best)
        remaining.discard(best)

    # 2-opt + anchor-optimization refinement, interleaved until neither helps anymore
    improved = True
    while improved:
        improved = False

        # 2-opt: try reversing every possible run of the tour
        for i in range(n):
            leftPrev = startPos if i == 0 else endPt(order[i-1]) # None only when i==0 and startPos is None
            for j in range(i, n):
                oldLeft = 0.0 if leftPrev is None else abs(leftPrev - startPt(order[i]))
                newLeft = 0.0 if leftPrev is None else abs(leftPrev - endPt(order[j])) # order[j] reversed becomes the new start

                if j == n-1:
                    oldRight = 0.0 if endPos is None else abs(endPt(order[j]) - endPos)
                    newRight = 0.0 if endPos is None else abs(startPt(order[i]) - endPos) # order[i] reversed becomes the new end
                else:
                    oldRight = abs(endPt(order[j]) - startPt(order[j+1]))
                    newRight = abs(startPt(order[i]) - startPt(order[j+1]))

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
            rightNeighbor = endPos if i == n-1 else startPt(order[i+1]) # None only when i==n-1 and endPos is None

            def anchorCost(v: complex) -> float:
                cost = 0.0
                if leftNeighbor is not None:
                    cost += abs(leftNeighbor-v)
                if rightNeighbor is not None:
                    cost += abs(v-rightNeighbor)
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
def orderPaths(document: Document, startPos: complex = 0, endPos: complex = 0):
    # hendled in 2 passes to reduce runtime
    # pass 1: object subpath order
    for obj in document.objects:
        if len(obj.geometry) > 1:
            obj.geometry = _orderSequence(obj.geometry, None, None)
    # pass 2: object order
    document.objects = _orderSequence(document.objects, startPos, endPos)
