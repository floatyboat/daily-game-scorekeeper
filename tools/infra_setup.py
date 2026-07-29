"""Idempotent rebuild of the scoreboard's AWS infrastructure.

Run from the repository root. `dotenv` supplies the global env vars the
functions need; AWS credentials come from the usual boto3 chain:

    dotenv run -- python3 tools/infra_setup.py --plan   # diff only, changes nothing
    dotenv run -- python3 tools/infra_setup.py          # converge to the state below
    dotenv run -- python3 tools/infra_setup.py --prune  # also remove what isn't declared

`FUNCTIONS` and the constants above it are the definition of the stack: table,
one IAM role per lambda, the three functions, their log retention, the two
EventBridge schedules, and the interaction lambda's Function URL. Every step
reads the live state first and writes only the difference, so a run against a
healthy install prints all-ok and touches nothing, and a run against an empty
account builds the whole thing.

Deliberately conservative about two things, because a rebuild script that
clobbers a live install is worse than one that reports:

- **Env values that differ** from the local environment are reported, never
  overwritten -- a rotated secret must not be reverted by a stale `.env`.
  Missing keys *are* set, which is what makes a fresh function work.
- **Undeclared** env vars and layers are reported and removed only under
  `--prune`.

`--plan` exits non-zero when it finds drift, so it doubles as a check.

What this script cannot do, printed as a checklist at the end: push the
function code (the three GitHub Actions workflows own that), point Discord's
Interactions Endpoint at the Function URL, and register the slash commands.
Anything the running identity lacks permission for is printed as a command to
re-run with an admin identity.

Data is not infrastructure: this script never writes table items. The table
carries point-in-time recovery for that, and `tools/backfill.py` rebuilds
aggregates from the archived days.
"""
import argparse
import io
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

REGION = 'us-east-1'
TABLE = 'daily-game-tracker'
POLICY_NAME = 'daily-game-tracker-access'
LOG_RETENTION_DAYS = 30
BASIC_EXECUTION = 'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'

# Provisioned rather than on-demand: 5/5 sits inside the account's always-free
# tier and covers the daily write burst, the per-minute sticky reads, and
# interaction clicks (docs/SPEC.md, "Free-tier math").
TABLE_THROUGHPUT = {'ReadCapacityUnits': 5, 'WriteCapacityUnits': 5}

LAMBDA_TRUST = json.dumps({
    'Version': '2012-10-17',
    'Statement': [{'Effect': 'Allow', 'Principal': {'Service': 'lambda.amazonaws.com'},
                   'Action': 'sts:AssumeRole'}],
})

# The Function URL is public by design: Discord calls it unauthenticated, and
# interaction_lambda's ed25519 signature check authenticates the caller.
FUNCTION_URL_CORS = {'AllowOrigins': ['https://discord.com']}

DENIED = ('AccessDenied', 'AccessDeniedException')

# Codes that all mean "there is nothing there to read", across four services.
ABSENT = ('ResourceNotFoundException', 'NoSuchEntity', 'TableNotFoundException')

# Distinct from absent: the resource may well be correct, this identity just
# cannot look. Only the caller knows whether that is worth reporting.
UNREADABLE = object()


@dataclass(frozen=True)
class Function:
    """One lambda and everything that hangs off it."""
    name: str
    handler: str
    runtime: str
    timeout: int
    memory: int
    env: tuple
    rule: str = ''
    schedule: str = ''
    function_url: bool = False
    self_invoke: bool = False


# Global identity only. Per-server settings live in the table's GUILDS
# partition (managed by /setup) and the code has no env fallback for them, so
# anything server-shaped appearing here would be dead weight. TABLE_NAME is
# derived from the constant above; the rest are read from the local environment.
COMMON_ENV = ('TABLE_NAME', 'DISCORD_BOT_TOKEN', 'DISCORD_BOT_ID', 'MINIMUM_STREAK')

# Declared, but the code carries a working default, so leaving one unset is a
# choice rather than a gap -- absent values are set when available and not
# reported when not.
OPTIONAL_ENV = frozenset({'MINIMUM_STREAK'})

