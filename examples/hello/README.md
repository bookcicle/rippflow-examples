# Hello Workflow (`examples/hello`)

Minimal single-phase Python workflow matching the UI quickstart example.

## Run Locally from `examples/hello` or Repository Root

```sh
rippflow validate examples/hello/workflow.yaml --handlers examples/hello/handlers.json
(cd examples/hello && rippflow dev workflow.json --handlers handlers.json --report ../../hello-report.json)
```
