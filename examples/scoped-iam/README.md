# Read an order with an approved IAM role

The managed Python handler reads `app-orders` using short-lived credentials for the approved `orders-reader` role. No dedicated handler Lambda or queue ARN belongs in the workflow.

```yaml
name: read-order-with-approved-role
version: 1.0.0
default_group: python-standard
phases:
  read_order:
    service: orders.read
    role: orders-reader
    isolationBoundary: hardened_subprocess_with_scoped_session
    inputs:
      table: app-orders
      orderId: order-123
    terminal: true
```

`handler.py` uses the normal boto3 credential chain. Do not call AssumeRole in the handler, embed AWS keys in inputs, or copy `__*` authorization fields into a workflow.

## Validate the definition

From the core checkout, using a CLI built against this patch:

```sh
rippflow validate examples/scoped-iam/workflow.json
rippflow validate examples/scoped-iam/workflow.yaml
```

Expected: `Valid workflow: read-order-with-approved-role v1.0.0 (1 phases)` for each format. This does not request credentials. Local `rippflow dev` does not perform installation IAM-role propagation unless STS role assumption (`RIPPFLOW_ASSUME_PHASE_ROLES=1` or an AWS STS endpoint) is explicitly configured.

## Operator setup for AWS

Requires an installation containing this core/runner patch, with approved-role catalog and signed deployment validation configured. Raw task options without a signed authorized envelope are rejected. The operator supplies its installed runner role ARN/name and installation ID. The optional template creates an application table and a read-only customer role; it does not install Rippflow or a handler Lambda.

```sh
aws cloudformation deploy \
  --stack-name example-orders-reader \
  --template-file examples/scoped-iam/role-template.json \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    RunnerRoleArn="$EXAMPLE_RUNNER_ROLE_ARN" \
    InstallationId="$EXAMPLE_INSTALLATION_ID"

EXAMPLE_READER_ROLE_ARN="$(aws cloudformation describe-stacks \
  --stack-name example-orders-reader \
  --query 'Stacks[0].Outputs[?OutputKey==`RoleArn`].OutputValue | [0]' \
  --output text)"

python3 - "$EXAMPLE_READER_ROLE_ARN" <<'PY'
import json, sys
policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["sts:AssumeRole", "sts:TagSession"], "Resource": sys.argv[1]}]}
with open('/tmp/assume-orders-reader.json', 'w') as stream:
    json.dump(policy, stream)
PY
aws iam put-role-policy \
  --role-name "$EXAMPLE_RUNNER_ROLE_NAME" \
  --policy-name AssumeExampleOrdersReader \
  --policy-document file:///tmp/assume-orders-reader.json

aws dynamodb put-item --table-name app-orders \
  --item '{"orderId":{"S":"order-123"},"status":{"S":"ready"}}'
```

Use an operator AWS profile in the target account. These commands create workload resources and modify the selected runner's assume-role permission; automated example checks do not execute them. Changing `TableName` also requires changing the workflow input.

In trusted installation release tooling, register the ARN with alias `orders-reader`:

```rust,ignore
registry.register_approved_role_with_dedicated_lambda(
    role_arn, Some("orders-reader"), None,
)?;
```

Resolve the alias before signing the deployment manifest; bind the role ARN, `python-standard`, artifact digest and `HardenedSubprocessWithScopedSession` to `read_order`. Publish the handler artifact with boto3 included, then dispatch through the installation's existing intake. The current CLI has no role-registration command; a workflow role field alone does not create a role or grant access.

Expected handler result:

```json
{"outcome":"completed","payload":{"orderId":"order-123","status":"ready"}}
```

## Permission boundary

The approved role allows only `dynamodb:GetItem` on the application table. Writes and other tables remain unauthorized. STS intersects role permissions with the session guardrail; resource policies, permissions boundaries and SCPs still participate in AWS authorization. See [AWS session-policy semantics](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html#policies_session).

Rippflow explicitly denies IAM operations (including PassRole), further AssumeRole calls, and reserved Rippflow DynamoDB/SQS/Lambda/Kinesis/cache/log/S3 resource patterns. Operators must keep custom-named infrastructure out of workload grants. The template trusts only the selected runner and requires the installation session tag. These trusted subprocesses are not a hardware isolation boundary; strict separation requires a dedicated Lambda.

Cleanup: remove the `AssumeExampleOrdersReader` inline policy from the selected runner and delete the `example-orders-reader` stack when the example is no longer needed.