FUNCTIONS = [
    Function(
        name='daily-game-score',
        handler='lambda_function.lambda_handler',
        # 3.13 while the other two are 3.14: deploy.yml builds this zip in the
        # matching SAM container, so the runtime and the build image move together.
        runtime='python3.13',
        # The hourly tick loops every guild and re-parses each one's history.
        timeout=120,
        memory=128,
        env=COMMON_ENV + ('TEST_CHANNEL_ID',),
        # Named 'time' for historical reasons. EventBridge cannot rename a rule
        # in place, so correcting it means create-new/delete-old plus a fresh
        # invoke permission on the target -- not worth it for a cosmetic gain.
        rule='time',
        # Hourly, not daily: each guild posts when its own local post_hour comes
        # around, and per-guild gating in the handler decides who posts on a tick.
        schedule='cron(0 * * * ? *)',
    ),
    Function(
        name='daily-game-sticky',
        handler='sticky_lambda.lambda_handler',
        runtime='python3.14',
        timeout=30,
        # Pillow decodes Wordle result images; 128MB leaves no headroom for it.
        memory=512,
        env=COMMON_ENV + ('TEST_CHANNEL_ID',),
        rule='daily-game-sticky',
        schedule='cron(* * * * ? *)',
    ),
    Function(
        name='daily-game-play',
        handler='interaction_lambda.lambda_handler',
        runtime='python3.14',
        # Discord's own deadline is 3s and the handler defers past it by
        # re-invoking itself; 30s bounds that second, asynchronous half.
        timeout=30,
        memory=512,
        env=COMMON_ENV + ('DISCORD_PUBLIC_KEY', 'DEV_CHANNEL_ID'),
        function_url=True,
        self_invoke=True,
    ),
]


def env_value(key):
    """What this stack wants a global env var set to, or None when the local
    environment has nothing to offer."""
    if key == 'TABLE_NAME':
        return TABLE
    return os.environ.get(key) or None


def policy_document(table_arn, self_invoke_arn=None):
    statements = [{
        'Effect': 'Allow',
        'Action': [
            'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
            'dynamodb:Query', 'dynamodb:Scan',
            'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem',
            'dynamodb:DescribeTable',
        ],
        'Resource': table_arn,
    }]
    if self_invoke_arn:
        # Scoped to the one function: this is a function invoking itself
        # (interaction_lambda.defer), not a general grant to invoke anything.
        statements.append({
            'Effect': 'Allow',
            'Action': ['lambda:InvokeFunction'],
            'Resource': self_invoke_arn,
        })
    return json.dumps({'Version': '2012-10-17', 'Statement': statements})


def bootstrap_zip(handler):
    """A placeholder deploy package, used only when creating a function.

    The real code ships from the GitHub Actions workflows, which call
    update-function-code and so need the function to already exist. This fills
    that gap with a module named after the configured handler, so the function
    is createable and every downstream resource (rule, URL, permissions) can be
    wired before any code lands. It raises rather than returning a
    plausible-looking success, so a stack left half-built announces itself.
    """
    module = handler.split('.')[0]
    source = (
        'def lambda_handler(event, context):\n'
        '    raise RuntimeError(\n'
        '        "placeholder from tools/infra_setup.py -- '
        'run the deploy workflow for this function")\n'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f'{module}.py', source)
    return buffer.getvalue()


def swallow(action, *codes):
    """Run a create whose "already exists" error means the step is done.

    Creates are the one place idempotency can't be decided by the read before
    them: two runs racing, or a read this identity wasn't allowed to make, both
    land here.
    """
    try:
        return action()
    except ClientError as e:
        if e.response['Error']['Code'] not in codes:
            raise


