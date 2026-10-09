---
description: Rules and guidelines for writing and running React tests in keep-ui
globs: keep-ui/**/*.{test,spec}.{ts,tsx}
---

# Writing frontend tests

Place tests in `__tests__` folder in the module, e.g. tests for file `/features/workflows/model/useWorkflows.tsx` should be `/features/workflows/models/__tests__/useWorkflows.test.tsx`

# Running frontend tests

Please run tests with command: `npm run test` in `keep-ui` folder.
For example:
```bash
cd keep-ui && npm run test
```
