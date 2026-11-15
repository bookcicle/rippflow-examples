# Native edge examples

Build the native Rust edge handlers in this repository with Cargo, validate the workflow definitions with `rippflow validate`, and run `rippflow dev` locally:

```sh
cargo build --locked -p rippflow-edge-example-handler
rippflow validate examples/edge-native/sensor/workflow.json
rippflow validate examples/edge-native/vision/workflow.json
rippflow validate examples/edge-native/document/workflow.json
```

Each workflow's `handlers.json` expects `./handler` inside its workflow directory. Link the compiled native handler binary before running locally with `rippflow dev`:

```sh
ln -sf ../../../target/debug/rippflow-edge-example-handler examples/edge-native/sensor/handler
rippflow dev examples/edge-native/sensor/workflow.json \
  --handlers examples/edge-native/sensor/handlers.json \
  --input examples/edge-native/sensor/input.json

ln -sf ../../../target/debug/rippflow-edge-example-handler examples/edge-native/vision/handler
rippflow dev examples/edge-native/vision/workflow.json \
  --handlers examples/edge-native/vision/handlers.json \
  --input examples/edge-native/vision/input.json

ln -sf ../../../target/debug/rippflow-edge-example-handler examples/edge-native/document/handler
rippflow dev examples/edge-native/document/workflow.json \
  --handlers examples/edge-native/document/handlers.json \
  --input examples/edge-native/document/input.json
```

- **Sensor (`sensor/`):** Read a temperature and emit `alert: true` when above threshold.
- **Vision (`vision/`):** Evaluate simulated brightness and object branches and join them into an inspection decision.
- **Document (`document/`):** Ingest -> validate -> transform -> artifact (simulated checksum and byte count).
- **Recovery (`recovery/`):** Fault-injection fixture backed by `rippflow-edge-fault-handler` (`examples/native-handlers/src/fault.rs`) for testing checkpoint and recovery paths.