class Converge:
    """Applies a step, or records what applying it would do.

    Every mutation in this script goes through `do`, which is what keeps
    `--plan` honest: one place decides whether writes happen, so a step cannot
    accidentally mutate during a plan. AccessDenied is not fatal -- the step is
    queued as a command to re-run with an admin identity, which is the normal
    case for the IAM half of this stack.
    """

    def __init__(self, apply, prune):
        self.apply = apply
        self.prune = prune
        self.pending = []
        self.todo = []
        self.manual = []

    def ok(self, message):
        print(f'    ok      {message}')

    def skip(self, message):
        """Drift left in place on purpose (see the module docstring)."""
        print(f'    note    {message}')

    def do(self, message, action, admin_command=None, unverified=False):
        """Run `action` unless planning. Returns its result, or None.

        `unverified` marks a write whose current value this identity is not
        allowed to read back. Converge still applies it -- re-applying is a
        no-op, and it is the only way to repair a grant that really is missing
        -- but it is not counted as drift, because otherwise `--plan` would
        report the same unfixable "change" on every run and its exit code would
        stop meaning anything.
        """
        if not self.apply:
            if unverified:
                print(f'    note    {message}')
                return None
            self.pending.append(message)
            print(f'    would   {message}')
            return None
        try:
            result = action()
        except ClientError as e:
            if e.response['Error']['Code'] not in DENIED:
                raise
            print(f'    denied  {message} -- queued for admin')
            self.todo.append(admin_command or f'# {message} (no permission from here)')
            return None
        print(f'    done    {message}')
        return result

    def read(self, action, default=None, denied=None):
        """A describe/get that treats "absent" as `default`.

        "Not allowed to look" collapses into the same answer unless the caller
        passes `denied` -- either way it falls through to the write, which is
        itself idempotent; the distinction only changes what gets printed.
        """
        try:
            return action()
        except ClientError as e:
            code = e.response['Error']['Code']
            if code in DENIED:
                return default if denied is None else denied
            if code in ABSENT:
                return default
            raise


def update_function_config(cv, lam, name, message, admin_command=None, **kwargs):
    """update_function_configuration, retrying the deploy lock.

    A workflow deploying this function holds it for a few seconds; that is a
    wait, not a failure.
    """
    def call():
        for attempt in range(5):
            try:
                return lam.update_function_configuration(FunctionName=name, **kwargs)
            except ClientError as e:
                if (e.response['Error']['Code'] == 'ResourceConflictException'
                        and attempt < 4):
                    time.sleep(5)
                    continue
                raise
    return cv.do(f'{name}: {message}', call,
                 admin_command or f'# {name}: {message} (Lambda console > Configuration)')


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def ensure_table(cv, ddb):
    print(f'table {TABLE}')
    live = cv.read(lambda: ddb.describe_table(TableName=TABLE)['Table'])
    if live is None:
        cv.do(f'create {TABLE} (PK/SK strings, provisioned 5/5)',
              lambda: (swallow(lambda: ddb.create_table(
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
                  ProvisionedThroughput=TABLE_THROUGHPUT,
              ), 'ResourceInUseException'),
                  ddb.get_waiter('table_exists').wait(TableName=TABLE)),
              f'aws dynamodb create-table --table-name {TABLE} '
              f'--attribute-definitions AttributeName=PK,AttributeType=S '
              f'AttributeName=SK,AttributeType=S '
              f'--key-schema AttributeName=PK,KeyType=HASH '
              f'AttributeName=SK,KeyType=RANGE '
              f'--provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 '
              f'--region {REGION}')
    else:
        throughput = live['ProvisionedThroughput']
        actual = {k: throughput[k] for k in TABLE_THROUGHPUT}
        cv.ok(f"exists, {live['ItemCount']} items, "
              f"{actual['ReadCapacityUnits']}/{actual['WriteCapacityUnits']} provisioned")
        if actual != TABLE_THROUGHPUT:
            cv.skip(f'throughput is {actual}, declared {TABLE_THROUGHPUT} '
                    f'-- left alone (resizing a live table is not a rebuild step)')

    # PITR is the only backup this stack has, and the table holds the one thing
    # the rest of the account cannot recompute.
    backups = cv.read(lambda: ddb.describe_continuous_backups(
        TableName=TABLE)['ContinuousBackupsDescription'])
    status = ((backups or {}).get('PointInTimeRecoveryDescription') or {}).get(
        'PointInTimeRecoveryStatus')
    if status == 'ENABLED':
        cv.ok('point-in-time recovery enabled')
    else:
        cv.do('enable point-in-time recovery',
              lambda: ddb.update_continuous_backups(
                  TableName=TABLE,
                  PointInTimeRecoverySpecification={'PointInTimeRecoveryEnabled': True}),
              f'aws dynamodb update-continuous-backups --table-name {TABLE} '
              f'--point-in-time-recovery-specification '
              f'PointInTimeRecoveryEnabled=true --region {REGION}')


