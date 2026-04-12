// MapLibre helpers: create a locked raster map fit to a bbox.
// The global `maplibregl` is loaded via the CDN <script> tag in index.html.

export function makeRasterStyle(tileserverUrlTemplate, maxZoom = 22) {
    return {
        version: 8,
        sources: {
            sat: {
                type: "raster",
                tiles: [tileserverUrlTemplate],
                tileSize: 256,
                minzoom: 0,
                maxzoom: maxZoom,
            },
        },
        layers: [{ id: "sat-layer", type: "raster", source: "sat" }],
    };
}

export function createLockedMap(containerId, tileserverUrlTemplate, bbox) {
    const [w, s, e, n] = bbox;
    const map = new maplibregl.Map({
        container: containerId,
        style: makeRasterStyle(tileserverUrlTemplate),
        bounds: [[w, s], [e, n]],
        fitBoundsOptions: { padding: 0, animate: false, linear: true },
        interactive: false,
        attributionControl: false,
        renderWorldCopies: false,
    });
    return map;
}

export function setMapBbox(map, bbox) {
    const [w, s, e, n] = bbox;
    map.fitBounds([[w, s], [e, n]], { padding: 0, animate: false, duration: 0, linear: true });
}

export function updateMapSource(map, tileserverUrlTemplate) {
    // Replace raster source tiles (e.g. when config changes)
    const style = map.getStyle();
    style.sources.sat.tiles = [tileserverUrlTemplate];
    map.setStyle(style);
}
