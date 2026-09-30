/// <reference types="geojson" />
import * as maplibregl from 'maplibre-gl'
import type { GeoJSONSource, Map as MlMap, MapLayerMouseEvent } from 'maplibre-gl'
import { useEffect, useRef } from 'react'
import { api, type Station } from '../api'

type Props = {
  theme: 'dark' | 'light'
  selectedVin: string | null
  selectedPos: [number, number] | null
  route: [number, number][] | null
  highlight: Station[]
  onSelectVin: (vin: string) => void
}

maplibregl.setWorkerUrl('/maplibre/maplibre-gl-worker.mjs')

const CENTER: [number, number] = [77.5946, 12.9716]
// OpenFreeMap: free, keyless OpenStreetMap vector tiles (https://openfreemap.org)
const STYLE = { dark: 'https://tiles.openfreemap.org/styles/dark', light: 'https://tiles.openfreemap.org/styles/positron' }
const empty: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }
const pointFC = (c: [number, number] | null): GeoJSON.FeatureCollection =>
  c ? { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: c }, properties: {} }] } : empty

export function MapView({ theme, selectedVin, selectedPos, route, highlight, onSelectVin }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const map = useRef<MlMap | null>(null)
  const selectRef = useRef(onSelectVin)
  selectRef.current = onSelectVin
  // last data per overlay source, re-applied whenever the base style is swapped (theme change)
  const data = useRef<Record<string, GeoJSON.GeoJSON>>({ route: empty, chargers: empty, vehicles: empty, selected: empty, highlight: empty })
  const themeRef = useRef(theme)
  themeRef.current = theme

  const setData = (id: string, d: GeoJSON.GeoJSON) => {
    data.current[id] = d
    ;(map.current?.getSource(id) as GeoJSONSource | undefined)?.setData(d)
  }

  // ------------------------------------------------------------ init
  useEffect(() => {
    if (!ref.current || map.current) return
    const m = new maplibregl.Map({ container: ref.current, center: CENTER, zoom: 11, style: STYLE[theme],
      attributionControl: { compact: true } })
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right')
    map.current = m
    const ro = new ResizeObserver(() => m.resize())
    ro.observe(ref.current)

    const addOverlays = () => {
      const dark = themeRef.current === 'dark'
      for (const id of Object.keys(data.current)) {
        if (!m.getSource(id)) m.addSource(id, { type: 'geojson', data: data.current[id] })
      }
      m.addLayer({ id: 'route-glow', type: 'line', source: 'route', paint: { 'line-color': '#3e6ae1', 'line-width': 14, 'line-opacity': 0.2, 'line-blur': 4 } })
      m.addLayer({ id: 'route', type: 'line', source: 'route', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#3e6ae1', 'line-width': 5 } })
      m.addLayer({
        id: 'chargers', type: 'circle', source: 'chargers', minzoom: 11.5,
        paint: {
          'circle-radius': ['case', ['get', 'dc'], 5.5, 4],
          'circle-color': ['case', ['>', ['get', 'free'], 0], '#30c267', '#8e8e93'],
          'circle-stroke-color': dark ? '#000' : '#fff', 'circle-stroke-width': 1.5,
        },
      })
      m.addLayer({
        id: 'clusters', type: 'circle', source: 'vehicles', filter: ['==', ['get', 'kind'], 'cell'],
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['sqrt', ['get', 'n']], 1, 5, 40, 24, 100, 38],
          'circle-color': ['interpolate', ['linear'], ['get', 'avg_soc'], 20, '#e82127', 40, '#f5a623', 60, '#3e6ae1'],
          'circle-opacity': dark ? 0.3 : 0.22,
          'circle-stroke-color': ['interpolate', ['linear'], ['get', 'avg_soc'], 20, '#e82127', 40, '#f5a623', 60, '#3e6ae1'],
          'circle-stroke-width': 1.2,
        },
      })
      m.addLayer({
        id: 'cluster-count', type: 'symbol', source: 'vehicles', filter: ['==', ['get', 'kind'], 'cell'],
        layout: { 'text-field': ['to-string', ['get', 'n']], 'text-size': 11, 'text-font': ['Noto Sans Bold'], 'text-allow-overlap': true },
        paint: { 'text-color': dark ? '#f5f5f5' : '#171a20' },
      })
      m.addLayer({
        id: 'points', type: 'circle', source: 'vehicles', filter: ['==', ['get', 'kind'], 'vehicle'],
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 13, 3.5, 17, 7],
          'circle-color': ['match', ['get', 'status'], 'DRIVING', '#3e6ae1', 'CHARGING', '#30c267', 'OFFLINE', '#5a5b60', dark ? '#a0a0a5' : '#6b6d72'],
          'circle-stroke-width': ['case', ['<', ['get', 'soc'], 15], 2, 0], 'circle-stroke-color': '#e82127',
        },
      })
      m.addLayer({ id: 'highlight', type: 'circle', source: 'highlight', paint: { 'circle-radius': 9, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': '#30c267', 'circle-stroke-width': 2.5 } })
      m.addLayer({ id: 'selected-halo', type: 'circle', source: 'selected', paint: { 'circle-radius': 16, 'circle-color': '#3e6ae1', 'circle-opacity': 0.22 } })
      m.addLayer({ id: 'selected', type: 'circle', source: 'selected', paint: { 'circle-radius': 7, 'circle-color': '#fff', 'circle-stroke-color': '#3e6ae1', 'circle-stroke-width': 3 } })
    }
    m.on('style.load', addOverlays)

    m.on('click', 'points', (e: MapLayerMouseEvent) => { const v = e.features?.[0]?.properties?.vin; if (v) selectRef.current(String(v)) })
    m.on('click', 'clusters', (e: MapLayerMouseEvent) => {
      const f = e.features?.[0]
      if (f && f.geometry.type === 'Point') m.easeTo({ center: f.geometry.coordinates as [number, number], zoom: 14 })
    })
    m.on('click', 'chargers', (e: MapLayerMouseEvent) => {
      const f = e.features?.[0]
      if (!f || f.geometry.type !== 'Point') return
      const p = f.properties
      const el = document.createElement('div')
      const title = document.createElement('div'); title.style.fontWeight = '500'; title.textContent = String(p.name)
      const line = document.createElement('div'); line.style.cssText = 'font-size:12px;color:var(--muted);margin-top:2px'
      line.textContent = `${p.free}/${p.connectors} free · up to ${p.max_kw} kW · ₹${p.price_now}/kWh`
      el.append(title, line)
      new maplibregl.Popup({ offset: 10 }).setLngLat(f.geometry.coordinates as [number, number]).setDOMContent(el).addTo(m)
    })
    for (const l of ['points', 'clusters', 'chargers']) {
      m.on('mouseenter', l, () => { m.getCanvas().style.cursor = 'pointer' })
      m.on('mouseleave', l, () => { m.getCanvas().style.cursor = '' })
    }

    let alive = true
    async function refreshVehicles() {
      if (!alive) return
      const b = m.getBounds()
      const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((x) => x.toFixed(5)).join(',')
      try {
        const d = await api<{ mode: string; cells?: { lat: number; lon: number; n: number; avg_soc: number; low: number }[]; vehicles?: { vin: string; lat: number; lon: number; soc: number; status: string }[] }>(`/v1/map/vehicles?bbox=${bbox}&zoom=${m.getZoom().toFixed(1)}`)
        const features: GeoJSON.Feature[] = d.mode === 'clusters'
          ? d.cells!.map((c) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [c.lon, c.lat] }, properties: { kind: 'cell', ...c } }))
          : d.vehicles!.map((v) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [v.lon, v.lat] }, properties: { kind: 'vehicle', ...v } }))
        setData('vehicles', { type: 'FeatureCollection', features })
      } catch { /* transient - next tick retries */ }
    }
    async function refreshChargers() {
      if (!alive) return
      try {
        const d = await api<{ stations: Station[] }>('/v1/map/chargers')
        setData('chargers', {
          type: 'FeatureCollection',
          features: d.stations.map((s) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [s.lon, s.lat] }, properties: s })),
        })
      } catch { /* ignore */ }
    }
    refreshVehicles()
    refreshChargers()
    const t1 = setInterval(refreshVehicles, 3000)
    const t2 = setInterval(refreshChargers, 20000)
    m.on('moveend', refreshVehicles)
    return () => { alive = false; ro.disconnect(); clearInterval(t1); clearInterval(t2); m.remove(); map.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ------------------------------------------------------------ theme: swap base style, overlays re-added on style.load
  const firstTheme = useRef(true)
  useEffect(() => {
    if (firstTheme.current) { firstTheme.current = false; return }
    map.current?.setStyle(STYLE[theme], { diff: false })
  }, [theme])

  // ------------------------------------------------------------ selection / route / highlights
  useEffect(() => { setData('selected', pointFC(selectedPos)) }, [selectedPos])

  useEffect(() => {
    const m = map.current
    if (m && selectedPos && selectedVin) m.flyTo({ center: selectedPos, zoom: Math.max(m.getZoom(), 14), speed: 1.4 })
    // only when the selected vehicle changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedVin])

  useEffect(() => {
    setData('route', route && route.length > 1 ? { type: 'Feature', geometry: { type: 'LineString', coordinates: route }, properties: {} } : empty)
    setData('highlight', { type: 'FeatureCollection', features: highlight.map((s) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [s.lon, s.lat] }, properties: {} })) })
    const m = map.current
    if (m && route && route.length > 1) {
      const b = new maplibregl.LngLatBounds(route[0], route[0])
      route.forEach((c) => b.extend(c))
      m.fitBounds(b, { padding: 80, maxZoom: 15, duration: 800 })
    }
  }, [route, highlight])

  // maplibre's CSS forces `.maplibregl-map { position: relative }`, so size it via a wrapper
  return (
    <div className="absolute inset-0">
      <div ref={ref} className="h-full w-full" aria-label="Fleet map" />
    </div>
  )
}
