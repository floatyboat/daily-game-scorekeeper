"""One-time AWS setup + migration steps for the scoreboard. Safe to re-run.

    python3 infra_setup.py               # table, IAM grants, TABLE_NAME env
    python3 infra_setup.py --migrate     # copy legacy GUILD#<id>/CONFIG items
                                         # to the GUILDS partition (pre-deploy safe)
    python3 infra_setup.py --hourly      # 'time' rule daily -> hourly + daily
                                         # lambda timeout bump (run AFTER deploy)
    python3 infra_setup.py --prune-env   # strip per-server env vars from the
                                         # lambdas (run AFTER deploy)

Each step no-ops when already applied. Anything this identity lacks permission
for is printed as a command to run with an admin identity.
"""
import argparse
import json
import time

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

REGION = 'us-east-1'
TABLE = 'daily-game-tracker'
FUNCTIONS = ['daily-game-score', 'daily-game-sticky', 'daily-game-play']
POLICY_NAME = 'daily-game-tracker-access'
DAILY_RULE = 'time'

# Per-server settings now live exclusively in the table (GUILDS partition);
# these env vars configure nothing once the multi-server code is deployed.
PER_SERVER_ENV = ['INPUT_CHANNEL_ID', 'OUTPUT_CHANNEL_ID', 'TIMEZONE',
                  'TIME_WINDOW_HOURS', 'HOURS_AFTER_MIDNIGHT', 'MINIMUM_PLAYERS',
                  'HUNDREDS_OF_MESSAGES', 'WORDLE_BOT_ID', 'UTC_OFFSET']

# Config fields carried over from a legacy CONFIG item as-is.
MIGRATE_FIELDS = ['input_channel_id', 'output_channel_id', 'timezone',
                  'hours_after_midnight', 'time_window_hours', 'minimum_players',
                  'hundreds_of_messages', 'wordle_bot_id', 'last_finalized_day']


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


def _update_function_config(lam, function_name, todo, describe, **kwargs):
    """update_function_configuration with deploy-lock retries and the
    queue-for-admin fallback on AccessDenied."""
    for attempt in range(5):
        try:
            lam.update_function_configuration(FunctionName=function_name, **kwargs)
            print(f'  {function_name}: {describe}')
            return
        except ClientError as e:
            code = e.response['Error']['Code']
            if code == 'ResourceConflictException' and attempt < 4:
                time.sleep(5)
                continue
            if code == 'AccessDenied':
                print(f'  {function_name}: no lambda:UpdateFunctionConfiguration -- '
                      f'queued for admin ({describe})')
                todo.append(f'# {function_name}: {describe} '
                            f'(Lambda console > Configuration)')
                return
            raise


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
    _update_function_config(lam, function_name, todo, f'TABLE_NAME={TABLE} set',
                            Environment={'Variables': env})


def migrate_configs():
    """Copy legacy GUILD#<gid>/CONFIG items into the GUILDS partition.

    Pre-deploy safe: the old code never reads the new key and the new code
    never reads the old one. Legacy items are left in place (the still-deployed
    old code keeps touching them) -- delete them by hand once the new code is
    live. Existing GUILDS items are never overwritten.
    """
    table = boto3.resource('dynamodb', region_name=REGION).Table(TABLE)
    scan = {'FilterExpression': Attr('SK').eq('CONFIG')}
    items = []
    while True:
        resp = table.scan(**scan)
        items += resp['Items']
        if 'LastEvaluatedKey' not in resp:
            break
        scan['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    if not items:
        print('no legacy CONFIG items found')
        return
    for item in items:
        gid = str(item.get('guild_id') or item['PK'].split('#', 1)[1])
        key = {'PK': 'GUILDS', 'SK': f'GUILD#{gid}'}
        if table.get_item(Key=key).get('Item'):
            print(f'  guild {gid}: already migrated, skipped')
            continue
        new_item = {**key, 'guild_id': gid,
                    'daily_enabled': True, 'sticky_enabled': True}
        new_item.update({f: item[f] for f in MIGRATE_FIELDS if item.get(f) is not None})
        table.put_item(Item=new_item)
        print(f'  guild {gid}: migrated '
              f"(input={new_item.get('input_channel_id')}, "
              f"output={new_item.get('output_channel_id')})")
    print('review each guild: output_channel_id correct? post_hour wanted? '
          '(unset post_hour = post at day-start hour)')


def hourly_schedule(events, lam, todo):
    """Flip the daily rule to hourly (per-guild gating decides who posts when)
    and give the loop headroom on the daily lambda's timeout. AFTER deploy:
    the old single-guild code posts on every invocation."""
    try:
        rule = events.describe_rule(Name=DAILY_RULE)
        print(f"rule {DAILY_RULE}: currently {rule.get('ScheduleExpression')}")
        if rule.get('ScheduleExpression') == 'cron(0 * * * ? *)':
            print(f'rule {DAILY_RULE}: already hourly')
        else:
            events.put_rule(Name=DAILY_RULE, ScheduleExpression='cron(0 * * * ? *)',
                            State='ENABLED')
            print(f'rule {DAILY_RULE}: now cron(0 * * * ? *)')
    except ClientError as e:
        if e.response['Error']['Code'] not in ('AccessDenied', 'AccessDeniedException'):
            raise
        print(f'rule {DAILY_RULE}: no events:PutRule from here -- queued for admin')
        todo.append(f'aws events put-rule --name {DAILY_RULE} '
                    f"--schedule-expression 'cron(0 * * * ? *)' --region {REGION}")
    _update_function_config(lam, 'daily-game-score', todo, 'timeout -> 120s', Timeout=120)


def prune_env(lam, todo):
    """Remove per-server env vars the deployed code no longer reads, so the
    table is visibly the only config source. AFTER deploy."""
    for function_name in FUNCTIONS:
        try:
            cfg = lam.get_function_configuration(FunctionName=function_name)
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
            print(f'  {function_name}: not found, skipped')
            continue
        env = cfg.get('Environment', {}).get('Variables', {})
        removed = [k for k in PER_SERVER_ENV if k in env]
        if not removed:
            print(f'  {function_name}: nothing to prune')
            continue
        for k in removed:
            del env[k]
        _update_function_config(lam, function_name, todo,
                                f"pruned {', '.join(removed)}",
                                Environment={'Variables': env})


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--migrate', action='store_true',
                    help='copy legacy CONFIG items to the GUILDS partition')
    ap.add_argument('--hourly', action='store_true',
                    help='switch the daily rule to hourly (run after deploy)')
    ap.add_argument('--prune-env', action='store_true',
                    help='strip per-server env vars from the lambdas (run after deploy)')
    args = ap.parse_args()

    todo = []
    if args.migrate:
        migrate_configs()
    if args.hourly:
        hourly_schedule(boto3.client('events', region_name=REGION),
                        boto3.client('lambda', region_name=REGION), todo)
    if args.prune_env:
        prune_env(boto3.client('lambda', region_name=REGION), todo)

    if not (args.migrate or args.hourly or args.prune_env):
        ddb = boto3.client('dynamodb', region_name=REGION)
        lam = boto3.client('lambda', region_name=REGION)
        iam = boto3.client('iam')
        table_arn = ensure_table(ddb)
        print(f'table ready: {table_arn}')
        print('wiring lambdas:')
        for function_name in FUNCTIONS:
            grant_function(lam, iam, function_name, table_arn, todo)

    if todo:
        print('\nRun these with an admin identity (e.g. CloudShell in the console):')
        for cmd in todo:
            print(f'  {cmd}')


if __name__ == '__main__':
    main()
