// 3x3 context minimap powered by MapLibre (raster satellite) + a red bbox highlight.
import { createLockedMap } from "./maplib.js";

// Compute the bbox of a 3x3 window centered on the current tile (in lon/lat).
function contextBbox(tile) {
    const w = tile.bbox_east - tile.bbox_west;
    const h = tile.bbox_north - tile.bbox_south;
    return [
        tile.bbox_west - w,
        tile.bbox_south - h,
        tile.bbox_east + w,
        tile.bbox_north + h,
    ];
}

export function renderMinimap(existingMap, tile, tileserverUrl) {
    const bbox = contextBbox(tile);
    let map = existingMap;
    if (!map) {
        map = createLockedMap("minimap", tileserverUrl, bbox);
        map.on("load", () => addOrUpdateHighlight(map, tile));
    } else {
        map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding: 0, animate: false, duration: 0 });
        if (map.isStyleLoaded()) addOrUpdateHighlight(map, tile);
        else map.once("load", () => addOrUpdateHighlight(map, tile));
    }
    return map;
}

function addOrUpdateHighlight(map, tile) {
    const coords = [
        [tile.bbox_west, tile.bbox_north],
        [tile.bbox_east, tile.bbox_north],
        [tile.bbox_east, tile.bbox_south],
        [tile.bbox_west, tile.bbox_south],
        [tile.bbox_west, tile.bbox_north],
    ];
    const geojson = { type: "Feature", geometry: { type: "LineString", coordinates: coords } };
    if (map.getSource("highlight")) {
        map.getSource("highlight").setData(geojson);
    } else {
        map.addSource("highlight", { type: "geojson", data: geojson });
        map.addLayer({
            id: "highlight-line",
            type: "line",
            source: "highlight",
            paint: { "line-color": "#ff3b30", "line-width": 3, "line-opacity": 0.9 },
        });
    }
}