def ensure_role(cv, iam, fn, existing_role_arn, table_arn, account):
    """Return the role ARN for a function, creating the role when it is new.

    An existing function keeps whatever role it already has -- these were
    console-created with random `service-role/...-role-xxxxxxxx` names, and
    re-pointing a live function at a fresh role for tidiness would be a
    gratuitous risk. Only a from-zero build gets the predictable name.
    """
    if existing_role_arn:
        role_name = existing_role_arn.split('/')[-1]
        cv.ok(f'role {role_name} (existing)')
    else:
        role_name = f'{fn.name}-role'
        cv.do(f'create role {role_name}',
              lambda: swallow(lambda: iam.create_role(
                  RoleName=role_name, AssumeRolePolicyDocument=LAMBDA_TRUST,
                  Description=f'Execution role for {fn.name}'),
                  'EntityAlreadyExists'),
              f"aws iam create-role --role-name {role_name} "
              f"--assume-role-policy-document '{LAMBDA_TRUST}'")
        existing_role_arn = f'arn:aws:iam::{account}:role/{role_name}'
        cv.do(f'attach AWSLambdaBasicExecutionRole to {role_name}',
              lambda: iam.attach_role_policy(RoleName=role_name,
                                             PolicyArn=BASIC_EXECUTION),
              f'aws iam attach-role-policy --role-name {role_name} '
              f'--policy-arn {BASIC_EXECUTION}')

    self_invoke_arn = (f'arn:aws:lambda:{REGION}:{account}:function:{fn.name}'
                       if fn.self_invoke else None)
    document = policy_document(table_arn, self_invoke_arn)
    live = cv.read(lambda: json.dumps(iam.get_role_policy(
        RoleName=role_name, PolicyName=POLICY_NAME)['PolicyDocument'],
        sort_keys=True, separators=(',', ':')), denied=UNREADABLE)
    wanted = json.dumps(json.loads(document), sort_keys=True, separators=(',', ':'))
    if live == wanted:
        cv.ok(f'{POLICY_NAME} up to date')
    else:
        suffix = ' (+ self-invoke)' if self_invoke_arn else ''
        # Re-putting an identical policy is a no-op, so the unreadable case
        # applies the same write rather than guessing the grant is missing.
        verb = ('re-put (cannot read it from here)' if live is UNREADABLE
                else 'put')
        cv.do(f'{verb} {POLICY_NAME} on {role_name}{suffix}',
              lambda: iam.put_role_policy(RoleName=role_name, PolicyName=POLICY_NAME,
                                          PolicyDocument=document),
              f"aws iam put-role-policy --role-name {role_name} "
              f"--policy-name {POLICY_NAME} --policy-document '{document}'",
              unverified=live is UNREADABLE)
    return existing_role_arn


def create_function_with_retry(lam, fn, role_arn, env):
    """create_function, waiting out IAM's eventual consistency.

    A role created seconds ago is not yet assumable by Lambda, which surfaces
    as InvalidParameterValueException rather than anything retryable-looking.
    """
    for attempt in range(10):
        try:
            return lam.create_function(
                FunctionName=fn.name,
                Runtime=fn.runtime,
                Role=role_arn,
                Handler=fn.handler,
                Code={'ZipFile': bootstrap_zip(fn.handler)},
                Timeout=fn.timeout,
                MemorySize=fn.memory,
                Architectures=['x86_64'],
                Environment={'Variables': env},
            )
        except ClientError as e:
            error = e.response['Error']
            if error['Code'] == 'ResourceConflictException':
                return None             # created by a previous partial run
            retryable = (error['Code'] == 'InvalidParameterValueException'
                         and 'assumed by Lambda' in error.get('Message', ''))
            if retryable and attempt < 9:
                time.sleep(3)
                continue
            raise


