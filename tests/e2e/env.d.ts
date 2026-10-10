// The only Node surface the specs touch is process.env. @types/node is not a dependency of
// this repo, so declare just that, scoped to tests/e2e/tsconfig.json (the frontend program
// does not include this file).
declare const process: { env: Record<string, string | undefined> };
