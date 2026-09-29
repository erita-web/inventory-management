---
name: verify
description: How to start and drive this app (FastAPI backend + Vue frontend) to verify a change end to end in a real browser and against the real API.
---

# Verifying changes in this app

## Start it
- **Backend** (port 8001): `cd server && uv run python main.py`. There is no auto-reload, so restart it after any change in `server/` (code or `data/*.json`).
- **Frontend** (port 3000): `cd client && npm install && npm run dev`. It hot-reloads. The API address is hard-coded to `http://localhost:8001/api` in `client/src/api.js`.
- `./scripts/start.sh` starts both. Needs Python 3.11+ and Node.
- If esbuild's native binary cannot run on the machine (`npm install` fails in esbuild's install script, or the binary gets killed): install esbuild another way, run `npm install --ignore-scripts`, pin the npm `esbuild` package to the same version as that binary (a temporary `overrides` entry in `package.json`, restored afterwards), and start Vite with `ESBUILD_BINARY_PATH=<path to the binary>`. Vite 5 with esbuild 0.22 or newer also needs `optimizeDeps.esbuildOptions.target: 'es2020'`; put that in a separate Vite config file passed with `--config` so `client/vite.config.js` stays unchanged.

## Drive it
- **UI:** use the Playwright MCP tools (see `CLAUDE.md`) against `http://localhost:3000`. Without them, headless Chrome over the DevTools protocol works: set an `<input type="range">` through the native `value` setter, then dispatch an `input` event so Vue sees it.
- **API:** `curl http://localhost:8001/api/...`; interactive docs at `/docs`.

## Flows worth driving
- **Restocking** (`/restocking`): move the slider (the table refreshes after about 300 ms, and Place Order is disabled meanwhile), drag it to 0 (empty state), place an order (success banner; the button stays off until the slider moves), then open **Orders**: the order is listed under "Submitted Orders" with its lead time.
- Dashboard, Inventory, Orders, Demand and Spending follow the global filter bar; Reports and Restocking ignore it.
- Language: set `localStorage['app-locale'] = 'ja'` and reload (currency switches to yen at a fixed 150 rate).

## Gotchas
- Placing a restock order writes `server/data/restock_orders.json` (gitignored). To avoid leaving test orders behind, verify against a scratch copy of `server/` (`main.py`, `mock_data.py`, `restocking.py`, `data/`) started with `python -m uvicorn main:app --port 8001`. If you used the real server, that file holds only orders you placed; check it before removing anything.
- A damaged `restock_orders.json` stops the server at startup on purpose, with a message naming the file.
- New restock orders are stamped with the machine's current date; the sample customer orders are all from 2025.
