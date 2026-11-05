# src

The source of the frontend.

Parent: [client](../README.md).

## Files

| Path | Content |
|---|---|
| `main.tsx` | Mounts `App` and the toast area (`sonner`) |
| `App.tsx` | Page frame: theme provider, top bar, and either the translate page or, when the address is `/shared/<token>`, the shared read-only page. There is no router |
| [`components/translate/`](components/translate/README.md) | Every screen of the application |
| `components/layout/TopBar.tsx` | The fixed top bar with the logo and the application name, read from `GET /api/config/app` |
| `components/shared/Flag.tsx` | Small inline SVG flags used for language badges, so they look the same on every platform |
| `contexts/ThemeContext.tsx` | Provides the colours, typography and background settings, applies them as CSS variables, keeps custom themes in `localStorage` |
| `lib/themes.ts` | The theme definitions |
| `lib/config.ts` | `getAppConfig` and `getUserMe`, cached after the first call, with defaults when the API is unreachable |
| `lib/fetchJson.ts` | `fetchJson<T>(url, init)`: parses JSON, throws an `Error` with the `error` field of a failed answer. Unit-tested in `fetchJson.test.ts` |
| `styles/globals.css` | Fonts, Tailwind layers, base styles |
| `styles/theme.css` | Static design tokens: spacing, radii, shadows, layout sizes, animations. Colours are set at run time from the theme |
| `styles/textures.css` | The grain overlay |
| `vite-env.d.ts` | Vite type declarations |

## Rules

- `@/` is an alias of this folder (`vite.config.ts`, `tsconfig.json`).
- Colours come from CSS variables (`var(--color-bg-primary)`, ...), never from fixed values, so a theme change reaches every
  component.
- The `/api` prefix is relative: the same build works behind Databricks Apps and, through the Vite proxy, locally.

## See also

- [client](../README.md): build and tooling.
- [translate components](components/translate/README.md).
