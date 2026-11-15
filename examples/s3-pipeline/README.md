# Rippflow S3 pull → mutate → push pipeline example

Demonstrates local and `--docker` execution of a 3-phase S3 workflow with per-phase IAM role assumption (`sts:AssumeRole` + Rippflow session-policy guardrails) against an S3/STS Testcontainer (such as LocalStack):

1. `pull_from_s3` (`s3.pull`, `role: s3-reader`, `hardened_subprocess_with_scoped_session`): assumes `arn:aws:iam::111122223333:role/s3-reader` via STS and reads `s3://app-orders/raw/<shard>.json` using `boto3`.
2. `mutate_records` (`orders.mutate`, `shared_runner_no_aws_credentials`, no `role`): runs with all AWS credentials scrubbed (`RIPPFLOW_EFFECTIVE_ROLE_ARN=minimal-default-role`), normalizes line items, computes tax/totals and a SHA-256 digest, and emits the mutated record.
3. `push_to_s3` (`s3.push`, `role: s3-writer`, `hardened_subprocess_with_scoped_session`): assumes `arn:aws:iam::111122223333:role/s3-writer` via STS and writes `s3://app-orders/processed/<shard>.json` back to S3 using `boto3`.

## Validate and run with Docker + Testcontainer

```sh
rippflow validate examples/s3-pipeline/workflow.yaml --handlers examples/s3-pipeline/handlers.json
AWS_ENDPOINT_URL=http://127.0.0.1:4566 \
  rippflow dev examples/s3-pipeline/workflow.yaml \
  --handlers examples/s3-pipeline/handlers.json \
  --input examples/s3-pipeline/input.json \
  --shards a,b \
  --docker \
  --report report.json
```

When `--docker` and `AWS_ENDPOINT_URL*` are used, `rippflow` assumes each phase's declared role on the host with Rippflow's least-privilege session policy, adds `--add-host=host.docker.internal:host-gateway`, rewrites loopback `AWS_ENDPOINT_URL*` to `host.docker.internal`, and injects only that phase's short-lived STS credentials into the Lambda container (`public.ecr.aws/lambda/python:3.13`). Phases without a `role` receive zero AWS credentials.