def ensure_function(cv, lam, fn, cfg, role_arn):
    """Create the function, or converge its configuration and env."""
    env, unavailable = {}, []
    for key in fn.env:
        value = env_value(key)
        if value is None:
            if key not in OPTIONAL_ENV:
                unavailable.append(key)
        else:
            env[key] = value

    if cfg is None:
        cv.do(f'create {fn.name} ({fn.runtime}, {fn.timeout}s, {fn.memory}MB, '
              f'placeholder code)',
              lambda: create_function_with_retry(lam, fn, role_arn, env),
              f'aws lambda create-function --function-name {fn.name} '
              f'--runtime {fn.runtime} --handler {fn.handler} --role {role_arn} '
              f'--timeout {fn.timeout} --memory-size {fn.memory} '
              f'--zip-file fileb://placeholder.zip --region {REGION}')
        if unavailable:
            # A function created without these starts up and then fails on the
            # first real request, so it belongs in the checklist, not just here.
            cv.skip(f'no local value for {", ".join(unavailable)}')
            cv.manual.append(f'set {", ".join(unavailable)} on {fn.name} '
                             f'(missing from the local environment)')
        cv.manual.append(f'push real code to {fn.name}: '
                         f'gh workflow run {workflow_for(fn.name)}')
        return

    settings = {}
    if cfg['Runtime'] != fn.runtime:
        settings['Runtime'] = fn.runtime
    if cfg['Handler'] != fn.handler:
        settings['Handler'] = fn.handler
    if cfg['Timeout'] != fn.timeout:
        settings['Timeout'] = fn.timeout
    if cfg['MemorySize'] != fn.memory:
        settings['MemorySize'] = fn.memory
    if settings:
        update_function_config(
            cv, lam, fn.name,
            ', '.join(f'{k} {cfg.get(k)} -> {v}' for k, v in settings.items()),
            **settings)
    else:
        cv.ok(f'{fn.runtime}, {fn.handler}, {fn.timeout}s, {fn.memory}MB')

    current = dict(cfg.get('Environment', {}).get('Variables', {}))
    add = {k: v for k, v in env.items() if k not in current}
    differs = sorted(k for k, v in env.items() if k in current and current[k] != v)
    extra = sorted(k for k in current if k not in fn.env)
    absent_here = [k for k in unavailable if k not in current]

    if add:
        merged = {**current, **add}
        update_function_config(cv, lam, fn.name, f"set {', '.join(sorted(add))}",
                               Environment={'Variables': merged})
        current = merged
    if differs:
        cv.skip(f"{', '.join(differs)} differ from the local environment "
                f'-- not overwritten (see the module docstring)')
    if absent_here:
        cv.skip(f'{", ".join(absent_here)} unset here and on the function')
    if extra:
        if cv.prune:
            update_function_config(cv, lam, fn.name, f"prune {', '.join(extra)}",
                                   Environment={'Variables': {
                                       k: v for k, v in current.items() if k in fn.env}})
        else:
            cv.skip(f"undeclared env: {', '.join(extra)} -- remove with --prune")
    if not (add or differs or absent_here or extra):
        cv.ok(f'env: {", ".join(sorted(env))}')

    layers = [layer['Arn'] for layer in cfg.get('Layers') or []]
    if layers:
        # Every dependency ships in the zip now (the workflows assert it), so a
        # layer here is a leftover, shadowed by /var/task.
        names = ', '.join(arn.split(':')[6] for arn in layers)
        if cv.prune:
            update_function_config(cv, lam, fn.name, f'detach layers {names}', Layers=[])
        else:
            cv.skip(f'undeclared layers: {names} -- detach with --prune')


def ensure_log_group(cv, logs, fn):
    """Create the log group up front so retention is set before the function's
    first invocation creates it unbounded."""
    name = f'/aws/lambda/{fn.name}'
    groups = cv.read(lambda: logs.describe_log_groups(
        logGroupNamePrefix=name)['logGroups'], default=[])
    live = next((g for g in groups if g['logGroupName'] == name), None)
    if live is None:
        cv.do(f'create log group {name}',
              lambda: swallow(lambda: logs.create_log_group(logGroupName=name),
                              'ResourceAlreadyExistsException'),
              f'aws logs create-log-group --log-group-name {name} --region {REGION}')
    elif live.get('retentionInDays') == LOG_RETENTION_DAYS:
        cv.ok(f'log retention {LOG_RETENTION_DAYS}d')
        return
    cv.do(f'set log retention {LOG_RETENTION_DAYS}d on {name}',
          lambda: logs.put_retention_policy(logGroupName=name,
                                            retentionInDays=LOG_RETENTION_DAYS),
          f'aws logs put-retention-policy --log-group-name {name} '
          f'--retention-in-days {LOG_RETENTION_DAYS} --region {REGION}')


