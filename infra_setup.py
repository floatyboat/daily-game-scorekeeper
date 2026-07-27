"""One-time AWS setup for the streak store (SPEC.md Phase 1). Safe to re-run.

Creates the DynamoDB table (provisioned 5/5, inside the always-free tier),
grants each lambda's role access, and merges TABLE_NAME into each function's
environment without touching its other variables. Each step no-ops when
already applied; a missing function is reported and skipped so partial
environments still set up what they can.

    python3 infra_setup.py
"""
import json
import time

import boto3
from botocore.exceptions import ClientError

REGION = 'us-east-1'
TABLE = 'daily-game-tracker'
FUNCTIONS = ['daily-game-score', 'daily-game-sticky', 'daily-game-play']
POLICY_NAME = 'daily-game-tracker-access'


def ensure_table(ddb):
    try:
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {'AttributeName': 'PK', 'AttributeType': 'S'},
                {'AttributeName': 'SK', 'AttributeType': 'S'},
            ],
            KeySchema=[
                {'AttributeName': 'PK', 'KeyType': 'HASH'},
                {'AttributeName': 'SK', 'KeyType': 'RANGE'},
            ],
            BillingMode='PROVISIONED',
            ProvisionedThroughput={'ReadCapacityUnits': 5, 'WriteCapacityUnits': 5},
        )
        print(f'creating table {TABLE}...')
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceInUseException':
            raise
        print(f'table {TABLE} already exists')
    ddb.get_waiter('table_exists').wait(TableName=TABLE)
    return ddb.describe_table(TableName=TABLE)['Table']['TableArn']


def policy_document(table_arn):
    return json.dumps({
        'Version': '2012-10-17',
        'Statement': [{
            'Effect': 'Allow',
            'Action': [
                'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
                'dynamodb:Query', 'dynamodb:Scan',
                'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem',
                'dynamodb:DescribeTable',
            ],
            'Resource': table_arn,
        }],
    })


def grant_function(lam, iam, function_name, table_arn, todo):
    """Apply what this identity can; queue remediation commands for the rest."""
    try:
        cfg = lam.get_function_configuration(FunctionName=function_name)
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            raise
        print(f'  {function_name}: not found, skipped')
        return

    role_name = cfg['Role'].split('/')[-1]
    try:
        iam.put_role_policy(RoleName=role_name, PolicyName=POLICY_NAME,
                            PolicyDocument=policy_document(table_arn))
        print(f'  {function_name}: role {role_name} granted')
    except ClientError as e:
        if e.response['Error']['Code'] != 'AccessDenied':
            raise
        print(f'  {function_name}: no iam:PutRolePolicy from here -- queued for admin')
        todo.append(f"aws iam put-role-policy --role-name {role_name} "
                    f"--policy-name {POLICY_NAME} --policy-document '{policy_document(table_arn)}'")

    env = cfg.get('Environment', {}).get('Variables', {})
    if env.get('TABLE_NAME') == TABLE:
        print(f'  {function_name}: TABLE_NAME already set')
        return
    env['TABLE_NAME'] = TABLE
    # A deploy in flight holds a conflict lock briefly; retry rather than fail.
    for attempt in range(5):
        try:
            lam.update_function_configuration(
                FunctionName=function_name, Environment={'Variables': env})
            print(f'  {function_name}: TABLE_NAME set')
            return
        except ClientError as e:
            code = e.response['Error']['Code']
            if code == 'ResourceConflictException' and attempt < 4:
                time.sleep(5)
                continue
            if code == 'AccessDenied':
                print(f'  {function_name}: no lambda:UpdateFunctionConfiguration -- '
                      f'queued for admin (or set TABLE_NAME={TABLE} in the console)')
                todo.append(f"# add env var TABLE_NAME={TABLE} to {function_name} "
                            f"(Lambda console > Configuration > Environment variables)")
                return
            raise


def main():
    ddb = boto3.client('dynamodb', region_name=REGION)
    lam = boto3.client('lambda', region_name=REGION)
    iam = boto3.client('iam')

    table_arn = ensure_table(ddb)
    print(f'table ready: {table_arn}')
    print('wiring lambdas:')
    todo = []
    for function_name in FUNCTIONS:
        grant_function(lam, iam, function_name, table_arn, todo)
    if todo:
        print('\nRun these with an admin identity (e.g. CloudShell in the console):')
        for cmd in todo:
            print(f'  {cmd}')


if __name__ == '__main__':
    main()
