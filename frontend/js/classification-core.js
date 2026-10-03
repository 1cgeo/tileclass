// Pure logic for the classification editor (testable without DOM/MapLibre).

// Digit shortcuts 1–9 pick the Nth class in the project's display order.
// Returns the class id, or null when the key isn't a digit shortcut or the
// project has fewer classes than the digit.
export function classIdForKey(key, classes) {
    if (typeof key !== "string" || !/^[1-9]$/.test(key)) return null;
    const cls = (classes || [])[Number(key) - 1];
    return cls ? cls.id : null;
}

// GeoJSON frame that makes the tile boundary explicit on the map: `shade` is
// a world polygon with the tile bbox punched out (dims everything outside the
// tile), `outline` is the bbox ring itself. The map container isn't square,
// so without this the operator sees satellite beyond the tile with no cue.
export function tileFrameGeoJSON({ bbox_west: w, bbox_south: s, bbox_east: e, bbox_north: n }) {
    const ring = [[w, s], [e, s], [e, n], [w, n], [w, s]];
    const world = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]];
    return {
        shade: {
            type: "Feature",
            properties: {},
            geometry: { type: "Polygon", coordinates: [world, ring] },
        },
        outline: {
            type: "Feature",
            properties: {},
            geometry: { type: "LineString", coordinates: ring },
        },
    };
}