def ensure_schedule(cv, events, lam, fn, account):
    """The EventBridge rule, its target, and the permission letting it fire."""
    function_arn = f'arn:aws:lambda:{REGION}:{account}:function:{fn.name}'
    rule_arn = f'arn:aws:events:{REGION}:{account}:rule/{fn.rule}'

    rule = cv.read(lambda: events.describe_rule(Name=fn.rule))
    if (rule or {}).get('ScheduleExpression') == fn.schedule \
            and rule.get('State') == 'ENABLED':
        cv.ok(f'rule {fn.rule}: {fn.schedule}')
    else:
        current = (rule or {}).get('ScheduleExpression', 'absent')
        cv.do(f'rule {fn.rule}: {current} -> {fn.schedule}',
              lambda: events.put_rule(Name=fn.rule, ScheduleExpression=fn.schedule,
                                      State='ENABLED'),
              f'aws events put-rule --name {fn.rule} '
              f"--schedule-expression '{fn.schedule}' --state ENABLED --region {REGION}")

    targets = cv.read(lambda: events.list_targets_by_rule(Rule=fn.rule)['Targets'],
                      default=[])
    if any(t['Arn'] == function_arn for t in targets):
        cv.ok(f'rule {fn.rule} targets {fn.name}')
    else:
        cv.do(f'point rule {fn.rule} at {fn.name}',
              lambda: events.put_targets(Rule=fn.rule, Targets=[
                  {'Id': f'{fn.name}-target', 'Arn': function_arn}]),
              f'aws events put-targets --rule {fn.rule} '
              f"--targets 'Id={fn.name}-target,Arn={function_arn}' --region {REGION}")

    statement_id = f'{fn.rule}-invoke'
    if has_statement(cv, lam, fn.name, source_arn=rule_arn):
        cv.ok(f'{fn.name} invokable by rule {fn.rule}')
    else:
        cv.do(f'allow rule {fn.rule} to invoke {fn.name}',
              lambda: swallow(lambda: lam.add_permission(
                  FunctionName=fn.name, StatementId=statement_id,
                  Action='lambda:InvokeFunction', Principal='events.amazonaws.com',
                  SourceArn=rule_arn), 'ResourceConflictException'),
              f'aws lambda add-permission --function-name {fn.name} '
              f'--statement-id {statement_id} --action lambda:InvokeFunction '
              f'--principal events.amazonaws.com --source-arn {rule_arn} '
              f'--region {REGION}')


def ensure_function_url(cv, lam, fn):
    """The public Function URL Discord posts interactions to."""
    config = cv.read(lambda: lam.get_function_url_config(FunctionName=fn.name))
    if config is None:
        created = cv.do(
            f'create Function URL for {fn.name} (auth NONE, CORS discord.com)',
            lambda: lam.create_function_url_config(
                FunctionName=fn.name, AuthType='NONE', Cors=FUNCTION_URL_CORS,
                InvokeMode='BUFFERED'),
            f'aws lambda create-function-url-config --function-name {fn.name} '
            f'--auth-type NONE --region {REGION}')
        # A rebuilt URL is a new hostname; interactions keep failing until
        # Discord is told, and nothing on the AWS side can surface that.
        cv.manual.append(
            f"set the Discord Interactions Endpoint URL to "
            f"{(created or {}).get('FunctionUrl', '<the new URL>')} "
            f"(Developer Portal > General Information)")
    else:
        cv.ok(f"Function URL {config['FunctionUrl']} (auth {config['AuthType']})")
        origins = (config.get('Cors') or {}).get('AllowOrigins')
        if origins != FUNCTION_URL_CORS['AllowOrigins']:
            cv.do(f"set Function URL CORS to {FUNCTION_URL_CORS['AllowOrigins']}",
                  lambda: lam.update_function_url_config(
                      FunctionName=fn.name, Cors=FUNCTION_URL_CORS),
                  f'aws lambda update-function-url-config --function-name {fn.name} '
                  f'--region {REGION}')

    if has_statement(cv, lam, fn.name, action='lambda:InvokeFunctionUrl'):
        cv.ok(f'{fn.name} URL is publicly invokable')
    else:
        cv.do(f'allow public invoke of the {fn.name} URL',
              lambda: swallow(lambda: lam.add_permission(
                  FunctionName=fn.name, StatementId='FunctionURLAllowPublicAccess',
                  Action='lambda:InvokeFunctionUrl', Principal='*',
                  FunctionUrlAuthType='NONE'), 'ResourceConflictException'),
              f'aws lambda add-permission --function-name {fn.name} '
              f'--statement-id FunctionURLAllowPublicAccess '
              f'--action lambda:InvokeFunctionUrl --principal "*" '
              f'--function-url-auth-type NONE --region {REGION}')


