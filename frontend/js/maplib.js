// MapLibre helpers: create a locked raster map fit to a bbox.
// The global `maplibregl` is loaded via the CDN <script> tag in index.html.
// `maxZoom` is the highest zoom level the *source* has tiles for; MapLibre
// overzooms (scales tiles from this level) when the viewport zooms beyond it,
// so we avoid 204 requests for zoom levels the tileserver does not serve.

const OVERLAY_KEYS = ["secondary", "tertiary", "wc"];

export function makeRasterStyle(primaryUrl, primaryMaxZoom = 22, overlays = {}) {
    const sources = {
        sat: {
            type: "raster",
            tiles: [primaryUrl],
            tileSize: 256,
            minzoom: 0,
            maxzoom: primaryMaxZoom,
        },
    };
    const layers = [{ id: "sat-layer", type: "raster", source: "sat" }];
    for (const key of OVERLAY_KEYS) {
        const o = overlays[key];
        if (!o || !o.url) continue;
        sources[key] = {
            type: "raster",
            tiles: [o.url],
            tileSize: 256,
            minzoom: o.minZoom ?? 0,
            maxzoom: o.maxZoom ?? primaryMaxZoom,
        };
        // Opacity-toggle (not visibility) so tiles prefetch and the first
        // hold has no fetch latency.
        layers.push({
            id: `${key}-layer`,
            type: "raster",
            source: key,
            paint: { "raster-opacity": 0, "raster-resampling": "nearest" },
        });
    }
    return { version: 8, sources, layers };
}

export function createLockedMap(containerId, primaryUrl, bbox, primaryMaxZoom = 22, overlays = {}) {
    const [w, s, e, n] = bbox;
    const map = new maplibregl.Map({
        container: containerId,
        style: makeRasterStyle(primaryUrl, primaryMaxZoom, overlays),
        bounds: [[w, s], [e, n]],
        fitBoundsOptions: { padding: 0, animate: false, linear: true },
        interactive: false,
        attributionControl: false,
        renderWorldCopies: false,
    });
    return map;
}

export function setOverlayVisible(map, key, visible) {
    if (!map) return;
    const id = `${key}-layer`;
    if (!map.getLayer(id)) return;
    map.setPaintProperty(id, "raster-opacity", visible ? 1 : 0);
}

export function setMapBbox(map, bbox) {
    const [w, s, e, n] = bbox;
    map.fitBounds([[w, s], [e, n]], { padding: 0, animate: false, duration: 0, linear: true });
}

export function updateMapSource(map, primaryUrl, primaryMaxZoom = 22) {
    const style = map.getStyle();
    style.sources.sat.tiles = [primaryUrl];
    style.sources.sat.maxzoom = primaryMaxZoom;
    map.setStyle(style);
}
