// Browser half of dsh-shisei: the left-sidebar Shisei page and the plugin page (Plugins › dsh-shisei).
// Hand-written for the dsh client module loader. Talks to lib/host.js over the connection RPC.
// This entry stays small: it registers the slots at once and loads the UI (lib/client.ui.js, built from src/client by
// `npm run build`) the first time a Shisei surface renders.
// ui: 90dd63194f1803814e12
window.__ModuleLoader__.load({
  id: 'dsh-shisei',
  factory: (require) => {
    const module = { exports: {} }
    const React = require('react')
    const h = React.createElement

    const PANEL_ID = 'dsh-shisei'

    // a copy of FIGURE in src/client/logo.jsx (this file is not bundled); test/logo.test.js keeps them equal
    function ShiseiIcon({ size }) {
      const s = size || 16
      return h('svg', { width: s, height: s, viewBox: '0 0 16 16', 'aria-hidden': 'true' },
        h('g', { fill: 'none', stroke: 'currentColor', strokeWidth: 1.4, strokeLinecap: 'round' },
          h('circle', { cx: 8, cy: 3.2, r: 1.8 }), h('path', { d: 'M8 5.4v4.8M8 10.2l-2 4.3M8 10.2l2 4.3M8 6.4L4.6 8.6M8 6.4l3.3-2.2' })))
    }

    // The UI chunk, fetched once; a failed fetch is tried again on the next render.
    let parts = null
    const loadParts = () => {
      if (!parts) {
        parts = require.async('./client.ui.js')
        parts.catch(() => { parts = null })
      }
      return parts
    }
    // Same shape dsh uses for its own lazy chunks: React.lazy under a Suspense that shows nothing while the files arrive.
    const lazyPart = (name) => {
      const Loaded = React.lazy(() => loadParts().then((M) => ({ default: M[name] })))
      return (props) => h(React.Suspense, { fallback: null }, h(Loaded, props))
    }
    const ShiseiPanel = lazyPart('ShiseiPanel')
    const ShiseiApp = lazyPart('ShiseiApp')

    const inject = ['slots', 'connection']

    function apply(ctx) {
      const connection = ctx.get('connection')
      const rpc = (endpoint, payload, signal) => connection.rpc.call('/api', `dsh-shisei/${endpoint}`, payload ?? {}, signal)
      ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({
        name: 'plugins.bundle.config',
        key: 'dsh-shisei',
        inject: () => ({ rpc }),
      }, ShiseiPanel))
      // left-sidebar entry + full main panel (same id addresses the `main` keyed slot)
      ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: PANEL_ID, inject: () => ({ rpc }) }, ShiseiApp))
      ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: PANEL_ID, order: 20, label: 'Shisei' }, ShiseiIcon))
    }

    module.exports.ShiseiPanel = ShiseiPanel
    module.exports.inject = inject
    module.exports.apply = apply
    module.exports.loadParts = loadParts
    return module.exports
  },
})
