# client

The React and TypeScript frontend of LatLang, built with Vite. The build goes to `client/out`, which the backend serves.

Parent: [root README](../README.md).

## Files

| Path | Content |
|---|---|
| [`src/`](src/README.md) | The source |
| `public/` | Images and logos copied to the build (`images/`, `logos/`). The folder has no README |
| `index.html` | The page the build starts from |
| `package.json`, `bun.lock`, `package-lock.json` | Dependencies and scripts |
| `vite.config.ts` | Vite: React plugin, the `@` alias, port 3000, a proxy of `/api` to `http://localhost:8000`, output to `out`, `vitest` in a Node environment |
| `tsconfig.json`, `tsconfig.node.json` | TypeScript settings |
| `tailwind.config.ts`, `postcss.config.mjs` | Tailwind and PostCSS |

## Commands

```bash
bun install
bun run dev      # development server on :3000
bun run build    # tsc, then vite build -> client/out
bun run lint
bun run test     # vitest
```

`npm` works the same. `bun run build` fails on a type error, so a green build means the types hold.

## Stack

React 18, Tailwind CSS 3, `lucide-react` icons, `sonner` toasts, `pdfjs-dist` to render the review PDFs. No router, no
state library: the page keeps its state in React hooks.

## Rules

- `client/out` is not committed. The deployment script builds it and uploads it with the code; a backend without
  `client/out` serves the API only.
- The sources under `client/src` and `client/public` are not uploaded to the workspace; only the build is.

## See also

- [src](src/README.md): the source.
- [utils/deploy](../utils/deploy/README.md): how the build reaches the app.
