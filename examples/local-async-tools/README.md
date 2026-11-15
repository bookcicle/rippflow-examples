# Slim launchers and local callbacks

From the CLI repository root:

```sh
rippflow validate --project examples/local-async-tools/rippflow.yaml --json
rippflow run --project examples/local-async-tools/rippflow.yaml --timeout-seconds 30 --json
rippflow profile --project examples/local-async-tools/rippflow.yaml --timeout-seconds 30 --json
```

Or deploy locally over HTTPS (sharing the deployment server's `/v1/callbacks` endpoint):

```sh
rippflow deploy --local --project examples/local-async-tools/rippflow.yaml --json
```

A slim Python launcher passes its stdin envelope to an independent child, redirects both child stdout and stderr, acknowledges dispatch, and exits. External work takes 1.5 seconds while the launcher has a one-second active timeout. The build callback unlocks two parallel reviewer callbacks; both are required before finish. These are deterministic fixtures, with no real model calls, PR creation, or human approval.

Read [the exact transport, security, resource, and recovery contract](../../docs/features/local-async-tool-callbacks.md) before adapting it. This illustrative local catalog is not a deployment catalog. Python3 is required; use an installed versioned runtime catalog and suitable provider adapter for deployment.
