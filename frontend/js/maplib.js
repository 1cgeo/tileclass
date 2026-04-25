// MapLibre helpers: create a locked raster map fit to a bbox.
// The global `maplibregl` is loaded via the CDN <script> tag in index.html.
// `maxZoom` is the highest zoom level the *source* has tiles for; MapLibre
// overzooms (scales tiles from this level) when the viewport zooms beyond it,
// so we avoid 204 requests for zoom levels the tileserver does not serve.

const OVERLAY_KEYS = ["secondary", "tertiary", "wc"];

// Bing Maps uses quadkeys instead of z/x/y. We expose a custom URL scheme
// `bingmaps://{z}/{x}/{y}` so config.yaml can stay declarative; the frontend
// rewrites these on the fly via MapLibre's transformRequest hook.
const BING_RE = /^bingmaps:\/\/(\d+)\/(\d+)\/(\d+)/;

function bingQuadkey(x, y, z) {
    let qk = "";
    for (let i = z; i > 0; i--) {
        let digit = 0;
        const mask = 1 << (i - 1);
        if ((x & mask) !== 0) digit++;
        if ((y & mask) !== 0) digit += 2;
        qk += digit;
    }
    return qk;
}

export function tileTransformRequest(url) {
    const m = url.match(BING_RE);
    if (!m) return { url };
    const z = +m[1], x = +m[2], y = +m[3];
    const sub = (x + y) % 4;
    return { url: `https://ecn.t${sub}.tiles.virtualearth.net/tiles/a${bingQuadkey(x, y, z)}.jpeg?g=1&n=z` };
}

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
        layers.push({
            id: `${key}-layer`,
            type: "raster",
            source: key,
            layout: { visibility: "none" },
            paint: { "raster-resampling": "nearest" },
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
        transformRequest: tileTransformRequest,
    });
    return map;
}

export function setOverlayVisible(map, key, visible) {
    if (!map) return;
    const id = `${key}-layer`;
    if (!map.getLayer(id)) return;
    map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
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
