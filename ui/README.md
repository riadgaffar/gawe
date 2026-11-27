# GAWE UI

Next.js (App Router, React, TypeScript, no UI library). It is a thin client: the API decides
everything; the UI shows state and relays the user's choices.

```bash
make ui-install     # once
make ui-dev         # http://localhost:3000, needs `make api` running
make ui-check       # typecheck + model tests + rendered component tests + production build
```

Node 22.6 or newer (the unit tests run TypeScript directly).

## How it works

- **Sign-in:** paste a dev token. A server action checks it with `GET /v1/me` and stores it in an
  httpOnly, SameSite=Lax cookie. Page code and browser JavaScript never see it.
- **Pages** are server components that call the API with the cookie's token (`lib/session.ts`,
  `lib/api-client.ts`). Mutations are server actions (`app/actions.ts`); Next checks their origin.
- **Live updates:** the run page opens an `EventSource` on `/api/runs/{id}/events`, a route handler
  that forwards that one API stream with the token added. Each event triggers a page refresh.
- **Approvals:** the card shows the exact resolved arguments and `action_hash` and sends that same
  hash back. If the action changed, the API answers 409 and the UI says to reload.

## Tests

`make ui-check` runs TypeScript, both test suites and the production build. To run one suite
from `ui/`:

```bash
npm run test:models  # Node: pure view models, formatting and API client
npm run test:render  # Vitest + jsdom: actual React components and interaction
npm test            # both suites; neither needs an API server or model credentials
```

The rendering suite mounts ApprovalCard and RecordEvidenceCard using React Testing Library.
It checks typed facts before collapsed model prose, replay rows, hashes, history and differences,
safe text rendering, selected record values, refusal states, observation timing, partial coverage
and original undo provenance. Approve/Reject tests inspect submitted FormData and exercise
pending, success and error states. Only the server action is mocked; React form hooks, child
components and Next Link stay real. Test fixtures are synthetic and no approval reaches an API.
The two old component-source inspections are replaced by these rendered assertions; the model
tests remain. Vitest includes only `*.render.test.tsx`, keeping Node tests out of its collection.

Compatible development-only package lines are locked in `package-lock.json`; the Node minimum
and production dependency versions are unchanged. The suite uses jsdom, so it does not prove
browser layout, full asynchronous page rendering, Next routing/session handling or server-action
transport. It also does not measure whether the presentation changes human approval decisions.
See [M28's design and checkpoint](../docs/m28-design.md).

## Configuration

`GAWE_API_URL` (default `http://127.0.0.1:8000`) and `GAWE_UI_SECURE_COOKIE=1` over HTTPS.
Samples in `public/samples/` are copied from the apps by `python scripts/sync_ui_samples.py`.

## Not included (MVP)

User management, registration, pagination, profile discovery (the profile is a text field).