def has_statement(cv, lam, name, source_arn=None, action=None):
    """True when the function's resource policy already grants this.

    Matched on what the statement *does* rather than on its Sid: the live
    statements were written by the console and carry generated Sids, so
    matching by name would re-add a permission that is already there.
    """
    raw = cv.read(lambda: lam.get_policy(FunctionName=name)['Policy'])
    if raw is None:
        return False
    for statement in json.loads(raw).get('Statement', []):
        actions = statement.get('Action')
        actions = actions if isinstance(actions, list) else [actions]
        if action and action in actions:
            return True
        if source_arn and (statement.get('Condition', {}).get('ArnLike', {})
                           .get('AWS:SourceArn')) == source_arn:
            return True
    return False


def workflow_for(name):
    return {'daily-game-score': 'deploy.yml',
            'daily-game-sticky': 'deploy-sticky.yml',
            'daily-game-play': 'deploy-interaction.yml'}[name]


def report_guilds(cv):
    """A stack with no guild config posts nothing, which from the AWS side
    looks identical to a broken deploy. Say which it is."""
    print('config')
    items = cv.read(lambda: boto3.resource('dynamodb', region_name=REGION)
                    .Table(TABLE)
                    .query(KeyConditionExpression=Key('PK').eq('GUILDS'))['Items'])
    if items is None:
        cv.skip('cannot read the GUILDS partition from here')
        return
    if not items:
        cv.skip('no guilds configured -- run /setup input and /setup output in the '
                'server (nothing posts until then)')
        return
    for item in items:
        gid = item.get('guild_id') or item['SK'].split('#', 1)[-1]
        cv.ok(f"guild {gid}: input={item.get('input_channel_id') or 'unset'} "
              f"output={item.get('output_channel_id') or 'unset'} "
              f"daily={item.get('daily_enabled')} sticky={item.get('sticky_enabled')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--plan', action='store_true',
                    help='show what would change and exit non-zero if anything would')
    ap.add_argument('--prune', action='store_true',
                    help='also remove undeclared env vars and layers')
    args = ap.parse_args()

    cv = Converge(apply=not args.plan, prune=args.prune)
    account = boto3.client('sts').get_caller_identity()['Account']
    table_arn = f'arn:aws:dynamodb:{REGION}:{account}:table/{TABLE}'
    lam = boto3.client('lambda', region_name=REGION)
    iam = boto3.client('iam')
    logs = boto3.client('logs', region_name=REGION)
    events = boto3.client('events', region_name=REGION)

    print(f"{'planning' if args.plan else 'converging'} account {account} / {REGION}\n")
    ensure_table(cv, boto3.client('dynamodb', region_name=REGION))

    for fn in FUNCTIONS:
        print(f'\n{fn.name}')
        cfg = cv.read(lambda: lam.get_function_configuration(FunctionName=fn.name))
        role_arn = ensure_role(cv, iam, fn, cfg['Role'] if cfg else None,
                               table_arn, account)
        ensure_function(cv, lam, fn, cfg, role_arn)
        ensure_log_group(cv, logs, fn)
        if fn.rule:
            ensure_schedule(cv, events, lam, fn, account)
        if fn.function_url:
            ensure_function_url(cv, lam, fn)

    print()
    report_guilds(cv)

    if cv.todo:
        print('\nrun with an admin identity (e.g. CloudShell in the console):')
        for command in cv.todo:
            print(f'  {command}')
    if cv.manual:
        print('\nfinish by hand:')
        for step in cv.manual:
            print(f'  - {step}')
        print('  - register the slash commands: '
              'dotenv run -- python3 tools/register_commands.py')
    if args.plan and cv.pending:
        print(f'\n{len(cv.pending)} change(s) pending; re-run without --plan to apply')
        sys.exit(1)
    if not args.plan and not (cv.todo or cv.manual):
        print('\nnothing left to do')


if __name__ == '__main__':
    main()
